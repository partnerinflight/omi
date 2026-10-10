-- Milestone 1: devices, auth, todos, ordered change log, operation receipts.
-- ponytail: single owner, so rows carry no owner_id; add one before a second account exists.

-- One row. epoch changes on restore so devices re-bootstrap (sync invariant 8 / backup rule).
-- seq is the last allocated change-log position; writers lock this row, so commits are ordered.
CREATE TABLE meta (
  id boolean PRIMARY KEY DEFAULT true CHECK (id),
  epoch uuid NOT NULL,
  seq bigint NOT NULL DEFAULT 0
);

CREATE TABLE pairing_codes (
  code_hash text PRIMARY KEY,
  device_name text NOT NULL,
  expires_at timestamptz NOT NULL,
  used_at timestamptz
);

CREATE TABLE devices (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz
);

-- Only hashes are stored. A used refresh token is kept so reuse can be detected.
CREATE TABLE tokens (
  hash text PRIMARY KEY,
  device_id uuid NOT NULL REFERENCES devices,
  kind text NOT NULL CHECK (kind IN ('access', 'refresh')),
  expires_at timestamptz NOT NULL,
  used_at timestamptz
);
CREATE INDEX tokens_device ON tokens (device_id);

CREATE TABLE todos (
  id uuid PRIMARY KEY,
  title text NOT NULL,
  state text NOT NULL CHECK (state IN ('open', 'completed', 'dismissed')),
  revision integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

-- Full entity snapshot per committed revision; clients replay in seq order.
CREATE TABLE change_log (
  seq bigint PRIMARY KEY,
  entity_type text NOT NULL,
  entity_id uuid NOT NULL,
  revision integer NOT NULL,
  data jsonb NOT NULL,
  committed_at timestamptz NOT NULL DEFAULT now()
);

-- Every outcome is stored so a retried operation id returns the identical result.
CREATE TABLE receipts (
  operation_id uuid PRIMARY KEY,
  device_id uuid NOT NULL REFERENCES devices,
  payload_hash text NOT NULL,
  result jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO meta (epoch) VALUES (gen_random_uuid());
