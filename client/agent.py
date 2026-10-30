"""Five-second Windows collector; UI reads sanitized local status, never credentials."""
import argparse
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import random
import sys
import time

from client.core import Ledger, OutboxFull
from client.transport import Api, ApiFailure, load_credentials
from client.quota import QuotaMonitor
from client.active_sessions import ActiveSessionReader
from client.identity import IdentityTracker, bound_quota
from client.paths import state_path, credential_path
from client.profiles import ProfilesMonitor
from client.view_policy import window_filters
from client.cycles import CycleMonitor
from client.display_names import DisplayNameSync
from client.source_review import SourceReview
from client.source_inventory import SourceInventory
from server.protocol import HEALTH_REASONS, UTC, iso


class InstanceLock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        import msvcrt
        self.handle = open(self.path, "a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self.handle.close()
            self.handle = None
            return False

    def close(self):
        if self.handle:
            self.handle.close()


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    for attempt in range(4):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.03)


def upload_once(ledger, api):
    stream = ledger.next_stream()
    if stream is None:
        return False
    stream_id = stream["id"]
    try:
        if stream["mode"] != "ready":
            try:
                remote = api.state(stream_id)
            except ApiFailure as error:
                if error.status == 404:
                    ledger.ready(stream_id, replay=True)
                else:
                    raise
            else:
                if remote["anchor_id"] != stream["anchor"]:
                    if not ledger.rebase(stream_id, remote):
                        return False
                else:
                    ledger.ready(stream_id)
        pending = ledger.pending_batch(stream_id)
        if pending["records"]:
            ledger.acknowledge(stream_id, pending, api.send(pending))
        return True
    except ApiFailure as error:
        if error.status == 409:
            if error.code in {"anchor_conflict", "baseline_required"}:
                with ledger.db:
                    ledger.db.execute("UPDATE streams SET mode='unverified',next_retry=? WHERE id=?", (time.time()+5, stream_id))
            else:
                ledger.pause(stream_id, "unknown_identity")
        elif error.status in {400, 413, 415, 422}:
            ledger.pause(stream_id, "unsupported_usage")
        else:
            delay = max(error.retry_after, min(300, 2**min(8, stream["attempts"]+1)) + random.random())
            if error.status in {401, 403}:
                delay = max(delay, 300)
            ledger.retry(stream_id, delay)
        raise


class Agent:
    def __init__(self, state, config, homes, api=None):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.api = api or Api(config)
        self.ledger = Ledger(self.state / "collector.sqlite3", config["identity_key"], config["tenant_id"])
        self.homes = list(map(Path, homes))
        self.last_scan = None
        self.last_heartbeat = 0
        self.last_summary = 0
        self.server_summary = None
        self.error = None
        self.paused = False
        self.last_upload = None
        self.homes = list(dict.fromkeys(h.resolve() for h in self.homes))
        quota_network = self.state / "quota-network.json"
        self.quota = QuotaMonitor(self.state,reader=bound_quota(self.homes[0],config["identity_key"],quota_network),network_path=quota_network) if hasattr(self.api,"register_identity") else QuotaMonitor(self.state)
        self.quotas = [self.quota]
        if hasattr(self.api,"register_identity"):
            for home in self.homes[1:]:
                self.quotas.append(QuotaMonitor(self.state/"quotas"/self.ledger.identify(["quota-home",str(home)]),reader=bound_quota(home,config["identity_key"],quota_network),network_path=quota_network))
        self.active_sessions = ActiveSessionReader(self.homes)
        self.identity = IdentityTracker(self.homes,config["identity_key"]) if hasattr(self.api,"register_identity") else None
        self.profiles=ProfilesMonitor(self.homes,config["identity_key"],self.state) if self.identity else None
        self.display_names=DisplayNameSync(self.state,self.api) if self.profiles and hasattr(self.api,'record_display_names') else None
        self.cycles=CycleMonitor(self.state,Api(config)) if hasattr(self.api,"record_quota_cycles") else None
        self.inventory=SourceInventory(self.homes,self.ledger)
        self.source_review=SourceReview(self.state,self.ledger,self.api,self.homes,self.inventory)
        self.compressed_paths=[]
        self.identity_registered = False
        self.filters, self.catalog, self.last_catalog = {}, {}, 0
        if self.identity:
            self.ledger.attribution_provider = lambda *args: self.identity.claim(*args) if self.identity_registered else None
        try:
            saved = json.loads((self.state / "status.json").read_text(encoding="utf-8"))
            # Never render a saved administrator-wide summary in the current-user window.
            self.server_summary = None if self.identity else saved.get("summary")
        except (OSError, ValueError):
            pass

    def publish(self, running=True):
        queue = self.ledger.pending_count()
        source_health = self.ledger.source_health(self.compressed_paths)
        reasons = self.ledger.warnings(source_health)
        active_ids = {item["stream_id"] for item in source_health["active_alerts"]}
        blocked = [row[0] for row in self.ledger.db.execute("SELECT id FROM streams WHERE paused IS NOT NULL")]
        started = self.ledger.db.execute("SELECT value FROM meta WHERE key='started_at'").fetchone()[0]
        atomic_json(self.state / "status.json", dict(version="0.9.0", pid=os.getpid(), running=running,
                    updated_at=iso(datetime.now(UTC)), started_at=started,
                    last_scan_at=iso(self.last_scan) if self.last_scan else None, last_upload_at=self.last_upload,
                    queued_records=queue, sources=self.ledger.db.execute("SELECT count(*) FROM sources").fetchone()[0],
                    streams=self.ledger.db.execute("SELECT count(*) FROM streams").fetchone()[0],
                    blocked_streams=sum(stream in active_ids for stream in blocked), blocked_streams_total=len(blocked),
                    checkpoints=self.ledger.db.execute("SELECT count(*) FROM events").fetchone()[0],
                    paused=self.paused, error=self.error, local_reasons=sorted(reasons), source_health=source_health, source_tracking=self.inventory.snapshot(), summary=self.server_summary,
                    quota=self.quota.snapshot(), quotas=[q.snapshot() for q in self.quotas], active_sessions=self.active_view(),
                    identity=self.identity.snapshot() if self.identity else {}, catalog=self.catalog, filters=self.filters,
                    local_profiles=self.profiles.snapshot() if self.profiles else [], local_account_keys=self.local_account_keys(),
                    display_name_sync=self.display_names.snapshot() if self.display_names else {},
                    view_scope="current_windows_user",quota_cycle_stats=self.cycles.snapshot() if self.cycles else {}))

    def local_account_keys(self):
        if not self.identity:return []
        keys=set(self.identity.snapshot()["accounts"])
        principal=(self.identity.subject or {}).get("principal_key")
        for row in self.ledger.db.execute("SELECT DISTINCT json_extract(wire,'$.attribution.account_key') FROM events WHERE json_extract(wire,'$.attribution.principal_key')=?",(principal,)):
            if row[0]:keys.add(row[0])
        return sorted(keys)

    def active_view(self):
        view = self.active_sessions.snapshot()
        if not self.identity: return view
        if self.filters is None:return dict(view,sessions=[],scope_note="无法确认当前 Windows 用户")
        subject = self.identity.subject or {}
        if any(self.filters.get(name) and self.filters[name] != subject.get(name+"_key") for name in ("principal","machine")):
            return dict(view,sessions=[],scope_note="所选来源的会话详情未上报")
        account = self.filters.get("account")
        selected, rows = None if account == "unassigned" else account, []
        for source in view["sessions"]:
            stream = self.ledger.stream_id(source["id"])
            total, matches, previous = 0, False, None
            for record in self.ledger.db.execute("SELECT wire FROM events WHERE stream_id=? ORDER BY seq",(stream,)):
                point = json.loads(record[0])
                claim=point.get("attribution",{})
                if previous and claim.get("principal_key")==subject.get("principal_key") and (not account or claim.get("account_key")==selected):
                    total += sum(int(point["counters"][k])-int(previous["counters"][k]) for k in ("input","output"))
                    matches = True
                previous = point
            if matches:
                rows.append(dict(source,tokens=str(total),counter_scope="当前 Windows 用户已采集"))
            else:
                claim = self.identity.session_claim(stream)
                if (claim and claim.get("principal_key") == subject.get("principal_key")
                        and (not account or claim.get("account_key") == selected)):
                    rows.append(dict(source,tokens=None,counter_scope="等待当前用户用量检查点"))
        return dict(view,sessions=rows,scope_note="暂无当前用户的活跃会话")

    def step(self):
        reasons = set()
        requested={}
        try:
            control = json.loads((self.state / "control.json").read_text(encoding="utf-8-sig"))
            self.paused = bool(control.get("paused"))
            requested = control.get("filters",{})
            if not isinstance(requested,dict):
                requested = {}
        except (OSError, ValueError):
            self.paused = False
        if self.identity:
            # Establish the principal even when paused, but do not continue source observation.
            snapshot=self.identity.snapshot()
            desired=window_filters(snapshot["subject"],requested,snapshot["accounts"],snapshot.get("primary_account"))
        else:desired={}
        if desired!=self.filters:
            self.filters,self.server_summary,self.last_summary=desired,None,0
        self.source_review.tick()
        if self.paused:
            self.error = "paused"
            self.publish()
            return
        for quota in self.quotas: quota.tick()
        self.active_sessions.refresh()
        self.error = None
        if self.identity:
            self.identity.refresh()
            snapshot=self.identity.snapshot()
            desired=window_filters(snapshot["subject"],requested,snapshot["accounts"],snapshot.get("primary_account"))
            if desired!=self.filters:
                self.filters,self.server_summary,self.last_summary=desired,None,0
            self.profiles.tick(snapshot["accounts"])
            if self.identity.subject and not self.identity_registered:
                try:
                    self.api.register_identity(self.identity.subject)
                    self.identity_registered = True
                except ApiFailure as error:
                    self.error = error.code
        changed=self.inventory.poll()
        found=self.inventory.roots_exist()
        self.compressed_paths=self.inventory.compressed()
        if self.inventory.errors:reasons.add('scan_failed')
        if self.ledger.path.stat().st_size > 500*1024*1024:
            reasons.add("queue_full")
        else:
            for path in changed:
                try:
                    self.ledger.scan_file(path,force=True)
                    self.inventory.acknowledge(path)
                except OutboxFull:
                    reasons.add('queue_full');break
                except FileNotFoundError:
                    self.inventory.acknowledge(path)
                except (PermissionError,OSError):
                    reasons.add('scan_failed')
        if not found:
            reasons.add("source_missing")
        if not reasons & {"scan_failed", "source_missing", "queue_full"}:
            self.last_scan = datetime.now(UTC)
        reasons.update(self.ledger.warnings(self.ledger.source_health(self.compressed_paths)))
        try:
            for _ in range(4):
                if not upload_once(self.ledger, self.api):
                    break
                self.last_upload = iso(datetime.now(UTC))
        except ApiFailure as error:
            self.error = "authentication_failed" if error.status in {401, 403} else error.code
            logging.warning("upload status=%s code=%s", error.status, error.code)
        now = time.monotonic()
        if now - self.last_heartbeat >= 60:
            self.last_heartbeat = now
            oldest = self.ledger.db.execute("SELECT wire FROM events WHERE acked=0 ORDER BY rowid LIMIT 1").fetchone()
            heartbeat = dict(queued_records=str(self.ledger.pending_count()),
                             oldest_queued_at=json.loads(oldest[0])["observed_at"] if oldest else None,
                             last_scan_at=iso(self.last_scan) if self.last_scan else None,
                             quality="partial" if reasons else "complete", quality_reasons=sorted(reasons & HEALTH_REASONS))
            try:
                self.api.heartbeat(heartbeat)
            except ApiFailure as error:
                self.error = "authentication_failed" if error.status in {401, 403} else error.code
        if now - self.last_summary >= 10:
            self.last_summary = now
            try:
                if self.identity and self.filters is None:
                    self.error="subject_unavailable"
                    self.server_summary=None
                else:self.server_summary = self.api.summary(self.filters,[]) if self.identity else self.api.summary()
            except ApiFailure as error:
                self.error = "authentication_failed" if error.status in {401, 403} else error.code
        # Administrator catalogs stay available through the separate server query tool.
        self.catalog={}
        if self.cycles and self.identity_registered:
            quotas=[q.snapshot() for q in self.quotas]
            identity=self.identity.snapshot()
            self.cycles.observe(quotas,identity["accounts"])
            selected=(self.filters or {}).get("account")
            target=(quotas[0].get("account_key") if not selected else (None if selected=="unassigned" else selected))
            self.cycles.tick((self.identity.subject or {}).get("principal_key"),target)
        if self.display_names and self.identity_registered:
            self.display_names.tick(self.profiles.snapshot(),self.identity.snapshot()['accounts'])
        health = self.ledger.source_health(self.compressed_paths)
        active_kinds = {kind for item in health["active_alerts"] for kind in item["kinds"]}
        if reasons & {"scan_failed", "queue_full"}:
            self.error = sorted(reasons & {"scan_failed", "queue_full"})[0]
        elif not self.error:
            if not found:
                self.error = "source_directory_missing"
            elif "missing" in active_kinds:
                self.error = "source_missing"
            elif "compressed" in active_kinds:
                self.error = "compressed_source_unverified"
        self.publish()


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", type=Path, default=credential_path())
    parser.add_argument("--state", type=Path, default=state_path())
    parser.add_argument("--codex-home", action="append")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(args.state / "agent.log", maxBytes=1024*1024, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
    instance = InstanceLock(args.state / "collector.lock")
    if not instance.acquire():
        return
    agent = None
    try:
        config = load_credentials(args.credentials)
        homes = args.codex_home or [os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))]
        if not args.codex_home:
            try:
                configured = json.loads((args.state/"sources.json").read_text(encoding="utf-8-sig"))
                if not isinstance(configured,list) or not all(isinstance(h,str) and Path(h).is_absolute() for h in configured):
                    raise ValueError("invalid_sources")
                homes = list(dict.fromkeys(homes+configured))
            except FileNotFoundError: pass
        agent = Agent(args.state, config, homes)
        while not (args.state / "stop.request").exists():
            started = time.monotonic()
            try:
                agent.step()
            except Exception as error:
                logging.error("collector exception=%s", type(error).__name__)
                agent.error = "collector_error"
                agent.publish()
            if args.once:
                break
            wait = max(0, 5 - (time.monotonic() - started))
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and not (args.state / "stop.request").exists():
                time.sleep(min(0.2, max(0, deadline-time.monotonic())))
    except Exception as error:
        logging.error("startup exception=%s", type(error).__name__)
        atomic_json(args.state / "status.json", dict(running=False, error="startup_failed", updated_at=iso(datetime.now(UTC))))
        raise
    finally:
        if agent:
            if agent.cycles:agent.cycles.close()
            if agent.display_names:agent.display_names.close()
            if agent.profiles:agent.profiles.close()
            for quota in agent.quotas: quota.close()
            agent.publish(running=False)
            agent.ledger.close()
        instance.close()


if __name__ == "__main__":
    main()
