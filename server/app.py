"""Small ASGI surface; database access runs in bounded worker threads."""
import asyncio
import logging
import time
from collections import OrderedDict
from pathlib import Path

import psycopg
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse
from starlette.routing import Route
from starlette.concurrency import run_in_threadpool

from server import protocol
from server.store import Store


def create_app(store=None):
    store = store or Store()
    capacity = asyncio.Semaphore(8)
    clients = OrderedDict()

    async def read_body(request):
        if request.headers.get("content-encoding", "identity") != "identity":
            raise protocol.ApiError("unsupported_encoding", 415)
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise protocol.ApiError("json_required", 415)
        chunks, length = [], 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > 524288:
                raise protocol.ApiError("body_too_large", 413)
            chunks.append(chunk)
        return protocol.parse_json(b"".join(chunks))

    async def dispatch(request, action):
        try:
            now = time.monotonic()
            authorization = request.headers.get("authorization", "")
            if not authorization.startswith("Bearer ") or not 32 <= len(authorization[7:]) <= 256:
                raise protocol.ApiError("unauthorized", 401)
            token = authorization[7:]
            client = protocol.token_hash(token)
            # Bound per credential, without storing the credential itself in the limiter.
            bucket = int(now // 60)
            prior_bucket, count = clients.get(client, (bucket, 0))
            clients[client] = (bucket, count + 1 if prior_bucket == bucket else 1)
            clients.move_to_end(client)
            while len(clients) > 4096:
                clients.popitem(last=False)
            if clients[client][1] > 180:
                raise protocol.ApiError("rate_limited", 429, retryable=True)
            try:
                await asyncio.wait_for(capacity.acquire(), timeout=0.2)
            except TimeoutError:
                raise protocol.ApiError("busy", 429, retryable=True) from None
            try:
                if action == "ingest":
                    value = protocol.batch(await read_body(request))
                    result = await run_in_threadpool(store.ingest, token, value)
                elif action == "heartbeat":
                    value = protocol.heartbeat(await read_body(request))
                    result = await run_in_threadpool(store.heartbeat, token, value)
                elif action == "summary":
                    device = request.query_params.get("device_id")
                    device = protocol.uuid_id(device) if device else None
                    result = await run_in_threadpool(store.summary, token, device)
                elif action == "identity":
                    result = await run_in_threadpool(store.register_identity, token, protocol.subject(await read_body(request)))
                elif action == "record_cycles":
                    # Legacy collectors send the same numeric observation fields.
                    # Route them through recognition too; they must not create fragments.
                    value=protocol.quota_cycles(await read_body(request))
                    result=await run_in_threadpool(store.record_boundary_observations,token,value)
                elif action == "observe_boundaries":
                    value=protocol.quota_observations(await read_body(request))
                    result=await run_in_threadpool(store.record_boundary_observations,token,value)
                elif action == "current_cycles":
                    protocol.require(set(request.query_params)<={"account","principal"},"invalid_filter")
                    protocol.require(all(len(request.query_params.getlist(k))==1 for k in request.query_params),"duplicate_filter")
                    account=protocol.hex_id(request.query_params.get("account"))
                    principal=protocol.hex_id(request.query_params["principal"]) if request.query_params.get("principal") else None
                    result=await run_in_threadpool(store.current_quota_cycles,token,account,principal)
                elif action == 'bucket_history':
                    protocol.require(set(request.query_params)<={'account','principal','bucket','before','limit'},'invalid_filter')
                    protocol.require(all(len(request.query_params.getlist(k))==1 for k in request.query_params),'duplicate_filter')
                    account=protocol.hex_id(request.query_params.get('account'))
                    principal=protocol.hex_id(request.query_params['principal']) if request.query_params.get('principal') else None
                    bucket=request.query_params.get('bucket');protocol.require(bucket in ('primary','secondary'),'invalid_bucket')
                    before=protocol.timestamp(request.query_params['before']) if request.query_params.get('before') else None
                    limit=protocol.integer(request.query_params.get('limit','50'));protocol.require(1<=limit<=200,'invalid_limit')
                    result=await run_in_threadpool(store.quota_cycle_history,token,account,principal,limit,bucket,before)
                elif action == "cycle_history":
                    protocol.require(set(request.query_params)<={"account","principal","limit"},"invalid_filter")
                    protocol.require(all(len(request.query_params.getlist(k))==1 for k in request.query_params),"duplicate_filter")
                    account=protocol.hex_id(request.query_params.get("account"))
                    principal=protocol.hex_id(request.query_params["principal"]) if request.query_params.get("principal") else None
                    limit=protocol.integer(request.query_params.get("limit","50"));protocol.require(1<=limit<=200,"invalid_limit")
                    result=await run_in_threadpool(store.quota_cycle_history,token,account,principal,limit)
                elif action == "identities":
                    result = await run_in_threadpool(store.identities, token)
                elif action == 'display_names':
                    value=protocol.display_names(await read_body(request))
                    result=await run_in_threadpool(store.record_display_names,token,value)
                elif action == 'trend':
                    protocol.require(set(request.query_params)<={'account','period'},'invalid_filter')
                    protocol.require(all(len(request.query_params.getlist(k))==1 for k in request.query_params),'duplicate_filter')
                    account=protocol.hex_id(request.query_params['account']) if 'account' in request.query_params else None
                    period=request.query_params.get('period','7d')
                    protocol.require(period in ('24h','7d','30d','90d'),'invalid_period')
                    result=await run_in_threadpool(store.usage_trend,token,account,period)
                elif action == "scoped_summary":
                    allowed = {"account", "principal", "machine", "group_by"}
                    protocol.require(set(request.query_params) <= allowed, "invalid_filter")
                    protocol.require(all(len(request.query_params.getlist(k)) == 1 for k in request.query_params), "duplicate_filter")
                    filters = {k: v if v == "unassigned" else protocol.hex_id(v) for k,v in request.query_params.items() if k != "group_by"}
                    groups = request.query_params.get("group_by", "").split(",") if request.query_params.get("group_by") else []
                    protocol.require(len(groups) == len(set(groups)) and set(groups) <= {"account","principal","machine"}, "invalid_grouping")
                    result = await run_in_threadpool(store.summary_scoped, token, filters, groups)
                elif action == "state":
                    stream = protocol.hex_id(request.path_params["stream_id"])
                    epoch_text = request.query_params.get("epoch", "0")
                    epoch = protocol.integer(epoch_text)
                    protocol.require(epoch <= 2147483647, "invalid_epoch")
                    result = await run_in_threadpool(store.state, token, stream, epoch)
                else:
                    result = await run_in_threadpool(store.devices, token)
            finally:
                capacity.release()
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except protocol.ApiError as error:
            headers = {"Cache-Control": "no-store"}
            if error.status == 429:
                headers["Retry-After"] = "60"
            if error.status == 401:
                headers["WWW-Authenticate"] = "Bearer"
            return JSONResponse(dict(code=error.code, message=error.message, retryable=error.retryable), error.status, headers=headers)
        except psycopg.Error as error:
            # SQL diagnostics can contain user values. Log the SQLSTATE only.
            logging.error("database request failed SQLSTATE=%s", error.sqlstate)
            return JSONResponse(dict(code="database_unavailable", message="Retry later", retryable=True), 503,
                                headers={"Retry-After": "5", "Cache-Control": "no-store"})

    async def ingest(request):
        return await dispatch(request, "ingest")

    async def heartbeat(request):
        return await dispatch(request, "heartbeat")

    async def summary(request):
        return await dispatch(request, "summary")

    async def devices(request):
        return await dispatch(request, "devices")

    async def state(request):
        return await dispatch(request, "state")

    async def identity(request): return await dispatch(request, "identity")
    async def identities(request): return await dispatch(request, "identities")
    async def scoped_summary(request): return await dispatch(request, "scoped_summary")
    async def cycles(request):return await dispatch(request,"record_cycles" if request.method=="POST" else "cycle_history")
    async def observations(request):return await dispatch(request,'observe_boundaries')
    async def current_cycles(request):return await dispatch(request,'current_cycles')
    async def bucket_history(request):return await dispatch(request,'bucket_history')
    async def trend(request):return await dispatch(request,'trend')
    async def display_names(request):return await dispatch(request,'display_names')

    async def dashboard(request):
        name=request.path_params.get('asset','index.html')
        if name not in ('index.html','dashboard.js','dashboard.css'):
            return JSONResponse({'code':'not_found'},404)
        return FileResponse(Path(__file__).parent/'web'/name,headers={
            'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})

    async def live(request):
        return JSONResponse({"status": "ok", "service": "codex-usage-monitor", "version": "0.5.0"})

    async def ready(request):
        try:
            return JSONResponse(await run_in_threadpool(store.ready))
        except psycopg.Error:
            return JSONResponse({"status": "unavailable"}, 503)

    return Starlette(routes=[Route("/", dashboard), Route('/dashboard/{asset}',dashboard), Route('/v3/usage/trend',trend), Route("/health/live", live), Route("/health/ready", ready),
                            Route('/v3/accounts/display-names',display_names,methods=['POST']),
                            Route("/v2/identity", identity, methods=["POST"]), Route("/v2/identities", identities), Route("/v2/usage/summary", scoped_summary),
                            Route("/v2/usage/quota-cycles",cycles,methods=["GET","POST"]),
                            Route("/v3/usage/quota-observations",observations,methods=["POST"]),
                            Route("/v3/usage/quota-current",current_cycles),
                            Route("/v3/usage/quota-history",bucket_history),
                            Route("/v1/usage/batches", ingest, methods=["POST"]),
                            Route("/v1/usage/summary", summary), Route("/v1/devices/status", devices),
                            Route("/v1/devices/heartbeat", heartbeat, methods=["POST"]),
                            Route("/v1/streams/{stream_id}/state", state)])
