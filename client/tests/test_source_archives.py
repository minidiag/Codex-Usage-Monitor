import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import zstandard

from client.agent import Agent, upload_once
from client.core import Ledger
from client.source_review import SourceReview
from client.tests.test_collector import Fixture, FakeApi, KEY, TENANT


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root/'collector.sqlite3', KEY, TENANT)
        self.fixture = Fixture(self.root/'session.jsonl')
        self.fixture.point(0, 100)
        self.ledger.scan_file(self.fixture.path)
        self.fixture.point(1, 200)
        self.fixture.point(2, 300)
        self.ledger.scan_file(self.fixture.path)
        self.api = FakeApi()
        self.archive = Path(str(self.fixture.path)+'.zst')

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def compress(self, data=None, **options):
        raw = self.fixture.path.read_bytes() if data is None else data
        self.archive.write_bytes(zstandard.ZstdCompressor(**options).compress(raw))
        self.fixture.path.unlink(missing_ok=True)

    def health(self):
        return self.ledger.source_health([self.archive])

    def records(self):
        return [tuple(row) for row in self.ledger.db.execute('SELECT * FROM events ORDER BY seq')]

    def test_synced_archive_preserves_history_and_survives_restart_without_decoding(self):
        upload_once(self.ledger, self.api)
        before = self.records()
        self.compress()
        health = self.health()
        self.assertEqual(health['synced_archives'], 1)
        self.assertFalse(health['missing'])
        self.assertNotIn('source_missing', self.ledger.warnings(health))
        self.assertEqual(self.records(), before)
        self.ledger.close()
        self.ledger = Ledger(self.root/'collector.sqlite3', KEY, TENANT)
        with patch('client.source_archives.verify_archive', side_effect=AssertionError('unnecessary decode')):
            self.assertEqual(self.health()['synced_archives'], 1)

    def test_pending_records_still_upload_and_ack_does_not_require_another_decode(self):
        self.compress()
        self.assertEqual(self.health()['archives'][0]['status'], 'pending')
        self.assertEqual(self.ledger.pending_count(), 3)
        upload_once(self.ledger, self.api)
        with patch('client.source_archives.verify_archive', side_effect=AssertionError('unnecessary decode')):
            self.assertEqual(self.health()['synced_archives'], 1)
        self.assertEqual(len(self.api.sent), 3)

    def test_unsampled_tail_is_never_claimed_synced_and_outbox_is_preserved(self):
        upload_once(self.ledger, self.api)
        self.fixture.point(3, 400)
        self.compress()
        before = self.records()
        health = self.health()
        self.assertEqual(health['unverified_archives'], 1)
        self.assertFalse(health['missing'])
        self.assertIn('compressed_source_unverified', self.ledger.warnings(health))
        self.assertEqual(self.records(), before)

    def test_changed_archive_invalidates_cache_even_when_it_has_same_session(self):
        upload_once(self.ledger, self.api)
        raw = self.fixture.path.read_bytes()
        self.compress()
        self.assertEqual(self.health()['synced_archives'], 1)
        self.archive.write_bytes(zstandard.ZstdCompressor().compress(raw.replace(b'"input_tokens": 200', b'"input_tokens": 201')))
        self.assertEqual(self.health()['unverified_archives'], 1)

    def test_corrupt_and_truncated_frames_are_not_treated_as_retired(self):
        self.compress()
        frame = self.archive.read_bytes()
        for data in (b'invalid frame', frame[:-1], frame+frame):
            with self.subTest(length=len(data)):
                self.archive.write_bytes(data)
                self.assertEqual(self.health()['unverified_archives'], 1)

    def test_unknown_decompressed_size_is_supported_with_a_limit(self):
        self.compress(write_content_size=False)
        self.assertTrue(self.health()['archives'][0]['proof']['verified'])

    def test_partial_last_line_cannot_be_retired(self):
        self.fixture.point(3, 400, partial=True)
        self.ledger.scan_file(self.fixture.path)
        self.compress()
        self.assertEqual(self.health()['unverified_archives'], 1)

    def test_original_returning_resumes_incremental_collection(self):
        raw = self.fixture.path.read_bytes()
        self.compress()
        self.health()
        self.fixture.path.write_bytes(raw)
        self.fixture.point(3, 400)
        self.ledger.scan_file(self.fixture.path)
        self.assertFalse(self.health()['archives'])
        self.assertEqual(self.ledger.pending_count(), 4)

    def test_genuinely_missing_file_still_warns_and_excluded_file_does_not(self):
        self.fixture.path.unlink()
        self.assertIn('source_missing', self.ledger.warnings())
        source = self.ledger.db.execute('SELECT path,stream_id FROM sources').fetchone()
        with self.ledger.db:
            self.ledger.db.execute('INSERT INTO source_exclusions VALUES (?,?,?)', (*source, 'review'))
        self.assertNotIn('source_missing', self.ledger.warnings())

    def test_missing_decoder_is_explicit_and_can_be_rechecked_after_install(self):
        self.compress()
        with patch('client.source_archives.zstandard', None):
            health = self.health()
            self.assertIn('缺少 zstandard', health['archives'][0]['message'])
        self.assertTrue(self.health()['archives'][0]['proof']['verified'])

    def test_report_lists_compressed_and_missing_sources_without_pausing_or_sending(self):
        self.compress()
        before = self.records()
        report = SourceReview(self.root, self.ledger, self.api).build_report('inspect')
        self.assertFalse(report['streams'])
        self.assertEqual(report['source_health']['archives'][0]['path'], str(self.archive))
        self.assertIn(str(self.archive), (self.root/'source-review.md').read_text(encoding='utf-8'))
        self.assertEqual(self.records(), before)
        self.assertEqual(self.api.calls, 0)

    def test_unregistered_archive_does_not_create_a_baseline_or_hide_coverage_gap(self):
        other = self.root/'other.jsonl.zst'
        other.write_bytes(zstandard.ZstdCompressor().compress(self.fixture.path.read_bytes()))
        health = self.ledger.source_health([other])
        self.assertEqual(len(health['unregistered_archives']), 1)
        self.assertIn('unsupported_usage', self.ledger.warnings(health))
        self.assertEqual(len(self.records()), 3)

    def test_decoding_limit_rejects_large_frame_before_allocating(self):
        self.compress()
        with patch('client.source_archives.MAX_FILE', 10):
            self.assertEqual(self.health()['unverified_archives'], 1)

    def test_unsupported_empty_history_does_not_pretend_to_have_synced_usage(self):
        empty = Fixture(self.root/'empty.jsonl')
        empty.path.write_text(empty.path.read_text().replace('"vscode"', '{"subagent":"spawn"}'))
        self.ledger.scan_file(empty.path)
        archive = Path(str(empty.path)+'.zst')
        archive.write_bytes(zstandard.ZstdCompressor().compress(empty.path.read_bytes()))
        empty.path.unlink()
        health = self.ledger.source_health([archive])
        self.assertEqual(health['archives'][0]['status'], 'no_usage')
        self.assertEqual(health['synced_archives'], 0)
        self.assertEqual(health['unverified_archives'], 0)
        self.assertNotIn('source_missing', self.ledger.warnings(health))
        self.assertIn('unsupported_usage', self.ledger.warnings(health))
        self.assertEqual(len(self.records()), 3)

    def test_frame_output_size_limit_cannot_be_bypassed_by_a_small_compressed_file(self):
        self.compress(b'x'*20000)
        with patch('client.source_archives.MAX_FILE', 1024):
            item = self.health()['archives'][0]
            self.assertEqual(item['status'], 'unverified')
            self.assertIn('128 MiB', item['message'])

    def test_agent_publishes_specific_archive_status_and_heartbeat_has_no_local_paths(self):
        home = self.root/'codex'
        sessions = home/'sessions'
        sessions.mkdir(parents=True)
        state = self.root/'agent-state'
        class Api(FakeApi):
            def heartbeat(self, data): self.health = data
            def summary(self): return {'quality':'partial'}
        api = Api()
        with patch('client.agent.QuotaMonitor') as quota, patch('client.agent.ActiveSessionReader') as active:
            quota.return_value.snapshot.return_value = {}
            active.return_value.snapshot.return_value = {'sessions':[]}
            agent = Agent(state, {'identity_key':KEY, 'tenant_id':TENANT}, [home], api=api)
            try:
                source = Fixture(sessions/'run.jsonl')
                source.point(0, 100)
                agent.step()
                # A new point missed before compression must remain a visible gap.
                source.point(1, 200)
                archive = Path(str(source.path)+'.zst')
                archive.write_bytes(zstandard.ZstdCompressor().compress(source.path.read_bytes()))
                source.path.unlink()
                agent.last_heartbeat = 0
                agent.step()
                status = json.loads((state/'status.json').read_text(encoding='utf-8'))
                self.assertEqual(status['error'], 'compressed_source_unverified')
                self.assertEqual(status['source_health']['unverified_archives'], 1)
                self.assertNotIn(str(home), json.dumps(api.health))
                self.assertIn('unsupported_usage', api.health['quality_reasons'])
                self.assertNotIn('compressed_source_unverified', api.health['quality_reasons'])
            finally:
                agent.ledger.close()


if __name__ == '__main__':
    unittest.main()
