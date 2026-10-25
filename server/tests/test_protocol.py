import copy
import json
import unittest
from datetime import datetime

from starlette.testclient import TestClient

from server.app import create_app
from server.protocol import ApiError, UTC, batch, iso, parse_json, windows
from server.tests.helpers import make_batch, make_record


class ProtocolTests(unittest.TestCase):
    def test_hash_ignores_transport_and_normalizes_timestamp(self):
        first = make_batch()
        changed = copy.deepcopy(first)
        changed["parser_version"] = "compatible-new-parser"
        changed["batch_id"] = "retransmitted"
        changed["records"][1]["observed_at"] = "2026-10-25T10:00:00.001Z"
        instant = first["records"][1]["event_time"]
        changed["records"][1]["event_time"] = instant.replace("Z", "+00:00")
        self.assertEqual(batch(first)["records"][1]["digest"], batch(changed)["records"][1]["digest"])
        changed["records"][1]["counters"]["input"] = "101"
        self.assertNotEqual(batch(first)["records"][1]["digest"], batch(changed)["records"][1]["digest"])

    def test_rejects_precision_loss_and_unknown_fields(self):
        for value in ("01", "-1", "9223372036854775808", 42, 1.0, True):
            with self.subTest(value=value):
                data = make_batch()
                data["records"][1]["counters"]["input"] = value
                with self.assertRaises(ApiError):
                    batch(data)
        data = make_batch()
        data["records"][1]["conversation"] = "must never be accepted"
        with self.assertRaises(ApiError):
            batch(data)
        with self.assertRaises(ApiError):
            parse_json(b'{"a":1,"a":2}')

    def test_big_int_preserved_and_counter_subset(self):
        data = make_batch([make_record(0), make_record(1, (9223372036854775807, 0, 9223372036854775807, 0))])
        self.assertEqual(batch(data)["records"][1]["vector"][0], 9223372036854775807)
        data["records"][1]["counters"]["cached_input"] = "9223372036854775807"
        data["records"][1]["counters"]["input"] = "1"
        with self.assertRaises(ApiError):
            batch(data)

    def test_shanghai_monday_month_and_year_boundaries(self):
        now = datetime(2026, 10, 25, 16, 0, 0, tzinfo=UTC)  # Monday 00:00 in Shanghai
        start = dict(windows(now))
        self.assertEqual(iso(start["today"]), "2026-10-25T16:00:00.000Z")
        self.assertEqual(start["today"], start["week"])
        self.assertEqual(iso(start["month"]), "2026-09-30T16:00:00.000Z")
        start = dict(windows(datetime(2026, 12, 31, 16, tzinfo=UTC)))
        self.assertEqual(iso(start["month"]), "2026-12-31T16:00:00.000Z")

    def test_malformed_types_are_validation_errors(self):
        for value in ([], {}, 123, True, None):
            data = make_batch()
            data["records"][0]["baseline_reason"] = value
            with self.assertRaises(ApiError):
                batch(data)

    def test_attribution_changes_neither_numeric_identity_nor_legacy_hash(self):
        original = make_batch()
        updated = make_batch(copy.deepcopy(original["records"]))
        updated["schema_version"] = "1.1"
        updated["records"][1]["attribution"] = dict(account_key="b"*64,principal_key="d"*64,machine_key="e"*64,observed_from="2026-10-25T00:00:00Z")
        self.assertEqual(batch(original)["records"][1]["digest"],batch(updated)["records"][1]["digest"])
        updated["records"][1]["attribution"]["email"] = "must-not-upload"
        with self.assertRaises(ApiError): batch(updated)


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        # No mocked ledger: invalid requests must be rejected before any database call.
        self.client = TestClient(create_app(object()))
        self.auth = {"Authorization": "Bearer " + "x"*40}

    def test_health_and_auth(self):
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.assertEqual(self.client.get("/v1/usage/summary").status_code, 401)

    def test_scoped_filter_validation_precedes_database_access(self):
        for query in ("account=bad","account="+"a"*64+"&account="+"b"*64,"group_by=account,account","group_by=tenant","tenant_id=x"):
            self.assertEqual(self.client.get("/v2/usage/summary?"+query,headers=self.auth).status_code,422)

    def test_size_encoding_and_json_validation(self):
        result = self.client.post("/v1/usage/batches", headers=self.auth, json={"bad": "field"})
        self.assertEqual(result.status_code, 422)
        result = self.client.post("/v1/usage/batches", headers={**self.auth, "Content-Type": "application/json"}, content=b" "*524289)
        self.assertEqual(result.status_code, 413)
        result = self.client.post("/v1/usage/batches", headers={**self.auth, "Content-Type": "application/json", "Content-Encoding": "gzip"}, content=b"bad")
        self.assertEqual(result.status_code, 415)
        result = self.client.post("/v1/usage/batches", headers={**self.auth, "Content-Type": "application/json"}, content=b'{"a":1,"a":2}')
        self.assertEqual(result.status_code, 400)


if __name__ == "__main__":
    unittest.main()
