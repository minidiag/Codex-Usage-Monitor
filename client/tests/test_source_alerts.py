import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

import zstandard

from client.agent import Agent, upload_once
from client.core import Ledger
from client.source_review import SourceReview
from client.tests.test_collector import Fixture, FakeApi, KEY, TENANT
from client.transport import ApiFailure


class SourceAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root/'collector.sqlite3',KEY,TENANT)
        self.api = FakeApi()
        self.review = SourceReview(self.root,self.ledger,self.api)
        self.source = Fixture(self.root/'run.jsonl')
        self.source.point(0,100)
        self.ledger.scan_file(self.source.path)
        self.source.point(1,200)
        self.ledger.scan_file(self.source.path)
        self.raw = self.source.path.read_bytes()
        self.source.path.unlink()

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def inspect(self):
        return self.review.handle(dict(id=str(uuid.uuid4()),action='inspect'))

    def request(self,report,action='close_alert',reason='日志已删除且无法找回，接受覆盖缺口',index=0):
        items = report['source_health']['active_alerts' if action=='close_alert' else 'closed_alerts']
        item = items[index]
        return dict(id=str(uuid.uuid4()),action=action,revision=report['revision'],stream_id=item['stream_id'],
                    fingerprint=item['fingerprint'],closure_id=item.get('closure_id'),reason=reason)

    def numeric_state(self):
        return {name:[tuple(row) for row in self.ledger.db.execute('SELECT * FROM '+name+' ORDER BY rowid')]
                for name in ('streams','events','sources','source_exclusions')}

    def test_close_preserves_ledger_queue_and_coverage_with_backup_and_audit(self):
        before = self.numeric_state()
        result = self.review.handle(self.request(self.inspect()))
        self.assertEqual(result['result']['code'],'alert_closed')
        self.assertFalse(result['source_health']['active_alerts'])
        self.assertEqual(result['source_health']['closed_alert_count'],1)
        self.assertEqual(before,self.numeric_state())
        self.assertEqual(self.ledger.pending_count(),2)
        self.assertIn('source_missing',self.ledger.warnings())
        audit = result['alert_audit'][0]
        self.assertEqual(audit['action'],'close')
        self.assertTrue(audit['actor'])
        self.assertTrue(Path(audit['backup']).is_file())
        backup = sqlite3.connect(audit['backup'])
        self.assertEqual(backup.execute('SELECT count(*) FROM events').fetchone()[0],2)
        backup.close()
        self.assertIn('日志已删除',(self.root/'source-review.md').read_text(encoding='utf-8'))

    def test_closing_is_idempotent_and_survives_restart(self):
        request = self.request(self.inspect())
        self.review.handle(request)
        repeated = self.review.handle(request)
        self.assertEqual(repeated['result']['code'],'alert_closed')
        self.assertEqual(len(repeated['alert_audit']),1)
        self.ledger.close()
        self.ledger = Ledger(self.root/'collector.sqlite3',KEY,TENANT)
        self.assertEqual(self.ledger.source_health()['closed_alert_count'],1)

    def test_manual_reopen_is_audited_and_does_not_change_collection(self):
        closed = self.review.handle(self.request(self.inspect()))
        before = self.numeric_state()
        request = self.request(closed,action='reopen_alert')
        result = self.review.handle(request)
        self.assertEqual(result['result']['code'],'alert_reopened')
        self.assertEqual(result['source_health']['active_alert_count'],1)
        self.assertEqual(result['source_health']['closed_alert_count'],0)
        self.assertEqual(before,self.numeric_state())
        self.review.handle(request)
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM source_alert_audit').fetchone()[0],2)

    def test_returning_original_resolves_old_missing_alert_without_faking_usage(self):
        self.review.handle(self.request(self.inspect()))
        self.source.path.write_bytes(self.raw)
        health = self.ledger.source_health()
        self.assertFalse(health['closed_alerts'])
        self.assertFalse(health['active_alerts'])
        self.assertEqual(self.ledger.pending_count(),2)
        self.assertEqual(self.ledger.db.execute('SELECT action FROM source_alert_audit ORDER BY rowid DESC').fetchone()[0],'resolved')

    def test_returning_conflicted_file_reopens_instead_of_unpausing(self):
        stream = self.ledger.db.execute('SELECT id FROM streams').fetchone()[0]
        self.ledger.pause(stream,'unknown_identity')
        self.review.handle(self.request(self.inspect()))
        self.source.path.write_bytes(self.raw)
        health = self.ledger.source_health()
        self.assertEqual(health['active_alert_count'],1)
        self.assertEqual(health['closed_alert_count'],0)
        self.assertEqual(self.ledger.db.execute('SELECT paused FROM streams').fetchone()[0],'unknown_identity')
        self.assertEqual(self.ledger.db.execute('SELECT action FROM source_alert_audit ORDER BY rowid DESC').fetchone()[0],'changed')

    def test_new_source_gap_is_not_muted_by_an_existing_closure(self):
        self.review.handle(self.request(self.inspect()))
        other = Fixture(self.root/'other.jsonl');other.point(0,100)
        self.ledger.scan_file(other.path);other.path.unlink()
        health = self.ledger.source_health()
        self.assertEqual(health['active_alert_count'],1)
        self.assertEqual(health['closed_alert_count'],1)

    def test_pending_upload_and_ack_do_not_reopen_a_closed_missing_alert(self):
        self.review.handle(self.request(self.inspect()))
        upload_once(self.ledger,self.api)
        self.assertEqual(self.ledger.pending_count(),0)
        self.assertEqual(self.ledger.source_health()['closed_alert_count'],1)

    def test_offline_server_does_not_block_acknowledging_a_local_gap(self):
        self.ledger.pause(self.ledger.db.execute('SELECT id FROM streams').fetchone()[0],'unknown_identity')
        self.api.offline=True
        report = self.inspect()
        self.assertEqual(report['streams'][0]['server']['status'],'unavailable')
        self.assertEqual(self.review.handle(self.request(report))['result']['code'],'alert_closed')

    def test_changed_file_or_report_rejects_a_stale_closure(self):
        request = self.request(self.inspect())
        self.source.path.write_bytes(self.raw)
        self.assertEqual(self.review.handle(request)['result']['code'],'stale_review')
        self.source.path.unlink()
        request = self.request(self.inspect())
        self.inspect()
        self.assertEqual(self.review.handle(request)['result']['code'],'stale_review')

    def test_reason_is_required_and_bounded(self):
        for reason in ('',' '*3,'x'*201,'a\nb',None):
            with self.subTest(reason=reason):
                result = self.review.handle(self.request(self.inspect(),reason=reason))
                self.assertEqual(result['result']['code'],'reason_required')
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM source_alert_closures').fetchone()[0],0)

    def test_changed_compressed_file_reopens_its_closed_alert(self):
        archive = Path(str(self.source.path)+'.zst')
        archive.write_bytes(b'corrupt')
        self.review.handle(self.request(self.inspect()))
        archive.write_bytes(zstandard.ZstdCompressor().compress(self.raw+b'\n'))
        health = self.ledger.source_health()
        self.assertEqual(health['active_alert_count'],1)
        self.assertEqual(health['closed_alert_count'],0)

    def test_old_close_request_cannot_silence_a_new_problem(self):
        request = self.request(self.inspect())
        self.review.handle(request)
        self.ledger.pause(self.ledger.db.execute('SELECT id FROM streams').fetchone()[0],'unknown_identity')
        self.ledger.source_health()
        self.review.handle(request)
        self.assertEqual(self.ledger.source_health()['active_alert_count'],1)

    def test_unidentified_missing_sources_have_separate_closable_alerts(self):
        for name in ('invalid-a.jsonl','invalid-b.jsonl'):
            path=self.root/name;path.write_text('invalid json\n')
            self.ledger.scan_file(path);path.unlink()
        report=self.inspect()
        self.assertEqual(report['source_health']['active_alert_count'],3)
        index=next(i for i,item in enumerate(report['source_health']['active_alerts']) if item['stream_id'].startswith('unidentified:'))
        result=self.review.handle(self.request(report,index=index))
        self.assertEqual(result['result']['code'],'alert_closed')
        self.assertEqual(result['source_health']['active_alert_count'],2)
        self.assertEqual(result['source_health']['closed_alert_count'],1)

    def test_agent_suppresses_only_acknowledged_source_alerts_and_keeps_gap_in_heartbeat(self):
        home = self.root/'home';sessions=home/'sessions';sessions.mkdir(parents=True)
        class Api(FakeApi):
            auth_error=False
            def heartbeat(self,value):self.health=value
            def summary(self):
                if self.auth_error:raise ApiFailure(401,'authentication_failed')
                return {'quality':'partial'}
        api=Api()
        with patch('client.agent.QuotaMonitor') as quota,patch('client.agent.ActiveSessionReader') as active:
            quota.return_value.snapshot.return_value={};active.return_value.snapshot.return_value={'sessions':[]}
            agent=Agent(self.root/'agent-state',dict(identity_key=KEY,tenant_id=TENANT),[home],api=api)
            try:
                source=Fixture(sessions/'new.jsonl');source.point(0,100)
                agent.step()
                agent.ledger.pause(agent.ledger.db.execute('SELECT id FROM streams').fetchone()[0],'unknown_identity')
                agent.step()
                self.assertIsNone(agent.error)
                report=agent.source_review.handle(dict(id=str(uuid.uuid4()),action='inspect'))
                agent.source_review.handle(self.request(report))
                agent.last_heartbeat=0;agent.step()
                status=json.loads((agent.state/'status.json').read_text(encoding='utf-8'))
                self.assertIsNone(status['error'])
                self.assertEqual(status['source_health']['closed_alert_count'],1)
                self.assertEqual(status['blocked_streams'],0)
                self.assertEqual(status['blocked_streams_total'],1)
                self.assertEqual(api.health['quality'],'partial')
                self.assertIn('unknown_identity',api.health['quality_reasons'])
                self.assertNotIn('reason',api.health)
                self.assertNotIn('日志已删除',json.dumps(api.health,ensure_ascii=False))
                api.auth_error=True;agent.last_summary=0;agent.step()
                self.assertEqual(agent.error,'authentication_failed')
                api.auth_error=False;agent.last_summary=0;source.path.unlink();sessions.rmdir();agent.step()
                self.assertEqual(agent.error,'source_directory_missing')
            finally:agent.ledger.close()


if __name__=='__main__':unittest.main()
