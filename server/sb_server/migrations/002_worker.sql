-- Milestone 2: read-only shadow import from the Windows worker (docs/mobile/implementation-plan.md).
-- Producers authenticate with their own long-lived token; device tokens cannot reach worker routes.
CREATE TABLE producers (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  token_hash text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  last_seen_at timestamptz,
  status jsonb  -- counts only, no content
);

-- Latest exported state per worker record. Model output in data is untrusted text.
CREATE TABLE worker_records (
  producer_id uuid NOT NULL REFERENCES producers,
  kind text NOT NULL CHECK (kind IN ('source', 'item', 'person', 'observation')),
  id text NOT NULL,
  hash text NOT NULL,
  data jsonb,
  deleted_at timestamptz,
  received_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (producer_id, kind, id)
);
CREATE INDEX worker_records_recent ON worker_records (kind, updated_at DESC);
