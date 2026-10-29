import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from client.display_names import DisplayNameSync,name_rows
from client.transport import ApiFailure

A='a'*64;B='b'*64


class DisplayNameSyncTests(unittest.TestCase):
    def test_agent_sends_name_through_separate_api_only_after_identity_registration(self):
        from client.agent import Agent
        from client.tests.test_collector import FakeApi,KEY,TENANT
        subject=dict(principal_key='1'*64,machine_key='9'*64)
        class Identity:
            def __init__(self):self.subject=subject
            def snapshot(self):return dict(subject=self.subject,accounts=[A])
            def refresh(self):pass
            def claim(self,*_):return None
        class Profiles:
            def tick(self,*_):pass
            def snapshot(self):return [dict(account_key=A,display_name='展示名称',avatar_path='PRIVATE_IMAGE',email='PRIVATE_EMAIL')]
        class Quota:
            def tick(self):pass
            def snapshot(self):return {}
        class Api(FakeApi):
            names=[];telemetry=[];registered=False
            def register_identity(self,value):self.telemetry.append(value);self.registered=True
            def record_display_names(self,rows):
                if not self.registered:raise AssertionError('name_before_identity')
                self.names.extend(rows);return dict(recorded=len(rows))
            def heartbeat(self,value):self.telemetry.append(value)
            def summary(self,*_):return dict(windows=[],quality='partial')
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp)/'home';(home/'sessions').mkdir(parents=True)
            api=Api()
            with patch('client.agent.IdentityTracker',return_value=Identity()),patch('client.agent.ProfilesMonitor',return_value=Profiles()),patch('client.agent.QuotaMonitor',return_value=Quota()):
                agent=Agent(Path(temp)/'state',dict(identity_key=KEY,tenant_id=TENANT),[home],api=api)
                try:
                    agent.step();agent.display_names.worker.join(2)
                    self.assertEqual(api.names,[dict(account_key=A,display_name='展示名称')])
                    self.assertNotIn('展示名称',json.dumps(api.telemetry,ensure_ascii=False))
                    self.assertNotIn('PRIVATE_IMAGE',json.dumps(api.names))
                    self.assertNotIn('PRIVATE_EMAIL',json.dumps(api.names))
                finally:agent.display_names.close();agent.ledger.close()

    def test_only_valid_current_account_names_cross_the_wire(self):
        profiles=[dict(account_key=A,display_name=' 李明 ',avatar_path='PRIVATE_AVATAR',email='PRIVATE_EMAIL',token='PRIVATE_TOKEN'),
                  dict(account_key=B,display_name='旧账号'),dict(account_key='c'*64,display_name='未读取到账号名称')]
        self.assertEqual(name_rows(profiles,[A,'c'*64]),[dict(account_key=A,display_name='李明')])
        for name in ('','未读取到账号名称','bad\nname','bad\u202ename','x'*121):
            self.assertEqual(name_rows([dict(account_key=A,display_name=name)],[A]),[])

    def test_name_changes_restart_deduplication_and_ack_validation(self):
        class Api:
            rows=[];bad=False
            def record_display_names(self,rows):
                self.rows.extend(rows);return dict(recorded=0 if self.bad else len(rows))
        api=Api()
        with tempfile.TemporaryDirectory() as temp:
            sync=DisplayNameSync(temp,api)
            try:
                sync.tick([dict(account_key=A,display_name='原名称')],[A]);sync.worker.join(2)
                self.assertEqual(sync.sent[A],'原名称')
                sync.next_tick=0;sync.tick([dict(account_key=A,display_name='原名称')],[A])
                self.assertEqual(len(api.rows),1)
                api.bad=True;sync.next_tick=0;sync.tick([dict(account_key=A,display_name='新名称')],[A]);sync.worker.join(2)
                self.assertEqual(sync.sent[A],'原名称')
                api.bad=False;sync.next_tick=0;sync.tick([dict(account_key=A,display_name='新名称')],[A]);sync.worker.join(2)
                self.assertEqual(sync.sent[A],'新名称')
                restarted=DisplayNameSync(temp,api)
                try:
                    restarted.tick([dict(account_key=A,display_name='新名称')],[A]);self.assertIsNone(restarted.worker)
                finally:restarted.close()
            finally:sync.close()

    def test_rejected_account_does_not_block_another_name_and_is_retried(self):
        class Api:
            fail=True
            def record_display_names(self,rows):
                if rows[0]['account_key']==A and self.fail:raise ApiFailure(403,'account_not_observed')
                return dict(recorded=1)
        with tempfile.TemporaryDirectory() as temp:
            api=Api();sync=DisplayNameSync(temp,api)
            rows=[dict(account_key=A,display_name='甲'),dict(account_key=B,display_name='乙')]
            try:
                sync.tick(rows,[A,B]);sync.worker.join(2)
                self.assertEqual(sync.sent,{B:'乙'})
                self.assertEqual(sync.snapshot()['error'],'account_not_observed')
                api.fail=False;sync.next_tick=0;sync.tick(rows,[A,B]);sync.worker.join(2)
                self.assertEqual(sync.sent,{A:'甲',B:'乙'})
                self.assertIsNone(sync.snapshot()['error'])
            finally:sync.close()


if __name__=='__main__':unittest.main()
