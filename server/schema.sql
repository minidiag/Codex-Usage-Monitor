-- Codex Usage Monitor migration 001 (based on reviewed DD core)
-- PostgreSQL 18; service role permissions are applied by server.admin.
CREATE TABLE tenants (
  id uuid PRIMARY KEY,
  timezone text NOT NULL DEFAULT 'Asia/Shanghai',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE devices (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  id uuid NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','revoked')),
  last_seen_at timestamptz,
  queued_records bigint NOT NULL DEFAULT 0 CHECK (queued_records >= 0),
  oldest_queued_at timestamptz,
  last_scan_at timestamptz,
  health_quality text NOT NULL DEFAULT 'unknown' CHECK (health_quality IN ('complete','partial','unknown')),
  health_reasons text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id,id)
);
CREATE TABLE streams (
  tenant_id uuid NOT NULL,
  stream_id text NOT NULL CHECK (stream_id ~ '^[0-9a-f]{64}$'),
  epoch integer NOT NULL CHECK (epoch >= 0),
  anchor_id text NOT NULL CHECK (anchor_id ~ '^[0-9a-f]{64}$'),
  owner_device_id uuid NOT NULL,
  baseline_reason text NOT NULL CHECK (baseline_reason IN ('known_zero','capture_start','inherited_history')),
  durable_through bigint NOT NULL DEFAULT -1 CHECK (durable_through >= -1),
  processed_through bigint NOT NULL DEFAULT -1 CHECK (processed_through >= -1),
  quality text NOT NULL DEFAULT 'unknown' CHECK (quality IN ('complete','partial','unknown')),
  quality_reasons text[] NOT NULL DEFAULT '{}',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,stream_id,epoch),
  FOREIGN KEY (tenant_id,owner_device_id) REFERENCES devices(tenant_id,id),
  CHECK (processed_through <= durable_through)
);
CREATE TABLE usage_events (
  tenant_id uuid NOT NULL,
  stream_id text NOT NULL,
  epoch integer NOT NULL,
  source_seq bigint NOT NULL CHECK (source_seq >= 0),
  event_id text NOT NULL CHECK (event_id ~ '^[0-9a-f]{64}$'),
  semantic_hash text NOT NULL CHECK (semantic_hash ~ '^[0-9a-f]{64}$'),
  kind text NOT NULL CHECK (kind IN ('baseline','snapshot')),
  event_time timestamptz NOT NULL,
  observed_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  received_device_id uuid NOT NULL,
  model text NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
  parser_version text NOT NULL CHECK (length(parser_version) BETWEEN 1 AND 64),
  cumulative_input bigint NOT NULL CHECK (cumulative_input >= 0),
  cumulative_cached_input bigint NOT NULL CHECK (cumulative_cached_input >= 0),
  cumulative_output bigint NOT NULL CHECK (cumulative_output >= 0),
  cumulative_reasoning_output bigint NOT NULL CHECK (cumulative_reasoning_output >= 0),
  processing_state text NOT NULL DEFAULT 'pending' CHECK (processing_state IN ('pending','processed','quarantined')),
  PRIMARY KEY (tenant_id,stream_id,epoch,source_seq),
  UNIQUE (tenant_id,event_id),
  FOREIGN KEY (tenant_id,stream_id,epoch) REFERENCES streams(tenant_id,stream_id,epoch),
  FOREIGN KEY (tenant_id,received_device_id) REFERENCES devices(tenant_id,id),
  CHECK (cumulative_cached_input <= cumulative_input),
  CHECK (cumulative_reasoning_output <= cumulative_output),
  CHECK ((source_seq = 0 AND kind = 'baseline') OR (source_seq > 0 AND kind = 'snapshot'))
);
CREATE TABLE usage_deltas (
  tenant_id uuid NOT NULL,
  stream_id text NOT NULL,
  epoch integer NOT NULL,
  source_seq bigint NOT NULL CHECK (source_seq > 0),
  event_time timestamptz NOT NULL,
  model text NOT NULL,
  input_tokens bigint NOT NULL CHECK (input_tokens >= 0),
  cached_input_tokens bigint NOT NULL CHECK (cached_input_tokens >= 0),
  output_tokens bigint NOT NULL CHECK (output_tokens >= 0),
  reasoning_output_tokens bigint NOT NULL CHECK (reasoning_output_tokens >= 0),
  total_tokens numeric(30,0) GENERATED ALWAYS AS (input_tokens::numeric + output_tokens::numeric) STORED,
  calculation_version integer NOT NULL DEFAULT 1 CHECK (calculation_version > 0),
  PRIMARY KEY (tenant_id,stream_id,epoch,source_seq),
  FOREIGN KEY (tenant_id,stream_id,epoch,source_seq) REFERENCES usage_events(tenant_id,stream_id,epoch,source_seq),
  CHECK (cached_input_tokens <= input_tokens),
  CHECK (reasoning_output_tokens <= output_tokens)
);
CREATE INDEX usage_deltas_window_idx ON usage_deltas (tenant_id,event_time);
CREATE INDEX usage_events_time_idx ON usage_events (tenant_id,event_time);
CREATE TABLE ingest_conflicts (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  stream_id text NOT NULL,
  epoch integer NOT NULL,
  source_seq bigint NOT NULL,
  error_code text NOT NULL,
  expected_hash text,
  received_hash text,
  recorded_at timestamptz NOT NULL DEFAULT now()
);
-- A conflict audit must be recorded separately after a rejected ingest transaction rolls back.
-- Credentials are managed outside this core schema and never stored as plaintext.
-- Delta derivation and stream watermarks must be updated together in one transaction.
-- SELECT ... FROM streams ... FOR UPDATE before checking conflicts and advancing.
-- Never ON CONFLICT DO UPDATE cumulative values; compare semantic hashes and reject mismatches.

CREATE TABLE credentials (
  token_hash text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  device_id uuid,
  scope text NOT NULL CHECK (scope IN ('read','write')),
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id,device_id) REFERENCES devices(tenant_id,id),
  CHECK ((scope='write' AND device_id IS NOT NULL) OR (scope='read' AND device_id IS NULL))
);
CREATE TABLE schema_version (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
INSERT INTO schema_version(version) VALUES (1);