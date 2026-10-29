"""Durable quota-boundary outbox; read server-confirmed per-reset token snapshots asynchronously."""
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from client.transport import ApiFailure
from server.protocol import ApiError,UTC,timestamp


def observed_cycles(quotas,accounts,now=None):
    now=now or datetime.now(UTC)
    rows={}
    for quota in quotas:
        if not isinstance(quota,dict):continue
        account=quota.get("account_key")
        if account not in accounts or not isinstance(account,str) or not re.fullmatch(r"[0-9a-f]{64}",account):continue
        try:
            fetched=timestamp(quota.get("fetched_at"))
            if not -5<=(now-fetched).total_seconds()<=90:continue
        except (ApiError,ValueError,TypeError,AttributeError):
            # Invalid local quota fields are unavailable, never guessed as zero.
            continue
        windows=quota.get("windows",[])
        if not isinstance(windows,list):continue
        for window in windows:
            if not isinstance(window,dict):continue
            bucket=window.get("key");duration=window.get("duration_mins");reset=window.get("resets_at")
            if (not isinstance(bucket,str) or bucket not in {"primary","secondary"} or type(duration) not in (int,float) or not math.isfinite(duration) or duration!=int(duration)
                or not 1<=duration<=525600 or type(reset) is not int or not 0<reset<=253402300799):continue
            if not reset-int(duration)*60<=fetched.timestamp()<reset:continue
            row=dict(account_key=account,bucket=bucket,duration_mins=str(int(duration)),resets_at=str(reset),observed_at=quota["fetched_at"])
            rows[(account,bucket,reset)]=row
    return list(rows.values())


class CycleMonitor:
    def __init__(self,state,api):
        self.state=Path(state);self.state.mkdir(parents=True,exist_ok=True)
        self.path=self.state/"quota-cycles.sqlite3";self.cache_path=self.state/"quota-cycle-cache.json"
        self.api=api;self.lock=threading.Lock();self.values={};self.error=None
        self.worker=None;self.next_tick=0;self.target=None;self.cancel=threading.Event()
        self.db=sqlite3.connect(self.path,timeout=.5)
        self.db.execute("PRAGMA journal_mode=WAL");self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS reports(account TEXT NOT NULL,bucket TEXT NOT NULL,reset INTEGER NOT NULL,
            wire TEXT NOT NULL,pending INTEGER NOT NULL DEFAULT 1,sent_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(account,bucket,reset))""");self.db.commit()
        self.db.execute('''CREATE TABLE IF NOT EXISTS boundary_observations(
            account TEXT NOT NULL,bucket TEXT NOT NULL,observed TEXT NOT NULL,wire TEXT NOT NULL,
            pending INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(account,bucket,observed))''');self.db.commit()
        try:
            saved=json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(saved,dict):self.values=saved
        except (OSError,ValueError):pass

    def observe(self,quotas,accounts,now=None):
        rows=observed_cycles(quotas,accounts,now)
        with self.db:
            for row in rows:
                if hasattr(self.api,'record_boundary_observations'):
                    self.db.execute('INSERT OR IGNORE INTO boundary_observations(account,bucket,observed,wire) VALUES(?,?,?,?)',
                        (row['account_key'],row['bucket'],row['observed_at'],json.dumps(row)))
                    continue
                self.db.execute("""INSERT INTO reports(account,bucket,reset,wire) VALUES(?,?,?,?)
                    ON CONFLICT(account,bucket,reset) DO UPDATE SET wire=excluded.wire,
                    pending=CASE WHEN reports.pending=-1 THEN -1 WHEN reports.sent_at<=? THEN 1 ELSE reports.pending END""",
                    (row["account_key"],row["bucket"],int(row["resets_at"]),json.dumps(row),time.time()-60))

    def tick(self,principal,account):
        if self.cancel.is_set() or (self.worker and self.worker.is_alive()):return
        target=(principal,account)
        if time.monotonic()<self.next_tick and target==self.target:return
        self.next_tick=time.monotonic()+10;self.target=target
        self.worker=threading.Thread(target=self._run,args=target,daemon=True);self.worker.start()

    def _run(self,principal,account):
        db=sqlite3.connect(self.path,timeout=1)
        try:
            if hasattr(self.api,'record_boundary_observations'):
                self._run_current(db,principal,account)
                return
            rows=db.execute("SELECT account,bucket,reset,wire FROM reports WHERE pending=1 ORDER BY reset LIMIT 8").fetchall()
            metadata_error=None
            if rows and not self.cancel.is_set():
                try:self._send(db,rows)
                except ApiFailure as error:
                    if error.status!=409 or error.code!="cycle_boundary_conflict":raise
                    for row in rows:
                        try:self._send(db,[row])
                        except ApiFailure as single:
                            if single.status!=409 or single.code!="cycle_boundary_conflict":raise
                            with db:db.execute("UPDATE reports SET pending=-1 WHERE account=? AND bucket=? AND reset=?",row[:3])
                            metadata_error="cycle_boundary_conflict"
            if self.cancel.is_set():return
            if account and principal:
                value=self.api.quota_cycles(account,principal)
                if value.get("account_key")!=account or value.get("principal_key")!=principal:raise ValueError("cycle_scope_mismatch")
                timestamp(value["as_of"])
                if not isinstance(value.get("cycles"),list):raise ValueError("invalid_cycles")
                with self.lock:
                    self.values[account]=value;self.error=metadata_error
                    encoded=json.dumps(self.values)
                if not self.cancel.is_set():
                    temporary=self.cache_path.with_suffix("."+str(os.getpid())+".tmp");temporary.write_text(encoded,encoding="utf-8");os.replace(temporary,self.cache_path)
        except Exception as error:
            with self.lock:self.error=error.code if isinstance(error,ApiFailure) else "cycle_sync_failed"
        finally:db.close()

    def _run_current(self,db,principal,account):
        rows=db.execute('SELECT account,bucket,observed,wire FROM boundary_observations WHERE pending=1 ORDER BY observed LIMIT 100').fetchall()
        if self.cancel.is_set():return
        metadata_error=None
        if rows:
            try:self._send_observations(db,rows)
            except ApiFailure as error:
                if error.status!=409 or error.code!='observation_conflict':raise
                for row in rows:
                    try:self._send_observations(db,[row])
                    except ApiFailure as single:
                        if single.status!=409 or single.code!='observation_conflict':raise
                        with db:db.execute('UPDATE boundary_observations SET pending=-1 WHERE account=? AND bucket=? AND observed=?',row[:3])
                        metadata_error='observation_conflict'
        if not account or not principal or self.cancel.is_set():return
        value=self.api.current_quota_cycles(account,principal)
        if value.get('account_key')!=account or value.get('principal_key')!=principal:raise ValueError('cycle_scope_mismatch')
        timestamp(value['as_of'])
        buckets=value.get('buckets')
        if not isinstance(buckets,dict) or set(buckets)!={'primary','secondary'}:raise ValueError('invalid_current_cycles')
        with self.lock:
            self.values[account]=value;self.error=metadata_error;encoded=json.dumps(self.values)
        temporary=self.cache_path.with_suffix('.'+str(os.getpid())+'.tmp')
        temporary.write_text(encoded,encoding='utf-8');os.replace(temporary,self.cache_path)

    def _send_observations(self,db,rows):
        ack=self.api.record_boundary_observations([json.loads(r[3]) for r in rows])
        if type(ack.get('recorded')) is not int or ack['recorded']!=len(rows):raise ValueError('invalid_cycle_ack')
        with db:db.executemany('UPDATE boundary_observations SET pending=0 WHERE account=? AND bucket=? AND observed=?',[r[:3] for r in rows])

    def _send(self,db,rows):
        ack=self.api.record_quota_cycles([json.loads(r[3]) for r in rows])
        if type(ack.get("recorded")) is not int or ack["recorded"]!=len(rows):raise ValueError("invalid_cycle_ack")
        with db:db.executemany("UPDATE reports SET pending=0,sent_at=? WHERE account=? AND bucket=? AND reset=?",
            [(time.time(),r[0],r[1],r[2]) for r in rows])

    def snapshot(self):
        table='boundary_observations' if hasattr(self.api,'record_boundary_observations') else 'reports'
        blocked=self.db.execute('SELECT count(*) FROM '+table+' WHERE pending=-1').fetchone()[0]
        with self.lock:return dict(accounts=dict(self.values),error=self.error,blocked_reports=blocked)

    def close(self):
        self.cancel.set()
        if self.worker:self.worker.join(timeout=2)
        self.db.close()
