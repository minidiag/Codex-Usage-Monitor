"""Local-only active session view, following the original monitor's index + rollout approach."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import UUID

from server.protocol import MAX_INT, UTC, iso

START = {"task_started", "turn_started"}
END = {"task_complete", "task_completed", "task_aborted", "turn_aborted", "turn_complete", "turn_completed"}
RUNTIME_TYPES = START | END | {"turn_context", "thread_settings_applied"}
MAX_TAIL = 16*1024*1024


def safe_text(value, maximum):
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value.splitlines()[0] if ord(c) >= 32 and ord(c) != 127).strip()[:maximum] if value.splitlines() else ""


def session_id(value):
    try:
        return str(UUID(bytes=value)) if isinstance(value, bytes) and len(value) == 16 else str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def counter(value):
    if type(value) is int and 0 <= value <= MAX_INT:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", value) and int(value) <= MAX_INT:
        return value
    return None


def local_path(value):
    # SQLite may store Windows extended-length paths; compare the same path form.
    value = str(value)
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\"+value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value).resolve()


def read_runtime(path, now=None):
    """Reverse-read complete records, bounded like the original 16 MiB tail scan."""
    now = time.time() if now is None else now
    active, model, effort = None, "", ""
    needles = tuple(('"'+kind+'"').encode() for kind in RUNTIME_TYPES)
    stat = Path(path).stat()
    with open(path, "rb") as handle:
        position = stat.st_size
        handle.seek(max(0, position-1))
        dropping_partial = position > 0 and handle.read(1) != b"\n"
        scanned, suffix = 0, b""
        while position > 0 and scanned < MAX_TAIL:
            length = min(1024*1024, position, MAX_TAIL-scanned)
            position -= length
            handle.seek(position)
            block = handle.read(length)+suffix
            scanned += length
            lines = block.split(b"\n")
            has_separator = len(lines) > 1
            suffix = lines.pop(0) if position > 0 else b""
            if dropping_partial:
                if has_separator:
                    lines.pop()  # Retry an unfinished final record after its newline arrives.
                    dropping_partial = False
                else:
                    lines = []
            for line in reversed(lines):
                if not line or not any(needle in line for needle in needles):
                    continue
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    kind = record.get("type")
                    payload = record.get("payload") or {}
                    event = payload.get("type") if kind == "event_msg" else kind
                    if active is None:
                        if event in START:
                            active = True
                        elif event in END:
                            active = False
                    if not model and (kind == "turn_context" or event == "thread_settings_applied"):
                        model = safe_text(payload.get("model"), 128)
                        effort = safe_text(payload.get("effort") or payload.get("reasoning_effort"), 64)
                        if not effort:
                            settings = (payload.get("collaboration_mode") or {}).get("settings") or {}
                            effort = safe_text(settings.get("reasoning_effort"), 64)
                except (ValueError, TypeError, AttributeError):
                    continue
                if active is False or (active is True and model):
                    break
            if active is False or (active is True and model):
                break
    recent = -5 <= now-stat.st_mtime <= 600
    return dict(active=active is True or (active is None and recent), model=model, effort=effort,
                activity_source="task_event" if active is not None else "recent_log", log_mtime=stat.st_mtime)


class ActiveSessionReader:
    def __init__(self, homes):
        self.homes = list(map(Path, homes))
        self.runtime_cache = {}
        self.title_cache = {}
        self.value = dict(updated_at=None, available=False, error="initializing", sessions=[], candidate_limit=12)

    @staticmethod
    def find_database(home):
        paths = [p for p in home.glob("state_*.sqlite") if re.fullmatch(r"state_[0-9]+\.sqlite", p.name)]
        def changed(path):
            wal = Path(str(path)+"-wal")
            return max(path.stat().st_mtime_ns, wal.stat().st_mtime_ns if wal.exists() else 0)
        return max(paths, key=changed) if paths else None

    @staticmethod
    def candidates(database):
        connection = sqlite3.connect(database.as_uri()+"?mode=ro", uri=True, timeout=0.2)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            columns = {r[1] for r in connection.execute("PRAGMA table_info(threads)")}
            if not {"id", "rollout_path", "title", "archived", "tokens_used"} <= columns:
                raise ValueError("unsupported_index")
            optional = [name if name in columns else "NULL AS "+name for name in ("model", "reasoning_effort")]
            order = "updated_at_ms" if "updated_at_ms" in columns else ("updated_at" if "updated_at" in columns else "rowid")
            query = "SELECT id,rollout_path,title,tokens_used,"+",".join(optional)+" FROM threads WHERE archived=0 ORDER BY "+order+" DESC LIMIT 12"
            return [dict(row) for row in connection.execute(query)]
        finally:
            connection.close()

    def titles(self, home):
        path = home / "session_index.jsonl"
        if not path.exists():
            return {}
        stat = path.stat()
        stamp = stat.st_size, stat.st_mtime_ns
        old = self.title_cache.get(str(path))
        if old and old[0] == stamp:
            return old[1]
        result = {}
        if stat.st_size > 16*1024*1024:
            return result
        with path.open("rb") as handle:
            while True:
                line = handle.readline(1024*1024+1)
                if not line:
                    break
                if len(line) > 1024*1024:
                    raise ValueError("invalid_title_index")
                if not line.endswith(b"\n"):
                    break
                try:
                    record = json.loads(line)
                    identity = session_id(record.get("id"))
                    title = safe_text(record.get("thread_name"), 80)
                    if identity and title:
                        result[identity] = title
                except (ValueError, TypeError, AttributeError):
                    continue
        self.title_cache[str(path)] = stamp, result
        return result

    def snapshot(self):
        return dict(self.value)

    def refresh(self):
        sessions, errors, any_index, seen_paths = {}, set(), False, set()
        for raw_home in self.homes:
            try:
                home = local_path(raw_home)
                database = self.find_database(home)
                if database is None:
                    errors.add("index_unavailable")
                    continue
                rows = self.candidates(database)
                any_index = True
                try:
                    titles = self.titles(home)
                except (OSError, ValueError):
                    titles = {}
                for row in rows:
                    identity = session_id(row["id"])
                    if not identity or not isinstance(row["rollout_path"], str):
                        errors.add("session_unavailable")
                        continue
                    raw_path = row["rollout_path"]
                    if os.name == "nt" and raw_path.startswith("\\\\?\\"):
                        path = local_path(raw_path)
                    else:
                        path = Path(raw_path)
                    if not path.is_absolute():
                        path = home / path
                    path = local_path(path)
                    if not path.is_relative_to(home):
                        errors.add("session_unavailable")
                        continue
                    try:
                        stat = path.stat()
                        stamp = stat.st_size, stat.st_mtime_ns
                        seen_paths.add(str(path))
                        cached = self.runtime_cache.get(str(path))
                        # A recent-log fallback must age out even when the file doesn't change.
                        if cached and cached[0] == stamp and cached[1]["activity_source"] == "task_event":
                            runtime = cached[1]
                        else:
                            runtime = read_runtime(path)
                            self.runtime_cache[str(path)] = stamp, runtime
                    except (OSError, ValueError):
                        errors.add("session_unavailable")
                        continue
                    if not runtime["active"]:
                        continue
                    item = dict(id=identity, title=titles.get(identity) or safe_text(row["title"], 80) or "未命名会话",
                                model=runtime["model"] or safe_text(row["model"], 128) or "unknown",
                                effort=runtime["effort"] or safe_text(row["reasoning_effort"], 64),
                                tokens=counter(row["tokens_used"]), activity_source=runtime["activity_source"])
                    previous = sessions.get(identity)
                    if previous is None or stat.st_mtime > previous[0]:
                        sessions[identity] = stat.st_mtime, item
            except (sqlite3.Error, OSError, ValueError):
                errors.add("index_unavailable")
        self.runtime_cache = {k:v for k,v in self.runtime_cache.items() if k in seen_paths}
        if not any_index:
            self.value = dict(self.value, available=False, error="index_unavailable")
        else:
            ordered = [pair[1] for pair in sorted(sessions.values(), key=lambda pair:pair[0], reverse=True)]
            self.value = dict(updated_at=iso(datetime.now(UTC)), available=True,
                              error="some_sessions_unavailable" if errors else None,
                              sessions=ordered, candidate_limit=12)
        return self.snapshot()


if __name__ == "__main__":
    home = Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex")))
    result = ActiveSessionReader([home]).refresh()
    print(json.dumps(dict(available=result["available"], error=result["error"], count=len(result["sessions"]),
                          sessions=[{k:s[k] for k in ("model", "tokens", "activity_source")} for s in result["sessions"]])))
