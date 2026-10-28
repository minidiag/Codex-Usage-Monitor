"""Local source inspection and audited recovery. Reports never enter telemetry."""
import argparse
from datetime import datetime
import hashlib
import getpass
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from client.core import compact
from client.paths import state_path
from client.source import MAX_FILE, read_source
from client.transport import ApiFailure
from server.protocol import COUNTERS, UTC, iso


MESSAGES = {
    "inspected": "核实完成。只有能够验证基线和已有记录的来源才可恢复。",
    "resumed": "已恢复选定来源；其余已登记冲突文件仅在采集中排除，原文件和历史记录均保留。",
    "auto_complete": "自动核实完成；仅恢复已证明为同序列完整副本或前缀副本的来源。",
    "stale_review": "核实报告或文件已经变化，请重新核实后再选择。",
    "not_eligible": "该来源尚不能安全恢复，请查看文件和服务器核实结果。",
    "not_registered": "来源未登记在当前采集器，拒绝恢复。",
    "invalid_request": "核实请求无效，请重新打开核实窗口。",
    "review_failed": "核实未完成，请重新核实；未改变采集账本。",
    "alert_closed": "已关闭此来源当前问题的告警，保留覆盖缺口、历史数据和待传记录；来源变化后会重新告警。",
    "alert_reopened": "已重新打开此来源告警；采集和上传仍遵守原有校验规则。",
    "reason_required": "请填写 1 至 200 字的关闭原因。",
}


class ReviewError(Exception):
    pass


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_id(path):
    return hashlib.sha256(str(Path(path).resolve()).encode("utf-8")).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class SourceReview:
    def __init__(self, state, ledger, api, homes=(), inventory=None):
        self.state, self.ledger, self.api = Path(state), ledger, api
        self.homes = list(map(Path, homes))
        self.inventory=inventory
        self.request_path = self.state / "source-review-request.json"
        self.report_path = self.state / "source-review.json"

    def _server(self, stream):
        try:
            remote = self.api.state(stream["id"])
        except ApiFailure as error:
            if error.status == 404:
                return dict(status="absent", message="服务器尚无该来源，可沿用本地原基线。", durable=-1)
            return dict(status="unavailable", message="服务器核实失败，稍后重试：" + error.code, durable=-1)
        baseline = json.loads(stream["baseline"])
        if not isinstance(remote.get("baseline_event"), dict):
            return dict(status="unavailable", message="服务器返回的基线结构无效，未允许恢复。", durable=-1)
        fields = ("event_id", "source_seq", "kind", "event_time", "model", "counters", "baseline_reason")
        same = remote.get("anchor_id") == stream["anchor"] and all(
            remote.get("baseline_event", {}).get(key) == baseline.get(key) for key in fields)
        try:
            durable = int(remote.get("durable_through", -1))
        except (ValueError, TypeError):
            same, durable = False, -1
        return dict(status="matches" if same else "different", durable=durable,
                    message="服务器基线与本地原基线一致。" if same else "服务器基线不同，需要找回匹配日志；本入口不会重写基线。")

    def inspect_stream(self, stream):
        baseline = json.loads(stream["baseline"])
        events = self.ledger.db.execute("SELECT * FROM events WHERE stream_id=? ORDER BY seq", (stream["id"],)).fetchall()
        server = self._server(stream)
        report = dict(stream_id=stream["id"], reason=stream["paused"], baseline_time=baseline["event_time"],
                      baseline_model=baseline["model"], pending=sum(not row["acked"] for row in events),
                      retained_records=len(events), server=server, sources=[], auto_source_id=None)
        scans = {}
        for row in self.ledger.db.execute("SELECT * FROM sources WHERE stream_id=? ORDER BY path", (stream["id"],)):
            if self.ledger.existing_only() and not self.ledger.db.execute("SELECT 1 FROM source_presence WHERE present=1 AND path IN (?,?)",(row['path'],row['path']+'.zst')).fetchone():
                continue
            path = Path(row["path"])
            item = dict(source_id=source_id(path), path=str(path), eligible=False, exists=path.exists(),
                        excluded=bool(self.ledger.db.execute("SELECT 1 FROM source_exclusions WHERE path=? AND stream_id=?", (str(path), stream["id"])).fetchone()),
                        points=0, anchor_matches=False, records_match=False, issues=[])
            report["sources"].append(item)
            if not item["exists"]:
                item["issues"].append("原文件已压缩，请查看下方压缩来源核实结果。" if Path(str(path)+".zst").is_file()
                                      else "文件不存在，需要找回日志或选择已迁移的完整副本。")
                continue
            try:
                before = path.stat()
                if before.st_size > MAX_FILE:
                    item["issues"].append("日志超过当前解析器支持的大小，需要适配后再核实。")
                    continue
                scan = read_source(path)
                fingerprint = file_hash(path)
                after = path.stat()
            except (OSError, ValueError):
                item["issues"].append("文件暂不可读。")
                continue
            item.update(fingerprint=fingerprint, size=after.st_size, points=len(scan.points), warnings=sorted(scan.warnings),
                        first_time=scan.points[0].time if scan.points else None, last_time=scan.points[-1].time if scan.points else None)
            scans[item["source_id"]] = scan
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                item["issues"].append("文件在核实期间变化，请重新核实。")
            if scan.unsupported or scan.warnings & {"parse_failed", "unknown_identity"}:
                item["issues"].append("日志格式不支持或含解析异常。")
            if not scan.session or self.ledger.stream_id(scan.session) != stream["id"]:
                item["issues"].append("日志会话标识与登记来源不一致。")
            anchor = next((point for point in scan.points if point.ordinal == stream["anchor_ordinal"]), None)
            item["anchor_matches"] = bool(anchor and self.ledger.anchor_id(stream["id"], anchor) == stream["anchor"])
            if not item["anchor_matches"]:
                item["issues"].append("缺少原基线，或同位置检查点内容与原基线不同。")
            points = {point.ordinal: point for point in scan.points}
            item["records_match"] = True
            for event in events:
                point = points.get(stream["anchor_ordinal"] + event["seq"])
                if point is None:
                    item["records_match"] = False
                    break
                expected = self.ledger.record(stream["id"], point, event["seq"], event["seq"] == 0)
                stored = json.loads(event["wire"])
                if any(expected.get(key) != stored.get(key) for key in ("event_id", "source_seq", "kind", "event_time", "model", "counters", "baseline_reason")):
                    item["records_match"] = False
                    break
            if not item["records_match"]:
                item["issues"].append("不能逐条复现已保存的检查点；禁止覆盖历史记录。")
            previous = anchor
            for point in scan.points:
                if previous is None or point.ordinal <= stream["anchor_ordinal"]:
                    continue
                delta = [int(point.counters[key]) - int(previous.counters[key]) for key in COUNTERS]
                if point.time < previous.time or any(value < 0 for value in delta) or delta[1] > delta[0] or delta[3] > delta[2]:
                    item["issues"].append("基线后存在时间倒退或累计用量异常。")
                    break
                previous = point
            if server["status"] not in {"absent", "matches"}:
                item["issues"].append(server["message"])
            elif server["status"] == "matches" and scan.ordinal - stream["anchor_ordinal"] < server["durable"]:
                item["issues"].append("该文件未覆盖服务器已保存的检查点，需找回较完整日志。")
            if stream["paused"] not in {"unknown_identity", "source_missing"}:
                item["issues"].append("该暂停原因需要修复解析器或异常数据，不能通过选择文件绕过。")
            item["eligible"] = not item["issues"]
        eligible = [item for item in report["sources"] if item["eligible"]]
        canonical = max(eligible, key=lambda item: item["points"], default=None)
        if canonical:
            base = scans[canonical["source_id"]]
            automatic = len(report["sources"]) > 1
            for item in report["sources"]:
                scan = scans.get(item["source_id"])
                if scan is None:
                    automatic = False
                    continue
                signatures = {point.signature for point in base.points}
                item["common_points"] = sum(point.signature in signatures for point in scan.points)
                item["aligned_prefix"] = bool(scan.session == base.session and len(scan.points) <= len(base.points)
                    and all(a.signature == b.signature for a, b in zip(scan.points, base.points)))
                if not item["aligned_prefix"] or scan.unsupported or scan.warnings:
                    automatic = False
            if automatic:
                report["auto_source_id"] = canonical["source_id"]
        return report, scans

    def build_report(self, request_id, code="inspected", details=None):
        streams = []
        for stream in self.ledger.paused_sources():
            streams.append(self.inspect_stream(stream)[0])
        audit = [dict(id=row["id"], created_at=row["created_at"], **json.loads(row["details"]))
                 for row in self.ledger.db.execute("SELECT * FROM source_review_audit ORDER BY rowid DESC LIMIT 20")]
        compressed = self.inventory.compressed() if self.inventory else [path for home in self.homes for name in ("sessions", "archived_sessions")
                      for path in (home/name).rglob("*.jsonl.zst")]
        health = self.ledger.source_health(compressed, refresh=code=='inspected')
        alert_audit = [dict(id=row["id"], action=row["action"], created_at=row["created_at"],
                            stream_id=row["stream_id"], closure_id=row["closure_id"], **json.loads(row["details"]))
                       for row in self.ledger.db.execute("SELECT * FROM source_alert_audit ORDER BY rowid DESC LIMIT 30")]
        report = dict(version=1, revision=str(uuid.uuid4()), generated_at=iso(datetime.now(UTC)), request_id=request_id,
                      result=dict(code=code, message=MESSAGES.get(code, MESSAGES["review_failed"]), details=details or []), streams=streams, audit=audit, source_health=health, alert_audit=alert_audit)
        report['source_changes']=[dict(row) for row in self.ledger.db.execute('SELECT * FROM source_changes ORDER BY id DESC LIMIT 50')]
        report['source_tracking']=self.inventory.snapshot() if self.inventory else {}
        atomic_json(self.report_path, report)
        lines = ["# 本地来源核实报告", "", "更新时间：" + report["generated_at"], "", report["result"]["message"],
                 "", "本报告只保存在本机；包含日志路径和用量定位信息，请勿公开。", ""]
        for index, stream in enumerate(streams, 1):
            lines.extend([f"## 暂停来源 {index}", "", f"原因：`{stream['reason']}`；待传 {stream['pending']} 条；原基线 {stream['baseline_time']}。", stream["server"]["message"], ""])
            for item in stream["sources"]:
                lines.extend(["- 文件：`" + item["path"] + "`", f"  检查点 {item['points']}；基线匹配 {item['anchor_matches']}；已有记录一致 {item['records_match']}；可选择恢复 {item['eligible']}。",
                              "  时间范围：" + str(item.get("first_time")) + " 至 " + str(item.get("last_time")),
                              "  核实结果：" + ("；".join(item["issues"]) or "原基线和历史记录均可验证；需明确选择该来源。"), ""])
        lines.extend(["## 已执行的来源选择", ""])
        for decision in audit:
            lines.extend(["- " + decision["created_at"] + "：`" + decision["selected_path"] + "`；排除其他文件 " + str(len(decision["excluded_paths"])) + " 个。备份：`" + decision["backup"] + "`。"])
        lines.extend(["", "## 压缩与缺失来源", "", "核实只读取本机数据，不新增、删除或覆盖用量事件。", ""])
        for item in health["archives"]:
            lines.extend(["- 压缩文件：`"+item["path"]+"`", "  原文件：`"+item["original_path"]+"`",
                          "  "+item["message"]+"；待传 "+str(item["pending"])+" 条。", ""])
        for item in health["missing"]:
            lines.extend(["- 缺失文件：`"+item["path"]+"`；未找到可用副本，请恢复原文件或检查来源目录。", ""])
        for item in health["unregistered_archives"]:
            lines.extend(["- 未登记压缩文件：`"+item["path"]+"`；"+item["message"], ""])
        lines.extend(["## 来源告警与已确认覆盖缺口", "", "关闭只停止当前问题的重复告警，不删除记录、不解除暂停、不修补缺口；来源变化会重新告警。", ""])
        for item in health["active_alerts"]:
            lines.extend(["- 待处理："+item["message"]+"；待传 "+str(item["pending"])+" 条；最后已采集时间 "+str(item["last_record_at"]),
                          "  路径："+"；".join(item["paths"]), ""])
        for item in health["closed_alerts"]:
            lines.extend(["- 已关闭："+item["closed_at"]+"；原因："+item["reason"], "  路径："+"；".join(item["paths"]), ""])
        lines.extend(["## 告警操作记录", ""])
        for entry in alert_audit:
            lines.extend(["- "+entry["created_at"]+" · "+entry["action"]+" · "+entry.get("reason",entry.get("message","")),
                          "  备份："+entry.get("backup","自动状态变更，不修改用量记录"), ""])
        lines.extend(['## 来源文件变化记录','','只记录采集器观察到的路径变化；无法可靠追溯执行者或删除意图。消失的路径移出活动追踪，历史用量保留。',''])
        for entry in report['source_changes']:
            lines.extend(['- '+entry['observed_at']+' · '+entry['kind']+' · `'+entry['path']+'`',
                          '  '+entry['message']+(' 关联路径：`'+entry['related_path']+'`' if entry['related_path'] else ''),''])
        (self.state / "source-review.md").write_text("\n".join(lines), encoding="utf-8")
        return report

    def _alert_action(self, request):
        receipt = self.ledger.db.execute("SELECT details FROM source_alert_audit WHERE id=?", (request["id"],)).fetchone()
        if receipt:
            return json.loads(receipt[0])
        saved = json.loads(self.report_path.read_text(encoding="utf-8"))
        if saved.get("revision") != request.get("revision"):
            raise ReviewError("stale_review")
        action, stream_id_value = request["action"], request.get("stream_id")
        health = self.ledger.source_health()
        collection = "active_alerts" if action == "close_alert" else "closed_alerts"
        old = next((item for item in saved.get("source_health",{}).get(collection,[]) if item["stream_id"] == stream_id_value),None)
        current = next((item for item in health[collection] if item["stream_id"] == stream_id_value),None)
        if not old or not current or old["fingerprint"] != request.get("fingerprint") or current["fingerprint"] != request.get("fingerprint"):
            raise ReviewError("stale_review")
        reason = request.get("reason", "")
        if action == "close_alert" and (not isinstance(reason,str) or not 1 <= len(reason.strip()) <= 200 or any(ord(char)<32 for char in reason)):
            raise ReviewError("reason_required")
        if action == "reopen_alert" and (old.get("closure_id") != request.get("closure_id") or current.get("closure_id") != request.get("closure_id")):
            raise ReviewError("stale_review")
        backup_dir = self.state/"source-review-backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir/("alert-"+request["id"]+".sqlite3")
        if backup.exists():
            raise ReviewError("stale_review")
        destination = sqlite3.connect(backup)
        try:self.ledger.db.backup(destination)
        finally:destination.close()
        # Re-check after the backup; files can change independently of the agent thread.
        fresh = next((item for item in self.ledger.source_health()[collection] if item["stream_id"] == stream_id_value),None)
        if not fresh or fresh["fingerprint"] != current["fingerprint"]:
            raise ReviewError("stale_review")
        now = iso(datetime.now(UTC))
        closure_id = request["id"] if action == "close_alert" else current["closure_id"]
        details = dict(reason=reason.strip() if action == "close_alert" else current["reason"],
                       actor=getpass.getuser(), backup=str(backup), snapshot=current)
        with self.ledger.db:
            if action == "close_alert":
                self.ledger.db.execute("INSERT INTO source_alert_closures VALUES(?,?,?,?,?,?,NULL)",
                    (closure_id,stream_id_value,current["fingerprint"],now,details["reason"],compact(current)))
            else:
                self.ledger.db.execute("UPDATE source_alert_closures SET reopened_at=? WHERE id=? AND reopened_at IS NULL",(now,closure_id))
            self.ledger.db.execute("INSERT INTO source_alert_audit VALUES(?,?,?,?,?,?)",
                (request["id"],"close" if action == "close_alert" else "reopen",now,stream_id_value,closure_id,compact(details)))
        return details

    def _apply(self, stream_id_value, selected_id, fingerprint, decision_id, automatic=False):
        previous = self.ledger.db.execute("SELECT details FROM source_review_audit WHERE id=?", (decision_id,)).fetchone()
        if previous:
            return json.loads(previous[0])
        stream = self.ledger.db.execute("SELECT * FROM streams WHERE id=? AND paused IS NOT NULL", (stream_id_value,)).fetchone()
        if stream is None:
            raise ReviewError("not_registered")
        report, scans = self.inspect_stream(stream)
        item = next((item for item in report["sources"] if item["source_id"] == selected_id), None)
        if not item or not item["eligible"]:
            raise ReviewError("not_eligible")
        if item["fingerprint"] != fingerprint:
            raise ReviewError("stale_review")
        if automatic and report["auto_source_id"] != selected_id:
            raise ReviewError("not_eligible")
        scan, path = scans[selected_id], Path(item["path"])
        before = path.stat()
        if file_hash(path) != fingerprint:
            raise ReviewError("stale_review")
        excluded = [candidate["path"] for candidate in report["sources"] if candidate["source_id"] != selected_id]
        backup_dir = self.state / "source-review-backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / ("collector-" + decision_id + ".sqlite3")
        # The original immutable events are retained; the snapshot also permits offline investigation.
        if backup.exists():
            raise ReviewError("stale_review")
        destination = sqlite3.connect(backup)
        try:
            self.ledger.db.backup(destination)
        finally:
            destination.close()
        details = dict(selected_path=str(path), excluded_paths=excluded, fingerprint=fingerprint,
                       backup=str(backup), automatic=automatic, stream_id=stream_id_value, previous_pause=stream["paused"])
        with self.ledger.db:
            self.ledger.db.execute("DELETE FROM source_exclusions WHERE path=? AND stream_id=?", (str(path), stream_id_value))
            self.ledger.db.executemany("INSERT OR REPLACE INTO source_exclusions VALUES(?,?,?)", [(other, stream_id_value, decision_id) for other in excluded])
            self.ledger.db.execute("UPDATE streams SET paused=NULL,mode='unverified',next_retry=0,attempts=0 WHERE id=?", (stream_id_value,))
            self.ledger._advance(stream, [point for point in scan.points if point.ordinal > stream["anchor_ordinal"] + stream["last_seq"]])
            if self.ledger.db.execute("SELECT paused FROM streams WHERE id=?", (stream_id_value,)).fetchone()[0]:
                raise ReviewError("not_eligible")
            with path.open("rb") as handle:
                start = max(0, scan.offset - 256)
                handle.seek(start)
                tail = hashlib.sha256(handle.read(scan.offset - start)).hexdigest()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ReviewError("stale_review")
            self.ledger.db.execute("UPDATE sources SET offset=?,ordinal=?,model=?,head=?,tail=?,mtime=?,size=?,warnings=? WHERE path=?",
                (scan.offset, scan.ordinal, scan.model, scan.head, tail, after.st_mtime_ns, after.st_size, compact(sorted(scan.warnings)), str(path)))
            self.ledger.db.execute("INSERT INTO source_review_audit VALUES(?,?,?,?)", (decision_id, iso(datetime.now(UTC)), stream_id_value, compact(details)))
        return details

    def handle(self, request):
        request_id = str(uuid.UUID(request["id"]))
        action = request.get("action")
        details = []
        code = "inspected"
        try:
            if action in {"close_alert", "reopen_alert"}:
                details.append(self._alert_action(dict(request,id=request_id)))
                code = "alert_closed" if action == "close_alert" else "alert_reopened"
            elif action == "resume":
                saved = json.loads(self.report_path.read_text(encoding="utf-8"))
                # A durable audit receipt makes repeating a completed request harmless.
                receipt = self.ledger.db.execute("SELECT 1 FROM source_review_audit WHERE id=?", (request_id,)).fetchone()
                if not receipt and saved.get("revision") != request.get("revision"):
                    raise ReviewError("stale_review")
                if not receipt:
                    saved_stream = next((stream for stream in saved.get("streams", []) if stream["stream_id"] == request.get("stream_id")), None)
                    saved_source = next((item for item in saved_stream["sources"] if item["source_id"] == request.get("source_id")), None) if saved_stream else None
                    if not saved_source or not saved_source["eligible"] or saved_source.get("fingerprint") != request.get("fingerprint"):
                        raise ReviewError("not_eligible")
                details.append(self._apply(request["stream_id"], request["source_id"], request["fingerprint"], request_id))
                code = "resumed"
            elif action == "auto":
                for stream in self.ledger.paused_sources():
                    report, _ = self.inspect_stream(stream)
                    if report["auto_source_id"]:
                        item = next(item for item in report["sources"] if item["source_id"] == report["auto_source_id"])
                        decision_id = str(uuid.uuid5(uuid.UUID(request_id), stream["id"]))
                        details.append(self._apply(stream["id"], item["source_id"], item["fingerprint"], decision_id, automatic=True))
                code = "auto_complete"
            elif action != "inspect":
                raise ReviewError("invalid_request")
        except ReviewError as error:
            code = str(error)
        except (OSError, ValueError, KeyError, sqlite3.Error):
            code = "review_failed"
        return self.build_report(request_id, code, details)

    def tick(self):
        try:
            if self.request_path.stat().st_size > 65536:
                return
            request = json.loads(self.request_path.read_text(encoding="utf-8-sig"))
            self.handle(request)
            current = json.loads(self.request_path.read_text(encoding="utf-8-sig"))
            if current.get("id") == request.get("id"):
                self.request_path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            # A malformed request never authorizes a source change.
            pass


def main():
    parser = argparse.ArgumentParser(description="请求运行中的采集器核实来源，报告仅保存在本机。")
    parser.add_argument("--action", choices=("inspect", "auto", "resume", "close_alert", "reopen_alert"), default="inspect")
    parser.add_argument("--stream")
    parser.add_argument("--source")
    parser.add_argument("--reason")
    parser.add_argument("--wait", type=float, default=40)
    args = parser.parse_args()
    state = state_path()
    request = dict(id=str(uuid.uuid4()), action=args.action)
    if args.action == "resume":
        saved = json.loads((state / "source-review.json").read_text(encoding="utf-8"))
        stream = next((stream for stream in saved["streams"] if stream["stream_id"] == args.stream), None)
        item = next((item for item in stream["sources"] if item["source_id"] == args.source), None) if stream else None
        if not item:
            parser.error("请先核实并从报告中选择来源。")
        request.update(stream_id=args.stream, source_id=args.source, revision=saved["revision"], fingerprint=item.get("fingerprint"))
    elif args.action in {"close_alert","reopen_alert"}:
        saved = json.loads((state/"source-review.json").read_text(encoding="utf-8"))
        key = "active_alerts" if args.action == "close_alert" else "closed_alerts"
        item = next((item for item in saved.get("source_health",{}).get(key,[]) if item["stream_id"] == args.stream),None)
        if not item:parser.error("请先核实并从报告中选择当前告警或已关闭告警。")
        request.update(stream_id=args.stream,revision=saved["revision"],fingerprint=item["fingerprint"],closure_id=item.get("closure_id"),reason=args.reason)
    atomic_json(state / "source-review-request.json", request)
    deadline = time.monotonic() + max(0, min(args.wait, 55))
    while time.monotonic() < deadline:
        try:
            report = json.loads((state / "source-review.json").read_text(encoding="utf-8"))
            if report.get("request_id") == request["id"]:
                print(report["result"]["message"])
                print("待核实来源：" + str(len(report["streams"])))
                print("本地报告：" + str(state / "source-review.md"))
                return 0 if report["result"]["code"] in {"inspected", "resumed", "auto_complete", "alert_closed", "alert_reopened"} else 1
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    print("核实请求已保存；请确认采集器正在运行，再查看本地核实窗口。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
