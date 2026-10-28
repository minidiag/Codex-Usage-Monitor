"""Acknowledged local coverage gaps. Closing an alert never changes usage or collection."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid

from server.protocol import UTC, iso


def stamp(path):
    try:
        value = Path(path).stat()
        return [value.st_size, value.st_mtime_ns, value.st_ctime_ns]
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"


def reconcile(ledger, health):
    groups, unidentified = {}, {}
    def add(stream, kind, path=None):
        if not stream:
            stream = "unidentified:"+hashlib.sha256(path.encode()).hexdigest()
            unidentified[stream] = path
        groups.setdefault(stream, set()).add(kind)
    for item in health["missing"]:
        add(item["stream_id"], "missing",item["path"])
    for item in health["archives"]:
        if item["status"] == "unverified":
            add(item["stream_id"], "compressed",item["original_path"])
    for row in ledger.paused_sources():
        add(row["id"], "paused")
    alerts = {}
    for stream_id, kinds in sorted(groups.items()):
        stream = ledger.db.execute("SELECT id,anchor,last_seq,last_point,paused FROM streams WHERE id=?", (stream_id,)).fetchone()
        rows = ledger.db.execute("""SELECT s.* FROM sources s WHERE stream_id=? AND NOT EXISTS
            (SELECT 1 FROM source_exclusions e WHERE e.path=s.path AND e.stream_id=s.stream_id) ORDER BY path""", (stream_id,)).fetchall()
        if stream_id in unidentified:
            rows = ledger.db.execute("SELECT * FROM sources WHERE path=? AND stream_id IS NULL",(unidentified[stream_id],)).fetchall()
        if ledger.existing_only():
            active_paths={row['path'] for row in ledger.source_rows()}
            rows=[row for row in rows if row['path'] in active_paths]
        evidence = [dict(row) for row in rows]
        files = [[row["path"], stamp(row["path"]), stamp(row["path"]+".zst")] for row in rows]
        archived = [[item["path"], item["proof"]] for item in health["archives"] if item["stream_id"] == stream_id]
        fingerprint = hashlib.sha256(json.dumps(["source-alert-v1", sorted(kinds), dict(stream) if stream else None,
            evidence, files, archived], sort_keys=True).encode()).hexdigest()
        count = ledger.db.execute("SELECT count(*),coalesce(sum(acked=0),0) FROM events WHERE stream_id=?", (stream_id,)).fetchone()
        last_time = json.loads(stream["last_point"])["event_time"] if stream else None
        alerts[stream_id] = dict(stream_id=stream_id, fingerprint=fingerprint, kinds=sorted(kinds),
            paths=[row["path"] for row in rows], pending=count[1], retained_records=count[0],
            last_record_at=last_time, pause_reason=stream["paused"] if stream else None,
            message="来源文件缺失" if "missing" in kinds else "压缩来源待核实" if "compressed" in kinds else "来源因校验问题暂停")
    closed = []
    for row in ledger.db.execute("SELECT * FROM source_alert_closures WHERE reopened_at IS NULL ORDER BY closed_at").fetchall():
        alert = alerts.get(row["stream_id"])
        if alert and alert["fingerprint"] == row["fingerprint"]:
            closed.append(dict(alert, closure_id=row["id"], closed_at=row["closed_at"], reason=row["reason"]))
            del alerts[row["stream_id"]]
            continue
        retired = not alert and ledger.existing_only() and not any(source['stream_id']==row['stream_id'] for source in ledger.source_rows())
        action = "changed" if alert else "retired" if retired else "resolved"
        now = iso(datetime.now(UTC))
        details = dict(message="来源发生变化，原关闭记录失效，重新告警。" if alert else "来源已移出活动追踪，历史缺口和关闭记录保留。" if retired else "原来源问题已消失，关闭记录转为历史。",
                       previous_fingerprint=row["fingerprint"], current_fingerprint=alert["fingerprint"] if alert else None)
        with ledger.db:
            ledger.db.execute("UPDATE source_alert_closures SET reopened_at=? WHERE id=?", (now, row["id"]))
            ledger.db.execute("INSERT INTO source_alert_audit VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), action, now, row["stream_id"], row["id"], json.dumps(details, ensure_ascii=False)))
    health.update(active_alerts=list(alerts.values()), closed_alerts=closed,
                  active_alert_count=len(alerts), closed_alert_count=len(closed))
    return health
