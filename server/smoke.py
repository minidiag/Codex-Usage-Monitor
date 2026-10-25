"""Exercise the running API with a temporary tenant; always clean synthetic rows."""
import json
import os
import secrets
import urllib.error
import urllib.request
import uuid
from datetime import datetime

from server.protocol import UTC, iso, token_hash
from server.store import Store
from server.tests.helpers import make_batch


def main():
    store = Store(os.environ["OWNER_DATABASE_URL"])
    tenant, device = uuid.uuid4(), uuid.uuid4()
    read_token, write_token = secrets.token_hex(32), secrets.token_hex(32)
    base = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8080")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, token=None, data=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        try:
            with opener.open(req, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    try:
        with store.connect() as db:
            db.execute("INSERT INTO tenants(id) VALUES (%s)", (tenant,))
            db.execute("INSERT INTO devices(tenant_id,id) VALUES (%s,%s)", (tenant, device))
            for token, scope, owner in ((read_token, "read", None), (write_token, "write", device)):
                db.execute("INSERT INTO credentials(token_hash,tenant_id,device_id,scope) VALUES (%s,%s,%s,%s)",
                           (token_hash(token), tenant, owner, scope))
        assert request("/v1/usage/summary")[0] == 401
        assert request("/v1/usage/summary", write_token)[0] == 403
        heartbeat = dict(queued_records="0", oldest_queued_at=None, last_scan_at=iso(datetime.now(UTC)), quality="complete", quality_reasons=[])
        assert request("/v1/devices/heartbeat", write_token, heartbeat)[0] == 200
        data = make_batch()
        status, ack = request("/v1/usage/batches", write_token, data)
        assert status == 200 and ack["accepted"] == 2, (status, ack)
        status, ack = request("/v1/usage/batches", write_token, data)
        assert status == 200 and ack["duplicates"] == 2, (status, ack)
        status, summary = request("/v1/usage/summary", read_token)
        assert status == 200 and summary["windows"][0]["totals"]["total"] == "150", (status, summary)
        assert summary["quality"] == "complete", summary["quality_reasons"]
        status, state = request("/v1/streams/" + "a"*64 + "/state", write_token)
        assert status == 200 and state["processed_through"] == "1"
        status, devices = request("/v1/devices/status", read_token)
        assert status == 200 and len(devices["devices"]) == 1
        data["records"][1]["counters"]["input"] = "101"
        assert request("/v1/usage/batches", write_token, data)[0] == 409
        print("Running API smoke passed: authentication, scopes, heartbeat, ingestion, duplicate, summary=150, state, devices, conflict.")
    finally:
        with store.connect() as db:
            for table in ("ingest_conflicts", "usage_deltas", "usage_events", "streams", "credentials", "devices", "tenants"):
                field = "id" if table == "tenants" else "tenant_id"
                db.execute(f"DELETE FROM {table} WHERE {field}=%s", (tenant,))
        print("Temporary smoke tenant and all synthetic records removed.")


if __name__ == "__main__":
    main()
