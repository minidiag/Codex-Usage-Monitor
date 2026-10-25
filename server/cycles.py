"""Persist observed quota windows and materialized, deduplicated per-principal token totals."""
from datetime import datetime,timedelta

from server.protocol import ApiError,COUNTERS,UTC,iso


class QuotaCycleStore:
    def record_quota_cycles(self,token,rows):
        with self.connect() as db:
            auth=self.authenticate(db,token,"write")
            tenant=auth["tenant_id"]
            subject=db.execute("SELECT * FROM device_subjects WHERE tenant_id=%s AND device_id=%s",(tenant,auth["device_id"])).fetchone()
            if not subject:raise ApiError("identity_not_registered",403)
            now=datetime.now(UTC)
            affected=set()
            for account in sorted({r["account_key"] for r in rows}):
                db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(str(tenant)+"/quota/"+account,))
            for row in rows:
                if row["observed_at"]>now+timedelta(minutes=5):raise ApiError("cycle_clock_anomaly")
                existing=db.execute("SELECT * FROM quota_cycles WHERE tenant_id=%s AND account_key=%s AND bucket=%s AND (resets_at=%s OR starts_at=%s)",
                    (tenant,row["account_key"],row["bucket"],row["resets_at"],row["starts_at"])).fetchone()
                if existing and existing["cycle_id"]!=row["cycle_id"]:raise ApiError("cycle_boundary_conflict",409)
                db.execute("INSERT INTO accounts(tenant_id,account_key) VALUES(%s,%s) ON CONFLICT DO NOTHING",(tenant,row["account_key"]))
                db.execute("""INSERT INTO quota_cycles(tenant_id,cycle_id,account_key,bucket,duration_mins,starts_at,resets_at,ends_at,first_observed_at,last_observed_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,cycle_id) DO UPDATE
                    SET first_observed_at=LEAST(quota_cycles.first_observed_at,EXCLUDED.first_observed_at),last_observed_at=GREATEST(quota_cycles.last_observed_at,EXCLUDED.last_observed_at)""",
                    (tenant,row["cycle_id"],row["account_key"],row["bucket"],row["duration_mins"],row["starts_at"],row["resets_at"],row["resets_at"],row["observed_at"],row["observed_at"]))
                db.execute("""INSERT INTO quota_cycle_observations VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,cycle_id,device_id)
                    DO UPDATE SET first_observed_at=LEAST(quota_cycle_observations.first_observed_at,EXCLUDED.first_observed_at),last_observed_at=GREATEST(quota_cycle_observations.last_observed_at,EXCLUDED.last_observed_at)""",
                    (tenant,row["cycle_id"],auth["device_id"],row["observed_at"],row["observed_at"]))
                # A newly observed reset may end an older window early. Keep the scheduled reset
                # for auditing, and make effective intervals disjoint within the same bucket.
                changed=db.execute("""WITH ordered AS (
                    SELECT cycle_id,LEAST(resets_at,lead(starts_at) OVER(ORDER BY starts_at)) AS ending
                    FROM quota_cycles WHERE tenant_id=%s AND account_key=%s AND bucket=%s)
                    UPDATE quota_cycles c SET ends_at=o.ending FROM ordered o
                    WHERE c.tenant_id=%s AND c.cycle_id=o.cycle_id AND c.ends_at<>o.ending RETURNING c.cycle_id""",
                    (tenant,row["account_key"],row["bucket"],tenant)).fetchall()
                affected.update(r["cycle_id"] for r in changed);affected.add(row["cycle_id"])
                previous=db.execute("SELECT cycle_id FROM quota_cycles WHERE tenant_id=%s AND account_key=%s AND bucket=%s AND starts_at<%s ORDER BY starts_at DESC LIMIT 1",
                    (tenant,row["account_key"],row["bucket"],row["starts_at"])).fetchone()
                if previous:affected.add(previous["cycle_id"])
                db.execute("""INSERT INTO quota_cycle_totals(tenant_id,cycle_id,principal_key,machine_key) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (tenant,row["cycle_id"],subject["principal_key"],subject["machine_key"]))
            self.refresh_cycle_totals(db,tenant,affected)
        return dict(recorded=len(rows))

    @staticmethod
    def refresh_cycle_totals(db,tenant,identities):
        if not identities:return
        cycles=db.execute("SELECT * FROM quota_cycles WHERE tenant_id=%s AND cycle_id=ANY(%s) ORDER BY cycle_id FOR UPDATE",(tenant,sorted(identities))).fetchall()
        for cycle in cycles:
            # Reset cached snapshots then rebuild from the authoritative numeric ledger. This
            # handles late data, backfills and an earlier-than-scheduled reset without double count.
            db.execute("""UPDATE quota_cycle_totals SET input_tokens=0,cached_input_tokens=0,output_tokens=0,reasoning_output_tokens=0,computed_at=now()
                WHERE tenant_id=%s AND cycle_id=%s""",(tenant,cycle["cycle_id"]))
            db.execute("""INSERT INTO quota_cycle_totals(tenant_id,cycle_id,principal_key,machine_key,input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens,computed_at)
                SELECT d.tenant_id,%s,s.principal_key,s.machine_key,sum(d.input_tokens),sum(d.cached_input_tokens),sum(d.output_tokens),sum(d.reasoning_output_tokens),now()
                FROM usage_deltas d JOIN event_subjects s USING(tenant_id,stream_id,epoch,source_seq)
                WHERE d.tenant_id=%s AND s.account_key=%s AND s.principal_key IS NOT NULL AND d.event_time >= %s AND d.event_time < LEAST(%s,now())
                GROUP BY d.tenant_id,s.principal_key,s.machine_key
                ON CONFLICT(tenant_id,cycle_id,principal_key,machine_key) DO UPDATE SET
                input_tokens=EXCLUDED.input_tokens,cached_input_tokens=EXCLUDED.cached_input_tokens,output_tokens=EXCLUDED.output_tokens,
                reasoning_output_tokens=EXCLUDED.reasoning_output_tokens,computed_at=EXCLUDED.computed_at""",
                (cycle["cycle_id"],tenant,cycle["account_key"],cycle["starts_at"],cycle["ends_at"]))

    def refresh_cycles_after_ingest(self,db,key,before,after):
        if after<=before:return
        accounts=db.execute("""SELECT DISTINCT s.account_key FROM usage_deltas d JOIN event_subjects s USING(tenant_id,stream_id,epoch,source_seq)
            WHERE d.tenant_id=%s AND d.stream_id=%s AND d.epoch=%s AND d.source_seq>%s AND d.source_seq<=%s AND s.account_key IS NOT NULL ORDER BY s.account_key""",(*key,before,after)).fetchall()
        for account in accounts:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(str(key[0])+"/quota/"+account["account_key"],))
        rows=db.execute("""SELECT DISTINCT c.cycle_id FROM usage_deltas d JOIN event_subjects s USING(tenant_id,stream_id,epoch,source_seq)
            JOIN quota_cycles c ON c.tenant_id=d.tenant_id AND c.account_key=s.account_key AND d.event_time>=c.starts_at AND d.event_time<c.ends_at
            WHERE d.tenant_id=%s AND d.stream_id=%s AND d.epoch=%s AND d.source_seq>%s AND d.source_seq<=%s""",(*key,before,after)).fetchall()
        self.refresh_cycle_totals(db,key[0],{r["cycle_id"] for r in rows})

    def quota_cycle_history(self,token,account,principal=None,limit=50):
        with self.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            auth=self.authenticate(db,token,"read");allowed=self.read_principal(db,auth)
            if allowed and principal and principal!=allowed:raise ApiError("forbidden",403)
            principal=allowed or principal
            now=db.execute("SELECT transaction_timestamp() t").fetchone()["t"]
            if principal:
                visible=db.execute("""SELECT 1 FROM event_subjects WHERE tenant_id=%s AND account_key=%s AND principal_key=%s
                    UNION ALL SELECT 1 FROM quota_cycle_observations o JOIN quota_cycles c USING(tenant_id,cycle_id)
                    JOIN device_subjects ds ON ds.tenant_id=o.tenant_id AND ds.device_id=o.device_id
                    WHERE c.tenant_id=%s AND c.account_key=%s AND ds.principal_key=%s LIMIT 1""",(auth["tenant_id"],account,principal,auth["tenant_id"],account,principal)).fetchone()
                if not visible:return dict(account_key=account,principal_key=principal,as_of=iso(now),cycles=[])
            rows=db.execute("""SELECT c.*,COALESCE(t.input,0) AS input,COALESCE(t.cached_input,0) AS cached_input,COALESCE(t.output,0) AS output,
                COALESCE(t.reasoning_output,0) AS reasoning_output,COALESCE(t.total,0) AS total,t.computed_at
                FROM quota_cycles c LEFT JOIN LATERAL (
                    SELECT sum(input_tokens) AS input,sum(cached_input_tokens) AS cached_input,sum(output_tokens) AS output,
                    sum(reasoning_output_tokens) AS reasoning_output,sum(total_tokens) AS total,max(computed_at) AS computed_at
                    FROM quota_cycle_totals t WHERE t.tenant_id=c.tenant_id AND t.cycle_id=c.cycle_id AND (%s::text IS NULL OR t.principal_key=%s)) t ON true
                WHERE c.tenant_id=%s AND c.account_key=%s ORDER BY c.resets_at DESC,c.bucket LIMIT %s""",
                (principal,principal,auth["tenant_id"],account,limit)).fetchall()
        cycles=[]
        for row in rows:
            cycles.append(dict(cycle_id=row["cycle_id"],bucket=row["bucket"],duration_mins=str(row["duration_mins"]),
                resets_at=str(int(row["resets_at"].timestamp())),starts_at=iso(row["starts_at"]),ends_at=iso(row["ends_at"]),
                totals={k:str(row[k]) for k in (*COUNTERS,"total")},computed_at=iso(row["computed_at"]) if row["computed_at"] else None,
                bounds_source=row["bounds_source"],quality="partial",closed=row["ends_at"]<=now,
                shortened=row["ends_at"]<row["resets_at"]))
        return dict(account_key=account,principal_key=principal,as_of=iso(now),cycles=cycles)
