import hashlib
from datetime import datetime, timedelta

from server.protocol import UTC, iso


def make_batch(records=None, stream="a" * 64, anchor="c" * 64):
    return dict(schema_version="1.0", parser_version="test-1", batch_id="synthetic",
                stream_id=stream, epoch=0, anchor_id=anchor, records=records or [make_record(0), make_record(1)])


def make_record(seq, vector=None, when=None, reason="known_zero"):
    when = when or datetime.now(UTC) - timedelta(minutes=10-seq)
    vector = vector or ((0, 0, 0, 0) if not seq else (100*seq, 20*seq, 50*seq, 10*seq))
    result = dict(event_id=hashlib.sha256(str(seq).encode()).hexdigest(), source_seq=str(seq),
                  kind="baseline" if seq == 0 else "snapshot", event_time=iso(when), observed_at=iso(when),
                  model="synthetic-model", counters=dict(zip(("input", "cached_input", "output", "reasoning_output"), map(str, vector))))
    if seq == 0:
        result["baseline_reason"] = reason
    return result
