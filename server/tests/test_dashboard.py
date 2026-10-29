import os
import unittest
from datetime import datetime,timedelta

from starlette.testclient import TestClient
from server.app import create_app
from server.protocol import UTC,SHANGHAI
from server.tests import test_cycles


class DashboardHttpTests(unittest.TestCase):
    def test_page_assets_and_authentication(self):
        client=TestClient(create_app(object()))
        response=client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertIn('用量仪表盘',response.text)
        self.assertIn("frame-ancestors 'none'",response.headers['content-security-policy'])
        for asset in ('dashboard.css','dashboard.js'):
            self.assertEqual(client.get('/dashboard/'+asset).status_code,200)
        self.assertEqual(client.get('/dashboard/private.json').status_code,404)
        self.assertEqual(client.get('/v3/usage/trend').status_code,401)

    def test_invalid_filters_never_reach_database(self):
        client=TestClient(create_app(object()));auth={'Authorization':'Bearer '+'x'*40}
        for query in ('period=all','account=bad','period=7d&period=24h','principal='+'1'*64,'start=2020-01-01'):
            self.assertEqual(client.get('/v3/usage/trend?'+query,headers=auth).status_code,422)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'disposable PostgreSQL required')
class DashboardStoreTests(unittest.TestCase):
    setUpClass=classmethod(test_cycles.CycleTests.setUpClass.__func__)
    setUp=test_cycles.CycleTests.setUp
    tearDown=test_cycles.CycleTests.tearDown
    register=test_cycles.CycleTests.register
    usage=test_cycles.CycleTests.usage
    at=test_cycles.CycleTests.at

    def test_account_and_reader_scope_and_http(self):
        one,read=self.register(restricted=True);two,_=self.register('2'*64)
        self.usage(one,'a',100,'a'*64)
        self.usage(two,'b',200,'a'*64,'2'*64)
        self.usage(one,'c',50,'b'*64)
        own=self.store.usage_trend(read,'a'*64,'7d')
        self.assertEqual(own['totals']['total'],'100')
        self.assertEqual(self.store.usage_trend(self.read,'a'*64)['totals']['total'],'300')
        self.assertEqual(self.store.usage_trend(read,None)['totals']['total'],'150')
        self.assertEqual(self.store.usage_trend(read,'c'*64)['totals']['total'],'0')
        response=TestClient(create_app(self.store)).get('/v3/usage/trend?account='+'a'*64,headers={'Authorization':'Bearer '+read})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['totals']['total'],'100')
        with self.store.connect() as db:
            from server.protocol import token_hash
            db.execute('UPDATE credentials SET revoked_at=now() WHERE token_hash=%s',(token_hash(read),))
        self.assertEqual(TestClient(create_app(self.store)).get('/v3/usage/trend',headers={'Authorization':'Bearer '+read}).status_code,401)

    def test_shanghai_day_boundary_zero_fill_and_exact_large_numbers(self):
        write,_=self.register()
        now=datetime.now(UTC)
        local_midnight=now.astimezone(SHANGHAI).replace(hour=0,minute=0,second=0,microsecond=0)
        with self.store.connect() as db:
            db.execute("UPDATE device_subjects SET created_at=now()-interval '3 days' WHERE tenant_id=%s",(self.tenant,))
        large=9007199254740993
        self.at(write,'before',large,local_midnight-timedelta(seconds=1))
        self.at(write,'after',13,local_midnight+timedelta(seconds=1))
        view=self.store.usage_trend(self.read,'a'*64,'7d',now=local_midnight+timedelta(hours=1))
        self.assertEqual(len(view['points']),7)
        self.assertEqual(view['totals']['total'],str(large+13))
        self.assertEqual(view['points'][-2]['totals']['total'],str(large))
        self.assertEqual(view['points'][-1]['totals']['total'],'13')
        self.assertEqual(view['points'][0]['totals']['total'],'0')
        for key,value in view['totals'].items():
            self.assertEqual(sum(int(p['totals'][key]) for p in view['points']),int(value))

    def test_rolling_range_is_half_open_and_bounded(self):
        write,_=self.register();now=datetime.now(UTC).replace(microsecond=0)
        with self.store.connect() as db:db.execute("UPDATE device_subjects SET created_at=now()-interval '3 days' WHERE tenant_id=%s",(self.tenant,))
        self.at(write,'start',11,now-timedelta(hours=24))
        self.at(write,'outside',22,now-timedelta(hours=24,seconds=1))
        self.at(write,'end',33,now)
        view=self.store.usage_trend(self.read,'a'*64,'24h',now=now)
        self.assertEqual(view['totals']['total'],'11')
        self.assertLessEqual(len(view['points']),25)
        self.assertEqual(len(self.store.usage_trend(self.read,None,'90d')['points']),90)
