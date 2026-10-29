from datetime import datetime,timedelta
import os
import unittest

from server.protocol import ApiError,UTC,iso,quota_observations
from server.tests import test_cycles


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'),'disposable PostgreSQL required')
class BoundaryStoreTests(unittest.TestCase):
    setUpClass=classmethod(test_cycles.CycleTests.setUpClass.__func__)
    setUp=test_cycles.CycleTests.setUp
    tearDown=test_cycles.CycleTests.tearDown
    register=test_cycles.CycleTests.register
    usage=test_cycles.CycleTests.usage
    report=test_cycles.CycleTests.report

    def observations(self,token,bucket='primary',duration=300,reset=None,times=None):
        now=datetime.now(UTC).replace(microsecond=0)
        reset=reset or now+timedelta(hours=1)
        times=times or [now-timedelta(seconds=s) for s in (65,30,0)]
        rows=quota_observations(dict(cycles=[dict(account_key='a'*64,bucket=bucket,duration_mins=str(duration),
            resets_at=str(int(reset.timestamp())),observed_at=iso(t)) for t in times]))
        self.store.record_boundary_observations(token,rows)
        return rows

    def current(self,token):return self.store.current_quota_cycles(token,'a'*64)

    def test_stability_duplicate_replay_and_independent_current_slots(self):
        write,read=self.register(restricted=True)
        rows=self.observations(write)
        self.store.record_boundary_observations(write,rows)
        current=self.current(read)
        self.assertEqual(current['buckets']['primary']['status'],'available')
        self.assertEqual(current['buckets']['secondary']['status'],'waiting_observation')
        cycle=current['buckets']['primary']['cycle']['cycle_id']
        self.assertNotEqual(cycle,rows[0]['cycle_id'])
        self.usage(write,'a',100,'a'*64)
        self.assertEqual(self.current(read)['buckets']['primary']['cycle']['totals']['total'],'100')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) n FROM quota_boundary_samples WHERE tenant_id=%s',(self.tenant,)).fetchone()['n'],3)
            self.assertEqual(db.execute('SELECT count(*) n FROM quota_cycles WHERE tenant_id=%s',(self.tenant,)).fetchone()['n'],1)

    def test_moving_boundary_stores_evidence_without_cycles(self):
        write,read=self.register(restricted=True)
        now=datetime.now(UTC).replace(microsecond=0)
        for ago in range(300,-1,-30):
            t=now-timedelta(seconds=ago)
            self.observations(write,'secondary',10080,t+timedelta(days=7),[t])
        slot=self.current(read)['buckets']['secondary']
        self.assertEqual(slot,dict(status='boundary_pending',cycle=None))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) n FROM quota_cycles WHERE tenant_id=%s',(self.tenant,)).fetchone()['n'],0)

    def test_early_changed_boundary_preserves_cycle_and_totals(self):
        write,read=self.register(restricted=True)
        now=datetime.now(UTC).replace(microsecond=0)
        self.observations(write,reset=now+timedelta(hours=1),times=[now-timedelta(seconds=s) for s in (130,100,70)])
        self.usage(write,'a',100,'a'*64)
        self.observations(write,reset=now+timedelta(hours=2))
        self.assertEqual(self.current(read)['buckets']['primary']['status'],'boundary_review')
        rows=self.store.quota_cycle_history(read,'a'*64)['cycles']
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['totals']['total'],'100')
        self.assertEqual(rows[0]['ends_at'],iso(now+timedelta(hours=1)))

    def test_current_primary_survives_large_weekly_history_and_scope(self):
        write,read=self.register(restricted=True)
        now=datetime.now(UTC).replace(microsecond=0)
        for offset in range(12):self.report(write,end=now+timedelta(days=6,seconds=offset),bucket='secondary',duration=10080)
        self.observations(write)
        self.assertEqual(self.current(read)['buckets']['primary']['status'],'available')
        with self.assertRaises(ApiError):self.store.current_quota_cycles(read,'a'*64,'2'*64)
        page=self.store.quota_cycle_history(read,'a'*64,bucket='secondary',limit=3)
        self.assertEqual(len(page['cycles']),3)
        nextpage=self.store.quota_cycle_history(read,'a'*64,bucket='secondary',limit=3,before=datetime.fromisoformat(page['next_before'].replace('Z','+00:00')))
        self.assertFalse({c['cycle_id'] for c in page['cycles']} & {c['cycle_id'] for c in nextpage['cycles']})

    def test_same_observation_time_different_boundary_is_rejected(self):
        write,_=self.register()
        rows=self.observations(write)
        rows[0]=dict(rows[0],resets_at=rows[0]['resets_at']+timedelta(seconds=1))
        with self.assertRaises(ApiError):self.store.record_boundary_observations(write,rows)

    def test_expired_cycle_rolls_over_without_rewriting_old_bounds(self):
        write,read=self.register(restricted=True)
        now=datetime.now(UTC).replace(microsecond=0)
        old_reset=now-timedelta(seconds=120)
        self.observations(write,reset=old_reset,times=[now-timedelta(seconds=s) for s in (200,170,140)])
        self.observations(write,reset=old_reset+timedelta(hours=5))
        current=self.current(read)['buckets']['primary']
        self.assertEqual(current['status'],'available')
        self.assertEqual(current['cycle']['starts_at'],iso(old_reset))
        history=self.store.quota_cycle_history(read,'a'*64,bucket='primary')['cycles']
        self.assertEqual(len(history),2)
        self.assertEqual(history[1]['ends_at'],history[0]['starts_at'])

    def test_upgrade_preserves_legacy_fragments_and_seeds_current(self):
        from pathlib import Path
        from psycopg import sql
        import uuid
        root=Path(__file__).parents[1]
        with self.store.connect() as db:
            # An isolated, transaction-local schema exercises the actual migration SQL.
            schema='migration_test_'+uuid.uuid4().hex
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            db.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
            for filename in ('schema.sql','migrations/002_accounts.sql','migrations/003_quota_cycles.sql'):
                db.execute((root/filename).read_text(encoding='utf-8'))
            db.execute('INSERT INTO tenants(id) VALUES(%s)',(self.tenant,))
            db.execute('INSERT INTO accounts(tenant_id,account_key) VALUES(%s,%s)',(self.tenant,'a'*64))
            db.execute('''INSERT INTO quota_cycles(tenant_id,cycle_id,account_key,bucket,duration_mins,starts_at,resets_at,ends_at,first_observed_at,last_observed_at)
                VALUES(%s,%s,%s,'primary',300,now()-interval '4 hours',now()+interval '1 hour',now()+interval '1 hour',now(),now()),
                      (%s,%s,%s,'primary',300,now()-interval '4 hours 1 second',now()+interval '59 minutes 59 seconds',now()-interval '4 hours',now(),now())''',
                (self.tenant,'b'*64,'a'*64,self.tenant,'c'*64,'a'*64))
            before=db.execute('SELECT * FROM quota_cycles ORDER BY cycle_id').fetchall()
            db.execute((root/'migrations/004_boundary_observations.sql').read_text(encoding='utf-8'))
            self.assertEqual(db.execute('SELECT * FROM quota_cycles ORDER BY cycle_id').fetchall(),before)
            state=db.execute('SELECT state FROM quota_boundary_states').fetchone()['state']
            self.assertEqual(state['current']['id'],'b'*64)
            db.rollback()

    def test_http_v3_and_legacy_posts_share_boundary_recognition(self):
        from starlette.testclient import TestClient
        from server.app import create_app
        write,read=self.register(restricted=True)
        client=TestClient(create_app(self.store))
        now=datetime.now(UTC).replace(microsecond=0)
        rows=[dict(account_key='a'*64,bucket='primary',duration_mins='300',
                   resets_at=str(int(now.timestamp())+3600),observed_at=iso(now-timedelta(seconds=s))) for s in (65,30,0)]
        auth={'Authorization':'Bearer '+write}
        response=client.post('/v2/usage/quota-cycles',headers=auth,json={'cycles':rows[:1]})
        self.assertEqual(response.status_code,200)
        response=client.post('/v3/usage/quota-observations',headers=auth,json={'cycles':rows[1:]})
        self.assertEqual(response.status_code,200)
        auth={'Authorization':'Bearer '+read}
        query='?account='+'a'*64+'&principal='+'1'*64
        response=client.get('/v3/usage/quota-current'+query,headers=auth)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['buckets']['primary']['status'],'available')
        response=client.get('/v3/usage/quota-history'+query+'&bucket=primary',headers=auth)
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json()['cycles']),1)
