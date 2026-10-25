from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from client.quota import QuotaError, QuotaMonitor, normalize_quota, read_quota
from client.agent import Agent
from client.tests.test_collector import Fixture, FakeApi, KEY, TENANT
from server.protocol import UTC


def payload(used=25, duration=300, reset=1792949203):
    return {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": used, "windowDurationMins": duration, "resetsAt": reset}, "secondary": None}}


class QuotaNormalizationTests(unittest.TestCase):
    def test_keyed_codex_has_priority_over_legacy_and_other_buckets(self):
        value = payload(5)
        value["rateLimitsByLimitId"] = {"codex": payload(75)["rateLimits"], "codex_other": payload(99)["rateLimits"]}
        self.assertEqual(normalize_quota(value)["windows"][0]["remaining_percent"], 25)

    def test_missing_invalid_used_is_unknown_instead_of_full_quota(self):
        for value in (None, -1, True, "25", float("nan"), float("inf")):
            with self.subTest(value=value):
                window = normalize_quota(payload(value))["windows"][0]
                self.assertIsNone(window["remaining_percent"])

    def test_zero_exhausted_and_over_limit_are_distinct_from_unknown(self):
        self.assertEqual(normalize_quota(payload(0))["windows"][0]["remaining_percent"], 100)
        self.assertEqual(normalize_quota(payload(100))["windows"][0]["remaining_percent"], 0)
        self.assertEqual(normalize_quota(payload(120))["windows"][0]["remaining_percent"], 0)

    def test_actual_duration_and_null_reset_are_preserved(self):
        window = normalize_quota(payload(25, duration=15, reset=None))["windows"][0]
        self.assertEqual(window["duration_mins"], 15)
        self.assertIsNone(window["resets_at"])
        self.assertIsNone(normalize_quota(payload(reset=1792949203000))["windows"][0]["resets_at"])

    def test_other_bucket_or_no_windows_is_unavailable(self):
        for value in ({}, {"rateLimits": None}, {"rateLimits": {"limitId": "codex_other"}}, {"rateLimits": {"primary": None, "secondary": None}}):
            with self.assertRaises(QuotaError):
                normalize_quota(value)

    def test_only_whitelisted_account_quota_fields_are_exposed(self):
        value = payload()
        value["accessToken"] = "must-not-persist"
        value["rateLimitResetCredits"] = {"credits": [{"id": "private-reset-credit"}]}
        value["rateLimits"]["credits"] = {"balance": "private-credit-balance"}
        encoded = json.dumps(normalize_quota(value))
        for forbidden in ("must-not-persist", "private-reset-credit", "private-credit-balance", "accessToken"):
            self.assertNotIn(forbidden, encoded)


class MonitorTests(unittest.TestCase):
    def test_account_quota_stays_local_and_never_enters_telemetry_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/"home/sessions").mkdir(parents=True)
            fixture = Fixture(root/"home/sessions/source.jsonl")
            fixture.point(0, 100)
            captured = []
            class LocalQuota:
                def tick(self):
                    pass
                def snapshot(self):
                    return {"quota_marker": "local-account-only"}
            class LocalSessions:
                def refresh(self):
                    pass
                def snapshot(self):
                    return {"sessions": [{"id": "private-session-id", "title": "local-session-title-only"}]}
            class RecordingApi(FakeApi):
                def send(self, data):
                    captured.append(data)
                    return super().send(data)
                def heartbeat(self, data):
                    captured.append(data)
                    return {}
                def summary(self):
                    return {"windows": [], "quality": "partial"}
            with patch("client.agent.QuotaMonitor", return_value=LocalQuota()), patch("client.agent.ActiveSessionReader", return_value=LocalSessions()):
                agent = Agent(root/"state", dict(identity_key=KEY, tenant_id=TENANT), [root/"home"], api=RecordingApi())
                try:
                    agent.step()
                    self.assertGreater(len(captured), 0)
                    self.assertNotIn("local-account-only", json.dumps(captured))
                    self.assertNotIn("quota_marker", json.dumps(captured))
                    self.assertNotIn("local-session-title-only", json.dumps(captured))
                    self.assertNotIn("private-session-id", json.dumps(captured))
                    self.assertNotIn("active_sessions", json.dumps(captured))
                    status = json.loads((root/"state/status.json").read_text())
                    self.assertEqual(status["quota"]["quota_marker"], "local-account-only")
                    self.assertEqual(status["active_sessions"]["sessions"][0]["title"], "local-session-title-only")
                finally:
                    agent.ledger.close()

    def test_failed_refresh_keeps_old_timestamp_and_sanitizes_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            monitor = QuotaMonitor(temporary, reader=lambda _: normalize_quota(payload(), datetime(2026, 10, 25, tzinfo=UTC)))
            monitor._refresh()
            old = monitor.snapshot()
            def fail(_):
                raise QuotaError("private error text")
            monitor.reader = fail
            monitor._refresh()
            current = monitor.snapshot()
            self.assertEqual(current["fetched_at"], old["fetched_at"])
            self.assertEqual(current["windows"], old["windows"])
            self.assertEqual(current["error"], "quota_unavailable")
            self.assertNotIn("private error text", (Path(temporary)/"quota-cache.json").read_text())

    def test_slow_reader_is_nonblocking_and_not_started_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            release = threading.Event()
            entered = threading.Event()
            calls = []
            def slow(cancel):
                calls.append(1)
                entered.set()
                release.wait(2)
                return normalize_quota(payload())
            monitor = QuotaMonitor(temporary, reader=slow)
            started = time.monotonic()
            monitor.tick()
            self.assertLess(time.monotonic()-started, 0.5)
            self.assertTrue(entered.wait(1))
            monitor.tick()
            self.assertEqual(len(calls), 1)
            release.set()
            monitor.worker.join(timeout=2)
            monitor.tick()
            self.assertEqual(len(calls), 1)
            monitor.close()


class RecordingInput(io.StringIO):
    def close(self):
        self.recorded = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, response):
        self.stdin = RecordingInput()
        self.stdout = io.StringIO(response)
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


class FakeJob:
    def __init__(self, process):
        pass

    def close(self):
        pass


class ReadOnlyProtocolTests(unittest.TestCase):
    def test_reader_only_initializes_and_reads_limits_without_model_or_account_changes(self):
        wire = json.dumps({"id": 1, "result": {}})+"\n"+json.dumps({"id": 2, "result": payload()})+"\n"
        process = FakeProcess(wire)
        with patch("client.quota.find_codex", return_value="codex.exe"), patch("client.quota.subprocess.Popen", return_value=process), patch("client.quota.WindowsJob", FakeJob):
            value = read_quota()
        self.assertEqual(value["windows"][0]["remaining_percent"], 75)
        methods = [json.loads(line)["method"] for line in process.stdin.recorded.splitlines()]
        self.assertEqual(methods, ["initialize", "initialized", "account/rateLimits/read"])
        self.assertTrue(process.terminated)

    def test_timeout_terminates_owned_reader(self):
        process = FakeProcess("")
        with patch("client.quota.find_codex", return_value="codex.exe"), patch("client.quota.subprocess.Popen", return_value=process), patch("client.quota.WindowsJob", FakeJob):
            with self.assertRaisesRegex(QuotaError, "quota_timeout"):
                read_quota(timeout=0.05)
        self.assertTrue(process.terminated)

    def test_cancelled_read_does_not_start_any_process(self):
        cancel = threading.Event()
        cancel.set()
        with patch("client.quota.subprocess.Popen") as factory:
            with self.assertRaisesRegex(QuotaError, "quota_cancelled"):
                read_quota(cancel)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
