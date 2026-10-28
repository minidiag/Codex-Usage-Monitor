import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import zstandard

from client.core import Ledger
from client.source_inventory import SourceInventory
from client.source_review import SourceReview
from client.tests.test_collector import Fixture,FakeApi,KEY,TENANT


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.home=self.root/'home';self.sessions=self.home/'sessions';self.sessions.mkdir(parents=True)
        self.ledger=Ledger(self.root/'ledger.sqlite3',KEY,TENANT)
        self.inventory=SourceInventory([self.home],self.ledger)

    def tearDown(self):
        self.ledger.close();self.temp.cleanup()

    def file(self,name='run.jsonl'):
        source=Fixture(self.sessions/name);source.point(0,100);return source

    def collect(self):
        changes=self.inventory.poll()
        for path in changes:self.ledger.scan_file(path,force=True);self.inventory.acknowledge(path)
        return changes

    def test_startup_census_then_no_directory_enumeration_or_content_reads_when_idle(self):
        source=self.file();self.assertEqual(self.collect(),[source.path])
        self.assertFalse(self.collect())  # One follow-up confirms startup/rename races.
        with patch('client.source_inventory.os.scandir',side_effect=AssertionError('unexpected directory scan')):
            self.assertFalse(self.collect())
        self.assertEqual(self.inventory.snapshot()['initial_censuses'],1)
        self.assertEqual(self.inventory.directory_reads,0)
        self.assertEqual(self.inventory.content_candidates,0)

    def test_only_appended_log_is_read_and_sequence_remains_contiguous(self):
        one=self.file();self.file('other.jsonl');self.collect()
        one.point(1,200)
        self.assertEqual(self.collect(),[one.path])
        stream=self.ledger.stream_id(one.session)
        self.assertEqual([r[0] for r in self.ledger.db.execute('SELECT seq FROM events WHERE stream_id=? ORDER BY seq',(stream,))],[0,1])
        self.assertFalse(self.collect())

    def test_new_nested_directory_and_file_are_discovered_without_a_new_census(self):
        self.file();self.collect()
        nested=self.sessions/'2026'/'10'/'28';nested.mkdir(parents=True)
        source=Fixture(nested/'new.jsonl');source.point(0,100)
        self.assertEqual(self.collect(),[source.path])
        self.assertEqual(self.inventory.censuses,1)

    def test_same_timestamp_directory_change_is_caught_by_one_followup(self):
        self.file();self.collect();old=self.sessions.stat()
        folder=self.sessions/'same-tick';folder.mkdir()
        source=Fixture(folder/'new.jsonl');source.point(0,100)
        os.utime(self.sessions,ns=(old.st_atime_ns,old.st_mtime_ns))
        self.assertEqual(self.collect(),[source.path])

    def test_removed_path_is_retired_without_deleting_pending_or_paused_history(self):
        source=self.file();self.collect()
        stream=self.ledger.stream_id(source.session);self.ledger.pause(stream,'unknown_identity')
        source.path.unlink();self.collect()
        self.assertFalse(self.ledger.source_rows())
        self.assertFalse(self.ledger.paused_sources())
        self.assertEqual(self.ledger.pending_count(),1)
        self.assertEqual(self.ledger.db.execute('SELECT paused FROM streams').fetchone()[0],'unknown_identity')
        self.assertEqual(self.ledger.db.execute('SELECT kind FROM source_changes ORDER BY id DESC').fetchone()[0],'removed_observed')
        self.assertFalse(self.ledger.source_health()['active_alerts'])
        self.assertIn('retired_source_history',self.ledger.warnings())

    def test_preexisting_missing_paths_are_logged_once_and_never_stat_again(self):
        source=self.file();self.ledger.scan_file(source.path);source.path.unlink();self.collect()
        count=self.ledger.db.execute('SELECT count(*) FROM source_changes').fetchone()[0]
        original=Path.stat
        def checked(path,*args,**kwargs):
            if path==source.path:raise AssertionError('retired historical path was checked again')
            return original(path,*args,**kwargs)
        with patch.object(Path,'stat',checked):
            self.assertFalse(self.collect())
            self.ledger.source_health();self.ledger.warnings()
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM source_changes').fetchone()[0],count)

    def test_move_preserves_events_and_records_observed_file_identity(self):
        source=self.file();self.collect();source.point(1,200);self.collect()
        before=[tuple(r) for r in self.ledger.db.execute('SELECT * FROM events')]
        archive=self.home/'archived_sessions';archive.mkdir()
        destination=archive/source.path.name;source.path.rename(destination)
        self.assertEqual(self.collect(),[destination])
        self.assertEqual([tuple(r) for r in self.ledger.db.execute('SELECT * FROM events')],before)
        row=self.ledger.db.execute("SELECT kind,related_path FROM source_changes WHERE kind='renamed_observed'").fetchone()
        self.assertIsNotNone(row);self.assertEqual(row['related_path'],str(destination))

    def test_compression_is_observed_once_and_verified_copy_stays_current(self):
        source=self.file();self.collect()
        zipped=Path(str(source.path)+'.zst')
        zipped.write_bytes(zstandard.ZstdCompressor().compress(source.path.read_bytes()));source.path.unlink()
        self.assertFalse(self.collect())
        self.assertEqual(self.inventory.compressed(),[zipped])
        health=self.ledger.source_health(self.inventory.compressed())
        self.assertEqual(len(health['archives']),1);self.assertTrue(health['archives'][0]['proof']['verified'])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM source_changes WHERE kind='compressed_observed'").fetchone()[0],1)
        self.assertFalse(self.collect())

    def test_failed_collection_stays_dirty_until_acknowledged(self):
        source=self.file();self.assertEqual(self.inventory.poll(),[source.path])
        self.assertEqual(self.inventory.poll(),[source.path])
        self.ledger.scan_file(source.path,force=True);self.inventory.acknowledge(source.path)
        self.assertFalse(self.inventory.poll())

    def test_access_failure_is_not_recorded_as_removal_and_retries_failed_directory(self):
        source=self.file();self.collect();old=self.inventory.files[source.path]
        real_stat=Path.stat
        def denied(path,*args,**kwargs):
            if path==source.path:raise PermissionError('synthetic sharing failure')
            return real_stat(path,*args,**kwargs)
        with patch.object(Path,'stat',denied):self.inventory.poll()
        self.assertTrue(self.inventory.errors);self.assertEqual(self.inventory.files[source.path],old)
        self.assertEqual(self.ledger.db.execute('SELECT present FROM source_presence WHERE path=?',(str(source.path),)).fetchone()[0],1)
        self.assertFalse(self.inventory.poll());self.assertFalse(self.inventory.errors)

    def test_manual_review_uses_inventory_instead_of_recursive_history_scan(self):
        self.file();self.collect()
        review=SourceReview(self.root,self.ledger,FakeApi(),[self.home],self.inventory)
        with patch.object(Path,'rglob',side_effect=AssertionError('recursive rescan')):
            report=review.build_report(str(uuid.uuid4()))
        self.assertEqual(report['source_tracking']['initial_censuses'],1)
        self.assertIn('source_changes',report)

    def test_same_size_replacement_with_preserved_mtime_is_checked_without_rewriting_events(self):
        source=self.file();self.collect();source.point(1,200);self.collect()
        before=[tuple(row) for row in self.ledger.db.execute('SELECT * FROM events ORDER BY seq')]
        stat=source.path.stat()
        replacement=self.sessions/'replacement.tmp'
        replacement.write_bytes(source.path.read_bytes().replace(b'"input_tokens": 200',b'"input_tokens": 210').replace(b'"total_tokens": 300',b'"total_tokens": 310'))
        os.utime(replacement,ns=(stat.st_atime_ns,stat.st_mtime_ns));replacement.replace(source.path)
        self.assertEqual(self.collect(),[source.path])
        self.assertEqual([tuple(row) for row in self.ledger.db.execute('SELECT * FROM events ORDER BY seq')],before)
        self.assertEqual(self.ledger.db.execute('SELECT paused FROM streams').fetchone()[0],'unknown_identity')

    def test_closed_paused_alert_is_retired_with_gap_record_when_source_disappears(self):
        source=self.file();self.collect();self.ledger.pause(self.ledger.stream_id(source.session),'unknown_identity')
        review=SourceReview(self.root,self.ledger,FakeApi(),[self.home],self.inventory)
        report=review.build_report(str(uuid.uuid4()));alert=report['source_health']['active_alerts'][0]
        result=review.handle(dict(id=str(uuid.uuid4()),action='close_alert',revision=report['revision'],stream_id=alert['stream_id'],fingerprint=alert['fingerprint'],reason='接受当前缺口'))
        self.assertEqual(result['result']['code'],'alert_closed')
        source.path.unlink();self.collect();self.ledger.source_health()
        self.assertEqual(self.ledger.db.execute('SELECT action FROM source_alert_audit ORDER BY rowid DESC').fetchone()[0],'retired')


if __name__=='__main__':unittest.main()
