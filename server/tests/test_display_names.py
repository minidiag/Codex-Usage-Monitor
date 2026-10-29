from datetime import datetime
import os
from pathlib import Path
import unittest
import uuid

from starlette.testclient import TestClient
from server.app import create_app
from server.protocol import ApiError,UTC,display_names,token_hash
from server.tests import test_accounts

A='a'*64;B='b'*64


class DisplayNameProtocolTests(unittest.TestCase):
    def test_strict_payload_validation(self):
        good=dict(account_key=A,display_name='李明 <b>· 👩\u200d💻')
        self.assertEqual(display_names(dict(profiles=[good])),[good])
        for name in ('',' ',123,'x'*121,'bad\nname','bad\u202ename'):
            with self.assertRaises(ApiError):display_names(dict(profiles=[dict(good,display_name=name)]))
        for value in (dict(profiles=[]),dict(profiles=[good,good]),dict(profiles=[dict(good,avatar='not_allowed')])):
            with self.assertRaises(ApiError):display_names(value)
        client=TestClient(create_app(object()))
        self.assertEqual(client.post('/v3/accounts/display-names',json={'profiles':[good]}).status_code,401)
        self.assertEqual(client.post('/v3/accounts/display-names',headers={'Authorization':'Bearer '+'x'*40},json={'profiles':[dict(good,email='private')]}).status_code,422)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'disposable PostgreSQL required')
class DisplayNameStoreTests(unittest.TestCase):
    setUpClass=classmethod(test_accounts.AccountTests.setUpClass.__func__)
    setUp=test_accounts.AccountTests.setUp
    tearDown=test_accounts.AccountTests.tearDown
    register=test_accounts.AccountTests.register
    usage=test_accounts.AccountTests.usage

    def send(self,write,name='显示名称',account=A):
        return self.store.record_display_names(write,display_names(dict(profiles=[dict(account_key=account,display_name=name)])))

    def names(self,read):return self.store.identities(read)['account_display_names']

    def test_scoped_names_update_and_repeat_without_changing_numeric_totals(self):
        one,read=self.register(restricted=True);two,other=self.register('2'*64)
        self.usage(one,'a',100,A);self.usage(two,'b',200,A,'2'*64)
        self.send(one,'账号甲');self.send(two,'另一用户的名称')
        self.assertEqual(self.names(read)[A]['display_name'],'账号甲')
        self.assertEqual(self.names(other)[A]['display_name'],'另一用户的名称')
        first=self.names(read)[A]['updated_at'];self.send(one,'账号甲')
        self.assertEqual(self.names(read)[A]['updated_at'],first)
        self.send(one,'改名后')
        self.assertEqual(self.names(read)[A]['display_name'],'改名后')
        self.assertEqual(self.store.summary_scoped(read,dict(account=A))['windows'][0]['totals']['total'],'100')
        self.assertIn(A,self.names(self.read))

    def test_account_evidence_readonly_and_revocation_are_enforced(self):
        write,read=self.register(restricted=True)
        with self.assertRaises(ApiError):self.send(write)
        self.usage(write,'a',100,A)
        with self.assertRaises(ApiError):self.send(write,account=B)
        with self.assertRaises(ApiError):self.send(read)
        self.send(write)
        another,_=self.register('2'*64)
        with self.assertRaises(ApiError):self.send(another,'不可覆盖')
        self.assertEqual(self.names(read)[A]['display_name'],'显示名称')
        with self.store.connect() as db:db.execute('UPDATE credentials SET revoked_at=now() WHERE token_hash=%s',(token_hash(write),))
        with self.assertRaises(ApiError):self.send(write,'撤销后')

    def test_quota_observation_is_sufficient_and_api_returns_names(self):
        from server.protocol import quota_observations,iso
        write,read=self.register(restricted=True);now=datetime.now(UTC)
        self.store.record_boundary_observations(write,quota_observations(dict(cycles=[dict(account_key=A,bucket='primary',duration_mins='300',resets_at=str(int(now.timestamp())+3600),observed_at=iso(now))])))
        client=TestClient(create_app(self.store));auth={'Authorization':'Bearer '+write}
        response=client.post('/v3/accounts/display-names',headers=auth,json=dict(profiles=[dict(account_key=A,display_name='零用量账号')]))
        self.assertEqual(response.status_code,200)
        result=client.get('/v2/identities',headers={'Authorization':'Bearer '+read}).json()
        self.assertEqual(result['accounts'],[A]);self.assertEqual(result['account_display_names'][A]['display_name'],'零用量账号')

    def test_migration_from_v4_is_additive_and_idempotent(self):
        from server.admin import migrate
        from psycopg import sql
        write,read=self.register();self.usage(write,'a',123,A)
        with self.store.connect() as db:
            schema='names_migration_'+uuid.uuid4().hex
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            db.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
            root=Path(__file__).parents[1]
            for name in ('schema.sql','migrations/002_accounts.sql','migrations/003_quota_cycles.sql','migrations/004_boundary_observations.sql'):
                db.execute((root/name).read_text(encoding='utf-8'))
            db.execute('INSERT INTO tenants(id) VALUES(%s)',(self.tenant,))
            before=db.execute('SELECT * FROM tenants').fetchall()
            db.execute((root/'migrations/005_account_display_names.sql').read_text(encoding='utf-8'))
            self.assertEqual(db.execute('SELECT * FROM tenants').fetchall(),before)
            self.assertEqual(db.execute('SELECT count(*) n FROM account_display_names').fetchone()['n'],0)
            db.rollback()
        migrate(self.store.dsn,Path(__file__).parents[1]/'schema.sql')
        self.assertEqual(self.store.summary_scoped(read,dict(account=A))['windows'][0]['totals']['total'],'123')
