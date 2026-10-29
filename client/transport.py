"""Current-user DPAPI credentials and direct, certificate-verified HTTPS."""
import ctypes
from ctypes import wintypes
from datetime import datetime
import email.utils
import json
from pathlib import Path
from uuid import UUID
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from server.protocol import UTC


def load_credentials(path):
    encrypted = bytes.fromhex(Path(path).read_text(encoding="utf-8-sig").strip())

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypto = ctypes.WinDLL("Crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("Kernel32", use_last_error=True)
    crypto.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypto.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    source_buffer = ctypes.create_string_buffer(encrypted)
    source = Blob(len(encrypted), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    if not crypto.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError("credential_decryption_failed")
    try:
        config = json.loads(ctypes.string_at(output.pbData, output.cbData).decode("utf-16-le"))
    finally:
        ctypes.memset(output.pbData, 0, output.cbData)
        kernel.LocalFree(output.pbData)
    return validate_credentials(config)


def validate_credentials(config):
    if not isinstance(config,dict) or set(config) != {"base_url", "tenant_id", "device_id", "read_token", "write_token", "identity_key"}:
        raise ValueError("invalid_credentials")
    if not isinstance(config["identity_key"],str) or re.fullmatch(r"[0-9a-fA-F]{64}",config["identity_key"]) is None:
        raise ValueError("invalid_identity_key")
    for field in ("tenant_id","device_id"): UUID(config[field])
    for field in ("read_token","write_token"):
        if not isinstance(config[field],str) or re.fullmatch(r"[A-Za-z0-9_-]{32,256}",config[field]) is None:
            raise ValueError("invalid_credentials")
    return config


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiFailure(Exception):
    def __init__(self, status=0, code="network_error", retry_after=0):
        super().__init__(code)
        self.status, self.code, self.retry_after = status, code, retry_after


class Api:
    def __init__(self, config):
        self.config = config
        self.base = config["base_url"].rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("https_required")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                  urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, path, scope="write", data=None):
        headers = {"Authorization": "Bearer " + self.config[scope + "_token"], "Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=8) as response:
                raw = response.read(1024*1024 + 1)
                if len(raw) > 1024*1024:
                    raise ApiFailure(0, "invalid_response")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ApiFailure(0, "invalid_response")
                return result
        except urllib.error.HTTPError as error:
            code = "http_error"
            try:
                candidate = json.loads(error.read(4096)).get("code", "http_error")
                if isinstance(candidate, str) and re.fullmatch(r"[a-z_]{1,64}", candidate):
                    code = candidate
            except (ValueError, AttributeError):
                pass
            retry = error.headers.get("Retry-After", "0")
            try:
                delay = float(retry)
            except ValueError:
                try:
                    delay = (email.utils.parsedate_to_datetime(retry) - datetime.now(UTC)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = 0
            raise ApiFailure(error.code, code, min(3600, max(0, delay))) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise ApiFailure() from None

    def state(self, stream_id):
        return self.request("/v1/streams/" + stream_id + "/state?epoch=0")

    def send(self, data):
        return self.request("/v1/usage/batches", data=data)

    def heartbeat(self, data):
        return self.request("/v1/devices/heartbeat", data=data)

    def summary(self, filters=None, group_by=None):
        if filters is None and group_by is None:
            return self.request("/v1/usage/summary", scope="read")
        query = dict(filters or {})
        if group_by: query["group_by"] = ",".join(group_by)
        return self.request("/v2/usage/summary?"+urllib.parse.urlencode(query), scope="read")

    def register_identity(self, subject):
        return self.request("/v2/identity",data=subject)

    def record_display_names(self,profiles):
        return self.request('/v3/accounts/display-names',data=dict(profiles=profiles))

    def identities(self):
        return self.request("/v2/identities",scope="read")

    def record_quota_cycles(self,rows):
        return self.request("/v2/usage/quota-cycles",data=dict(cycles=rows))

    def record_boundary_observations(self,rows):
        return self.request('/v3/usage/quota-observations',data=dict(cycles=rows))

    def current_quota_cycles(self,account,principal):
        return self.request('/v3/usage/quota-current?'+urllib.parse.urlencode(dict(account=account,principal=principal)),scope='read')

    def quota_cycles(self,account,principal=None):
        # Older servers sort all buckets by reset time, so weekly history can
        # fill a short page before the current five-hour cycle is reached.
        query=dict(account=account,limit="200")
        if principal:query["principal"]=principal
        return self.request("/v2/usage/quota-cycles?"+urllib.parse.urlencode(query),scope="read")
