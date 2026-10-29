"""Integration tests require a disposable PostgreSQL database (never production)."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import os
import secrets
import unittest
import uuid

from server.admin import migrate
from server.protocol import ApiError, UTC, batch, iso, token_hash
from server.store import Store
from server.tests.helpers import make_batch, make_record

DSN = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(DSN, "TEST_DATABASE_URL must identify a disposable PostgreSQL database")
class LedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        migrate(DSN, Path(__file__).parents[1] / "schema.sql")
        cls.store = Store(DSN)

    def setUp(self):
        self.tenant, self.device = uuid.uuid4(), uuid.uuid4()
        self.write, self.read = secrets.token_hex(32), secrets.token_hex(32)
        with self.store.connect() as db:
            db.execute("INSERT INTO tenants(id) VALUES (%s)", (self.tenant,))
            db.execute("INSERT INTO devices(tenant_id,id) VALUES (%s,%s)", (self.tenant, self.device))
            for token, scope, device in ((self.write, "write", self.device), (self.read, "read", None)):
                db.execute("INSERT INTO credentials(token_hash,tenant_id,device_id,scope) VALUES (%s,%s,%s,%s)",
                           (token_hash(token), self.tenant, device, scope))
        self.store.heartbeat(self.write, dict(queued_records=0, oldest_queued_at=None,
                    last_scan_at=datetime.now(UTC), quality="complete", quality_reasons=[]))

    def tearDown(self):
        # All rows belong to this test's random tenant, on the disposable database.
        with self.store.connect() as db:
            for table in ("account_display_names","quota_boundary_states","quota_boundary_samples","quota_cycle_totals","quota_cycle_observations","quota_cycles","source_observations","event_subjects","device_subjects","principals","machines","accounts","ingest_conflicts", "usage_deltas", "usage_events", "streams", "credentials", "devices", "tenants"):
                field = "id" if table == "tenants" else "tenant_id"
                db.execute(f"DELETE FROM {table} WHERE {field}=%s", (self.tenant,))

    def ingest(self, data):
        return self.store.ingest(self.write, batch(data))

    def totals(self):
        return self.store.summary(self.read)["windows"][0]["totals"]

    def test_duplicate_and_concurrent_retry_count_once(self):
        data = make_batch()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.ingest(data), range(4)))
        self.assertEqual(sum(r["accepted"] for r in results), 2)
        self.assertEqual(sum(r["duplicates"] for r in results), 6)
        self.assertEqual(self.totals(), dict(input="100", cached_input="20", output="50", reasoning_output="10", total="150"))

    def test_gap_backfill_advances_atomically(self):
        self.ingest(make_batch([make_record(0), make_record(2)]))
        state = self.store.state(self.write, "a"*64, 0)
        self.assertEqual(state["durable_through"], "0")
        self.assertEqual(self.totals()["total"], "0")
        ack = self.ingest(make_batch([make_record(1)]))
        self.assertEqual((ack["durable_through"], ack["processed_through"]), ("2", "2"))
        self.assertFalse(ack["pending_gap"])
        self.assertEqual(self.totals()["total"], "300")

    def test_conflict_rolls_back_entire_batch_and_audits(self):
        self.ingest(make_batch([make_record(0), make_record(2)]))
        conflicting = make_record(2, (999, 40, 100, 20))
        with self.assertRaises(ApiError) as caught:
            # seq 1 is inserted before the seq 2 conflict is discovered; both must roll back.
            self.ingest(make_batch([make_record(1), conflicting]))
        self.assertEqual(caught.exception.status, 409)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) n FROM usage_events WHERE tenant_id=%s", (self.tenant,)).fetchone()["n"], 2)
            self.assertEqual(db.execute("SELECT count(*) n FROM ingest_conflicts WHERE tenant_id=%s", (self.tenant,)).fetchone()["n"], 1)
            self.assertIsNone(db.execute("SELECT 1 FROM usage_events WHERE tenant_id=%s AND source_seq=1", (self.tenant,)).fetchone())
            conflict = db.execute("SELECT source_seq,expected_hash,received_hash FROM ingest_conflicts WHERE tenant_id=%s", (self.tenant,)).fetchone()
            self.assertEqual(conflict["source_seq"], 2)
            self.assertNotEqual(conflict["expected_hash"], conflict["received_hash"])
        self.assertEqual(self.totals()["total"], "0")

    def test_identical_stream_ids_cannot_cross_tenant_boundary(self):
        self.ingest(make_batch())
        other_tenant, other_device = uuid.uuid4(), uuid.uuid4()
        other_read, other_write = secrets.token_hex(32), secrets.token_hex(32)
        try:
            with self.store.connect() as db:
                db.execute("INSERT INTO tenants(id) VALUES (%s)", (other_tenant,))
                db.execute("INSERT INTO devices(tenant_id,id) VALUES (%s,%s)", (other_tenant, other_device))
                for token, scope, device in ((other_read, "read", None), (other_write, "write", other_device)):
                    db.execute("INSERT INTO credentials(token_hash,tenant_id,device_id,scope) VALUES (%s,%s,%s,%s)",
                               (token_hash(token), other_tenant, device, scope))
            self.assertEqual(self.store.summary(other_read)["windows"][0]["totals"]["total"], "0")
            with self.assertRaises(ApiError) as caught:
                self.store.state(other_write, "a"*64, 0)
            self.assertEqual(caught.exception.status, 404)
            other_batch = make_batch([make_record(0), make_record(1, (200, 40, 100, 20))])
            self.store.ingest(other_write, batch(other_batch))
            self.assertEqual(self.store.summary(other_read)["windows"][0]["totals"]["total"], "300")
            self.assertEqual(self.totals()["total"], "150")
        finally:
            with self.store.connect() as db:
                for table in ("quota_cycle_totals","quota_cycle_observations","quota_cycles","source_observations","event_subjects","device_subjects","principals","machines","accounts","ingest_conflicts", "usage_deltas", "usage_events", "streams", "credentials", "devices", "tenants"):
                    field = "id" if table == "tenants" else "tenant_id"
                    db.execute(f"DELETE FROM {table} WHERE {field}=%s", (other_tenant,))

    def test_cross_device_copy_uses_same_anchor_and_owner(self):
        data = make_batch()
        self.ingest(data)
        second, token = uuid.uuid4(), secrets.token_hex(32)
        with self.store.connect() as db:
            db.execute("INSERT INTO devices(tenant_id,id) VALUES (%s,%s)", (self.tenant, second))
            db.execute("INSERT INTO credentials(token_hash,tenant_id,device_id,scope) VALUES (%s,%s,%s,'write')",
                       (token_hash(token), self.tenant, second))
        duplicate = copy.deepcopy(data)
        duplicate["records"][1]["observed_at"] = iso(datetime.now(UTC))
        self.assertEqual(self.store.ingest(token, batch(duplicate))["duplicates"], 2)
        self.assertEqual(self.store.summary(self.read, str(second))["windows"][0]["totals"]["total"], "0")
        duplicate["anchor_id"] = "d"*64
        with self.assertRaises(ApiError):
            self.store.ingest(token, batch(duplicate))

    def test_negative_reset_and_future_time_are_quarantined(self):
        self.ingest(make_batch())
        ack = self.ingest(make_batch([make_record(2, (50, 10, 25, 5))]))
        self.assertIn("counter_reset", ack["quality_reasons"])
        self.assertEqual(ack["processed_through"], "1")
        self.assertEqual(self.totals()["total"], "150")
        ack = self.ingest(make_batch([make_record(3)]))
        self.assertEqual(ack["processed_through"], "1")
        future = make_record(0, when=datetime.now(UTC)+timedelta(hours=1))
        future["event_id"] = hashlib.sha256(b"future").hexdigest()
        ack = self.ingest(make_batch([future], stream="e"*64))
        self.assertIn("clock_anomaly", ack["quality_reasons"])
        self.assertEqual(ack["processed_through"], "-1")

    def test_capture_start_does_not_assign_old_total_to_today(self):
        baseline = make_record(0, (10000, 2000, 5000, 1000), reason="capture_start")
        after = make_record(1, (10100, 2020, 5050, 1010))
        ack = self.ingest(make_batch([baseline, after]))
        self.assertEqual(self.totals()["total"], "150")
        self.assertIn("history_not_imported", ack["quality_reasons"])

    def test_tenant_scope_revocation_and_missing_baseline(self):
        with self.assertRaises(ApiError):
            self.store.summary(self.write)
        with self.assertRaises(ApiError):
            self.ingest(make_batch([make_record(1)]))
        self.ingest(make_batch())
        with self.store.connect() as db:
            db.execute("UPDATE credentials SET revoked_at=now() WHERE token_hash=%s", (token_hash(self.write),))
        with self.assertRaises(ApiError) as caught:
            self.ingest(make_batch())
        self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(ApiError):
            self.store.summary(self.read, str(uuid.uuid4()))

    def test_half_open_periods_cross_midnight_and_exact_5h(self):
        now = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)
        # Earliest increment is exactly at rolling start; the final increment is exactly at as_of.
        moments = [now-timedelta(days=1), now-timedelta(hours=5), now-timedelta(seconds=1), now]
        self.ingest(make_batch([make_record(i, when=t) for i,t in enumerate(moments)]))
        result = self.store.summary(self.read, now=now)
        periods = {w["name"]: w["totals"]["total"] for w in result["windows"]}
        self.assertEqual(periods["rolling_5h"], "300")
        self.assertEqual(periods["today"], "0")
        self.assertEqual(periods["week"], "0")
        self.assertEqual(periods["month"], "300")

    def test_database_reconnection_and_large_aggregates(self):
        self.ingest(make_batch([make_record(0), make_record(1, (2**63-1, 0, 2**63-1, 0))]))
        reconnected = Store(DSN)
        self.assertEqual(reconnected.summary(self.read)["windows"][0]["totals"]["total"], "18446744073709551614")

    def test_stale_scan_cannot_claim_complete_coverage(self):
        self.ingest(make_batch())
        with self.store.connect() as db:
            db.execute("UPDATE devices SET last_scan_at=now()-interval '1 hour' WHERE tenant_id=%s", (self.tenant,))
        summary = self.store.summary(self.read)
        self.assertEqual(summary["quality"], "partial")
        self.assertIn("scan_stale", summary["quality_reasons"])


if __name__ == "__main__":
    unittest.main()
