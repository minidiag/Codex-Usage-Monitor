"""Independent execution subjects; uploader identity never participates in event deduplication."""
from datetime import datetime, timedelta
from psycopg.types.json import Jsonb

from server.protocol import ApiError, COUNTERS, UTC, iso, timestamp, windows

DIMENSIONS = {"account": "account_key", "principal": "principal_key", "machine": "machine_key"}


class IdentityStore:
    def record_display_names(self,token,rows):
        with self.connect() as db:
            auth=self.authenticate(db,token,'write');tenant=auth['tenant_id']
            subject=db.execute('SELECT principal_key FROM device_subjects WHERE tenant_id=%s AND device_id=%s',(tenant,auth['device_id'])).fetchone()
            if not subject:raise ApiError('identity_not_registered',403)
            principal=subject['principal_key']
            for row in sorted(rows,key=lambda r:r['account_key']):
                account=row['account_key']
                known=db.execute('''SELECT 1 FROM event_subjects WHERE tenant_id=%s AND account_key=%s AND principal_key=%s
                    UNION ALL SELECT 1 FROM quota_boundary_samples o JOIN device_subjects ds USING(tenant_id,device_id)
                    WHERE o.tenant_id=%s AND o.account_key=%s AND ds.principal_key=%s LIMIT 1''',
                    (tenant,account,principal,tenant,account,principal)).fetchone()
                if not known:raise ApiError('account_not_observed',403)
                db.execute('''INSERT INTO account_display_names(tenant_id,account_key,principal_key,display_name,source_device_id)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,account_key,principal_key) DO UPDATE SET
                    display_name=EXCLUDED.display_name,source_device_id=EXCLUDED.source_device_id,updated_at=now()
                    WHERE account_display_names.display_name IS DISTINCT FROM EXCLUDED.display_name''',
                    (tenant,account,principal,row['display_name'],auth['device_id']))
        return dict(recorded=len(rows))

    def register_identity(self, token, value):
        with self.connect() as db:
            auth = self.authenticate(db, token, "write")
            tenant, device = auth["tenant_id"], auth["device_id"]
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(tenant)+"/device/"+str(device),))
            existing = db.execute("SELECT * FROM device_subjects WHERE tenant_id=%s AND device_id=%s", (tenant, device)).fetchone()
            if existing and any(existing[k] != value[k] for k in ("principal_key", "machine_key")):
                raise ApiError("device_identity_mismatch", 403)
            db.execute("INSERT INTO machines VALUES (%s,%s) ON CONFLICT DO NOTHING", (tenant, value["machine_key"]))
            db.execute("INSERT INTO principals VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (tenant, value["principal_key"], value["machine_key"]))
            db.execute("INSERT INTO device_subjects(tenant_id,device_id,principal_key,machine_key) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                       (tenant, device, value["principal_key"], value["machine_key"]))
        return dict(registered=True)

    @staticmethod
    def validate_claim(db, auth, claim):
        if claim is None:
            return
        subject = db.execute("SELECT * FROM device_subjects WHERE tenant_id=%s AND device_id=%s", (auth["tenant_id"], auth["device_id"])).fetchone()
        if not subject or any(subject[k] != claim[k] for k in ("principal_key", "machine_key")):
            raise ApiError("unregistered_subject", 403)
        if timestamp(claim["observed_from"]) < subject["created_at"]-timedelta(minutes=5):
            raise ApiError("invalid_observation_boundary")

    @staticmethod
    def assign_subject(db, key, record, previous):
        claim = record["attribution_claim"]
        account = principal = machine = None
        status = "unknown"
        if claim and timestamp(claim["observed_from"]) <= previous["event_time"] <= record["event_time"]:
            account, principal, machine = (claim[k] for k in ("account_key", "principal_key", "machine_key"))
            status = "confirmed" if account else "account_unknown"
            if account:
                db.execute("INSERT INTO accounts(tenant_id,account_key) VALUES(%s,%s) ON CONFLICT DO NOTHING", (key[0], account))
        db.execute("INSERT INTO event_subjects VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                   (*key, record["source_seq"], account, principal, machine, status))

    @staticmethod
    def read_principal(db, auth):
        if not auth["reader_device_id"]:
            return None
        row = db.execute("SELECT principal_key FROM device_subjects WHERE tenant_id=%s AND device_id=%s", (auth["tenant_id"], auth["reader_device_id"])).fetchone()
        if not row:
            raise ApiError("identity_not_registered", 403)
        return row["principal_key"]

    def identities(self, token):
        with self.connect() as db:
            auth = self.authenticate(db, token, "read")
            allowed = self.read_principal(db, auth)
            subjects = db.execute("SELECT DISTINCT principal_key,machine_key FROM device_subjects WHERE tenant_id=%s AND (%s::text IS NULL OR principal_key=%s)",
                                  (auth["tenant_id"], allowed, allowed)).fetchall()
            accounts = db.execute("""SELECT DISTINCT a.account_key FROM accounts a WHERE a.tenant_id=%s AND
                (%s::text IS NULL OR EXISTS(SELECT 1 FROM event_subjects s WHERE s.tenant_id=a.tenant_id AND s.account_key=a.account_key AND s.principal_key=%s)
                OR EXISTS(SELECT 1 FROM quota_boundary_samples o JOIN device_subjects ds USING(tenant_id,device_id)
                WHERE o.tenant_id=a.tenant_id AND o.account_key=a.account_key AND ds.principal_key=%s)) ORDER BY a.account_key""",
                (auth["tenant_id"], allowed, allowed, allowed)).fetchall()
            names=db.execute('''SELECT DISTINCT ON(account_key) account_key,display_name,updated_at FROM account_display_names
                WHERE tenant_id=%s AND account_key=ANY(%s) AND (%s::text IS NULL OR principal_key=%s)
                ORDER BY account_key,updated_at DESC,principal_key''',
                (auth['tenant_id'],[r['account_key'] for r in accounts],allowed,allowed)).fetchall()
        return dict(accounts=[r["account_key"] for r in accounts], subjects=[dict(r) for r in subjects],
                    scope="tenant" if allowed is None else "principal", principal_key=allowed,
                    account_display_names={r['account_key']:dict(display_name=r['display_name'],updated_at=iso(r['updated_at'])) for r in names})

    def summary_scoped(self, token, filters=None, group_by=None, now=None, legacy_device=None):
        with self.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            auth = self.authenticate(db, token, "read")
            return self.scoped_snapshot(db, auth, filters or {}, group_by, now, legacy_device)

    def scoped_snapshot(self, db, auth, filters, group_by=None, now=None, legacy_device=None):
        now = now or db.execute("SELECT transaction_timestamp() t").fetchone()["t"]
        now = now.replace(microsecond=now.microsecond//1000*1000)
        allowed = self.read_principal(db, auth)
        where, params = ["d.tenant_id=%s", "d.event_time >= %s", "d.event_time < %s"], [auth["tenant_id"]]
        conditions, extra = [], []
        if allowed:
            conditions.append("s.principal_key=%s")
            extra.append(allowed)
        for name, column in DIMENSIONS.items():
            value = filters.get(name)
            if value == "unassigned":
                conditions.append("s."+column+" IS NULL")
            elif value:
                conditions.append("s."+column+"=%s")
                extra.append(value)
        if legacy_device:
            conditions.append("st.owner_device_id=%s::uuid")
            extra.append(legacy_device)
        suffix = " AND "+" AND ".join(conditions) if conditions else ""
        columns = [DIMENSIONS[name] for name in (group_by or [])]
        groups_sql = ",".join("s."+column for column in columns)
        select = (groups_sql+"," if columns else "")+"COALESCE(sum(d.input_tokens),0) AS input,COALESCE(sum(d.cached_input_tokens),0) AS cached_input,COALESCE(sum(d.output_tokens),0) AS output,COALESCE(sum(d.reasoning_output_tokens),0) AS reasoning_output,COALESCE(sum(d.total_tokens),0) AS total, count(*) FILTER (WHERE s.account_key IS NULL) AS unknown_events"
        base = " FROM usage_deltas d JOIN streams st USING(tenant_id,stream_id,epoch) LEFT JOIN event_subjects s USING(tenant_id,stream_id,epoch,source_seq) WHERE "+" AND ".join(where)+suffix
        output = []
        for name, start in windows(now):
            args = params+[start, now]+extra
            rows = db.execute("SELECT "+select+base+(" GROUP BY "+groups_sql+" ORDER BY "+groups_sql+" NULLS FIRST" if columns else ""), args).fetchall()
            totals = {k: str(sum(r[k] for r in rows)) for k in (*COUNTERS, "total")}
            groups = [dict(keys={dim: r[DIMENSIONS[dim]] for dim in group_by}, totals={k:str(r[k]) for k in (*COUNTERS,"total")}) for r in rows] if columns else []
            output.append(dict(name=name, start=iso(start), end=iso(now), totals=totals, groups=groups,
                               unassigned_account_events=str(sum(r["unknown_events"] for r in rows))))
        # Coverage is conservative across registered sources; filters must never imply full billing coverage.
        devices = db.execute("SELECT * FROM devices WHERE tenant_id=%s AND (%s::uuid IS NULL OR id=%s::uuid)",
                             (auth["tenant_id"], auth["reader_device_id"], auth["reader_device_id"])).fetchall()
        reasons = {"history_not_imported"}
        for device in devices:
            reasons.update(self.device_quality(device, now))
        if not devices:
            reasons.add("no_registered_sources")
        if any(int(w["unassigned_account_events"]) for w in output):
            reasons.add("account_unassigned")
        return dict(as_of=iso(now), timezone="Asia/Shanghai", windows=output, quality="partial", quality_reasons=sorted(reasons),
                    scope=dict(filters=filters, group_by=group_by or [], authorization="tenant" if allowed is None else "principal"))
