import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from client.active_sessions import ActiveSessionReader, counter, local_path, read_runtime


def event(kind):
    return dict(type="event_msg", payload=dict(type=kind))


class ActiveSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.db = self.home/"state_5.sqlite"
        self.writer = sqlite3.connect(self.db)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("CREATE TABLE threads(id TEXT,rollout_path TEXT,title TEXT,tokens_used INTEGER,archived INTEGER,model TEXT,reasoning_effort TEXT,updated_at_ms INTEGER)")
        self.writer.commit()

    def add(self, records=None, archived=0, tokens=123456789, identity=None, order=1, path=None):
        identity = identity or str(uuid4())
        path = path or self.home/(identity+".jsonl")
        path.write_bytes(b"".join(json.dumps(row, ensure_ascii=False).encode()+b"\n" for row in (records if records is not None else [event("task_started")])))
        self.writer.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)",
                            (identity, str(path), "数据库标题\n未展示内容", tokens, archived, "fallback-model", "high", order))
        self.writer.commit()
        return identity, path

    def reader(self):
        return ActiveSessionReader([self.home])

    def test_latest_start_and_runtime_model_are_displayed(self):
        context = dict(type="turn_context", payload=dict(model="live-model",
                       collaboration_mode=dict(settings=dict(reasoning_effort="medium"))))
        self.add([event("task_complete"), event("task_started"), context])
        view = self.reader().refresh()
        self.assertTrue(view["available"])
        self.assertIsNone(view["error"])
        self.assertEqual(len(view["sessions"]), 1)
        row = view["sessions"][0]
        self.assertEqual((row["tokens"], row["model"], row["effort"]), ("123456789", "live-model", "medium"))
        self.assertEqual(row["title"], "数据库标题")

    def test_completed_or_aborted_is_not_active_despite_fresh_mtime(self):
        for ending in ("task_complete", "turn_aborted", "task_completed", "task_aborted"):
            self.add([event("task_started"), event(ending)])
        self.assertEqual(self.reader().refresh()["sessions"], [])

    def test_new_turn_reappears_after_completion(self):
        _, path = self.add()
        reader = self.reader()
        self.assertEqual(len(reader.refresh()["sessions"]), 1)
        with path.open("ab") as handle:
            handle.write(json.dumps(event("task_complete")).encode()+b"\n")
        self.assertEqual(reader.refresh()["sessions"], [])
        with path.open("ab") as handle:
            handle.write(json.dumps(event("task_started")).encode()+b"\n")
        self.assertEqual(len(reader.refresh()["sessions"]), 1)

    def test_recent_log_fallback_ages_out_without_file_changes(self):
        _, path = self.add([dict(type="session_meta", payload={})])
        changed = path.stat().st_mtime
        reader = self.reader()
        with patch("client.active_sessions.time.time", return_value=changed+599):
            row = reader.refresh()["sessions"][0]
            self.assertEqual(row["activity_source"], "recent_log")
        with patch("client.active_sessions.time.time", return_value=changed+601):
            self.assertEqual(reader.refresh()["sessions"], [])

    def test_incomplete_final_record_is_not_applied_until_newline(self):
        _, path = self.add()
        with path.open("ab") as handle:
            handle.write(json.dumps(event("task_complete")).encode())
        self.assertTrue(read_runtime(path)["active"])
        with path.open("ab") as handle:
            handle.write(b"\n")
        self.assertFalse(read_runtime(path)["active"])

    def test_large_utf8_record_and_partial_tail_cross_chunk_boundaries(self):
        _, path = self.add([event("task_complete"), event("task_started")])
        context = json.dumps(dict(type="turn_context", payload=dict(model="boundary-model", padding="字"*700000)), ensure_ascii=False).encode()
        with path.open("ab") as handle:
            handle.write(context+b"\n"+b"x"*(2*1024*1024))
        runtime = read_runtime(path)
        self.assertTrue(runtime["active"])
        self.assertEqual(runtime["model"], "boundary-model")

    def test_scan_is_bounded_and_bad_record_does_not_break_reader(self):
        _, path = self.add([event("task_complete")])
        with path.open("ab") as handle:
            handle.write(b'{"type":"event_msg","payload":{"type":[]}}\n')
            handle.write(b"x"*(17*1024*1024)+b"\n")
        runtime = read_runtime(path, now=path.stat().st_mtime+601)
        self.assertFalse(runtime["active"])
        self.assertEqual(runtime["activity_source"], "recent_log")

    def test_archived_excluded_and_recent_twelve_candidate_limit(self):
        self.add(archived=1, order=100)
        for index in range(13):
            self.add([event("task_started") if index == 0 else event("task_complete")], order=index)
        view = self.reader().refresh()
        self.assertEqual(view["candidate_limit"], 12)
        self.assertEqual(view["sessions"], [])

    def test_index_title_override_is_local_and_sanitized(self):
        identity, _ = self.add()
        (self.home/"session_index.jsonl").write_text(json.dumps(dict(id=identity, thread_name="友好名称\n隐藏", other="private"))+"\n", encoding="utf-8")
        row = self.reader().refresh()["sessions"][0]
        self.assertEqual(row["title"], "友好名称")
        self.assertNotIn("private", json.dumps(row))

    def test_readonly_index_sees_wal_updates_and_preserves_large_integers(self):
        identity, _ = self.add(tokens=9223372036854775807)
        reader = self.reader()
        self.assertEqual(reader.refresh()["sessions"][0]["tokens"], "9223372036854775807")
        self.writer.execute("UPDATE threads SET tokens_used=0 WHERE id=?", (identity,))
        self.writer.commit()
        self.assertEqual(reader.refresh()["sessions"][0]["tokens"], "0")
        self.assertEqual(self.writer.execute("SELECT count(*) FROM threads").fetchone()[0], 1)
        self.assertEqual(counter(None), None)
        self.assertEqual(counter(-1), None)
        self.assertEqual(counter(True), None)
        self.assertEqual(counter("9223372036854775808"), None)

    def test_failed_index_retains_previous_value_and_original_timestamp(self):
        self.add()
        reader = self.reader()
        previous = reader.refresh()
        with patch.object(reader, "candidates", side_effect=sqlite3.OperationalError("private error")):
            current = reader.refresh()
        self.assertFalse(current["available"])
        self.assertEqual(current["updated_at"], previous["updated_at"])
        self.assertEqual(current["sessions"], previous["sessions"])
        self.assertNotIn("private error", json.dumps(current))

    def test_missing_index_does_not_create_a_database(self):
        missing = self.home/"missing"
        missing.mkdir()
        view = ActiveSessionReader([missing]).refresh()
        self.assertFalse(view["available"])
        self.assertEqual(list(missing.iterdir()), [])

    def test_duplicate_home_does_not_duplicate_session_and_outside_path_rejected(self):
        self.add()
        view = ActiveSessionReader([self.home, self.home]).refresh()
        self.assertEqual(len(view["sessions"]), 1)
        self.writer.execute("UPDATE threads SET rollout_path=?", (str(self.home.parent/"outside.jsonl"),))
        self.writer.commit()
        self.assertEqual(self.reader().refresh()["sessions"], [])

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_windows_extended_paths_resolve_to_same_home(self):
        _, path = self.add()
        extended = "\\\\?\\"+str(path)
        self.writer.execute("UPDATE threads SET rollout_path=?", (extended,))
        self.writer.commit()
        self.assertEqual(local_path(extended), path)
        self.assertEqual(len(self.reader().refresh()["sessions"]), 1)


if __name__ == "__main__":
    unittest.main()
