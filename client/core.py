"""SQLite cursor, stream identities and durable outbox form one transaction."""
from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time

from client.source import Point, read_source, start_point, anchored_points
from server.protocol import COUNTERS, UTC, batch, iso


class OutboxFull(Exception):
    pass


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Ledger:
    def __init__(self, path, identity_key, tenant_id, max_pending=50000, attribution_provider=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secret = bytes.fromhex(identity_key)
        self.max_pending = max_pending
        self.attribution_provider = attribution_provider
        self.capture_started = iso(datetime.now(UTC))
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS session_starts(
            session TEXT PRIMARY KEY,head TEXT NOT NULL,started_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS streams(
            id TEXT PRIMARY KEY, anchor TEXT NOT NULL, anchor_ordinal INTEGER NOT NULL,
            baseline TEXT NOT NULL,last_seq INTEGER NOT NULL,last_point TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'unverified',paused TEXT,next_retry REAL NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS sources(
            path TEXT PRIMARY KEY,stream_id TEXT,session TEXT,offset INTEGER NOT NULL,ordinal INTEGER NOT NULL,
            model TEXT NOT NULL,head TEXT NOT NULL,tail TEXT NOT NULL DEFAULT '',mtime INTEGER NOT NULL,size INTEGER NOT NULL,warnings TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(
            stream_id TEXT NOT NULL,seq INTEGER NOT NULL,event_id TEXT NOT NULL UNIQUE,
            wire TEXT NOT NULL,digest TEXT NOT NULL,acked INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(stream_id,seq));
          CREATE INDEX IF NOT EXISTS pending_events ON events(acked,stream_id,seq);
          CREATE TABLE IF NOT EXISTS source_exclusions(
            path TEXT NOT NULL,stream_id TEXT NOT NULL,decision_id TEXT NOT NULL,
            PRIMARY KEY(path,stream_id));
          CREATE TABLE IF NOT EXISTS source_review_audit(
            id TEXT PRIMARY KEY,created_at TEXT NOT NULL,stream_id TEXT NOT NULL,details TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS archive_checks(
            path TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS source_alert_closures(
            id TEXT PRIMARY KEY,stream_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
            closed_at TEXT NOT NULL,reason TEXT NOT NULL,snapshot TEXT NOT NULL,reopened_at TEXT);
          CREATE UNIQUE INDEX IF NOT EXISTS active_source_alert_closure
            ON source_alert_closures(stream_id) WHERE reopened_at IS NULL;
          CREATE TABLE IF NOT EXISTS source_alert_audit(
            id TEXT PRIMARY KEY,action TEXT NOT NULL,created_at TEXT NOT NULL,
            stream_id TEXT NOT NULL,closure_id TEXT NOT NULL,details TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS source_presence(
            path TEXT PRIMARY KEY,present INTEGER NOT NULL,kind TEXT NOT NULL,observed_at TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS current_source_presence ON source_presence(present,path);
          CREATE TABLE IF NOT EXISTS source_changes(
            id INTEGER PRIMARY KEY,observed_at TEXT NOT NULL,kind TEXT NOT NULL,
            path TEXT NOT NULL,related_path TEXT,message TEXT NOT NULL);
        """)
        if "tail" not in {row[1] for row in self.db.execute("PRAGMA table_info(sources)")}:
            self.db.execute("ALTER TABLE sources ADD COLUMN tail TEXT NOT NULL DEFAULT ''")
        fingerprint = self.identify(["identity-check", tenant_id])
        old = self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if old and old["value"] != fingerprint:
            self.db.close()
            raise ValueError("identity_changed")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('identity',?)", (fingerprint,))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('started_at',?)", (iso(datetime.now(UTC)),))

    def close(self):
        self.db.close()

    def identify(self, value):
        return hmac.new(self.secret, compact(value).encode(), hashlib.sha256).hexdigest()

    def stream_id(self, session):
        return self.identify(["codex-rollout-v1", session])

    def anchor_id(self, stream, point):
        return self.identify(["anchor-v1", stream, point.ordinal, point.signature])

    def record(self, stream, point, seq, baseline=False):
        value = dict(event_id=self.identify(["event-v1", stream, 0, str(seq)]), source_seq=str(seq),
                     kind="baseline" if baseline else "snapshot", event_time=point.time,
                     observed_at=iso(datetime.now(UTC)), model=point.model, counters=point.counters)
        if baseline:
            value["baseline_reason"] = "capture_start"
        return value

    @staticmethod
    def envelope(stream, anchor, records):
        return dict(schema_version="1.1" if any("attribution" in r for r in records) else "1.0", parser_version="windows-jsonl-0.4.0", batch_id="checkpoint-batch",
                    stream_id=stream, epoch=0, anchor_id=anchor, records=records)

    def _put(self, stream, anchor, record):
        digest = batch(self.envelope(stream, anchor, [record]))["records"][0]["digest"]
        prior = self.db.execute("SELECT digest,event_id FROM events WHERE stream_id=? AND seq=?",
                                (stream, int(record["source_seq"]))).fetchone()
        if prior:
            if prior["digest"] != digest or prior["event_id"] != record["event_id"]:
                self.db.execute("UPDATE streams SET paused='unknown_identity' WHERE id=?", (stream,))
                return False
            return True
        if self.pending_count() >= self.max_pending:
            raise OutboxFull()
        self.db.execute("INSERT INTO events(stream_id,seq,event_id,wire,digest) VALUES (?,?,?,?,?)",
                        (stream, int(record["source_seq"]), record["event_id"], compact(record), digest))
        return True

    def scan_file(self, path, force=False):
        path = str(Path(path).resolve())
        stat = Path(path).stat()
        prior = self.db.execute("SELECT * FROM sources WHERE path=?", (path,)).fetchone()
        if not force and prior and prior["mtime"] == stat.st_mtime_ns and prior["size"] == stat.st_size:
            return
        reset = prior is None or stat.st_size < prior["offset"]
        if prior and not reset:
            with open(path, "rb") as handle:
                current_head = hashlib.sha256(handle.readline(8*1024*1024 + 1)).hexdigest()
                start = max(0, prior["offset"]-256)
                handle.seek(start)
                current_tail = hashlib.sha256(handle.read(prior["offset"]-start)).hexdigest()
            reset = prior["head"] != current_head
            if prior["tail"] and current_tail != prior["tail"]:
                reset = True
        scan = read_source(path, offset=0 if reset else prior["offset"], ordinal=-1 if reset else prior["ordinal"],
                           model="unknown" if reset else prior["model"], session=None if reset else prior["session"],
                           old_warnings=() if reset else json.loads(prior["warnings"]))
        stream_id = self.stream_id(scan.session) if scan.session else None
        with open(path, "rb") as handle:
            start = max(0, scan.offset-256)
            handle.seek(start)
            tail = hashlib.sha256(handle.read(scan.offset-start)).hexdigest()
        with self.db:
            if (scan.session and scan.started_at and not scan.unsupported and not scan.warnings
                    and self.capture_started <= scan.started_at <= iso(datetime.now(UTC))):
                self.db.execute("INSERT OR IGNORE INTO session_starts VALUES(?,?,?)",
                                (scan.session, scan.head, scan.started_at))
            excluded = stream_id and self.db.execute("SELECT 1 FROM source_exclusions WHERE path=? AND stream_id=?", (path,stream_id)).fetchone()
            if stream_id and not scan.unsupported and not excluded:
                stream = self.db.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
                if stream is None and scan.points:
                    point = scan.points[-1]
                    observed = self.db.execute("SELECT * FROM session_starts WHERE session=?", (scan.session,)).fetchone()
                    if (observed and observed["head"] == scan.head and observed["started_at"] == scan.started_at
                            and not scan.warnings and scan.started_at <= scan.points[0].time):
                        point = start_point(scan, scan.points[0].model)
                    anchor = self.anchor_id(stream_id, point)
                    baseline = self.record(stream_id, point, 0, baseline=True)
                    self.db.execute("""INSERT INTO streams(id,anchor,anchor_ordinal,baseline,last_seq,last_point)
                        VALUES (?,?,?,?,0,?)""", (stream_id, anchor, point.ordinal, compact(baseline), compact(baseline)))
                    self._put(stream_id, anchor, baseline)
                    if point.ordinal == -1:
                        stream = self.db.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
                        self._advance(stream, scan.points)
                elif stream and not stream["paused"]:
                    # A renamed, archived or copied file must prove the same immutable anchor.
                    if reset:
                        matching = next((p for p in anchored_points(scan, stream) if p.ordinal == stream["anchor_ordinal"]), None)
                        if matching is None or self.anchor_id(stream_id, matching) != stream["anchor"]:
                            self.db.execute("UPDATE streams SET paused='unknown_identity' WHERE id=?", (stream_id,))
                        else:
                            self._advance(stream, scan.points)
                    else:
                        self._advance(stream, scan.points)
            self.db.execute("""INSERT INTO sources(path,stream_id,session,offset,ordinal,model,head,tail,mtime,size,warnings)
                VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET stream_id=excluded.stream_id,session=excluded.session,
                offset=excluded.offset,ordinal=excluded.ordinal,model=excluded.model,head=excluded.head,
                tail=excluded.tail,mtime=excluded.mtime,size=excluded.size,warnings=excluded.warnings""",
                (path, stream_id, scan.session, scan.offset, scan.ordinal, scan.model, scan.head, tail,
                 stat.st_mtime_ns, stat.st_size, compact(sorted(scan.warnings))))

    def _advance(self, stream, points):
        last_seq = stream["last_seq"]
        previous = json.loads(stream["last_point"])
        for point in points:
            seq = point.ordinal - stream["anchor_ordinal"]
            if seq <= 0:
                continue
            wire = self.record(stream["id"], point, seq)
            if seq > last_seq and self.attribution_provider:
                claim = self.attribution_provider(stream["id"], previous, point)
                if claim: wire["attribution"] = claim
            if seq > last_seq + 1:
                self.db.execute("UPDATE streams SET paused='source_missing' WHERE id=?", (stream["id"],))
                break
            if seq <= last_seq:
                if not self._put(stream["id"], stream["anchor"], wire):
                    break
                continue
            vector = tuple(int(point.counters[k]) - int(previous["counters"][k]) for k in COUNTERS)
            problem = None
            if point.time < previous["event_time"]:
                problem = "clock_anomaly"
            elif any(n < 0 for n in vector) or vector[1] > vector[0] or vector[3] > vector[2]:
                problem = "unsupported_usage"
            if problem:
                self.db.execute("UPDATE streams SET paused=? WHERE id=?", (problem, stream["id"]))
                break
            if not self._put(stream["id"], stream["anchor"], wire):
                break
            last_seq, previous = seq, wire
        self.db.execute("UPDATE streams SET last_seq=?,last_point=? WHERE id=?", (last_seq, compact(previous), stream["id"]))

    def rebase(self, stream_id, remote):
        """A second device locates the shared anchor in the full source, then replays identical sequence IDs."""
        paths = self.db.execute("""SELECT s.path FROM sources s WHERE s.stream_id=? AND NOT EXISTS
            (SELECT 1 FROM source_exclusions e WHERE e.path=s.path AND e.stream_id=s.stream_id)""", (stream_id,)).fetchall()
        selected = None
        for row in paths:
            if not Path(row["path"]).exists():
                continue
            scan = read_source(row["path"])
            if scan.unsupported:
                continue
            # A remote device may have observed this session from its creation.
            baseline = remote["baseline_event"]
            zero = start_point(scan, baseline["model"])
            candidates = ([zero] if zero else []) + scan.points
            point = next((p for p in candidates if self.anchor_id(stream_id, p) == remote["anchor_id"]), None)
            if point:
                baseline = remote["baseline_event"]
                if point.time == baseline["event_time"] and point.model == baseline["model"] and point.counters == baseline["counters"]:
                    selected = scan, point
                    break
        if selected is None:
            self.pause(stream_id, "unknown_identity")
            return False
        scan, point = selected
        baseline = remote["baseline_event"]
        if baseline["baseline_reason"] != "capture_start" or baseline["event_id"] != self.record(stream_id, point, 0, True)["event_id"]:
            self.pause(stream_id, "unknown_identity")
            return False
        with self.db:
            self.db.execute("DELETE FROM events WHERE stream_id=?", (stream_id,))
            self.db.execute("""UPDATE streams SET anchor=?,anchor_ordinal=?,baseline=?,last_seq=0,last_point=?,
                mode='ready',paused=NULL,next_retry=0,attempts=0 WHERE id=?""",
                (remote["anchor_id"], point.ordinal, compact(baseline), compact(baseline), stream_id))
            self._put(stream_id, remote["anchor_id"], baseline)
            stream = self.db.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
            self._advance(stream, scan.points)
        return True

    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM events WHERE acked=0").fetchone()[0]

    def next_stream(self):
        return self.db.execute("""SELECT * FROM streams s WHERE paused IS NULL AND next_retry<=?
            AND EXISTS(SELECT 1 FROM events e WHERE e.stream_id=s.id AND e.acked=0) ORDER BY rowid LIMIT 1""", (time.time(),)).fetchone()

    def pending_batch(self, stream_id):
        stream = self.db.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
        rows = self.db.execute("SELECT wire FROM events WHERE stream_id=? AND acked=0 ORDER BY seq LIMIT 500", (stream_id,)).fetchall()
        records = []
        for row in rows:
            candidate = records + [json.loads(row["wire"])]
            if len(compact(self.envelope(stream_id, stream["anchor"], candidate)).encode()) > 500*1024:
                break
            records = candidate
        return self.envelope(stream_id, stream["anchor"], records)

    def acknowledge(self, stream_id, sent, ack):
        if ack.get("accepted", -1) + ack.get("duplicates", -1) != len(sent["records"]):
            raise ValueError("invalid_ack")
        durable, processed = int(ack["durable_through"]), int(ack["processed_through"])
        if processed > durable or durable < -1:
            raise ValueError("invalid_ack")
        with self.db:
            self.db.executemany("UPDATE events SET acked=1 WHERE stream_id=? AND seq=?",
                                [(stream_id, int(r["source_seq"])) for r in sent["records"]])
            if ack.get("pending_gap"):
                self.db.execute("UPDATE events SET acked=0 WHERE stream_id=? AND seq>?", (stream_id, durable))
            self.db.execute("UPDATE streams SET next_retry=0,attempts=0 WHERE id=?", (stream_id,))

    def retry(self, stream_id, delay):
        with self.db:
            self.db.execute("UPDATE streams SET attempts=attempts+1,next_retry=? WHERE id=?", (time.time()+delay, stream_id))

    def pause(self, stream_id, reason):
        with self.db:
            self.db.execute("UPDATE streams SET paused=? WHERE id=?", (reason, stream_id))

    def ready(self, stream_id, replay=False):
        with self.db:
            self.db.execute("UPDATE streams SET mode='ready' WHERE id=?", (stream_id,))
            if replay:
                self.db.execute("UPDATE events SET acked=0 WHERE stream_id=?", (stream_id,))

    def source_health(self, compressed_paths=(), refresh=False):
        from client.source_archives import source_health
        return source_health(self, compressed_paths, refresh)

    def existing_only(self):
        return self.db.execute("SELECT 1 FROM meta WHERE key='source_tracking' AND value='existing'").fetchone() is not None

    def source_rows(self):
        query = """SELECT s.* FROM sources s WHERE NOT EXISTS
            (SELECT 1 FROM source_exclusions e WHERE e.path=s.path AND e.stream_id=s.stream_id)"""
        if self.existing_only():
            query += """ AND EXISTS (SELECT 1 FROM source_presence p
                WHERE p.present=1 AND (p.path=s.path OR p.path=s.path||'.zst'))"""
        return self.db.execute(query).fetchall()

    def paused_sources(self):
        active = {row['stream_id'] for row in self.source_rows()} if self.existing_only() else None
        return [row for row in self.db.execute("SELECT * FROM streams WHERE paused IS NOT NULL")
                if active is None or row['id'] in active]

    def warnings(self, health=None):
        reasons = set()
        for row in self.source_rows():
            reasons.update(json.loads(row["warnings"]))
        reasons.update(row['paused'] for row in self.paused_sources())
        if self.existing_only() and self.db.execute("SELECT 1 FROM source_presence WHERE present=0 LIMIT 1").fetchone():
            # Coverage remains partial even though retired paths are no longer active alarms.
            reasons.add("retired_source_history")
        health = health if health is not None else self.source_health()
        if health["missing"]:
            reasons.add("source_missing")
        if health["unverified_archives"]:
            reasons.update({"compressed_source_unverified", "unsupported_usage"})
        if health["unregistered_archives"]:
            reasons.add("unsupported_usage")
        return reasons
