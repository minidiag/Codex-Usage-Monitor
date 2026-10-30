"""Read only metadata and cumulative usage; never persist conversation content."""
from dataclasses import dataclass, field
import hashlib
import io
import json
from pathlib import Path
from uuid import UUID

from server.protocol import ApiError, COUNTERS, MAX_INT, iso, timestamp

MAX_LINE = 8 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024
SOURCE_COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


@dataclass
class Point:
    ordinal: int
    time: str
    model: str
    counters: dict

    @property
    def signature(self):
        value = [self.time, self.model, *(self.counters[k] for k in COUNTERS)]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass
class Scan:
    session: str | None = None
    started_at: str | None = None
    offset: int = 0
    ordinal: int = -1
    model: str = "unknown"
    points: list = field(default_factory=list)
    warnings: set = field(default_factory=set)
    unsupported: bool = False
    head: str = ""


def start_point(scan, model):
    """Reproducible zero checkpoint for a proven newly created, non-inherited session."""
    if scan.started_at and not scan.unsupported:
        return Point(-1, scan.started_at, model, dict.fromkeys(COUNTERS, "0"))
    return None


def anchored_points(scan, stream):
    points = list(scan.points)
    if stream["anchor_ordinal"] == -1:
        baseline = json.loads(stream["baseline"])
        zero = start_point(scan, baseline["model"])
        if zero:
            points.insert(0, zero)
    return points


def read_source(path, offset=0, ordinal=-1, model="unknown", session=None, old_warnings=()):
    with open(path, "rb") as handle:
        return _read_source(handle, Path(path).stat().st_size, offset, ordinal, model, session, old_warnings)


def read_source_data(data):
    """Inspect bounded decompressed bytes in memory; never write conversation text to disk."""
    return _read_source(io.BytesIO(data), len(data))


def _read_source(handle, size, offset=0, ordinal=-1, model="unknown", session=None, old_warnings=()):
    result = Scan(session=session, offset=offset, ordinal=ordinal, model=model, warnings=set(old_warnings))
    if size > MAX_FILE:
        result.warnings.add("unsupported_usage")
        result.unsupported = True
        return result
    first_line = handle.readline(MAX_LINE + 1)
    result.head = hashlib.sha256(first_line).hexdigest()
    # Also recover creation time during incremental reads, without persisting content.
    try:
        first = json.loads(first_line)
        if first.get("type") == "session_meta":
            result.started_at = iso(timestamp(first["payload"].get("timestamp") or first.get("timestamp")))
    except (ApiError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
        pass  # Older logs without creation metadata retain the historical baseline rule.
    if offset:
        handle.seek(offset)
    else:
        handle.seek(0)
    while True:
        start = handle.tell()
        line = handle.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            result.warnings.add("parse_failed")
            while line and not line.endswith(b"\n"):
                line = handle.readline(MAX_LINE + 1)
            if not line:
                break
            result.offset = handle.tell()
            continue
        if not line.endswith(b"\n"):
            # A writer can be in the middle of one JSON line. Re-read it next time.
            result.offset = start
            break
        result.offset = handle.tell()
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError()
            kind = record.get("type")
            payload = record.get("payload") or {}
            if kind == "session_meta":
                raw_id = payload.get("id") or payload.get("session_id")
                result.session = str(UUID(raw_id))
                source = payload.get("source")
                thread_source = payload.get("thread_source")
                inherited = any(payload.get(k) for k in ("forked_from_id", "parent_thread_id", "parent_session_id"))
                if (not isinstance(source, str) or source not in {"vscode", "cli", "exec", "app_server", "appServer"}
                        or inherited or isinstance(thread_source, dict)
                        or (isinstance(thread_source, str) and any(x in thread_source.lower() for x in ("subagent", "spawn", "fork")))):
                    result.unsupported = True
                    result.warnings.add("unsupported_usage")
            elif kind == "turn_context":
                candidate = payload.get("model")
                if isinstance(candidate, str) and 1 <= len(candidate) <= 128 and all(ord(c) >= 32 for c in candidate):
                    result.model = candidate
            elif kind in {"compacted", "context_compacted"}:
                # Supplemental remote-compaction accounting needs a separate adapter.
                result.warnings.add("unsupported_usage")
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                if info is None:
                    continue  # A rate-limit-only update has no usage checkpoint.
                vector = info.get("total_token_usage")
                if not isinstance(vector, dict):
                    raise ValueError()
                values = tuple(vector.get(k) for k in SOURCE_COUNTERS)
                if any(type(v) is not int or not 0 <= v <= MAX_INT for v in values):
                    raise ValueError()
                if values[1] > values[0] or values[3] > values[2]:
                    raise ValueError()
                if "total_tokens" in vector and vector["total_tokens"] != values[0] + values[2]:
                    raise ValueError()
                result.ordinal += 1
                result.points.append(Point(result.ordinal, iso(timestamp(record.get("timestamp"))), result.model,
                                           dict(zip(COUNTERS, map(str, values)))))
                if result.model == "unknown":
                    result.warnings.add("unsupported_usage")
        except (ApiError, ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError):
            result.warnings.add("parse_failed")
    if result.session is None:
        result.warnings.add("unknown_identity")
    return result
