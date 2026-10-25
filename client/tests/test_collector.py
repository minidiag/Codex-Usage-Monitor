from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from client.agent import upload_once
from client.core import Ledger, OutboxFull
from client.source import read_source
from client.transport import ApiFailure
from server.protocol import UTC, iso

KEY = "ab"*32
TENANT = "11111111-1111-4111-8111-111111111111"


class Fixture:
    def __init__(self, path, session=None):
        self.path = Path(path)
        self.session = session or str(uuid.uuid4())
        self.started = datetime.now(UTC)-timedelta(hours=1)
        self.append({"type": "session_meta", "payload": {"id": self.session, "source": "vscode"}})
        self.append({"type": "turn_context", "payload": {"model": "synthetic-model"}})

    def append(self, value):
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(value)+"\n")

    def point(self, index, count, partial=False):
        value = {"type": "event_msg", "timestamp": iso(self.started+timedelta(minutes=index)),
                 "payload": {"type": "token_count", "info": {"total_token_usage": {
                     "input_tokens": count, "cached_input_tokens": count//5, "output_tokens": count//2,
                     "reasoning_output_tokens": count//10, "total_tokens": count+count//2}}}}
        if partial:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(value))
        else:
            self.append(value)


class FakeApi:
    def __init__(self, offline=False):
        self.remote = None
        self.offline = offline
        self.sent = {}
        self.calls = 0

    def state(self, stream):
        if self.offline:
            raise ApiFailure()
        if self.remote is None:
            raise ApiFailure(404, "stream_not_found")
        return self.remote

    def send(self, data):
        self.calls += 1
        if self.offline:
            raise ApiFailure()
        accepted = duplicates = 0
        for record in data["records"]:
            if record["event_id"] in self.sent:
                duplicates += 1
            else:
                self.sent[record["event_id"]] = record
                accepted += 1
        maximum = max(int(r["source_seq"]) for r in self.sent.values())
        return dict(accepted=accepted, duplicates=duplicates, durable_through=str(maximum), processed_through=str(maximum), pending_gap=False)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = Fixture(self.root/"session.jsonl")
        self.fixture.point(0, 10000)
        self.fixture.point(1, 20000)
        self.ledger = Ledger(self.root/"state.sqlite3", KEY, TENANT)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def stream(self):
        return self.ledger.db.execute("SELECT * FROM streams").fetchone()

    def test_initial_capture_has_one_baseline_and_excludes_history(self):
        self.ledger.scan_file(self.fixture.path)
        records = self.ledger.pending_batch(self.stream()["id"])["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["counters"]["input"], "20000")
        self.assertEqual(records[0]["baseline_reason"], "capture_start")
        self.fixture.point(2, 20100)
        self.ledger.scan_file(self.fixture.path)
        records = self.ledger.pending_batch(self.stream()["id"])["records"]
        self.assertEqual([r["source_seq"] for r in records], ["0", "1"])
        self.assertEqual(int(records[1]["counters"]["input"])-int(records[0]["counters"]["input"]), 100)

    def test_restart_and_repeated_scan_do_not_duplicate(self):
        self.ledger.scan_file(self.fixture.path)
        self.fixture.point(2, 20100)
        self.ledger.scan_file(self.fixture.path)
        self.ledger.close()
        self.ledger = Ledger(self.root/"state.sqlite3", KEY, TENANT)
        self.ledger.scan_file(self.fixture.path)
        self.assertEqual(self.ledger.pending_count(), 2)
        self.assertEqual(self.stream()["last_seq"], 1)

    def test_incomplete_line_waits_without_advancing_cursor(self):
        self.ledger.scan_file(self.fixture.path)
        offset = self.ledger.db.execute("SELECT offset FROM sources").fetchone()[0]
        self.fixture.point(2, 20100, partial=True)
        self.ledger.scan_file(self.fixture.path)
        self.assertEqual(self.ledger.db.execute("SELECT offset FROM sources").fetchone()[0], offset)
        with self.fixture.path.open("a") as f:
            f.write("\n")
        self.ledger.scan_file(self.fixture.path)
        self.assertEqual(self.ledger.pending_count(), 2)

    def test_copy_and_archive_move_share_ids_and_sequences(self):
        self.ledger.scan_file(self.fixture.path)
        self.fixture.point(2, 20100)
        self.ledger.scan_file(self.fixture.path)
        archived = self.root/"archived.jsonl"
        archived.write_bytes(self.fixture.path.read_bytes())
        self.ledger.scan_file(archived)
        self.fixture.path.unlink()
        self.assertEqual(self.ledger.pending_count(), 2)
        self.assertNotIn("source_missing", self.ledger.warnings())

    def test_outbox_full_rolls_back_cursor_and_event_together(self):
        self.ledger.scan_file(self.fixture.path)
        offset = self.ledger.db.execute("SELECT offset FROM sources").fetchone()[0]
        self.ledger.max_pending = 1
        self.fixture.point(2, 20100)
        with self.assertRaises(OutboxFull):
            self.ledger.scan_file(self.fixture.path)
        self.assertEqual(self.ledger.db.execute("SELECT offset FROM sources").fetchone()[0], offset)
        self.assertEqual(self.stream()["last_seq"], 0)
        self.ledger.max_pending = 10
        self.ledger.scan_file(self.fixture.path)
        self.assertEqual(self.ledger.pending_count(), 2)

    def test_offline_queue_survives_and_retries_after_restart(self):
        self.ledger.scan_file(self.fixture.path)
        with self.assertRaises(ApiFailure):
            upload_once(self.ledger, FakeApi(offline=True))
        self.fixture.point(2, 20100)
        self.ledger.scan_file(self.fixture.path)
        self.ledger.close()
        self.ledger = Ledger(self.root/"state.sqlite3", KEY, TENANT)
        self.assertEqual(self.ledger.pending_count(), 2)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE streams SET next_retry=0")
        api = FakeApi()
        upload_once(self.ledger, api)
        self.assertEqual(self.ledger.pending_count(), 0)
        self.assertEqual(len(api.sent), 2)

    def test_second_device_adopts_remote_common_anchor(self):
        self.ledger.scan_file(self.fixture.path)
        first = self.stream()
        remote = dict(anchor_id=first["anchor"], baseline_event=json.loads(first["baseline"]))
        self.fixture.point(2, 20100)
        self.ledger.scan_file(self.fixture.path)
        second = Ledger(self.root/"second.sqlite3", KEY, TENANT)
        try:
            second.scan_file(self.fixture.path)  # Its own initial anchor is later.
            own_anchor = second.db.execute("SELECT anchor FROM streams").fetchone()[0]
            self.assertNotEqual(own_anchor, remote["anchor_id"])
            api = FakeApi()
            api.remote = remote
            upload_once(second, api)
            self.assertEqual(second.db.execute("SELECT anchor FROM streams").fetchone()[0], remote["anchor_id"])
            self.assertEqual({r["event_id"] for r in api.sent.values()},
                             {r[0] for r in self.ledger.db.execute("SELECT event_id FROM events")})
        finally:
            second.close()

    def test_lost_ack_retransmits_exact_event_ids_without_double_count(self):
        self.ledger.scan_file(self.fixture.path)
        api = FakeApi()
        original_send = api.send
        def lose_ack(data):
            original_send(data)
            raise ApiFailure()
        api.send = lose_ack
        with self.assertRaises(ApiFailure):
            upload_once(self.ledger, api)
        committed_ids = set(api.sent)
        self.assertEqual(self.ledger.pending_count(), 1)
        api.send = original_send
        with self.ledger.db:
            self.ledger.db.execute("UPDATE streams SET next_retry=0")
        upload_once(self.ledger, api)
        self.assertEqual(set(api.sent), committed_ids)
        self.assertEqual(self.ledger.pending_count(), 0)

    def test_reset_or_changed_file_never_creates_fake_delta(self):
        self.ledger.scan_file(self.fixture.path)
        self.fixture.point(2, 10)
        self.ledger.scan_file(self.fixture.path)
        self.assertEqual(self.stream()["paused"], "unsupported_usage")
        self.assertEqual(self.ledger.pending_count(), 1)

    def test_same_size_rewrite_is_detected_by_cursor_fingerprint(self):
        self.ledger.scan_file(self.fixture.path)
        original = self.fixture.path.read_text()
        self.fixture.path.write_text(original.replace('"input_tokens": 20000', '"input_tokens": 21000'), encoding="utf-8")
        self.ledger.scan_file(self.fixture.path)
        self.assertEqual(self.stream()["paused"], "unknown_identity")

    def test_failed_or_conflicting_ack_never_deletes_outbox(self):
        self.ledger.scan_file(self.fixture.path)
        stream = self.stream()["id"]
        with self.assertRaises(ValueError):
            self.ledger.acknowledge(stream, self.ledger.pending_batch(stream), dict(accepted=0, duplicates=0))
        self.assertEqual(self.ledger.pending_count(), 1)
        api = FakeApi()
        def conflict(data):
            raise ApiFailure(409, "immutable_event_conflict")
        api.send = conflict
        with self.assertRaises(ApiFailure):
            upload_once(self.ledger, api)
        self.assertEqual(self.ledger.pending_count(), 1)
        self.assertEqual(self.stream()["paused"], "unknown_identity")

    def test_unsupported_child_and_compaction_are_visible(self):
        self.fixture.append({"type": "compacted", "payload": {"body": "private content"}})
        result = read_source(self.fixture.path)
        self.assertIn("unsupported_usage", result.warnings)
        child = Fixture(self.root/"child.jsonl")
        content = child.path.read_text().replace('"source": "vscode"', '"source": {"subagent": "spawn"}')
        child.path.write_text(content)
        child.point(0, 20000)
        self.ledger.scan_file(child.path)
        self.assertIn("unsupported_usage", self.ledger.warnings())
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM streams").fetchone()[0], 0)

    def test_identity_change_does_not_reidentify_old_events(self):
        self.ledger.scan_file(self.fixture.path)
        with self.assertRaises(ValueError):
            Ledger(self.root/"state.sqlite3", "cd"*32, TENANT)


if __name__ == "__main__":
    unittest.main()
