-- Additive migration: legacy numeric events and identities remain unchanged.
CREATE TABLE accounts (
 tenant_id uuid NOT NULL REFERENCES tenants(id), account_key text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,account_key), CHECK(account_key ~ '^[0-9a-f]{64}$')
);
CREATE TABLE machines (
 tenant_id uuid NOT NULL REFERENCES tenants(id), machine_key text NOT NULL,
 PRIMARY KEY(tenant_id,machine_key), CHECK(machine_key ~ '^[0-9a-f]{64}$')
);
CREATE TABLE principals (
 tenant_id uuid NOT NULL, principal_key text NOT NULL, machine_key text NOT NULL,
 PRIMARY KEY(tenant_id,principal_key), UNIQUE(tenant_id,principal_key,machine_key),
 FOREIGN KEY(tenant_id,machine_key) REFERENCES machines(tenant_id,machine_key),
 CHECK(principal_key ~ '^[0-9a-f]{64}$')
);
CREATE TABLE device_subjects (
 tenant_id uuid NOT NULL, device_id uuid NOT NULL, principal_key text NOT NULL, machine_key text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,device_id),
 FOREIGN KEY(tenant_id,device_id) REFERENCES devices(tenant_id,id),
 FOREIGN KEY(tenant_id,principal_key,machine_key) REFERENCES principals(tenant_id,principal_key,machine_key)
);
ALTER TABLE credentials ADD COLUMN reader_device_id uuid;
ALTER TABLE credentials ADD FOREIGN KEY(tenant_id,reader_device_id) REFERENCES devices(tenant_id,id);
ALTER TABLE usage_events ADD COLUMN attribution_claim jsonb;
CREATE TABLE event_subjects (
 tenant_id uuid NOT NULL, stream_id text NOT NULL, epoch integer NOT NULL, source_seq bigint NOT NULL,
 account_key text, principal_key text, machine_key text,
 status text NOT NULL CHECK(status IN ('confirmed','account_unknown','unknown')),
 PRIMARY KEY(tenant_id,stream_id,epoch,source_seq),
 FOREIGN KEY(tenant_id,stream_id,epoch,source_seq) REFERENCES usage_events(tenant_id,stream_id,epoch,source_seq),
 FOREIGN KEY(tenant_id,account_key) REFERENCES accounts(tenant_id,account_key),
 FOREIGN KEY(tenant_id,principal_key,machine_key) REFERENCES principals(tenant_id,principal_key,machine_key)
);
CREATE TABLE source_observations (
 tenant_id uuid NOT NULL, event_id text NOT NULL, device_id uuid NOT NULL, claimed_account text,
 first_seen_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,event_id,device_id),
 FOREIGN KEY(tenant_id,event_id) REFERENCES usage_events(tenant_id,event_id),
 FOREIGN KEY(tenant_id,device_id) REFERENCES devices(tenant_id,id)
);
CREATE INDEX event_subjects_account_idx ON event_subjects(tenant_id,account_key);
CREATE INDEX event_subjects_principal_idx ON event_subjects(tenant_id,principal_key);
INSERT INTO schema_version(version) VALUES(2);
