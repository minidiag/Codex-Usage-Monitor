import base64
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from client.identity import IdentityTracker,account_metadata,pseudonym
from client.paths import user_root
from client.install_user import protect
from client.transport import load_credentials
from client.core import Ledger
from client.tests.test_collector import Fixture,KEY,TENANT
from server.protocol import UTC,iso


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)/"codex"
        self.home.mkdir()
        self.now=[datetime(2026,10,26,tzinfo=UTC).timestamp()]
        self.subject=dict(principal_key="1"*64,machine_key="2"*64)
        self.session=str(uuid.uuid4())
        self.auth("user-a","workspace-a")
        self.owner("user-a","workspace-a")
        self.tracker=IdentityTracker([self.home],KEY,self.subject,lambda:self.now[0])
        self.tracker.refresh()

    def auth(self,user,workspace):
        claims={"https://api.openai.com/auth":dict(chatgpt_user_id=user,chatgpt_account_id=workspace),"email":"never-upload@example.test"}
        segment=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        path=self.home/"auth.json"
        path.write_text(json.dumps(dict(tokens=dict(id_token="header."+segment+".signature",account_id=workspace,access_token="never-upload-access",refresh_token="never-upload-refresh"))),encoding="utf-8")
        os.utime(path,ns=(int(self.now[0]*1e9),int(self.now[0]*1e9)))

    def owner(self,user,workspace):
        with closing(sqlite3.connect(self.home/"state_5.sqlite")) as db:
            db.execute("CREATE TABLE IF NOT EXISTS threads(id TEXT,creator_user_id TEXT,creator_account_id TEXT)")
            db.execute("DELETE FROM threads")
            db.execute("INSERT INTO threads VALUES(?,?,?)",(self.session,user,workspace))
            db.commit()

    def claim(self,previous=None):
        return self.tracker.claim(pseudonym(KEY,"codex-rollout-v1",self.session),
                                  dict(event_time=iso(datetime.fromtimestamp(previous if previous is not None else self.now[0],UTC))),None)

    def test_same_codex_identity_merges_across_windows_subjects(self):
        other=IdentityTracker([self.home],KEY,dict(principal_key="3"*64,machine_key="4"*64),lambda:self.now[0])
        other.refresh()
        self.assertEqual(self.tracker.snapshot()["accounts"],other.snapshot()["accounts"])
        self.assertNotEqual(self.tracker.subject,other.subject)

    def test_different_logins_in_same_workspace_stay_separate(self):
        first=account_metadata(self.home,KEY)[0]
        self.now[0]+=5
        self.auth("user-b","workspace-a")
        self.assertNotEqual(first,account_metadata(self.home,KEY)[0])

    def test_stable_observation_can_attribute_new_increment(self):
        self.now[0]+=5
        self.tracker.refresh()
        claim=self.claim(self.now[0]-4)
        self.assertEqual(claim["account_key"],self.tracker.snapshot()["accounts"][0])
        self.assertEqual(claim["principal_key"],self.subject["principal_key"])

    def test_pre_start_history_and_gap_interval_remain_unassigned(self):
        self.assertIsNone(self.claim(self.now[0]-1))
        self.now[0]+=25
        self.tracker.refresh()
        self.assertIsNone(self.claim(self.now[0]-1))

    def test_switch_resets_boundary_and_old_thread_is_not_given_new_account(self):
        first=self.tracker.snapshot()["accounts"][0]
        self.now[0]+=5
        self.auth("user-b","workspace-b")
        self.tracker.refresh()
        self.assertIsNone(self.claim(self.now[0]-1))
        self.assertIsNone(self.claim()["account_key"])
        self.owner("user-b","workspace-b")
        self.now[0]+=5
        self.tracker.refresh()
        self.assertNotEqual(first,self.claim()["account_key"])

    def test_changed_auth_during_scan_invalidates_claim(self):
        self.now[0]+=5
        self.auth("user-b","workspace-b")
        self.assertIsNone(self.claim())

    def test_api_key_mode_is_not_fingerprinted_or_sent(self):
        (self.home/"auth.json").write_text(json.dumps(dict(OPENAI_API_KEY="private-api-key")))
        self.assertIsNone(account_metadata(self.home,KEY)[0])

    def test_api_key_mode_does_not_reuse_stale_oauth_identity(self):
        path=self.home/"auth.json"
        data=json.loads(path.read_text(encoding="utf-8"))
        data["auth_mode"]="apikey"
        path.write_text(json.dumps(data),encoding="utf-8")
        self.assertIsNone(account_metadata(self.home,KEY)[0])

    def test_unsupported_or_conflicting_metadata_fails_closed(self):
        path=self.home/"auth.json"
        for value in ([],{},dict(tokens=True),dict(tokens=dict(id_token="invalid"))):
            path.write_text(json.dumps(value))
            self.assertIsNone(account_metadata(self.home,KEY)[0])

    def test_snapshot_contains_no_raw_identity_email_or_token(self):
        encoded=json.dumps(self.tracker.snapshot())
        for value in ("user-a","workspace-a","never-upload","id_token","access_token","refresh_token"):
            self.assertNotIn(value,encoded)

    def test_wire_persists_claim_offline_and_numeric_identity_is_unchanged(self):
        fixture=Fixture(self.home/"source.jsonl",self.session)
        fixture.started=datetime.fromtimestamp(self.now[0],UTC)
        fixture.point(0,100)
        ledger=Ledger(Path(self.temp.name)/"ledger.sqlite",KEY,TENANT,attribution_provider=self.tracker.claim)
        self.addCleanup(ledger.close)
        ledger.scan_file(fixture.path)
        fixture.point(1,200)
        ledger.scan_file(fixture.path)
        stream=ledger.stream_id(self.session)
        body=ledger.pending_batch(stream)
        self.assertEqual(body["schema_version"],"1.1")
        self.assertEqual(body["records"][1]["attribution"]["account_key"],self.tracker.snapshot()["accounts"][0])
        before=body["records"][1]
        self.now[0]+=100
        self.auth("user-b","workspace-b")
        self.tracker.refresh()
        self.assertEqual(ledger.pending_batch(stream)["records"][1],before)
        self.assertNotIn("never-upload",json.dumps(body))

    def test_shared_program_has_different_per_user_state_roots(self):
        root=Path(self.temp.name)/"program"
        with patch.dict(os.environ,{"LOCALAPPDATA":str(Path(self.temp.name)/"user-a")}): one=user_root(root)
        with patch.dict(os.environ,{"LOCALAPPDATA":str(Path(self.temp.name)/"user-b")}): two=user_root(root)
        self.assertNotEqual(one,two)
        self.assertEqual(one.name,two.name)

    @unittest.skipUnless(os.name=="nt","Windows DPAPI")
    def test_import_encrypts_for_current_user_and_refuses_replacement(self):
        path=Path(self.temp.name)/"credentials"
        config=dict(base_url="https://example.test",tenant_id=TENANT,device_id=str(uuid.uuid4()),identity_key=KEY,read_token="r"*40,write_token="w"*40)
        protect(config,path)
        self.assertEqual(load_credentials(path),config)
        self.assertNotIn(config["read_token"],path.read_text())
        with self.assertRaises(FileExistsError): protect(config,path)


if __name__=="__main__": unittest.main()
