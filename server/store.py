"""All acknowledgement, delta and watermark writes share a database transaction."""
import os
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from server.identities import IdentityStore
from server.cycles import QuotaCycleStore
from server.dashboard import DashboardStore

from server.protocol import ApiError, COUNTERS, UTC, iso, windows, token_hash


class Store(IdentityStore,QuotaCycleStore,DashboardStore):
    def __init__(self, dsn=None):
        self.dsn = dsn or os.environ["DATABASE_URL"]

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5,
                               options="-c statement_timeout=15000 -c lock_timeout=5000 -c timezone=UTC")

    def ready(self):
        with self.connect() as db:
            db.execute("SELECT 1 FROM tenants LIMIT 1")
        return {"status": "ready"}

    @staticmethod
    def authenticate(db, token, scope):
        row = db.execute("""SELECT c.tenant_id,c.device_id,c.scope,c.reader_device_id,d.status,rd.status AS reader_status
            FROM credentials c LEFT JOIN devices d ON d.tenant_id=c.tenant_id AND d.id=c.device_id
            LEFT JOIN devices rd ON rd.tenant_id=c.tenant_id AND rd.id=c.reader_device_id
            WHERE c.token_hash=%s AND c.revoked_at IS NULL""", (token_hash(token),)).fetchone()
        if row is None:
            raise ApiError("unauthorized", 401)
        if row["scope"] != scope or (row["device_id"] and row["status"] != "active") or (row["reader_device_id"] and row["reader_status"]!="active"):
            raise ApiError("forbidden", 403)
        return row

    @staticmethod
    def key(auth, data):
        return auth["tenant_id"], data["stream_id"], data["epoch"]

    def ingest(self, token, data):
        auth = None
        try:
            with self.connect() as db:
                auth = self.authenticate(db, token, "write")
                result = self._ingest(db, auth, data)
            # Exiting the connection commits. Never return a successful ACK before it.
            return result
        except ApiError as error:
            if error.status == 409 and auth:
                with self.connect() as audit:
                    audit.execute("""INSERT INTO ingest_conflicts
                        (tenant_id,stream_id,epoch,source_seq,error_code,expected_hash,received_hash)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (*self.key(auth, data), getattr(error, "source_seq", data["records"][0]["seq"]), error.code,
                         getattr(error, "expected_hash", None), getattr(error, "received_hash", None)))
            raise

    def _ingest(self, db, auth, data):
        key = self.key(auth, data)
        # Serializes first creation as well as updates; hash collisions only serialize extra streams.
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("/".join(map(str, key)),))
        stream = db.execute("SELECT * FROM streams WHERE tenant_id=%s AND stream_id=%s AND epoch=%s FOR UPDATE", key).fetchone()
        if stream is None:
            baseline = next((r for r in data["records"] if r["seq"] == 0), None)
            if baseline is None:
                raise ApiError("baseline_required", 409)
            db.execute("""INSERT INTO streams(tenant_id,stream_id,epoch,anchor_id,owner_device_id,baseline_reason)
                VALUES (%s,%s,%s,%s,%s,%s)""", (*key, data["anchor_id"], auth["device_id"], baseline["baseline_reason"]))
            stream = db.execute("SELECT * FROM streams WHERE tenant_id=%s AND stream_id=%s AND epoch=%s", key).fetchone()
        if stream["anchor_id"] != data["anchor_id"]:
            raise ApiError("anchor_conflict", 409)
        accepted = duplicates = 0
        for record in sorted(data["records"], key=lambda r: r["seq"]):
            claim = record.get("attribution")
            self.validate_claim(db, auth, claim)
            inserted = db.execute("""INSERT INTO usage_events(tenant_id,stream_id,epoch,source_seq,event_id,
                semantic_hash,kind,event_time,observed_at,received_device_id,model,parser_version,
                cumulative_input,cumulative_cached_input,cumulative_output,cumulative_reasoning_output,attribution_claim)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING RETURNING source_seq""",
                (*key, record["seq"], record["event_id"], record["digest"], record["kind"], record["time"],
                 record["observed"], auth["device_id"], record["model"], data["parser_version"], *record["vector"], Jsonb(claim) if claim else None)).fetchone()
            if inserted:
                accepted += 1
            else:
                prior = db.execute("""SELECT semantic_hash,event_id FROM usage_events
                    WHERE tenant_id=%s AND stream_id=%s AND epoch=%s AND source_seq=%s""", (*key, record["seq"])).fetchone()
                if not prior or prior["semantic_hash"] != record["digest"] or prior["event_id"] != record["event_id"]:
                    error = ApiError("immutable_event_conflict", 409)
                    error.source_seq = record["seq"]
                    error.expected_hash = prior["semantic_hash"] if prior else None
                    error.received_hash = record["digest"]
                    raise error
                duplicates += 1
            db.execute("INSERT INTO source_observations(tenant_id,event_id,device_id,claimed_account) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                       (auth["tenant_id"], record["event_id"], auth["device_id"], claim["account_key"] if claim else None))
        durable = stream["durable_through"]
        for row in db.execute("""SELECT source_seq FROM usage_events
                WHERE tenant_id=%s AND stream_id=%s AND epoch=%s AND source_seq>%s ORDER BY source_seq""", (*key, durable)):
            if row["source_seq"] != durable + 1:
                break
            durable += 1
        processed = stream["processed_through"]
        previous = db.execute("""SELECT * FROM usage_events WHERE tenant_id=%s AND stream_id=%s AND epoch=%s
            AND source_seq=%s""", (*key, processed)).fetchone() if processed >= 0 else None
        reasons = set()
        if stream["baseline_reason"] != "known_zero":
            reasons.add("history_not_imported" if stream["baseline_reason"] == "capture_start" else "inherited_baseline")
        now = datetime.now(UTC)
        rows = db.execute("""SELECT * FROM usage_events WHERE tenant_id=%s AND stream_id=%s AND epoch=%s
            AND source_seq>%s AND source_seq<=%s ORDER BY source_seq""", (*key, processed, durable)).fetchall()
        # Acquire before assign_subject can insert an account row. Metadata registration takes
        # the same lock before its account insert, so unique-index waits cannot invert the order.
        accounts={r["attribution_claim"]["account_key"] for r in rows if r["attribution_claim"] and r["attribution_claim"]["account_key"]}
        for account in sorted(accounts):
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(str(auth["tenant_id"])+"/quota/"+account,))
        for record in rows:
            problem = None
            if record["processing_state"] == "quarantined":
                reasons.update(set(stream["quality_reasons"]) & {"clock_anomaly", "counter_reset", "invalid_delta_subset"})
                reasons.add("quarantined")
                break
            vector = tuple(record["cumulative_" + k] for k in COUNTERS)
            if record["event_time"] > now + timedelta(minutes=5) or (previous and record["event_time"] < previous["event_time"]):
                problem = "clock_anomaly"
            delta = None
            if previous:
                delta = tuple(a - previous["cumulative_" + k] for a, k in zip(vector, COUNTERS))
                if any(n < 0 for n in delta):
                    problem = "counter_reset"
                elif delta[1] > delta[0] or delta[3] > delta[2]:
                    problem = "invalid_delta_subset"
            if problem:
                db.execute("""UPDATE usage_events SET processing_state='quarantined'
                    WHERE tenant_id=%s AND stream_id=%s AND epoch=%s AND source_seq=%s""", (*key, record["source_seq"]))
                reasons.add(problem)
                break
            if delta is not None:
                self.assign_subject(db, key, record, previous)
                db.execute("""INSERT INTO usage_deltas(tenant_id,stream_id,epoch,source_seq,event_time,model,
                    input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (*key, record["source_seq"], record["event_time"], record["model"], *delta))
            db.execute("""UPDATE usage_events SET processing_state='processed'
                WHERE tenant_id=%s AND stream_id=%s AND epoch=%s AND source_seq=%s""", (*key, record["source_seq"]))
            processed = record["source_seq"]
            previous = record
        maximum = db.execute("""SELECT max(source_seq) AS maximum FROM usage_events
            WHERE tenant_id=%s AND stream_id=%s AND epoch=%s""", key).fetchone()["maximum"]
        self.refresh_cycles_after_ingest(db,key,stream["processed_through"],processed)
        gap = maximum > durable
        if gap:
            reasons.add("pending_gap")
        quality = "partial" if reasons else "complete"
        db.execute("""UPDATE streams SET durable_through=%s,processed_through=%s,quality=%s,quality_reasons=%s,updated_at=now()
            WHERE tenant_id=%s AND stream_id=%s AND epoch=%s""", (durable, processed, quality, sorted(reasons), *key))
        db.execute("UPDATE devices SET last_seen_at=now() WHERE tenant_id=%s AND id=%s", (auth["tenant_id"], auth["device_id"]))
        return dict(accepted=accepted, duplicates=duplicates, durable_through=str(durable),
                    processed_through=str(processed), pending_gap=gap, quality=quality, quality_reasons=sorted(reasons))

    def heartbeat(self, token, data):
        with self.connect() as db:
            auth = self.authenticate(db, token, "write")
            now = datetime.now(UTC)
            reasons = set(data["quality_reasons"])
            if any(data[k] and data[k] > now + timedelta(minutes=5) for k in ("oldest_queued_at", "last_scan_at")):
                reasons.add("clock_anomaly")
            quality = "partial" if reasons else data["quality"]
            db.execute("""UPDATE devices SET last_seen_at=%s,queued_records=%s,oldest_queued_at=%s,last_scan_at=%s,
                health_quality=%s,health_reasons=%s WHERE tenant_id=%s AND id=%s""",
                (now, data["queued_records"], data["oldest_queued_at"], data["last_scan_at"], quality,
                 sorted(reasons), auth["tenant_id"], auth["device_id"]))
        return {"received_at": iso(now)}

    @staticmethod
    def device_quality(device, now):
        reasons = set(device["health_reasons"])
        if device["status"] != "active":
            reasons.add("device_" + device["status"])
        if not device["last_seen_at"] or (now - device["last_seen_at"]).total_seconds() > 180:
            reasons.add("device_offline")
        if not device["last_scan_at"] or (now - device["last_scan_at"]).total_seconds() > 180:
            reasons.add("scan_stale")
        if device["queued_records"]:
            reasons.add("pending_upload")
        if device["health_quality"] != "complete":
            reasons.add("device_coverage_" + device["health_quality"])
        return reasons

    def devices(self, token):
        now = datetime.now(UTC)
        with self.connect() as db:
            auth = self.authenticate(db, token, "read")
            rows = db.execute("SELECT * FROM devices WHERE tenant_id=%s AND (%s::uuid IS NULL OR id=%s::uuid) ORDER BY id", (auth["tenant_id"],auth["reader_device_id"],auth["reader_device_id"])).fetchall()
        return {"devices": [dict(device_id=str(d["id"]), status=d["status"],
                last_seen_at=iso(d["last_seen_at"]) if d["last_seen_at"] else None,
                queued_records=str(d["queued_records"]), quality="partial" if self.device_quality(d, now) else "complete") for d in rows]}

    def state(self, token, stream_id, epoch):
        with self.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            auth = self.authenticate(db, token, "write")
            key = auth["tenant_id"], stream_id, epoch
            stream = db.execute("SELECT * FROM streams WHERE tenant_id=%s AND stream_id=%s AND epoch=%s", key).fetchone()
            if not stream:
                raise ApiError("stream_not_found", 404)
            baseline = db.execute("SELECT * FROM usage_events WHERE tenant_id=%s AND stream_id=%s AND epoch=%s AND source_seq=0", key).fetchone()
        record = dict(event_id=baseline["event_id"], source_seq="0", kind="baseline", event_time=iso(baseline["event_time"]),
                      observed_at=iso(baseline["observed_at"]), model=baseline["model"], baseline_reason=stream["baseline_reason"],
                      counters={k: str(baseline["cumulative_" + k]) for k in COUNTERS})
        return dict(stream_id=stream_id, epoch=epoch, anchor_id=stream["anchor_id"], baseline_reason=stream["baseline_reason"],
                    baseline_event=record, durable_through=str(stream["durable_through"]),
                    processed_through=str(stream["processed_through"]), quality=stream["quality"], quality_reasons=stream["quality_reasons"])

    def summary(self, token, device_id=None, now=None):
        with self.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            auth = self.authenticate(db, token, "read")
            if auth["reader_device_id"]:
                return self.scoped_snapshot(db, auth, {}, now=now, legacy_device=device_id)
            now = now or db.execute("SELECT transaction_timestamp() AS t").fetchone()["t"]
            now = now.replace(microsecond=now.microsecond // 1000 * 1000)
            devices = db.execute("SELECT * FROM devices WHERE tenant_id=%s AND (%s::uuid IS NULL OR id=%s::uuid)",
                                 (auth["tenant_id"], device_id, device_id)).fetchall()
            if device_id and not devices:
                raise ApiError("device_not_found", 404)
            reasons = set()
            for device in devices:
                reasons.update(self.device_quality(device, now))
            if not devices:
                reasons.add("no_registered_sources")
            streams = db.execute("SELECT quality,quality_reasons FROM streams WHERE tenant_id=%s AND (%s::uuid IS NULL OR owner_device_id=%s::uuid)",
                                 (auth["tenant_id"], device_id, device_id)).fetchall()
            if not streams:
                reasons.add("no_usage_observed")
            for stream in streams:
                reasons.update(stream["quality_reasons"])
                if stream["quality"] != "complete" and not stream["quality_reasons"]:
                    reasons.add("stream_coverage_unknown")
            output = []
            for name, start in windows(now):
                row = db.execute("""SELECT COALESCE(sum(d.input_tokens),0) AS input,
                    COALESCE(sum(d.cached_input_tokens),0) AS cached_input, COALESCE(sum(d.output_tokens),0) AS output,
                    COALESCE(sum(d.reasoning_output_tokens),0) AS reasoning_output, COALESCE(sum(d.total_tokens),0) AS total
                    FROM usage_deltas d JOIN streams s USING(tenant_id,stream_id,epoch)
                    WHERE d.tenant_id=%s AND d.event_time >= %s AND d.event_time < %s
                    AND (%s::uuid IS NULL OR s.owner_device_id=%s::uuid)""",
                    (auth["tenant_id"], start, now, device_id, device_id)).fetchone()
                output.append(dict(name=name, start=iso(start), end=iso(now), totals={k: str(v) for k, v in row.items()}))
            lag = db.execute("""SELECT max(EXTRACT(EPOCH FROM (e.received_at-e.event_time))) AS lag
                FROM usage_events e JOIN streams s USING(tenant_id,stream_id,epoch)
                WHERE e.tenant_id=%s AND e.received_at >= %s AND (%s::uuid IS NULL OR s.owner_device_id=%s::uuid)""",
                (auth["tenant_id"], now - timedelta(minutes=10), device_id, device_id)).fetchone()["lag"]
        quality = "unknown" if not devices or not streams else ("partial" if reasons else "complete")
        return dict(as_of=iso(now), timezone="Asia/Shanghai", windows=output, quality=quality,
                    quality_reasons=sorted(reasons), ingestion_lag_seconds=max(0, int(lag)) if lag is not None else None)
