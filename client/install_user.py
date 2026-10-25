"""Import credentials for the current Windows user and migrate that user's legacy ledger once."""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import shutil
import sqlite3

from client.paths import ROOT, credential_path, state_path
from client.transport import load_credentials,validate_credentials,Api


def protect(config,path):
    validate_credentials(config)
    Api(config)  # Validate HTTPS destination before writing anything; performs no request.
    class Blob(ctypes.Structure):
        _fields_ = [("size",wintypes.DWORD),("data",ctypes.POINTER(ctypes.c_ubyte))]
    raw = json.dumps(config).encode("utf-16-le")
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypto, kernel = ctypes.WinDLL("crypt32",use_last_error=True), ctypes.WinDLL("kernel32",use_last_error=True)
    crypto.CryptProtectData.argtypes = [ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    if not crypto.CryptProtectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output)):
        raise OSError("credential_encryption_failed")
    try: encrypted = ctypes.string_at(output.data,output.size).hex()
    finally:
        ctypes.memset(buffer,0,len(buffer))
        kernel.LocalFree(output.data)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("x",encoding="utf-8") as handle: handle.write(encrypted)
    load_credentials(path)  # Verify locally without printing decrypted values.


def install(import_path=None):
    target, state = credential_path(), state_path()
    state.mkdir(parents=True,exist_ok=True)
    if import_path:
        config = json.loads(Path(import_path).read_text(encoding="utf-8-sig"))
        protect(config,target)
        print("已为当前 Windows 用户导入独立凭据；未复制其他用户的账本。")
        return
    if target.exists():
        load_credentials(target)
        print("当前用户已完成配置。")
        return
    legacy = ROOT/".local/client.credentials"
    load_credentials(legacy)  # Fails for a different Windows user; never copy another user's ledger.
    old = ROOT/".local/client"
    try:
        status = json.loads((old/"status.json").read_text(encoding="utf-8"))
        if status.get("running"): raise RuntimeError("请先停止旧版采集器再迁移。")
    except FileNotFoundError: pass
    if (state/"collector.sqlite3").exists(): raise RuntimeError("目标账本已存在，拒绝覆盖。")
    if (old/"collector.sqlite3").exists():
        source = sqlite3.connect((old/"collector.sqlite3").as_uri()+"?mode=ro",uri=True)
        destination = sqlite3.connect(state/"collector.sqlite3")
        try: source.backup(destination)
        finally: source.close(); destination.close()
    for name in ("status.json","quota-cache.json","control.json","window.json"):
        if (old/name).exists(): shutil.copy2(old/name,state/name)
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(legacy,target)
    print("当前用户数据已迁移；原账本和凭据仍保留为备份。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--import-credentials",type=Path)
    install(parser.parse_args().import_credentials)
