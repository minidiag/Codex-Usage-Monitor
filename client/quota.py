"""Read Codex account quota locally; no login material or quota payload is uploaded."""
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

from server.protocol import UTC, iso


class QuotaError(Exception):
    pass


def find_codex():
    configured = os.environ.get("CODEX_USAGE_CODEX_EXE")
    candidates = [Path(configured)] if configured else []
    on_path = shutil.which("codex.exe")
    if on_path:
        candidates.append(Path(on_path))
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    root = local / "OpenAI/Codex/bin"
    if root.exists():
        candidates.extend(sorted(root.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True))
    candidates.append(local / "Programs/OpenAI/Codex/bin/codex.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise QuotaError("codex_not_found")


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def normalize_quota(result, now=None):
    if not isinstance(result, dict):
        raise QuotaError("invalid_quota")
    keyed = result.get("rateLimitsByLimitId")
    if isinstance(keyed, dict) and "codex" in keyed:
        limits = keyed["codex"]
    else:
        limits = result.get("rateLimits")
    if not isinstance(limits, dict) or limits.get("limitId") not in (None, "codex"):
        raise QuotaError("quota_unavailable")
    windows = []
    for key in ("primary", "secondary"):
        raw = limits.get(key)
        if not isinstance(raw, dict):
            continue
        used, duration, reset = raw.get("usedPercent"), raw.get("windowDurationMins"), raw.get("resetsAt")
        used = float(used) if numeric(used) and used >= 0 else None
        duration = float(duration) if numeric(duration) and 0 < duration <= 525600 else None
        reset = reset if type(reset) is int and 0 < reset <= 253402300799 else None
        windows.append(dict(key=key, duration_mins=duration, used_percent=used,
                            remaining_percent=max(0.0, min(100.0, 100-used)) if used is not None else None,
                            resets_at=reset))
    if not windows:
        raise QuotaError("quota_unavailable")
    return dict(source="codex_app_server", fetched_at=iso(now or datetime.now(UTC)), error=None, windows=windows)


class WindowsJob:
    """Only the reader's own process tree is ended when its job handle closes."""
    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
                        ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", Counters), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        handle = self.kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not handle:
            raise QuotaError("quota_process_unavailable")
        self.handle = handle
        if not self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise QuotaError("quota_process_unavailable")
        if not self.kernel.AssignProcessToJobObject(handle, int(process._handle)):
            self.close()
            raise QuotaError("quota_process_unavailable")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def validate_network_settings(settings):
    if isinstance(settings, dict) and set(settings) == {"mode"} and isinstance(settings["mode"], str) and settings["mode"] in {"direct", "environment"}:
        return settings
    try:
        if not isinstance(settings, dict) or set(settings) != {"proxy"} or not isinstance(settings["proxy"], str):
            raise ValueError()
        proxy = settings["proxy"]
        parsed = urlsplit(proxy)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or any(char.isspace() for char in proxy)):
            raise ValueError()
    except ValueError:
        raise QuotaError("quota_proxy_invalid") from None
    return settings


def quota_environment(codex_home=None, network_path=None):
    """Apply persisted routing only to the quota subprocess, never to telemetry."""
    environment = os.environ.copy()
    if codex_home is not None:
        environment["CODEX_HOME"] = str(codex_home)
    if network_path is None:
        return environment
    try:
        settings = validate_network_settings(json.loads(Path(network_path).read_text(encoding="utf-8-sig")))
    except FileNotFoundError:
        return environment
    except (OSError, ValueError):
        raise QuotaError("quota_proxy_invalid") from None
    if settings.get("mode") == "environment":
        return environment
    for name in list(environment):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy"} or (settings.get("mode") == "direct" and name.lower() == "no_proxy"):
            del environment[name]
    if settings.get("mode") == "direct":
        environment["NO_PROXY"] = "*"
    else:
        environment.update(HTTP_PROXY=settings["proxy"], HTTPS_PROXY=settings["proxy"])
    return environment


def read_quota(cancel=None, timeout=18, codex_home=None, network_path=None):
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise QuotaError("quota_cancelled")
    deadline = time.monotonic()+timeout
    environment = quota_environment(codex_home, network_path)
    process = subprocess.Popen([find_codex(), "app-server", "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, env=environment)
    job = None
    incoming = queue.Queue(maxsize=8)

    def read_stdout():
        try:
            while True:
                line = process.stdout.readline(1024*1024 + 1)
                if not line or len(line) > 1024*1024:
                    break
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("id") in (1, 2):
                    try:
                        incoming.put_nowait(value)
                    except queue.Full:
                        break
        except (OSError, ValueError):
            pass

    def send(value):
        process.stdin.write(json.dumps(value, separators=(",", ":"))+"\n")
        process.stdin.flush()

    def response(request_id):
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise QuotaError("quota_cancelled")
            try:
                value = incoming.get(timeout=min(0.2, max(0.001, deadline-time.monotonic())))
            except queue.Empty:
                if process.poll() is not None:
                    raise QuotaError("quota_unavailable")
                continue
            if value.get("id") == request_id:
                if "error" in value or not isinstance(value.get("result"), dict):
                    raise QuotaError("quota_unavailable")
                return value["result"]
        raise QuotaError("quota_timeout")

    try:
        job = WindowsJob(process)
        threading.Thread(target=read_stdout, daemon=True).start()
        send(dict(jsonrpc="2.0", id=1, method="initialize", params={
            "clientInfo": {"name": "codex-usage-monitor", "title": "Codex Usage Monitor", "version": "0.4.0"}, "capabilities": {}}))
        response(1)
        send(dict(jsonrpc="2.0", method="initialized", params={}))
        send(dict(jsonrpc="2.0", id=2, method="account/rateLimits/read"))
        return normalize_quota(response(2))
    except (OSError, ValueError):
        raise QuotaError("quota_unavailable") from None
    finally:
        if job:
            job.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class QuotaMonitor:
    def __init__(self, state, reader=None, network_path=None):
        self.path = Path(state) / "quota-cache.json"
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.network_path = Path(network_path) if network_path is not None else self.path.parent / "quota-network.json"
        self.reader = reader or (lambda cancel: read_quota(cancel, network_path=self.network_path))
        self.network_stamp = self._network_stamp()
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.worker = None
        self.next_read = 0
        self.value = dict(source="codex_app_server", fetched_at=None, error=None, windows=[])
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict) and set(saved) <= {"source", "fetched_at", "error", "windows", "account_key"}:
                self.value = saved
        except (OSError, ValueError):
            pass

    def snapshot(self):
        with self.lock:
            return dict(self.value)

    def _network_stamp(self):
        try:
            stat = self.network_path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def tick(self):
        network_stamp = self._network_stamp()
        if network_stamp != self.network_stamp:
            self.network_stamp, self.next_read = network_stamp, 0
        if self.cancel.is_set() or time.monotonic() < self.next_read or (self.worker and self.worker.is_alive()):
            return
        self.next_read = time.monotonic()+30
        self.worker = threading.Thread(target=self._refresh, daemon=True)
        self.worker.start()

    def _refresh(self):
        network_stamp = self._network_stamp()
        try:
            value = self.reader(self.cancel)
        except Exception as error:
            value = self.snapshot()
            allowed = {"quota_unavailable", "invalid_quota", "quota_timeout", "quota_cancelled",
                       "codex_not_found", "quota_process_unavailable", "quota_proxy_invalid", "account_changed"}
            value["error"] = str(error) if isinstance(error, QuotaError) and str(error) in allowed else "quota_unavailable"
        if network_stamp != self._network_stamp():
            self.next_read = 0
            return  # A result from the previous route cannot overwrite the new setting's cache.
        with self.lock:
            self.value = value
        try:
            temporary = self.path.with_name("quota-cache."+str(os.getpid())+".tmp")
            temporary.write_text(json.dumps(value), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            pass

    def close(self):
        self.cancel.set()
        if self.worker:
            self.worker.join(timeout=3)


if __name__ == "__main__":
    print(json.dumps(read_quota()))
