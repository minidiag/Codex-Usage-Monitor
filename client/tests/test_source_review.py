from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from client.core import Ledger
from client.source_review import SourceReview, atomic_json
from client.tests.test_collector import Fixture, FakeApi, KEY, TENANT
from client.transport import ApiFailure
from client.agent import upload_once


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root/'collector.sqlite3', KEY, TENANT)
        self.api = FakeApi()
        self.review = SourceReview(self.root, self.ledger, self.api)
        self.full = Fixture(self.root/'full.jsonl')
        for index, count in enumerate((100, 200, 300)):
            self.full.point(index, count)
        self.ledger.scan_file(self.full.path)
        self.other = Fixture(self.root/'other.jsonl', self.full.session)
        self.other.started = self.full.started-timedelta(days=1)
        self.other.point(0, 50)
        self.ledger.scan_file(self.other.path)
        self.stream = self.ledger.db.execute('SELECT id FROM streams').fetchone()[0]

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def inspect(self):
        return self.review.handle(dict(id=str(uuid.uuid4()), action='inspect'))

    def request(self, report, path=None):
        stream = report['streams'][0]
        item = next(item for item in stream['sources'] if item['path']==str(path or self.full.path))
        return dict(id=str(uuid.uuid4()), action='resume', revision=report['revision'],
                    stream_id=self.stream, source_id=item['source_id'], fingerprint=item.get('fingerprint'))

    def test_inspection_explains_baseline_and_history_without_mutation_or_upload(self):
        report = self.inspect()
        stream = report['streams'][0]
        self.assertEqual(stream['server']['status'], 'absent')
        self.assertEqual(stream['pending'], 1)
        short = next(item for item in stream['sources'] if item['path'] == str(self.other.path))
        self.assertFalse(any('服务器已保存' in issue for issue in short['issues']))
        eligible = [item for item in stream['sources'] if item['eligible']]
        self.assertEqual([item['path'] for item in eligible], [str(self.full.path)])
        self.assertIsNone(stream['auto_source_id'])
        self.assertEqual(self.ledger.pending_count(), 1)
        self.assertEqual(self.api.calls, 0)
        self.assertEqual(self.ledger.db.execute('SELECT paused FROM streams').fetchone()[0], 'unknown_identity')
        self.assertTrue((self.root/'source-review.md').is_file())

    def test_manual_choice_retains_events_excludes_alternative_and_collects_new_points(self):
        before = list(self.ledger.db.execute('SELECT event_id,digest,wire FROM events'))
        result = self.review.handle(self.request(self.inspect()))
        self.assertEqual(result['result']['code'], 'resumed')
        self.assertFalse(result['streams'])
        self.assertEqual([tuple(row) for row in self.ledger.db.execute('SELECT event_id,digest,wire FROM events')], [tuple(row) for row in before])
        self.assertEqual(len(result['audit']), 1)
        backup = Path(result['audit'][0]['backup'])
        db = sqlite3.connect(backup)
        try:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        finally:
            db.close()
        self.other.point(1, 70)
        self.ledger.scan_file(self.other.path)
        self.full.point(3, 400)
        self.ledger.scan_file(self.full.path)
        self.assertIsNone(self.ledger.db.execute('SELECT paused FROM streams').fetchone()[0])
        self.assertEqual(self.ledger.pending_count(), 2)
        self.assertEqual(self.ledger.db.execute('SELECT last_seq FROM streams').fetchone()[0], 1)
        self.assertNotIn('unknown_identity', self.ledger.warnings())

    def test_excluded_path_reused_for_different_session_is_not_blindly_skipped(self):
        self.review.handle(self.request(self.inspect()))
        self.other.path.unlink()
        replacement = Fixture(self.other.path)
        replacement.point(0, 90)
        self.ledger.scan_file(replacement.path)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM streams').fetchone()[0], 2)

    def test_missing_anchor_cannot_be_selected(self):
        result = self.review.handle(self.request(self.inspect(), self.other.path))
        self.assertEqual(result['result']['code'], 'not_eligible')
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM source_review_audit').fetchone()[0], 0)
        self.assertEqual(self.ledger.pending_count(), 1)

    def test_changed_file_after_inspection_is_rejected_and_can_be_reinspected(self):
        request = self.request(self.inspect())
        self.full.point(3, 400)
        result = self.review.handle(request)
        self.assertEqual(result['result']['code'], 'stale_review')
        self.assertEqual(self.ledger.pending_count(), 1)
        result = self.review.handle(self.request(self.inspect()))
        self.assertEqual(result['result']['code'], 'resumed')
        self.assertEqual(self.ledger.pending_count(), 2)

    def test_changed_report_revision_requires_a_fresh_decision(self):
        request = self.request(self.inspect())
        self.inspect()
        result = self.review.handle(request)
        self.assertEqual(result['result']['code'], 'stale_review')
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM source_exclusions').fetchone()[0], 0)

    def test_server_unavailable_or_different_baseline_blocks_recovery(self):
        request = self.request(self.inspect())
        self.api.offline = True
        result = self.review.handle(request)
        self.assertEqual(result['result']['code'], 'not_eligible')
        self.api.offline = False
        request = self.request(self.inspect())
        self.api.remote = dict(anchor_id='f'*64, baseline_event={})
        result = self.review.handle(request)
        self.assertEqual(result['result']['code'], 'not_eligible')
        self.assertEqual(result['streams'][0]['server']['status'], 'different')
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM source_review_audit').fetchone()[0], 0)

    def test_auto_recovery_proves_prefix_instead_of_picking_largest_file(self):
        result = self.review.handle(dict(id=str(uuid.uuid4()), action='auto'))
        self.assertEqual(len(result['streams']), 1)
        self.assertFalse(result['audit'])
        lines = self.full.path.read_text(encoding='utf-8').splitlines(keepends=True)
        self.other.path.write_text(''.join(lines[:-1]), encoding='utf-8')
        result = self.review.handle(dict(id=str(uuid.uuid4()), action='auto'))
        self.assertFalse(result['streams'])
        self.assertTrue(result['audit'][0]['automatic'])

    def test_repeated_completed_request_does_not_replay_or_rewrite_events(self):
        request = self.request(self.inspect())
        first = self.review.handle(request)
        second = self.review.handle(request)
        self.assertEqual(first['result']['code'], 'resumed')
        self.assertEqual(second['result']['code'], 'resumed')
        self.assertEqual(len(second['audit']), 1)
        self.assertEqual(self.ledger.pending_count(), 1)

    def test_matching_server_baseline_must_be_fully_covered(self):
        stream = self.ledger.db.execute('SELECT * FROM streams').fetchone()
        self.api.remote = dict(anchor_id=stream['anchor'], baseline_event=json.loads(stream['baseline']), durable_through='10')
        report = self.inspect()
        self.assertFalse(any(item['eligible'] for item in report['streams'][0]['sources']))

    def test_acknowledged_history_survives_recovery_and_only_new_events_are_queued(self):
        self.ledger.pause(self.stream, None)
        self.full.point(3, 400)
        self.ledger.scan_file(self.full.path)
        upload_once(self.ledger, self.api)
        stream = self.ledger.db.execute('SELECT * FROM streams').fetchone()
        self.api.remote = dict(anchor_id=stream['anchor'], baseline_event=json.loads(stream['baseline']), durable_through='1')
        self.ledger.pause(self.stream, 'unknown_identity')
        before = [tuple(row) for row in self.ledger.db.execute('SELECT event_id,digest,wire,acked FROM events ORDER BY seq')]
        self.full.point(4, 500)
        result = self.review.handle(self.request(self.inspect()))
        self.assertEqual(result['result']['code'], 'resumed')
        after = [tuple(row) for row in self.ledger.db.execute('SELECT event_id,digest,wire,acked FROM events ORDER BY seq')]
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(self.ledger.pending_count(), 1)
        upload_once(self.ledger, self.api)
        self.assertEqual(self.ledger.pending_count(), 0)
        self.assertEqual(len(self.api.sent), 3)

    def test_parse_failures_and_counter_resets_cannot_be_bypassed(self):
        self.full.append({'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{}}}})
        report = self.inspect()
        self.assertFalse(next(item for item in report['streams'][0]['sources'] if item['path']==str(self.full.path))['eligible'])

    def test_post_baseline_counter_reset_is_not_eligible_for_manual_recovery(self):
        self.full.point(3, 10)
        report = self.inspect()
        item = next(item for item in report['streams'][0]['sources'] if item['path']==str(self.full.path))
        self.assertTrue(item['anchor_matches'])
        self.assertFalse(item['eligible'])
        result = self.review.handle(self.request(report))
        self.assertEqual(result['result']['code'], 'not_eligible')
        self.assertEqual(self.ledger.pending_count(), 1)

    def test_request_file_is_consumed_and_result_is_locally_readable(self):
        request = dict(id=str(uuid.uuid4()), action='inspect')
        atomic_json(self.review.request_path, request)
        self.review.tick()
        self.assertFalse(self.review.request_path.exists())
        result = json.loads(self.review.report_path.read_text(encoding='utf-8'))
        self.assertEqual(result['request_id'], request['id'])


if __name__ == '__main__':
    unittest.main()
