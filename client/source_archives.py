"""Verify retired JSONL sources against compressed copies, without adding usage."""
import hashlib
import json
from pathlib import Path

from client.source import MAX_FILE, read_source_data

try:
    import zstandard
except ImportError:
    zstandard = None


def verify_archive(ledger, source, archive):
    if zstandard is None:
        return dict(verified=False, message="缺少 zstandard 解码组件，尚不能核实压缩日志。")
    try:
        before = archive.stat()
        if before.st_size > MAX_FILE:
            raise ValueError("压缩文件超过核实大小限制。")
        with archive.open("rb") as handle:
            compressed = handle.read(MAX_FILE + 1)
        if len(compressed) > MAX_FILE:
            raise ValueError("压缩文件超过核实大小限制。")
        size = zstandard.frame_content_size(compressed)
        if size not in {zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR} and size > MAX_FILE:
            raise ValueError("解压后日志超过 128 MiB 核实限制。")
        data = zstandard.ZstdDecompressor().decompress(compressed, max_output_size=MAX_FILE, allow_extra_data=False)
        if len(data) > MAX_FILE:
            raise ValueError("解压后日志超过 128 MiB 核实限制。")
        after = archive.stat()
        if (before.st_mtime_ns, before.st_size, before.st_ctime_ns) != (after.st_mtime_ns, after.st_size, after.st_ctime_ns):
            raise ValueError("压缩文件在核实期间变化，请稍后重试。")
        scan = read_source_data(data)
        stream = ledger.db.execute("SELECT * FROM streams WHERE id=?", (source["stream_id"],)).fetchone()
        if not scan.session or scan.session != source["session"] or ledger.stream_id(scan.session) != source["stream_id"]:
            raise ValueError("压缩日志会话标识与原来源不一致。")
        if scan.warnings & {"parse_failed", "unknown_identity"}:
            raise ValueError("压缩日志含未支持格式或解析异常，不能确认完整采集。")
        tail = hashlib.sha256(data[max(0, source["offset"]-256):source["offset"]]).hexdigest()
        if (len(data) != source["size"] or source["offset"] != len(data) or scan.offset != len(data)
                or scan.head != source["head"] or not source["tail"] or tail != source["tail"]
                or scan.ordinal != source["ordinal"]):
            raise ValueError("压缩日志与最后采集位置不一致，可能仍有未采集或不完整记录。")
        if not stream and not scan.points and scan.ordinal == -1:
            return dict(verified=True, no_usage=True, message="该历史日志没有可采集用量检查点，未建立用量流；不计入统计，原覆盖限制仍保留。",
                        sha256=hashlib.sha256(data).hexdigest(), checkpoints=0)
        if not stream:
            raise ValueError("该来源未建立采集基线，压缩内容不能当作已同步历史。")
        if scan.unsupported or scan.ordinal != stream["anchor_ordinal"] + stream["last_seq"]:
            raise ValueError("来源格式不支持或仍有未采集检查点，不能确认完整采集。")
        points = {point.ordinal: point for point in scan.points}
        anchor = points.get(stream["anchor_ordinal"])
        if not anchor or ledger.anchor_id(stream["id"], anchor) != stream["anchor"]:
            raise ValueError("压缩日志不能复现原基线。")
        events = ledger.db.execute("SELECT seq,wire FROM events WHERE stream_id=? ORDER BY seq", (stream["id"],)).fetchall()
        if len(events) != stream["last_seq"] + 1:
            raise ValueError("本地已采集记录不连续，不能确认同步范围。")
        fields = ("event_id", "source_seq", "kind", "event_time", "model", "counters", "baseline_reason")
        for seq, event in enumerate(events):
            point = points.get(stream["anchor_ordinal"] + seq)
            if event["seq"] != seq or point is None:
                raise ValueError("压缩日志缺少已有检查点。")
            expected, stored = ledger.record(stream["id"], point, seq, seq == 0), json.loads(event["wire"])
            if any(expected.get(key) != stored.get(key) for key in fields):
                raise ValueError("压缩日志与已保存检查点冲突，禁止自动覆盖。")
        return dict(verified=True, message="压缩日志已复现原基线、全部已采集检查点及文件末尾。",
                    sha256=hashlib.sha256(data).hexdigest(), checkpoints=len(events), last_time=scan.points[-1].time)
    except ValueError as error:
        return dict(verified=False, message=str(error))
    except (OSError, zstandard.ZstdError):
        return dict(verified=False, message="压缩文件不可读、损坏、未写完或格式暂不支持，请恢复原日志后重新核实。")


def source_health(ledger, compressed_paths=(), refresh=False):
    sources = ledger.source_rows()
    available = {row["stream_id"] for row in sources if row["stream_id"] and Path(row["path"]).is_file()}
    archives, missing, known = [], [], {str(Path(row["path"] + ".zst")) for row in sources}
    for row in sources:
        path = Path(row["path"])
        if path.is_file():
            continue
        archive = Path(str(path) + ".zst")
        if not archive.is_file():
            missing.append(dict(path=str(path), stream_id=row["stream_id"]))
            continue
        stream = ledger.db.execute("SELECT * FROM streams WHERE id=?", (row["stream_id"],)).fetchone()
        try:
            stat = archive.stat()
        except OSError:
            missing.append(dict(path=str(path), stream_id=row["stream_id"]))
            continue
        events = [tuple(event) for event in ledger.db.execute("SELECT seq,digest FROM events WHERE stream_id=? ORDER BY seq", (row["stream_id"],))]
        stream_identity = {key: stream[key] for key in ("id", "anchor", "anchor_ordinal", "last_seq", "baseline")} if stream else None
        fingerprint = hashlib.sha256(json.dumps(["archive-verifier-v1", dict(row), stream_identity, events,
            stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, getattr(zstandard, "__version__", None)], sort_keys=True).encode()).hexdigest()
        cached = ledger.db.execute("SELECT fingerprint,result FROM archive_checks WHERE path=?", (str(archive),)).fetchone()
        if not refresh and cached and cached["fingerprint"] == fingerprint:
            proof = json.loads(cached["result"])
        else:
            proof = verify_archive(ledger, row, archive)
            with ledger.db:
                ledger.db.execute("INSERT OR REPLACE INTO archive_checks VALUES (?,?,?)", (str(archive), fingerprint, json.dumps(proof)))
        pending = ledger.db.execute("SELECT count(*) FROM events WHERE stream_id=? AND acked=0", (row["stream_id"],)).fetchone()[0]
        status = "unverified"
        if proof.get("no_usage"):
            available.add(row["stream_id"])
            status = "no_usage"
        elif proof["verified"] and stream and not stream["paused"]:
            available.add(row["stream_id"])
            status = "synced" if not pending and stream["mode"] == "ready" else "pending"
        message = proof["message"]
        if status == "synced":
            message = "历史来源已压缩，已采集记录全部获服务器确认；缓存核实结果，不再反复解压。"
        elif status == "pending":
            message = "历史来源已压缩且采集范围已核实；已保存记录继续上传，等待服务器确认。"
        elif proof["verified"] and status != "no_usage":
            message = "压缩内容已匹配，但此来源仍有暂停原因，请核实后处理。"
        archives.append(dict(path=str(archive), original_path=str(path), stream_id=row["stream_id"],
                             status=status, pending=pending, message=message, proof=proof))
    missing = [item for item in missing if not item["stream_id"] or item["stream_id"] not in available]
    # Excluded conflict files remain excluded even after compression.
    known.update(row[0] + ".zst" for row in ledger.db.execute("SELECT path FROM source_exclusions"))
    unregistered = [dict(path=str(path), message="未登记的压缩日志，历史用量尚未导入；不影响其他来源继续采集。")
                    for path in sorted(set(map(Path, compressed_paths))) if str(path) not in known]
    from client.source_alerts import reconcile
    return reconcile(ledger, dict(archives=archives, missing=missing, unregistered_archives=unregistered,
                synced_archives=sum(item["status"] == "synced" for item in archives),
                unverified_archives=sum(item["status"] == "unverified" for item in archives)))
