"""Disposable PostgreSQL integration coverage for unrestricted many-to-many account use."""
import copy
from datetime import datetime, timedelta
import hashlib
import os
import secrets
import unittest
import uuid

from server.protocol import ApiError, UTC, batch, iso, token_hash
from server.tests import test_store
from server.tests.helpers import make_batch, make_record


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),"disposable PostgreSQL required")
class AccountTests(unittest.TestCase):
    setUpClass = classmethod(test_store.LedgerTests.setUpClass.__func__)
    setUp = test_store.LedgerTests.setUp
    tearDown = test_store.LedgerTests.tearDown

    def register(self, principal="1"*64, machine="9"*64, restricted=False):
        device, write, read = uuid.uuid4(), secrets.token_hex(32), secrets.token_hex(32)
        with self.store.connect() as db:
            db.execute("INSERT INTO devices(tenant_id,id) VALUES(%s,%s)",(self.tenant,device))
            db.execute("INSERT INTO credentials(token_hash,tenant_id,device_id,scope) VALUES(%s,%s,%s,'write')",(token_hash(write),self.tenant,device))
            db.execute("INSERT INTO credentials(token_hash,tenant_id,scope,reader_device_id) VALUES(%s,%s,'read',%s)",(token_hash(read),self.tenant,device if restricted else None))
        self.store.register_identity(write,dict(principal_key=principal,machine_key=machine))
        return write,read

    def usage(self, token, stream, amount, account, principal="1"*64, machine="9"*64, boundary=None):
        now = datetime.now(UTC)
        data = make_batch([make_record(0,when=now-timedelta(seconds=30)),make_record(1,(amount,0,0,0),when=now-timedelta(seconds=20))],stream=stream*64)
        for record in data["records"]:
            record["event_id"] = hashlib.sha256((stream+record["source_seq"]).encode()).hexdigest()
        data["schema_version"] = "1.1"
        data["records"][1]["attribution"] = dict(account_key=account,principal_key=principal,machine_key=machine,observed_from=iso(boundary or now-timedelta(seconds=40)))
        self.store.ingest(token,batch(data))
        return data

    def test_shared_account_and_multiple_accounts_per_windows_user(self):
        one,_ = self.register()
        two,_ = self.register("2"*64)
        three,_ = self.register("3"*64,"8"*64)
        self.usage(one,"a",100,"a"*64)
        self.usage(two,"b",200,"a"*64,"2"*64)
        self.usage(one,"c",50,"b"*64)
        self.usage(three,"d",80,"c"*64,"3"*64,"8"*64)
        for dimension,expected in (("account",{"a"*64:"300","b"*64:"50","c"*64:"80"}),
                                   ("principal",{"1"*64:"150","2"*64:"200","3"*64:"80"}),
                                   ("machine",{"9"*64:"350","8"*64:"80"})):
            view = self.store.summary_scoped(self.read,group_by=[dimension])["windows"][0]
            self.assertEqual(view["totals"]["total"],"430")
            self.assertEqual({g["keys"][dimension]:g["totals"]["total"] for g in view["groups"]},expected)
        view = self.store.summary_scoped(self.read,dict(account="a"*64,principal="2"*64))["windows"][0]
        self.assertEqual(view["totals"]["total"],"200")

    def test_copy_and_different_claim_never_reassign_or_double_count(self):
        first,_ = self.register()
        second,_ = self.register("2"*64)
        data = self.usage(first,"a",100,"a"*64)
        other = copy.deepcopy(data)
        other["records"][1]["attribution"].update(principal_key="2"*64,account_key="b"*64)
        self.assertEqual(self.store.ingest(second,batch(other))["duplicates"],2)
        rows = self.store.summary_scoped(self.read,group_by=["account","principal"])["windows"][0]
        self.assertEqual(rows["totals"]["total"],"100")
        self.assertEqual(rows["groups"][0]["keys"],dict(account="a"*64,principal="1"*64))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) n FROM source_observations WHERE tenant_id=%s",(self.tenant,)).fetchone()["n"],4)

    def test_unobserved_boundary_and_legacy_history_stay_unassigned(self):
        write,_ = self.register()
        self.usage(write,"a",100,"a"*64,boundary=datetime.now(UTC)-timedelta(seconds=25))
        view = self.store.summary_scoped(self.read,dict(account="unassigned"))["windows"][0]
        self.assertEqual(view["totals"]["total"],"100")
        self.assertEqual(self.store.summary_scoped(self.read,dict(account="a"*64))["windows"][0]["totals"]["total"],"0")

    def test_restricted_reader_cannot_bypass_via_legacy_summary(self):
        one,read = self.register(restricted=True)
        two,_ = self.register("2"*64)
        self.usage(one,"a",100,"a"*64)
        self.usage(two,"b",200,"a"*64,"2"*64)
        self.assertEqual(self.store.summary_scoped(read)["windows"][0]["totals"]["total"],"100")
        self.assertEqual(self.store.summary(read)["windows"][0]["totals"]["total"],"100")
        self.assertEqual(self.store.summary_scoped(read,dict(principal="2"*64))["windows"][0]["totals"]["total"],"0")
        self.assertEqual(len(self.store.identities(read)["subjects"]),1)

    def test_pinned_subject_rejects_impersonation_and_new_accounts_need_no_allowlist(self):
        write,_ = self.register()
        with self.assertRaises(ApiError):
            self.store.register_identity(write,dict(principal_key="2"*64,machine_key="9"*64))
        with self.assertRaises(ApiError):
            self.usage(write,"a",100,"a"*64,"2"*64)
        self.usage(write,"a",100,"f"*64)
        self.assertIn("f"*64,self.store.identities(self.read)["accounts"])

    def test_unknown_account_keeps_confirmed_windows_principal(self):
        write,_ = self.register()
        self.usage(write,"a",100,None)
        view = self.store.summary_scoped(self.read,dict(principal="1"*64,account="unassigned"))["windows"][0]
        self.assertEqual(view["totals"]["total"],"100")
