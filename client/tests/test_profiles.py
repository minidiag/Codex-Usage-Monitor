import base64
from datetime import datetime
import gzip
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from client.agent import Agent
from client.identity import pseudonym
from client.profiles import DesktopCache,ProfilesMonitor,fetch_avatar,avatar_type
from client.tests.test_collector import Fixture,FakeApi,KEY,TENANT
from client.view_policy import window_filters

PNG=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=")
ACCOUNT=pseudonym(KEY,"codex-account-v1","test-user","test-workspace")
SUBJECT=dict(principal_key="1"*64,machine_key="2"*64)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.home=self.root/"home";self.home.mkdir()
        claims={"name":"登录名称","https://api.openai.com/auth":dict(chatgpt_user_id="test-user",chatgpt_account_id="test-workspace")}
        part=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        (self.home/"auth.json").write_text(json.dumps(dict(tokens=dict(id_token="h."+part+".s",account_id="test-workspace",access_token="NEVER_STORE_TOKEN"))),encoding="utf-8")
        self.cache=self.root/"cache";self.cache.mkdir()

    def fixture_cache(self,user="test-user",picture="https://chatgpt.com/backend-api/estuary/avatar.png",image=PNG,oversize=False):
        profile=dict(id=user,name="桌面 App 账户名",picture=picture,email="NEVER_STORE_EMAIL",phone_number="NEVER_STORE_PHONE",private="NEVER_STORE_PRIVATE")
        if oversize:profile["padding"]="x"*(300*1024)
        body=gzip.compress(json.dumps(profile).encode())
        headers=b"HTTP/1.1 200 OK\x00content-encoding: gzip\x00"
        entries=[("https://chatgpt.com/backend-api/me",headers,body)]
        if image:entries.append((picture,b"HTTP/1.1 200 OK\x00content-type: image/png\x00",image))
        block=bytearray(8192+256*len(entries))
        for index,(url,head,data) in enumerate(entries):
            offset=8192+index*256;key=("_dk_https://chatgpt.com https://chatgpt.com "+url).encode()
            struct.pack_into("<i",block,offset+32,len(key));struct.pack_into("<Q",block,offset+24,index+100)
            addresses=[]
            for number,blob in enumerate((head,data),index*2+1):
                (self.cache/("f_%06x"%number)).write_bytes(blob);addresses.append(0x80000000|number)
            struct.pack_into("<4i",block,offset+40,len(head),len(data),0,0)
            struct.pack_into("<4I",block,offset+56,*addresses,0,0)
            block[offset+96:offset+96+len(key)]=key
        (self.cache/"data_1").write_bytes(block)

    def monitor(self,fetcher=lambda _:None):
        return ProfilesMonitor([self.home],KEY,self.root/"state",roots=[self.cache],fetcher=fetcher)

    def test_exact_desktop_name_and_avatar_are_matched_to_login(self):
        self.fixture_cache();monitor=self.monitor();monitor.refresh()
        profile=monitor.snapshot()[0]
        self.assertEqual(profile["account_key"],ACCOUNT)
        self.assertEqual(profile["display_name"],"桌面 App 账户名")
        self.assertEqual(Path(profile["avatar_path"]).read_bytes(),PNG)

    def test_cached_other_account_never_supplies_name_or_avatar(self):
        self.fixture_cache(user="different-user");monitor=self.monitor();monitor.refresh()
        profile=monitor.snapshot()[0]
        self.assertEqual(profile["display_name"],"登录名称")
        self.assertEqual(profile["avatar_path"],"")

    def test_only_local_presentation_fields_are_persisted(self):
        self.fixture_cache();monitor=self.monitor();monitor.refresh()
        encoded=monitor.path.read_text(encoding="utf-8")
        for secret in ("NEVER_STORE_TOKEN","NEVER_STORE_EMAIL","NEVER_STORE_PHONE","NEVER_STORE_PRIVATE","test-user","test-workspace","backend-api/estuary"):
            self.assertNotIn(secret,encoded)

    def test_missing_cache_uses_plain_name_and_initials_not_anonymous_key(self):
        monitor=self.monitor();monitor.refresh()
        profile=monitor.snapshot()[0]
        self.assertEqual(profile["display_name"],"登录名称")
        self.assertEqual(profile["avatar_source"],"initials")

    def test_oversize_compressed_profile_is_rejected(self):
        self.fixture_cache(oversize=True)
        cache=DesktopCache(self.cache)
        with self.assertRaises(ValueError):cache.profile(cache.entries()[0])

    def test_invalid_addresses_and_untrusted_image_destinations_fail_closed(self):
        cache=DesktopCache(self.cache)
        for address in (0,0x90000000,0xA0000000):
            with self.assertRaises((ValueError,OSError)):cache.blob(address,2*1024*1024)
        for url in ("file:///c:/secret","http://127.0.0.1/private","https://chatgpt.com.evil.test/image","https://user:secret@chatgpt.com/image"):
            self.assertIsNone(fetch_avatar(url))
        self.assertIsNone(avatar_type(b"<html>not an avatar</html>"))

    def test_avatar_change_replaces_image_and_old_cache_is_reused_offline(self):
        self.fixture_cache();monitor=self.monitor();monitor.refresh();old=monitor.snapshot()[0]["avatar_path"]
        self.fixture_cache(picture="https://chatgpt.com/backend-api/estuary/new.png")
        monitor.refresh();new=monitor.snapshot()[0]["avatar_path"]
        self.assertNotEqual(old,new)
        (self.cache/"data_1").unlink()
        restored=self.monitor();restored.refresh()
        self.assertEqual(restored.snapshot()[0]["avatar_path"],new)


class ViewPolicyTests(unittest.TestCase):
    def test_admin_window_cannot_select_other_windows_users_or_computers(self):
        value=window_filters(SUBJECT,dict(principal="9"*64,machine="8"*64,account=ACCOUNT),[])
        self.assertEqual(value,dict(principal=SUBJECT["principal_key"],account=ACCOUNT))

    def test_first_view_selects_current_login_but_explicit_all_is_preserved(self):
        self.assertEqual(window_filters(SUBJECT,{},[ACCOUNT])["account"],ACCOUNT)
        self.assertEqual(window_filters(SUBJECT,dict(account=""),[ACCOUNT]),dict(principal=SUBJECT["principal_key"]))
        self.assertEqual(window_filters(SUBJECT,dict(account="unassigned"),[ACCOUNT])["account"],"unassigned")
        self.assertEqual(window_filters(SUBJECT,{},["a"*64,"b"*64],"b"*64)["account"],"b"*64)

    def test_unknown_windows_principal_never_falls_back_to_tenant_summary(self):
        self.assertIsNone(window_filters(None,{},[ACCOUNT]))

    def test_plain_profiles_stay_local_and_summary_always_uses_running_principal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/"home/sessions").mkdir(parents=True)
            fixture=Fixture(root/"home/sessions/test.jsonl");fixture.point(0,100)
            state=root/"state";state.mkdir()
            (state/"control.json").write_text(json.dumps(dict(filters=dict(principal="9"*64,machine="8"*64,account=ACCOUNT))))
            captured=[]
            class Identity:
                subject=SUBJECT
                def snapshot(self):return dict(subject=SUBJECT,accounts=[ACCOUNT])
                def refresh(self):pass
                def claim(self,*_):return None
            class Profiles:
                def tick(self,*_):pass
                def snapshot(self):return [dict(account_key=ACCOUNT,display_name="PRIVATE_DISPLAY_NAME",avatar_path="PRIVATE_AVATAR_PATH")]
            class Quota:
                def tick(self):pass
                def snapshot(self):return {}
            class RecordingApi(FakeApi):
                def register_identity(self,value):captured.append(value)
                def send(self,value):captured.append(value);return super().send(value)
                def heartbeat(self,value):captured.append(value)
                def summary(self,filters,groups):
                    captured.append(filters);self.filters=filters
                    return dict(windows=[],quality="partial")
            api=RecordingApi()
            with patch("client.agent.IdentityTracker",return_value=Identity()),patch("client.agent.ProfilesMonitor",return_value=Profiles()),patch("client.agent.QuotaMonitor",return_value=Quota()):
                agent=Agent(state,dict(identity_key=KEY,tenant_id=TENANT),[root/"home"],api=api)
                try:
                    agent.step()
                    self.assertEqual(api.filters,dict(principal=SUBJECT["principal_key"],account=ACCOUNT))
                    self.assertNotIn("PRIVATE_DISPLAY_NAME",json.dumps(captured))
                    self.assertNotIn("PRIVATE_AVATAR_PATH",json.dumps(captured))
                    self.assertNotIn("local_profiles",json.dumps(captured))
                    saved=json.loads((state/"status.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved["local_profiles"][0]["display_name"],"PRIVATE_DISPLAY_NAME")
                    def claim(stream,previous,point):
                        if point.ordinal==3:return None
                        return dict(principal_key="9"*64 if point.ordinal==2 else SUBJECT["principal_key"],machine_key=SUBJECT["machine_key"],
                                    account_key=None if point.ordinal==4 else ACCOUNT,observed_from=previous["event_time"])
                    agent.ledger.attribution_provider=claim
                    for index in range(1,5):fixture.point(index,100*(index+1))
                    agent.ledger.scan_file(fixture.path)
                    agent.active_sessions.value=dict(sessions=[dict(id=fixture.session,tokens="huge-session-history")])
                    self.assertEqual(agent.active_view()["sessions"][0]["tokens"],"150")
                    agent.filters=dict(principal=SUBJECT["principal_key"])
                    self.assertEqual(agent.active_view()["sessions"][0]["tokens"],"300")
                    agent.filters=dict(principal=SUBJECT["principal_key"],account="unassigned")
                    self.assertEqual(agent.active_view()["sessions"][0]["tokens"],"150")
                finally:agent.ledger.close()


if __name__=="__main__":unittest.main()
