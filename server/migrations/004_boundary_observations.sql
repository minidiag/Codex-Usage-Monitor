-- Preserve all legacy cycles and numeric usage. New observations never close them.
CREATE TABLE quota_boundary_samples (
 tenant_id uuid NOT NULL, account_key text NOT NULL,
 bucket text NOT NULL CHECK(bucket IN ('primary','secondary')), device_id uuid NOT NULL,
 observed_at timestamptz NOT NULL, resets_at timestamptz NOT NULL, duration_mins integer NOT NULL,
 PRIMARY KEY(tenant_id,account_key,bucket,device_id,observed_at),
 FOREIGN KEY(tenant_id,account_key) REFERENCES accounts(tenant_id,account_key),
 FOREIGN KEY(tenant_id,device_id) REFERENCES devices(tenant_id,id)
);
CREATE TABLE quota_boundary_states (
 tenant_id uuid NOT NULL, account_key text NOT NULL, bucket text NOT NULL,
 state jsonb NOT NULL,
 PRIMARY KEY(tenant_id,account_key,bucket),
 FOREIGN KEY(tenant_id,account_key) REFERENCES accounts(tenant_id,account_key)
);
CREATE INDEX quota_boundary_recent_idx ON quota_boundary_samples(tenant_id,account_key,bucket,observed_at DESC);
-- Adopt only a currently effective legacy cycle. Historical fragments stay intact.
INSERT INTO quota_boundary_states
SELECT DISTINCT ON (tenant_id,account_key,bucket) tenant_id,account_key,bucket,
 jsonb_build_object('current',jsonb_build_object('id',cycle_id,'reset',extract(epoch FROM resets_at),
 'duration',duration_mins),'status','available','observed_at',extract(epoch FROM last_observed_at))
FROM quota_cycles WHERE starts_at<=now() AND ends_at>now()
ORDER BY tenant_id,account_key,bucket,last_observed_at DESC;
INSERT INTO schema_version(version) VALUES(4);
