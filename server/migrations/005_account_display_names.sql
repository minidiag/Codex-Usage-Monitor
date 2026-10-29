-- Presentation metadata never participates in numeric identity or attribution.
CREATE TABLE account_display_names (
 tenant_id uuid NOT NULL, account_key text NOT NULL, principal_key text NOT NULL,
 display_name text NOT NULL CHECK(length(display_name) BETWEEN 1 AND 120),
 source_device_id uuid NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,account_key,principal_key),
 FOREIGN KEY(tenant_id,account_key) REFERENCES accounts(tenant_id,account_key),
 FOREIGN KEY(tenant_id,principal_key) REFERENCES principals(tenant_id,principal_key),
 FOREIGN KEY(tenant_id,source_device_id) REFERENCES devices(tenant_id,id)
);
INSERT INTO schema_version(version) VALUES(5);
