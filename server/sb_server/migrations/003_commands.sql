-- Milestone 3: phone speaker/clarification commands applied by the Windows worker, and review clips.
ALTER TABLE worker_records DROP CONSTRAINT worker_records_kind_check,
  ADD CONSTRAINT worker_records_kind_check CHECK (kind IN ('source', 'item', 'person', 'observation', 'clarification'));

-- Server acceptance (pending_worker) and worker application are separate durable states.
CREATE TABLE commands (
  id uuid PRIMARY KEY,
  position bigserial UNIQUE,  -- delivery order to the worker
  device_id uuid NOT NULL REFERENCES devices,
  payload jsonb NOT NULL,
  state text NOT NULL CHECK (state IN ('pending_worker', 'applied_worker', 'failed_worker')),
  error text,
  revision integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX commands_pending ON commands (position) WHERE state = 'pending_worker';

-- ponytail: clips in Postgres (small WAVs, few at a time, covered by pg_dumpall); move to
-- a volume if the review backlog grows.
CREATE TABLE clips (
  observation_id text NOT NULL,
  n integer NOT NULL,
  sha256 text NOT NULL,
  bytes bytea NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (observation_id, n)
);
