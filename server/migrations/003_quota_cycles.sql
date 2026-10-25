CREATE TABLE quota_cycles (
 tenant_id uuid NOT NULL REFERENCES tenants(id), cycle_id text NOT NULL,
 account_key text NOT NULL, bucket text NOT NULL CHECK(bucket IN ('primary','secondary')),
 duration_mins integer NOT NULL CHECK(duration_mins BETWEEN 1 AND 525600),
 starts_at timestamptz NOT NULL, resets_at timestamptz NOT NULL, ends_at timestamptz NOT NULL,
 first_observed_at timestamptz NOT NULL, last_observed_at timestamptz NOT NULL,
 bounds_source text NOT NULL DEFAULT 'reset_minus_duration',
 PRIMARY KEY(tenant_id,cycle_id), UNIQUE(tenant_id,account_key,bucket,resets_at),
 UNIQUE(tenant_id,account_key,bucket,starts_at),
 FOREIGN KEY(tenant_id,account_key) REFERENCES accounts(tenant_id,account_key),
 CHECK(starts_at < resets_at AND starts_at <= ends_at AND ends_at <= resets_at)
);
CREATE TABLE quota_cycle_observations (
 tenant_id uuid NOT NULL, cycle_id text NOT NULL, device_id uuid NOT NULL,
 first_observed_at timestamptz NOT NULL, last_observed_at timestamptz NOT NULL,
 PRIMARY KEY(tenant_id,cycle_id,device_id),
 FOREIGN KEY(tenant_id,cycle_id) REFERENCES quota_cycles(tenant_id,cycle_id),
 FOREIGN KEY(tenant_id,device_id) REFERENCES devices(tenant_id,id)
);
CREATE TABLE quota_cycle_totals (
 tenant_id uuid NOT NULL, cycle_id text NOT NULL, principal_key text NOT NULL, machine_key text NOT NULL,
 input_tokens numeric(30,0) NOT NULL DEFAULT 0, cached_input_tokens numeric(30,0) NOT NULL DEFAULT 0,
 output_tokens numeric(30,0) NOT NULL DEFAULT 0, reasoning_output_tokens numeric(30,0) NOT NULL DEFAULT 0,
 total_tokens numeric(30,0) GENERATED ALWAYS AS(input_tokens+output_tokens) STORED,
 computed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,cycle_id,principal_key,machine_key),
 FOREIGN KEY(tenant_id,cycle_id) REFERENCES quota_cycles(tenant_id,cycle_id),
 FOREIGN KEY(tenant_id,principal_key,machine_key) REFERENCES principals(tenant_id,principal_key,machine_key),
 CHECK(input_tokens>=0 AND output_tokens>=0 AND cached_input_tokens BETWEEN 0 AND input_tokens AND reasoning_output_tokens BETWEEN 0 AND output_tokens)
);
CREATE INDEX quota_cycles_account_time_idx ON quota_cycles(tenant_id,account_key,starts_at,ends_at);
INSERT INTO schema_version(version) VALUES(3);
