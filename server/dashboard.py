"""Bounded, read-only usage series with the same scope as existing summaries."""
from datetime import timedelta
from server.protocol import ApiError, COUNTERS, SHANGHAI, iso


class DashboardStore:
    def usage_trend(self, token, account=None, period='7d', now=None):
        if period not in ('24h','7d','30d','90d'):
            raise ApiError('invalid_period')
        with self.connect() as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            auth=self.authenticate(db,token,'read')
            principal=self.read_principal(db,auth)
            now=now or db.execute('SELECT transaction_timestamp() t').fetchone()['t']
            local=now.astimezone(SHANGHAI)
            hourly=period=='24h'
            start=local-timedelta(hours=24) if hourly else local.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=int(period[:-1])-1)
            unit='hour' if hourly else 'day'
            where=['d.tenant_id=%s','d.event_time>=%s','d.event_time<%s']
            args=[auth['tenant_id'],start,now]
            if principal:
                where.append('s.principal_key=%s');args.append(principal)
            if account:
                where.append('s.account_key=%s');args.append(account)
            # unit is selected only from the two constants above, never request SQL.
            rows=db.execute("SELECT date_trunc('"+unit+"',d.event_time AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai' bucket,"+
                ','.join('sum(d.'+column+') AS '+name for name,column in
                    [('input','input_tokens'),('cached_input','cached_input_tokens'),('output','output_tokens'),('reasoning_output','reasoning_output_tokens'),('total','total_tokens')])+
                ',count(*) events FROM usage_deltas d LEFT JOIN event_subjects s USING(tenant_id,stream_id,epoch,source_seq) WHERE '+
                ' AND '.join(where)+' GROUP BY 1 ORDER BY 1',args).fetchall()
            found={r['bucket']:r for r in rows}
            cursor=start.replace(minute=0,second=0,microsecond=0) if hourly else start
            points=[];keys=(*COUNTERS,'total');totals={k:0 for k in keys};events=0
            while cursor<local:
                row=found.get(cursor,{})
                values={k:int(row.get(k,0)) for k in keys}
                n=int(row.get('events',0));events+=n
                for k in keys:totals[k]+=values[k]
                points.append(dict(start=iso(cursor),totals={k:str(v) for k,v in values.items()},events=str(n)))
                cursor+=timedelta(hours=1) if hourly else timedelta(days=1)
            return dict(as_of=iso(now),start=iso(start),end=iso(now),timezone='Asia/Shanghai',period=period,
                        interval=unit,account_key=account,scope='principal' if principal else 'tenant',
                        principal_key=principal,quality='partial',quality_reasons=['history_not_imported'],
                        totals={k:str(v) for k,v in totals.items()},events=str(events),points=points)
