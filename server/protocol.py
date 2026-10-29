"""Strict wire validation and deterministic, transport-independent identities."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
MAX_INT = 2**63 - 1
HEX = re.compile(r"[0-9a-f]{64}\Z")
DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
COUNTERS = ("input", "cached_input", "output", "reasoning_output")
BASELINES = {"known_zero", "capture_start", "inherited_history"}
HEALTH_REASONS = {"scan_failed", "unsupported_usage", "unknown_identity", "clock_anomaly",
                  "queue_full", "paused", "source_missing", "parse_failed"}


class ApiError(Exception):
    def __init__(self, code, status=422, message=None, retryable=False):
        super().__init__(code)
        self.code, self.status = code, status
        self.message, self.retryable = message or code, retryable


def require(condition, code="invalid_fields"):
    if not condition:
        raise ApiError(code)


def fields(value, required, optional=()):
    require(isinstance(value, dict))
    require(set(required) <= value.keys() and value.keys() <= set(required) | set(optional))


def integer(value):
    require(isinstance(value, str) and DECIMAL.fullmatch(value) is not None, "invalid_integer")
    number = int(value)
    require(number <= MAX_INT, "integer_overflow")
    return number


def text(value, maximum):
    require(isinstance(value, str) and 1 <= len(value) <= maximum)
    require(all(ord(c) >= 32 and c != "\x7f" for c in value))
    return value


def hex_id(value):
    require(isinstance(value, str) and HEX.fullmatch(value) is not None, "invalid_identity")
    return value


def uuid_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ApiError("invalid_device_id") from None


def timestamp(value):
    require(isinstance(value, str) and len(value) <= 40, "invalid_timestamp")
    require(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", value) is not None,
            "invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, OverflowError):
        raise ApiError("invalid_timestamp") from None
    return parsed.replace(microsecond=parsed.microsecond // 1000 * 1000)


def iso(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ApiError("duplicate_json_key", 400)
        result[key] = value
    return result


def parse_json(body):
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ApiError("invalid_json", 400) from None


def batch(value):
    fields(value, ("schema_version", "parser_version", "batch_id", "stream_id", "epoch", "anchor_id", "records"))
    require(value["schema_version"] in ("1.0", "1.1"), "unsupported_schema")
    text(value["parser_version"], 64)
    text(value["batch_id"], 64)
    hex_id(value["stream_id"])
    hex_id(value["anchor_id"])
    require(type(value["epoch"]) is int and 0 <= value["epoch"] <= 2147483647, "invalid_epoch")
    require(isinstance(value["records"], list) and 1 <= len(value["records"]) <= 500, "invalid_batch_size")
    normalized = dict(value, records=[])
    for record in value["records"]:
        fields(record, ("event_id", "source_seq", "kind", "event_time", "observed_at", "model", "counters"),
               ("baseline_reason", "attribution") if value["schema_version"] == "1.1" else ("baseline_reason",))
        hex_id(record["event_id"])
        seq = integer(record["source_seq"])
        require((seq == 0 and record["kind"] == "baseline" and isinstance(record.get("baseline_reason"), str)
                 and record.get("baseline_reason") in BASELINES)
                or (seq > 0 and record["kind"] == "snapshot" and "baseline_reason" not in record), "invalid_baseline")
        text(record["model"], 128)
        fields(record["counters"], COUNTERS)
        vector = tuple(integer(record["counters"][k]) for k in COUNTERS)
        require(vector[1] <= vector[0] and vector[3] <= vector[2], "invalid_counter_subset")
        require(record.get("baseline_reason") != "known_zero" or not any(vector), "nonzero_zero_baseline")
        event_time, observed_at = timestamp(record["event_time"]), timestamp(record["observed_at"])
        if "attribution" in record:
            attribution(record["attribution"])
            require(record["kind"] == "snapshot", "invalid_attribution")
        semantic = ["1.0", value["stream_id"], value["epoch"], value["anchor_id"], str(seq),
                    record["kind"], iso(event_time), record["model"],
                    *(str(n) for n in vector), record.get("baseline_reason")]
        digest = hashlib.sha256(json.dumps(semantic, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        normalized["records"].append(dict(record, seq=seq, time=event_time, observed=observed_at,
                                          vector=vector, digest=digest))
    return normalized


def attribution(value):
    fields(value, ("account_key", "principal_key", "machine_key", "observed_from"))
    if value["account_key"] is not None:
        hex_id(value["account_key"])
    hex_id(value["principal_key"])
    hex_id(value["machine_key"])
    timestamp(value["observed_from"])
    return value


def subject(value):
    fields(value, ("principal_key", "machine_key"))
    return {key: hex_id(value[key]) for key in ("principal_key", "machine_key")}


def display_names(value):
    import unicodedata
    fields(value,('profiles',))
    require(isinstance(value['profiles'],list) and 1<=len(value['profiles'])<=32,'invalid_profile_batch')
    result=[]
    for row in value['profiles']:
        fields(row,('account_key','display_name'))
        account=hex_id(row['account_key']);name=row['display_name']
        require(isinstance(name,str) and 1<=len(name)<=120 and name==name.strip(),'invalid_display_name')
        require(not any(unicodedata.category(c) in ('Cc','Cs') or c in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069' for c in name),'invalid_display_name')
        result.append(dict(account_key=account,display_name=name))
    require(len({r['account_key'] for r in result})==len(result),'duplicate_account')
    return result


def quota_cycles(value):
    fields(value,("cycles",))
    require(isinstance(value["cycles"],list) and 1<=len(value["cycles"])<=8,"invalid_cycle_batch")
    rows=[]
    for raw in value["cycles"]:
        fields(raw,("account_key","bucket","duration_mins","resets_at","observed_at"))
        account=hex_id(raw["account_key"])
        require(isinstance(raw["bucket"],str) and raw["bucket"] in {"primary","secondary"},"invalid_cycle_bucket")
        duration=integer(raw["duration_mins"]);reset=integer(raw["resets_at"])
        require(1<=duration<=525600 and 0<reset<=253402300799,"invalid_cycle_bounds")
        end=datetime.fromtimestamp(reset,UTC)
        try:start=end-timedelta(minutes=duration)
        except OverflowError:raise ApiError("invalid_cycle_bounds") from None
        observed=timestamp(raw["observed_at"])
        require(start<=observed<end,"invalid_cycle_observation")
        identity=hashlib.sha256(json.dumps(["quota-cycle-v1",account,raw["bucket"],str(duration),str(reset)],separators=(",", ":")).encode()).hexdigest()
        rows.append(dict(account_key=account,bucket=raw["bucket"],duration_mins=duration,resets_at=end,starts_at=start,observed_at=observed,cycle_id=identity))
    require(len({r["cycle_id"] for r in rows})==len(rows),"duplicate_cycle")
    return sorted(rows,key=lambda r:(r["account_key"],r["bucket"],r["starts_at"]))


def quota_observations(value):
    fields(value,('cycles',))
    require(isinstance(value['cycles'],list) and 1<=len(value['cycles'])<=100)
    rows=[quota_cycles(dict(cycles=[row]))[0] for row in value['cycles']]
    identities={(r['account_key'],r['bucket'],r['observed_at']) for r in rows}
    require(len(identities)==len(rows),'duplicate_observation')
    return rows


def heartbeat(value):
    fields(value, ("queued_records", "oldest_queued_at", "last_scan_at", "quality", "quality_reasons"))
    queued = integer(value["queued_records"])
    require(value["quality"] in ("complete", "partial", "unknown"))
    reasons = value["quality_reasons"]
    require(isinstance(reasons, list) and len(reasons) <= 20
            and all(isinstance(x, str) and x in HEALTH_REASONS for x in reasons))
    require(value["quality"] != "complete" or not reasons, "inconsistent_health")
    result = dict(value, queued_records=queued)
    for name in ("oldest_queued_at", "last_scan_at"):
        result[name] = timestamp(value[name]) if value[name] is not None else None
    return result


def windows(now):
    now = now.astimezone(UTC)
    today = now.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
    return [("rolling_5h", now - timedelta(hours=5)), ("today", today.astimezone(UTC)),
            ("week", (today - timedelta(days=today.weekday())).astimezone(UTC)),
            ("month", today.replace(day=1).astimezone(UTC))]


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()
