from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
import hashlib
import os
import unittest

from server.protocol import ApiError,UTC,batch,iso,quota_cycles,token_hash
from server.tests import test_accounts
from server.tests.helpers import make_batch,make_record


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),"disposable PostgreSQL required")
class CycleTests(unittest.TestCase):
    setUpClass=classmethod(test_accounts.AccountTests.setUpClass.__func__)
    setUp=test_accounts.AccountTests.setUp
    tearDown=test_accounts.AccountTests.tearDown
    register=test_accounts.AccountTests.register
    usage=test_accounts.AccountTests.usage

    def report(self,token,end=None,bucket="primary",duration=300,observed=None):
        now=datetime.now(UTC)
        value=dict(cycles=[dict(account_key="a"*64,bucket=bucket,duration_mins=str(duration),resets_at=str(int((end or now+timedelta(hours=1)).timestamp())),observed_at=iso(observed or now-timedelta(seconds=5)))])
        self.store.record_quota_cycles(token,quota_cycles(value))
        return value

    def history(self,principal=None):return self.store.quota_cycle_history(self.read,"a"*64,principal)["cycles"]

    def at(self,token,seed,amount,when):
        records=[make_record(0,when=when-timedelta(seconds=1)),make_record(1,(amount,0,0,0),when=when)]
        for record in records:record["event_id"]=hashlib.sha256((seed+record["source_seq"]).encode()).hexdigest()
        records[1]["attribution"]=dict(account_key="a"*64,principal_key="1"*64,machine_key="9"*64,observed_from=iso(when-timedelta(seconds=2)))
        data=make_batch(records,stream=hashlib.sha256(seed.encode()).hexdigest());data["schema_version"]="1.1"
        return self.store.ingest(token,batch(data))

    def old_subject(self):
        with self.store.connect() as db:db.execute("UPDATE device_subjects SET created_at=now()-interval '2 days' WHERE tenant_id=%s",(self.tenant,))

    def test_zero_is_persisted_and_new_usage_updates_materialized_totals_once(self):
        write,_=self.register();wire=self.report(write)
        self.assertEqual(self.history()[0]["totals"]["total"],"0")
        data=self.usage(write,"a",100,"a"*64)
        self.store.ingest(write,batch(data));self.store.record_quota_cycles(write,quota_cycles(wire))
        self.assertEqual(self.history()[0]["totals"]["total"],"100")
        with self.store.connect() as db:
            row=db.execute("SELECT count(*) n,sum(total_tokens) total FROM quota_cycle_totals WHERE tenant_id=%s",(self.tenant,)).fetchone()
            self.assertEqual((row["n"],str(row["total"])),(1,"100"))

    def test_shared_account_principal_scope_and_weekly_cycle_are_independent(self):
        one,restricted=self.register(restricted=True);two,_=self.register("2"*64)
        self.report(one);self.report(one,end=datetime.now(UTC)+timedelta(days=6),bucket="secondary",duration=10080)
        self.usage(one,"a",100,"a"*64);self.usage(two,"b",200,"a"*64,"2"*64)
        self.assertEqual({r["bucket"]:r["totals"]["total"] for r in self.history()},{"primary":"300","secondary":"300"})
        own=self.store.quota_cycle_history(restricted,"a"*64)
        self.assertEqual({r["totals"]["total"] for r in own["cycles"]},{"100"})
        with self.assertRaises(ApiError):self.store.quota_cycle_history(restricted,"a"*64,"2"*64)

    def test_exact_reset_boundary_and_late_old_period_upload(self):
        write,_=self.register();self.old_subject()
        end=datetime.now(UTC).replace(microsecond=0)-timedelta(hours=1)
        self.report(write,end=end,observed=end-timedelta(hours=1))
        self.report(write,end=end+timedelta(hours=5))
        self.at(write,"before",100,end-timedelta(seconds=1))
        self.at(write,"exact",200,end)
        self.at(write,"late",50,end-timedelta(seconds=2))
        rows=self.history();totals={r["resets_at"]:r["totals"]["total"] for r in rows}
        self.assertEqual(totals[str(int(end.timestamp()))],"150")
        self.assertEqual(totals[str(int((end+timedelta(hours=5)).timestamp()))],"200")
        self.assertTrue(next(r for r in rows if r["resets_at"]==str(int(end.timestamp())))["closed"])

    def test_early_reset_closes_prior_cycle_without_overlapping_totals(self):
        write,_=self.register();self.old_subject()
        now=datetime.now(UTC).replace(microsecond=0)
        old=self.report(write,end=now+timedelta(hours=3))
        self.at(write,"early-old",100,now-timedelta(minutes=90))
        self.at(write,"early-new",200,now-timedelta(minutes=30))
        self.report(write,end=now+timedelta(hours=4))
        self.store.record_quota_cycles(write,quota_cycles(old))
        rows=self.history();prior=next(r for r in rows if r["shortened"])
        self.assertEqual(prior["ends_at"],iso(now-timedelta(hours=1)))
        self.assertEqual(prior["totals"]["total"],"100")
        self.assertEqual(sum(int(r["totals"]["total"]) for r in rows),300)

    def test_cycle_registration_racing_ingestion_does_not_lose_tokens(self):
        write,_=self.register()
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs=[pool.submit(self.report,write)]
            jobs.extend(pool.submit(self.usage,write,seed,100,"a"*64) for seed in ("b","c","d"))
            for job in jobs:job.result()
        self.assertEqual(self.history()[0]["totals"]["total"],"300")

    def test_conflicting_duration_cannot_rewrite_existing_cycle(self):
        write,_=self.register();end=datetime.now(UTC)+timedelta(hours=1)
        self.report(write,end=end)
        with self.assertRaises(ApiError):self.report(write,end=end,duration=301)
        self.assertEqual(self.history()[0]["duration_mins"],"300")

    def test_revoked_collector_cannot_keep_reading_cycle_history(self):
        write,read=self.register(restricted=True);self.report(write)
        with self.store.connect() as db:
            db.execute("UPDATE devices SET status='revoked' WHERE tenant_id=%s AND id=(SELECT reader_device_id FROM credentials WHERE token_hash=%s)",(self.tenant,token_hash(read)))
        with self.assertRaises(ApiError):self.store.quota_cycle_history(read,"a"*64)


class CycleProtocolTests(unittest.TestCase):
    def test_wire_is_numeric_and_anonymous_only(self):
        now=datetime.now(UTC)
        row=dict(account_key="a"*64,bucket="primary",duration_mins="300",resets_at=str(int((now+timedelta(hours=1)).timestamp())),observed_at=iso(now))
        normalized=quota_cycles(dict(cycles=[row]))[0]
        self.assertEqual((normalized["resets_at"]-normalized["starts_at"]).total_seconds(),18000)
        for field in ("display_name","avatar_path","remaining_percent"):
            with self.assertRaises(ApiError):quota_cycles(dict(cycles=[dict(row,**{field:"must-not-send"})]))
        with self.assertRaises(ApiError):quota_cycles(dict(cycles=[dict(row,duration_mins="0")]))
        with self.assertRaises(ApiError):quota_cycles(dict(cycles=[row,row]))
