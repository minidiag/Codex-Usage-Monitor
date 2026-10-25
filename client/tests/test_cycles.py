from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from client.cycles import CycleMonitor,observed_cycles
from client.transport import ApiFailure
from server.protocol import UTC,iso

ACCOUNT="a"*64
PRINCIPAL="b"*64


def quota(now=None):
    now=now or datetime.now(UTC)
    return dict(account_key=ACCOUNT,fetched_at=iso(now),display_name="LOCAL_NAME",avatar_path="LOCAL_IMAGE",windows=[
        dict(key="primary",duration_mins=300.0,resets_at=int((now+timedelta(hours=1)).timestamp()),remaining_percent=50)])


class CycleClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.calls=[]
        calls=self.calls
        class Api:
            offline=False
            wrong_scope=False
            def record_quota_cycles(self,rows):
                if self.offline:raise ApiFailure()
                calls.append(rows);return dict(recorded=len(rows))
            def quota_cycles(self,account,principal):
                return dict(account_key=account,principal_key="c"*64 if self.wrong_scope else principal,as_of=iso(datetime.now(UTC)),cycles=[])
        self.api=Api()
        self.monitor=CycleMonitor(Path(self.temp.name),self.api)
        self.addCleanup(self.monitor.close)

    def test_only_anonymous_cycle_bounds_enter_outbox(self):
        self.monitor.observe([quota()],[ACCOUNT])
        wire=self.monitor.db.execute("SELECT wire FROM reports").fetchone()[0]
        self.assertEqual(set(json.loads(wire)),{"account_key","bucket","duration_mins","resets_at","observed_at"})
        for forbidden in ("LOCAL_NAME","LOCAL_IMAGE","remaining_percent"):self.assertNotIn(forbidden,wire)

    def test_missing_expired_fractional_and_bad_fields_never_become_fake_cycles(self):
        now=datetime.now(UTC)
        for value in (None,float("nan"),float("inf"),0,1.5,"300",True):
            q=quota(now);q["windows"][0]["duration_mins"]=value
            self.assertEqual(observed_cycles([q],[ACCOUNT],now),[])
        q=quota(now);q["fetched_at"]=iso(now-timedelta(seconds=91))
        self.assertEqual(observed_cycles([q],[ACCOUNT],now),[])
        q=quota(now);q["windows"][0]["resets_at"]=int(now.timestamp())-1
        self.assertEqual(observed_cycles([q],[ACCOUNT],now),[])
        q=quota(now);q["fetched_at"]="bad"
        self.assertEqual(observed_cycles([q],[ACCOUNT],now),[])
        self.assertEqual(observed_cycles([quota(now)],[],now),[])

    def test_restart_preserves_pending_cycle_without_duplicates(self):
        q=quota();self.monitor.observe([q],[ACCOUNT]);self.monitor.observe([q],[ACCOUNT])
        self.assertEqual(self.monitor.db.execute("SELECT count(*) FROM reports").fetchone()[0],1)
        other=CycleMonitor(Path(self.temp.name),self.api)
        try:self.assertEqual(other.db.execute("SELECT count(*) FROM reports WHERE pending=1").fetchone()[0],1)
        finally:other.close()

    def test_offline_metadata_retries_and_ack_clears_only_sent_records(self):
        self.monitor.observe([quota()],[ACCOUNT]);self.api.offline=True
        self.monitor._run(PRINCIPAL,ACCOUNT)
        self.assertEqual(self.monitor.db.execute("SELECT pending FROM reports").fetchone()[0],1)
        self.api.offline=False;self.monitor._run(PRINCIPAL,ACCOUNT)
        self.assertEqual(self.monitor.db.execute("SELECT pending FROM reports").fetchone()[0],0)
        self.assertEqual(self.monitor.snapshot()["accounts"][ACCOUNT]["principal_key"],PRINCIPAL)
        self.assertEqual(len(self.calls),1)

    def test_periodic_refresh_requeues_same_cycle_instead_of_creating_new_one(self):
        q=quota();self.monitor.observe([q],[ACCOUNT]);self.monitor._run(PRINCIPAL,ACCOUNT)
        import time
        with patch("client.cycles.time.time",return_value=time.time()+61):self.monitor.observe([q],[ACCOUNT])
        self.assertEqual(self.monitor.db.execute("SELECT count(*) FROM reports WHERE pending=1").fetchone()[0],1)

    def test_wrong_principal_response_is_not_displayed_or_cached(self):
        self.api.wrong_scope=True;self.monitor._run(PRINCIPAL,ACCOUNT)
        self.assertNotIn(ACCOUNT,self.monitor.snapshot()["accounts"])
        self.assertEqual(self.monitor.snapshot()["error"],"cycle_sync_failed")

    def test_conflicting_period_does_not_block_other_cycles(self):
        q=quota();q["windows"].append(dict(key="secondary",duration_mins=10080,resets_at=int(datetime.now(UTC).timestamp())+86400))
        self.monitor.observe([q],[ACCOUNT])
        original=self.api.record_quota_cycles
        def conflict(rows):
            if any(r["bucket"]=="primary" for r in rows):raise ApiFailure(409,"cycle_boundary_conflict")
            return original(rows)
        self.api.record_quota_cycles=conflict
        self.monitor._run(PRINCIPAL,ACCOUNT)
        states=dict(self.monitor.db.execute("SELECT bucket,pending FROM reports"))
        self.assertEqual(states,{"primary":-1,"secondary":0})
        self.monitor.observe([q],[ACCOUNT])
        self.assertEqual(self.monitor.snapshot()["blocked_reports"],1)


if __name__=="__main__":unittest.main()
