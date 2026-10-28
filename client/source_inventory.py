"""Incremental directory inventory. Unchanged logs are never re-read each tick."""
from datetime import datetime
import os
from pathlib import Path

from server.protocol import UTC, iso


def signature(value):
    return (value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_dev, value.st_ino)


def source_kind(path):
    name = str(path)
    return "jsonl" if name.endswith(".jsonl") else "zst" if name.endswith(".jsonl.zst") else None


class SourceInventory:
    def __init__(self, homes, ledger):
        self.roots = [home/name for home in map(Path,homes) for name in ("sessions","archived_sessions")]
        self.ledger = ledger
        self.files, self.directories, self.dirty = {}, {}, set()
        self.initialized = False
        self.censuses = self.directory_reads = self.content_candidates = self.metadata_checks = 0
        self.errors = False
        self.retry_directories=set()
        self.confirm_directories=set()
        self.seeded=False
        with ledger.db:
            ledger.db.execute("INSERT OR REPLACE INTO meta VALUES('source_tracking','existing')")

    def _discover(self, folder, seen, confirm=True):
        self.retry_directories.discard(folder)
        try:
            stat = folder.stat()
            with os.scandir(folder) as entries:
                self.directory_reads += 1
                if confirm:self.confirm_directories.add(folder)
                self.directories[folder] = (stat.st_mtime_ns,stat.st_ctime_ns)
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_symlink():continue
                    if entry.is_dir(follow_symlinks=False):
                        if path not in self.directories:self._discover(path,seen)
                    elif entry.is_file(follow_symlinks=False) and source_kind(path):
                        # DirEntry.stat can report st_ino=0 on Windows while Path.stat
                        # returns the actual file ID. Use one API for stable comparisons.
                        seen[path] = signature(path.stat())
        except FileNotFoundError:
            self.directories.pop(folder,None)
        except OSError:
            self.errors = True
            self.retry_directories.add(folder)

    def _record(self, kind, path, related, message):
        self.ledger.db.execute("INSERT INTO source_changes(observed_at,kind,path,related_path,message) VALUES(?,?,?,?,?)",
                              (iso(datetime.now(UTC)),kind,str(path),str(related) if related else None,message))

    def poll(self):
        self.errors=False;self.directory_reads=self.metadata_checks=0
        previous=dict(self.files)
        seen={}
        confirm=set(self.confirm_directories);self.confirm_directories.clear()
        if not self.initialized:
            self.censuses+=1
            for root in self.roots:self._discover(root,seen)
        else:
            # Read directory entries only where their metadata changed. A new subtree
            # is enumerated once; other existing subtrees are left alone.
            for root in self.roots:
                if root not in self.directories:self._discover(root,seen)
            for folder in list(self.retry_directories):self._discover(folder,seen)
            for folder in list(self.directories):
                try:
                    stat=folder.stat();current=(stat.st_mtime_ns,stat.st_ctime_ns)
                    if current!=self.directories[folder]:self._discover(folder,seen)
                    elif folder in confirm:self._discover(folder,seen,confirm=False)
                except FileNotFoundError:self.directories.pop(folder,None)
                except OSError:self.errors=True
            for path,old in previous.items():
                try:
                    self.metadata_checks+=1
                    seen[path]=signature(path.stat())
                except FileNotFoundError:pass
                except OSError:
                    seen[path]=old;self.errors=True
        if self.errors:
            # An inaccessible directory is not evidence that its contents were deleted.
            for path,old in previous.items():seen.setdefault(path,old)
        removed=set(previous)-set(seen)
        added=set(seen)-set(previous)
        renamed={}
        for old in removed:
            matches=[new for new in added if seen[new][3:]==previous[old][3:] and seen[new][4]]
            if len(matches)==1:renamed[old]=matches[0]
        now=iso(datetime.now(UTC))
        with self.ledger.db:
            for path in removed:
                related=renamed.get(path)
                if related:kind,message='renamed_observed','观察到同一文件标识出现在新路径；无法识别执行者。'
                elif Path(str(path)+'.zst') in seen:kind,related,message='compressed_observed',Path(str(path)+'.zst'),'原路径消失，同时发现同名压缩文件；内容另行核实。'
                else:kind,message='removed_observed','观察到原路径已不存在；无法区分删除、移出范围或其他清理，执行者未知。'
                self._record(kind,path,related,message)
                self.ledger.db.execute("UPDATE source_presence SET present=0,observed_at=? WHERE path=?",(now,str(path)))
                self.dirty.discard(path)
            if not self.seeded and not self.errors:
                known={row[0] for row in self.ledger.db.execute('SELECT path FROM sources')}
                known.update(row[0] for row in self.ledger.db.execute('SELECT path FROM source_presence'))
                for name in known:
                    path=Path(name)
                    if path in seen:continue
                    prior=self.ledger.db.execute('SELECT present FROM source_presence WHERE path=?',(name,)).fetchone()
                    if prior is None or prior[0]:
                        related=Path(name+'.zst') if Path(name+'.zst') in seen else None
                        self._record('not_present_at_startup',path,related,'启动时原路径已不存在；移出活动追踪，历史记录保留，无法追溯此前消失原因。')
                    self.ledger.db.execute('INSERT OR REPLACE INTO source_presence VALUES(?,0,?,?)',(name,source_kind(path) or 'jsonl',now))
                self.seeded=True
            for path,stat in seen.items():
                if previous.get(path)!=stat:
                    self.ledger.db.execute('INSERT OR REPLACE INTO source_presence VALUES(?,1,?,?)',(str(path),source_kind(path),now))
                    if path in added and self.initialized:
                        self._record('added_observed',path,None,'观察到现存来源进入采集范围。')
                    if source_kind(path)=='jsonl':self.dirty.add(path)
            self.ledger.db.execute('DELETE FROM source_changes WHERE id < coalesce((SELECT id FROM source_changes ORDER BY id DESC LIMIT 1 OFFSET 4999),-1)')
        self.files=seen;self.initialized=True
        self.content_candidates=len(self.dirty)
        return sorted(self.dirty)

    def acknowledge(self,path):
        self.dirty.discard(Path(path))

    def snapshot(self):
        return dict(mode='existing_incremental',initialized=self.initialized,initial_censuses=self.censuses,
                    existing_files=len(self.files),directory_reads=self.directory_reads,
                    metadata_checks=self.metadata_checks,content_candidates=self.content_candidates,
                    pending_files=len(self.dirty),tracking_error=self.errors)

    def compressed(self):return [path for path in self.files if source_kind(path)=='zst']

    def roots_exist(self):return any(root in self.directories for root in self.roots)
