"""Explicit name-only sync, separate from numeric telemetry and local avatars."""
import json
import os
from pathlib import Path
import threading
import time

from client.transport import ApiFailure
from server.protocol import ApiError,display_names


def name_rows(profiles,accounts):
    allowed=set(accounts);rows={}
    for profile in profiles:
        if not isinstance(profile,dict):continue
        account=profile.get('account_key');name=profile.get('display_name')
        if account not in allowed or not isinstance(name,str):continue
        name=name.strip()
        if not name or name=='未读取到账号名称':continue
        row=dict(account_key=account,display_name=name)
        try:display_names(dict(profiles=[row]))
        except ApiError:continue
        rows[account]=row
    return [rows[k] for k in sorted(rows)]


class DisplayNameSync:
    def __init__(self,state,api):
        self.path=Path(state)/'display-name-sync.json';self.api=api
        self.sent={};self.lock=threading.Lock();self.worker=None;self.cancel=threading.Event()
        self.next_tick=0;self.error=None
        try:
            saved=json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(saved,dict):self.sent={r['account_key']:r['display_name'] for r in name_rows([dict(account_key=k,display_name=v) for k,v in saved.items()],saved)}
        except (OSError,ValueError):pass

    def tick(self,profiles,accounts):
        if self.cancel.is_set() or (self.worker and self.worker.is_alive()) or time.monotonic()<self.next_tick:return
        rows=name_rows(profiles,accounts)
        with self.lock:rows=[r for r in rows if self.sent.get(r['account_key'])!=r['display_name']]
        if not rows:return
        self.next_tick=time.monotonic()+30
        self.worker=threading.Thread(target=self._run,args=(rows[:32],),daemon=True);self.worker.start()

    def _run(self,rows):
        errors=[]
        for row in rows:
            if self.cancel.is_set():break
            try:
                ack=self.api.record_display_names([row])
                if type(ack.get('recorded')) is not int or ack['recorded']!=1:raise ValueError('invalid_name_ack')
                with self.lock:
                    saved=dict(self.sent);saved[row['account_key']]=row['display_name']
                    encoded=json.dumps(saved,ensure_ascii=False)
                temp=self.path.with_suffix('.'+str(os.getpid())+'.tmp')
                temp.write_text(encoded,encoding='utf-8');os.replace(temp,self.path)
                with self.lock:self.sent=saved
            except Exception as error:
                # Never log profile values, response bodies or credentials.
                errors.append(error.code if isinstance(error,ApiFailure) else 'display_name_sync_failed')
        with self.lock:self.error=errors[0] if errors else None

    def snapshot(self):
        with self.lock:return dict(error=self.error,synced_accounts=len(self.sent))

    def close(self):
        self.cancel.set()
        if self.worker:self.worker.join(timeout=2)
