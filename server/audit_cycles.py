"""Read-only review of preserved quota-cycle fragments; never repairs the ledger.

Run with the owner DATABASE_URL and: python -m server.audit_cycles
Output contains anonymous cycle identifiers and bounds, never credentials.
"""
import json
import os

import psycopg
from psycopg.rows import dict_row


def report(dsn):
    with psycopg.connect(dsn, row_factory=dict_row) as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        rows=db.execute('''WITH ordered AS (
            SELECT tenant_id,account_key,bucket,cycle_id,duration_mins,starts_at,ends_at,resets_at,
                bounds_source,lag(starts_at) OVER w AS previous_start,
                lag(duration_mins) OVER w AS previous_duration
            FROM quota_cycles WINDOW w AS (PARTITION BY tenant_id,account_key,bucket ORDER BY starts_at))
            SELECT * FROM ordered WHERE ends_at<resets_at OR
                (previous_duration=duration_mins AND starts_at-previous_start<=interval '5 minutes')
            ORDER BY tenant_id,account_key,bucket,starts_at''').fetchall()
        counts=db.execute('SELECT count(*) AS cycles FROM quota_cycles').fetchone()
        return dict(cycle_count=counts['cycles'],review_candidate_count=len(rows),
                    candidates=rows,action='review_only_no_changes')


if __name__=='__main__':
    print(json.dumps(report(os.environ['DATABASE_URL']),default=str,indent=2))
