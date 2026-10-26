"""Local account metadata adapter. Only keyed pseudonyms can leave this module."""
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import time

from client.active_sessions import ActiveSessionReader
from server.protocol import UTC, iso, timestamp


def pseudonym(key, namespace, *values):
    return hmac.new(bytes.fromhex(key), json.dumps([namespace,*values], ensure_ascii=False, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def windows_subject(key):
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", access=winreg.KEY_READ|winreg.KEY_WOW64_64KEY) as handle:
        machine = winreg.QueryValueEx(handle, "MachineGuid")[0]
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,ctypes.POINTER(wintypes.LPWSTR)]
    token, length, sid = wintypes.HANDLE(), wintypes.DWORD(), wintypes.LPWSTR()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError("subject_unavailable")
    try:
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        buffer = ctypes.create_string_buffer(length.value)
        if not adv.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise OSError("subject_unavailable")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid)):
            raise OSError("subject_unavailable")
        machine_key = pseudonym(key, "machine-v1", machine)
        return dict(machine_key=machine_key, principal_key=pseudonym(key, "windows-user-v1", machine_key, sid.value))
    finally:
        if sid: kernel.LocalFree(sid)
        kernel.CloseHandle(token)


def account_metadata(home, key):
    """Decode identity claims only as self-reported telemetry, never as authentication proof.

    No token, email, raw identity or full response is returned, persisted or logged.
    Unknown credential modes fail closed rather than fingerprinting an API key.
    """
    path = Path(home)/"auth.json"
    try:
        stat = path.stat()
        if stat.st_size > 1024*1024:
            return None, None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("OPENAI_API_KEY") or data.get("auth_mode") not in (None,"chatgpt","chatgptAuthTokens"):
            return None,(stat.st_mtime_ns,stat.st_size)
        tokens = data.get("tokens") or {}
        token = tokens.get("id_token")
        if not isinstance(token, str) or token.count(".") != 2:
            return None, (stat.st_mtime_ns,stat.st_size)
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part+"="*(-len(part)%4)))
        identity = claims.get("https://api.openai.com/auth") or {}
        user, account = identity.get("chatgpt_user_id"), identity.get("chatgpt_account_id")
        stored_account = tokens.get("account_id")
        if not all(isinstance(x,str) and 1 <= len(x) <= 256 for x in (user,account)) or (stored_account and stored_account != account):
            return None, (stat.st_mtime_ns,stat.st_size)
        return pseudonym(key, "codex-account-v1", user, account), (stat.st_mtime_ns,stat.st_size)
    except (OSError,ValueError,TypeError,AttributeError,UnicodeError,RecursionError):
        return None, None


def bound_quota(home,key,network_path=None):
    from client.quota import QuotaError, read_quota
    def read(cancel):
        before, stamp = account_metadata(home,key)
        result = read_quota(cancel,codex_home=home,network_path=network_path)
        after, changed = account_metadata(home,key)
        if before != after or stamp != changed:
            raise QuotaError("account_changed")
        return dict(result,account_key=after)
    return read


class IdentityTracker:
    def __init__(self, homes, key, subject=None, clock=time.time):
        self.homes = list(map(Path,homes))
        self.key, self.subject, self.clock = key, subject, clock
        self.sources, self.owners = {}, {}
        self.last_poll = None
        if self.subject is None and os.name == "nt":
            try: self.subject = windows_subject(key)
            except OSError: pass

    def refresh(self):
        now = self.clock()
        gap = self.last_poll is None or now-self.last_poll > 20 or now < self.last_poll
        owners = {}
        for home in self.homes:
            account, stamp = account_metadata(home,self.key)
            old = self.sources.get(str(home))
            start = old["observed_from"] if old and not gap and stamp is not None and stamp == old["stamp"] and account == old["account_key"] else iso(datetime.fromtimestamp(now,UTC))
            self.sources[str(home)] = dict(account_key=account,stamp=stamp,observed_from=start)
            try:
                database = ActiveSessionReader.find_database(home.resolve())
                if database is None: continue
                connection = sqlite3.connect(database.as_uri()+"?mode=ro",uri=True,timeout=0.2)
                try:
                    for identity,user,workspace in connection.execute("SELECT id,creator_user_id,creator_account_id FROM threads"):
                        if not all(isinstance(x,str) and x for x in (identity,user,workspace)): continue
                        stream = pseudonym(self.key,"codex-rollout-v1",identity)
                        owner = pseudonym(self.key,"codex-account-v1",user,workspace)
                        owners.setdefault(stream,[]).append((str(home),owner))
                finally: connection.close()
            except (sqlite3.Error,OSError,ValueError):
                pass
        self.owners, self.last_poll = owners, now

    def claim(self, stream, previous, point):
        if not self.subject: return None
        matches = self.owners.get(stream,[])
        # Multiple conflicting environments, old data and resumed foreign-owned threads stay unknown.
        candidates = []
        for home,owner in matches:
            source = self.sources[home]
            if timestamp(previous["event_time"]) >= timestamp(source["observed_from"]):
                candidates.append((source,owner,home))
        if len(candidates) != 1: return None
        source,owner,home = candidates[0]
        account = source["account_key"] if source["account_key"] == owner else None
        # Auth changed while this refresh was scanning: never bind the ambiguous interval.
        _, stamp = account_metadata(Path(home),self.key)
        if stamp != source["stamp"]: return None
        return dict(self.subject,account_key=account,observed_from=source["observed_from"])

    def snapshot(self):
        accounts = sorted({s["account_key"] for s in self.sources.values() if s["account_key"]})
        primary=self.sources.get(str(self.homes[0]),{}).get("account_key") if self.homes else None
        return dict(subject=self.subject,accounts=accounts,primary_account=primary,automatic=True,error=None if self.subject else "subject_unavailable")
