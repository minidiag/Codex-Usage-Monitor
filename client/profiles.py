"""Local presentation data only. Never attach these fields to telemetry identities or requests.

Read only matching /backend-api/me entries and the referenced avatar from the desktop cache.
Chromium format: chromium.googlesource.com/chromium/src/net/+/HEAD/disk_cache/blockfile/disk_format.h
"""
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zlib

from client.active_sessions import ActiveSessionReader,safe_text
from client.identity import account_metadata,pseudonym
from client.transport import NoRedirect
from server.protocol import UTC,iso

LIMIT=256*1024


def cache_roots():
    roaming=Path(os.environ.get("APPDATA",Path.home()/"AppData/Roaming"))
    local=Path(os.environ.get("LOCALAPPDATA",Path.home()/"AppData/Local"))
    browsers=[roaming/"Codex/web/Codex",roaming/"ChatGPT"]
    packages=local/"Packages"
    for package in packages.glob("OpenAI.*"):
        browsers.extend([package/"LocalCache/Roaming/Codex/web/Codex",package/"LocalCache/Roaming/ChatGPT"])
    result=[]
    for browser in browsers:
        for relative in ("Default/Cache/Cache_Data","Default/Partitions/codex-browser-app/Cache/Cache_Data"):
            root=browser/relative
            if (root/"data_1").is_file():result.append(root)
    return list(dict.fromkeys(result))


class DesktopCache:
    def __init__(self,root):
        self.root=Path(root)

    def blob(self,address,size):
        if not 0 < size <= 2*1024*1024 or not address&0x80000000:raise ValueError("invalid_cache_blob")
        kind=(address>>28)&7
        if kind==0:
            path=self.root/("f_%06x"%(address&0xfffffff));offset=0
        elif kind in (2,3,4) and not address&0x0c000000:
            block={2:256,3:1024,4:4096}[kind]
            if size>(((address>>24)&3)+1)*block:raise ValueError("invalid_cache_blob")
            path=self.root/("data_"+str((address>>16)&255));offset=8192+(address&65535)*block
        else:raise ValueError("unsupported_cache_blob")
        with path.open("rb") as handle:
            handle.seek(offset);data=handle.read(size)
        if len(data)!=size:raise ValueError("incomplete_cache_blob")
        return data

    def entries(self):
        path=self.root/"data_1"
        if path.stat().st_size>16*1024*1024:return []
        data=path.read_bytes();result=[]
        for offset in range(8192,len(data)-255,256):
            length=struct.unpack_from("<i",data,offset+32)[0]
            if not 0<length<=4096 or struct.unpack_from("<i",data,offset+20)[0]!=0:continue
            try:
                long_key=struct.unpack_from("<I",data,offset+36)[0]
                raw=self.blob(long_key,length) if long_key else data[offset+96:offset+96+length]
                key=raw.decode("utf-8")
                # Cache partition prefixes are separated from the final request URL by spaces.
                url=key.rsplit(" ",1)[-1]
                parsed=urllib.parse.urlsplit(url)
                if parsed.scheme!="https" or parsed.username or parsed.password:continue
                sizes=struct.unpack_from("<4i",data,offset+40);addresses=struct.unpack_from("<4I",data,offset+56)
                created=struct.unpack_from("<Q",data,offset+24)[0]
                result.append(dict(url=url,sizes=sizes,addresses=addresses,created=created))
            except (OSError,ValueError,UnicodeError):continue
        return result

    def profile(self,entry):
        parsed=urllib.parse.urlsplit(entry["url"])
        if parsed.hostname not in {"chatgpt.com","chat.openai.com"} or parsed.path!="/backend-api/me":return None
        if not 0<entry["sizes"][1]<=LIMIT:return None
        headers=self.blob(entry["addresses"][0],entry["sizes"][0])
        raw=self.blob(entry["addresses"][1],entry["sizes"][1])
        enc=re.search(rb"content-encoding:\s*([^\x00\r\n]+)",headers,re.I)
        encoding=enc.group(1).lower().strip() if enc else b""
        if encoding in (b"gzip",b"deflate"):
            decoder=zlib.decompressobj(31 if encoding==b"gzip" else 15)
            raw=decoder.decompress(raw,LIMIT+1)
            if len(raw)>LIMIT or not decoder.eof:raise ValueError("oversize_profile")
        elif encoding==b"br":
            node=shutil.which("node.exe") or shutil.which("node")
            if not node:return None
            script='const z=require("node:zlib"),f=require("node:fs");process.stdout.write(z.brotliDecompressSync(f.readFileSync(0),{maxOutputLength:262144}));'
            run=subprocess.run([node,"--max-old-space-size=32","-e",script],input=raw,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                               timeout=3,creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            if run.returncode:return None
            raw=run.stdout
        elif encoding not in (b"",b"identity"):return None
        if len(raw)>LIMIT:return None
        value=json.loads(raw)
        if not isinstance(value,dict):return None
        # Drop phone, email and all other cached fields immediately.
        return dict(user_id=value.get("id"),name=safe_text(value.get("name"),120),picture=value.get("picture"))


def avatar_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):return "png"
    if data.startswith(b"\xff\xd8\xff"):return "jpg"
    if data.startswith((b"GIF87a",b"GIF89a")):return "gif"
    if data.startswith(b"BM"):return "bmp"
    return None


def fetch_avatar(url):
    """Official-host image GET only: no bearer token, cookies, login or redirects."""
    parsed=urllib.parse.urlsplit(url)
    if (parsed.scheme!="https" or parsed.hostname not in {"chatgpt.com","chat.openai.com","images.openai.com"}
        or parsed.username or parsed.password or parsed.port not in (None,443)):
        return None
    opener=urllib.request.build_opener(NoRedirect())
    request=urllib.request.Request(url,headers={"Accept":"image/png,image/jpeg,image/gif,image/*;q=0.5"})
    with opener.open(request,timeout=4) as response:
        if not response.headers.get("Content-Type","").lower().startswith("image/"):return None
        data=response.read(2*1024*1024+1)
    return data if len(data)<=2*1024*1024 and avatar_type(data) else None


def local_login(home,key):
    account,stamp=account_metadata(home,key)
    if not account:return None
    path=Path(home)/"auth.json"
    if path.stat().st_size>1024*1024:return None
    data=json.loads(path.read_text(encoding="utf-8-sig"))
    part=data["tokens"]["id_token"].split(".")[1]
    claims=json.loads(base64.urlsafe_b64decode(part+"="*(-len(part)%4)))
    user=(claims.get("https://api.openai.com/auth") or {}).get("chatgpt_user_id")
    if account_metadata(home,key)!=(account,stamp):return None
    return dict(account_key=account,user_id=user,name=safe_text(claims.get("name"),120))


class ProfilesMonitor:
    def __init__(self,homes,key,state,roots=None,fetcher=fetch_avatar):
        self.homes,self.key,self.state=list(map(Path,homes)),key,Path(state)
        self.roots,self.fetcher=roots,fetcher
        self.path=self.state/"local-profiles.json"
        self.profiles={};self.lock=threading.Lock();self.worker=None;self.next_read=0;self.cancel=threading.Event()
        self.login_keys=None
        self.avatars=self.state/"avatars";self.avatars.mkdir(parents=True,exist_ok=True)
        try:
            saved=json.loads(self.path.read_text(encoding="utf-8"))
            for account,value in saved.items():
                if re.fullmatch(r"[0-9a-f]{64}",account) and isinstance(value,dict):
                    self.profiles[account]=self.presentation(account,value)
        except (OSError,ValueError,AttributeError):pass

    def clean(self,value):
        result=dict(display_name=safe_text(value.get("display_name"),120),avatar_path="",avatar_source="initials")
        fingerprint=value.get("picture_fingerprint","")
        if isinstance(fingerprint,str) and re.fullmatch(r"[0-9a-f]{64}",fingerprint):result["picture_fingerprint"]=fingerprint
        path=value.get("avatar_path")
        if isinstance(path,str) and path:
            candidate=Path(path).resolve()
            if candidate.is_relative_to(self.avatars.resolve()) and candidate.is_file():
                result.update(avatar_path=str(candidate),avatar_source="cached_previous" if value.get("avatar_source")=="cached_previous" else "chatgpt_desktop")
        return result

    def presentation(self,account,value):
        result=self.clean(value)
        if result["avatar_path"] and Path(result["avatar_path"]).name.startswith(account+"-"):
            return result
        result.update(avatar_path="",avatar_source="initials")
        # Signed picture URLs can rotate while offline. Retain only this account's
        # previously saved local image; never borrow another account's avatar.
        candidates=[]
        for path in self.avatars.glob(account+"-*"):
            try:
                if (re.fullmatch(re.escape(account)+r"-[0-9a-f]{16}\.(png|jpg|gif|bmp)",path.name)
                        and path.is_file() and path.resolve().is_relative_to(self.avatars.resolve())
                        and path.stat().st_size<=2*1024*1024):
                    candidates.append((path.stat().st_mtime_ns,path))
            except OSError:continue
        for _,path in sorted(candidates,reverse=True):
            try:
                with path.open("rb") as handle:valid=avatar_type(handle.read(16))
                if valid:
                    result.update(avatar_path=str(path.resolve()),avatar_source="cached_previous")
                    break
            except OSError:continue
        return result

    def snapshot(self):
        with self.lock:return [dict(account_key=k,**v) for k,v in self.profiles.items()]

    def tick(self,accounts=None):
        if accounts is not None and tuple(accounts)!=self.login_keys:
            self.login_keys=tuple(accounts);self.next_read=0
        if self.cancel.is_set() or time.monotonic()<self.next_read or (self.worker and self.worker.is_alive()):return
        self.next_read=time.monotonic()+60
        self.worker=threading.Thread(target=self._refresh,daemon=True);self.worker.start()

    def _refresh(self):
        try:self.refresh()
        except Exception:pass  # No cache diagnostics may contain profile bodies or login material.

    def refresh(self):
        known={}
        for home in self.homes:
            try:
                login=local_login(home,self.key)
                if login:
                    known[login["account_key"]]=login
                    with self.lock:
                        old=self.profiles.get(login["account_key"],{})
                        self.profiles[login["account_key"]]=dict(self.presentation(login["account_key"],old),display_name=login["name"] or old.get("display_name","") or "未读取到账号名称")
                database=ActiveSessionReader.find_database(home.resolve())
                if database:
                    db=sqlite3.connect(database.as_uri()+"?mode=ro",uri=True,timeout=.2)
                    try:
                        for user,workspace in db.execute("SELECT DISTINCT creator_user_id,creator_account_id FROM threads"):
                            if all(isinstance(x,str) and x for x in (user,workspace)):
                                account=pseudonym(self.key,"codex-account-v1",user,workspace)
                                known.setdefault(account,dict(account_key=account,user_id=user,name=""))
                    finally:db.close()
            except (OSError,ValueError,sqlite3.Error,KeyError,TypeError,AttributeError):continue
        caches=[];candidates={}
        for root in self.roots if self.roots is not None else cache_roots():
            try:
                cache=DesktopCache(root);entries=cache.entries();caches.append((cache,entries))
                for entry in entries:
                    try:profile=cache.profile(entry)
                    except (OSError,ValueError,struct.error,subprocess.SubprocessError):continue
                    if not profile:continue
                    for account,login in known.items():
                        if profile["user_id"]==login["user_id"] and (account not in candidates or entry["created"]>candidates[account][0]):
                            candidates[account]=(entry["created"],profile)
            except (OSError,ValueError,struct.error,subprocess.SubprocessError):continue
        for account,(_,profile) in candidates.items():
            if self.cancel.is_set():break
            with self.lock:
                old=self.profiles.get(account,{})
                value=dict(self.presentation(account,old),display_name=profile["name"] or old.get("display_name","") or "未读取到账号名称")
                self.profiles[account]=value
            picture=profile["picture"]
            if not isinstance(picture,str) or not picture or len(picture)>4096:continue
            fingerprint=hashlib.sha256(picture.encode()).hexdigest()
            if value.get("picture_fingerprint")!=fingerprint:
                value=dict(value,avatar_source="cached_previous" if value["avatar_path"] else "initials",picture_fingerprint=fingerprint)
                with self.lock:self.profiles[account]=value
            image=None
            for cache,entries in caches:
                for entry in entries:
                    if entry["url"]==picture:
                        try:
                            data=cache.blob(entry["addresses"][1],entry["sizes"][1])
                            if avatar_type(data):image=data;break
                        except (OSError,ValueError):pass
                if image:break
            # Keep an existing verified avatar when this account is temporarily offline.
            if image is None and not value["avatar_path"]:
                try:image=self.fetcher(picture)
                except (OSError,ValueError):pass
            if self.cancel.is_set():return
            if image and avatar_type(image):
                target=self.avatars/(account+"-"+fingerprint[:16]+"."+avatar_type(image));temporary=target.with_suffix("."+str(os.getpid())+".tmp")
                temporary.write_bytes(image);os.replace(temporary,target)
                value=dict(value,avatar_path=str(target.resolve()),avatar_source="chatgpt_desktop")
                with self.lock:self.profiles[account]=value
        if self.cancel.is_set():return
        with self.lock:encoded=json.dumps(self.profiles,ensure_ascii=False)
        temporary=self.path.with_suffix("."+str(os.getpid())+".tmp");temporary.write_text(encoded,encoding="utf-8");os.replace(temporary,self.path)

    def close(self):
        self.cancel.set()
        if self.worker:self.worker.join(timeout=1)
