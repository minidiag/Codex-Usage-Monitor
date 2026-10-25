"""Read-only reconciliation while the collector is paused and its outbox is empty."""
import json
import argparse
from pathlib import Path
import sqlite3

from client.transport import Api, load_credentials
from client.paths import credential_path,state_path
from client.view_policy import window_filters
from server.protocol import COUNTERS, timestamp


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--window",action="store_true",help="Reconcile only the current Windows-user window scope")
    parser.add_argument("--cycles",action="store_true",help="Reconcile recorded quota-reset cycles for the current user and displayed account")
    args=parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    state = state_path()
    status = json.loads((state / "status.json").read_text(encoding="utf-8"))
    if (status.get("running") and not status.get("paused")) or status.get("queued_records"):
        raise RuntimeError("Pause the collector with an empty outbox before reconciliation")
    filters=None
    if args.window or args.cycles:
        control=json.loads((state/"control.json").read_text(encoding="utf-8-sig"))
        identity=status.get("identity",{})
        filters=window_filters(identity.get("subject"),control.get("filters",{}),identity.get("accounts",[]),identity.get("primary_account"))
        if filters is None:raise RuntimeError("Current Windows user unavailable")
    api=Api(load_credentials(credential_path()))
    if args.cycles:
        account=filters.get("account") or status.get("quota",{}).get("account_key")
        if not account or account=="unassigned":raise RuntimeError("Select a logged-in account with quota")
        filters["account"]=account
        value=api.quota_cycles(account,filters["principal"])
        summary=dict(as_of=value["as_of"],windows=[dict(name=c["bucket"]+":"+c["resets_at"],start=c["starts_at"],end=min(timestamp(c["ends_at"]),timestamp(value["as_of"])).isoformat(),totals=c["totals"]) for c in value["cycles"]])
        if not summary["windows"]:raise RuntimeError("No recorded quota cycles")
    else:summary = api.summary(filters,[]) if args.window else api.summary()
    with sqlite3.connect((state / "collector.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT stream_id,seq,wire,acked FROM events ORDER BY stream_id,seq").fetchall()
    report = []
    for window in summary["windows"]:
        start, end = timestamp(window["start"]), timestamp(window["end"])
        total = {name: 0 for name in COUNTERS}
        previous = {}
        for stream, seq, raw, acked in rows:
            if not acked:
                raise RuntimeError("Outbox changed during reconciliation")
            record = json.loads(raw)
            prior = previous.get(stream)
            if seq and prior:
                if int(prior["source_seq"]) != seq-1:
                    raise RuntimeError("Local ledger has a gap")
                claim=record.get("attribution",{})
                matches=not (args.window or args.cycles) or (claim.get("principal_key")==filters["principal"] and
                    ("account" not in filters or claim.get("account_key")== (None if filters["account"]=="unassigned" else filters["account"])))
                if matches and start <= timestamp(record["event_time"]) < end:
                    for name in COUNTERS:
                        total[name] += int(record["counters"][name])-int(prior["counters"][name])
            previous[stream] = record
        total["total"] = total["input"]+total["output"]
        expected = {name: str(value) for name, value in total.items()}
        report.append(dict(name=window["name"], total=expected["total"], matched=expected == window["totals"]))
    print(json.dumps(dict(as_of=summary["as_of"], scope="quota_cycles" if args.cycles else ("current_windows_user" if args.window else "tenant"),acknowledged_checkpoints=len(rows), windows=report,
                          matched=all(row["matched"] for row in report))))
    if not all(row["matched"] for row in report):
        raise RuntimeError("Local and remote totals differ")


if __name__ == "__main__":
    main()
