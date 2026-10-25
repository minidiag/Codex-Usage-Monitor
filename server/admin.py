"""Local-only operations. Credentials are written to a private file, never stdout."""
import argparse
import json
import os
from pathlib import Path
import secrets
import uuid

import psycopg
from psycopg import sql

from server.protocol import token_hash


def migrate(dsn, schema):
    with psycopg.connect(dsn) as db:
        db.execute("SELECT pg_advisory_xact_lock(728049131)")
        exists = db.execute("SELECT to_regclass('public.schema_version')").fetchone()[0]
        if exists:
            version = db.execute("SELECT max(version) FROM schema_version").fetchone()[0]
            if version not in (1, 2, 3):
                raise RuntimeError("Unexpected database schema version")
        else:
            db.execute(Path(schema).read_text(encoding="utf-8"))
            version = 1
        if version == 1:
            db.execute((Path(schema).parent/"migrations/002_accounts.sql").read_text(encoding="utf-8"))
            version=2
        if version==2:
            db.execute((Path(schema).parent/"migrations/003_quota_cycles.sql").read_text(encoding="utf-8"))


def app_role(dsn, password):
    with psycopg.connect(dsn) as db:
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='usage_app'").fetchone():
            db.execute(sql.SQL("CREATE ROLE usage_app LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(sql.Literal(password)))
        db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        db.execute("GRANT CONNECT ON DATABASE codex_usage TO usage_app")
        db.execute("GRANT USAGE ON SCHEMA public TO usage_app")
        db.execute("GRANT SELECT ON tenants,devices,streams,usage_events,usage_deltas,credentials TO usage_app")
        db.execute("GRANT INSERT ON streams,usage_events,usage_deltas,ingest_conflicts TO usage_app")
        db.execute("GRANT UPDATE(last_seen_at,queued_records,oldest_queued_at,last_scan_at,health_quality,health_reasons) ON devices TO usage_app")
        db.execute("GRANT UPDATE(durable_through,processed_through,quality,quality_reasons,updated_at) ON streams TO usage_app")
        db.execute("GRANT UPDATE(processing_state) ON usage_events TO usage_app")
        db.execute("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO usage_app")
        db.execute("GRANT SELECT,INSERT ON accounts,machines,principals,device_subjects,event_subjects,source_observations TO usage_app")
        db.execute("GRANT SELECT,INSERT ON quota_cycles,quota_cycle_observations,quota_cycle_totals TO usage_app")
        db.execute("GRANT UPDATE(ends_at,first_observed_at,last_observed_at) ON quota_cycles TO usage_app")
        db.execute("GRANT UPDATE(first_observed_at,last_observed_at) ON quota_cycle_observations TO usage_app")
        db.execute("GRANT UPDATE(input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens,computed_at) ON quota_cycle_totals TO usage_app")


def provision(dsn, output, base_url):
    path = Path(output)
    if path.exists():
        raise RuntimeError("Credential output already exists; refusing to replace it")
    tenant, device = str(uuid.uuid4()), str(uuid.uuid4())
    read_token, write_token = "cur_" + secrets.token_urlsafe(36), "cuw_" + secrets.token_urlsafe(36)
    result = dict(base_url=base_url, tenant_id=tenant, device_id=device, read_token=read_token,
                  write_token=write_token, identity_key=secrets.token_hex(32))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Creating before the transaction avoids committing credentials that cannot be delivered.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(result, handle)
        with psycopg.connect(dsn) as db:
            db.execute("INSERT INTO tenants(id) VALUES (%s)", (tenant,))
            db.execute("INSERT INTO devices(tenant_id,id) VALUES (%s,%s)", (tenant, device))
            for token, scope, owner in [(read_token, "read", None), (write_token, "write", device)]:
                db.execute("INSERT INTO credentials(token_hash,tenant_id,device_id,scope) VALUES (%s,%s,%s,%s)",
                           (token_hash(token), tenant, owner, scope))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    print("Provisioned one tenant and one device; credentials saved privately.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["migrate", "app-role", "provision", "revoke", "add-device"])
    parser.add_argument("--output")
    parser.add_argument("--token-hash")
    parser.add_argument("--from-credentials")
    parser.add_argument("--base-url", default="https://YOUR_SERVER_HOST/codex-usage")
    args = parser.parse_args()
    dsn = os.environ["DATABASE_URL"]
    if args.command == "migrate":
        migrate(dsn, Path(__file__).with_name("schema.sql"))
        print("Schema version 3 ready.")
    elif args.command == "app-role":
        app_role(dsn, os.environ["APP_DB_PASSWORD"])
        print("Restricted application role ready.")
    elif args.command == "provision":
        if not args.output:
            parser.error("--output is required")
        provision(dsn, args.output, args.base_url)
    elif args.command == "add-device":
        if not args.output or not args.from_credentials: parser.error("--output and --from-credentials required")
        enroll_device(dsn,args.from_credentials,args.output)
    else:
        if not args.token_hash or len(args.token_hash) != 64:
            parser.error("--token-hash is required")
        with psycopg.connect(dsn) as db:
            result = db.execute("UPDATE credentials SET revoked_at=now() WHERE token_hash=%s", (args.token_hash,))
        print("Revoked credentials:", result.rowcount)


def enroll_device(dsn,source,output):
    template = json.loads(Path(source).read_text(encoding="utf-8"))
    device, read, write = str(uuid.uuid4()), "cur_"+secrets.token_urlsafe(36), "cuw_"+secrets.token_urlsafe(36)
    result = dict(template,device_id=device,read_token=read,write_token=write)
    path = Path(output)
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(descriptor,"w",encoding="utf-8") as handle: json.dump(result,handle)
        with psycopg.connect(dsn) as db:
            tenant = template["tenant_id"]
            db.execute("INSERT INTO devices(tenant_id,id) VALUES(%s,%s)",(tenant,device))
            db.execute("INSERT INTO credentials(token_hash,tenant_id,device_id,scope) VALUES(%s,%s,%s,'write')",(token_hash(write),tenant,device))
            db.execute("INSERT INTO credentials(token_hash,tenant_id,scope,reader_device_id) VALUES(%s,%s,'read',%s)",(token_hash(read),tenant,device))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    print("New collector credentials saved privately; reads limited to its Windows principal, all Codex accounts allowed.")


if __name__ == "__main__":
    main()
