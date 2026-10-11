-- Milestone 5: import canonical todos from ToDos/Tasks.md and switch authority to the server.
ALTER TABLE todos
  ADD COLUMN legacy_id text UNIQUE,      -- the %% router:ID %% marker; id = uuid5(LEGACY_NS, legacy_id)
  ADD COLUMN snoozed_until date,         -- only while open
  ADD COLUMN notes text;                 -- indented lines under the row, kept verbatim

-- Who may change imported todos. 'legacy': ToDos/Tasks.md + Hermes, imports allowed, the
-- app cannot edit them. 'server': the API, imports refused (stale old writers cannot win).
CREATE TABLE authority (
  domain text PRIMARY KEY CHECK (domain IN ('todos')),
  mode text NOT NULL CHECK (mode IN ('legacy', 'server')),
  source_hash text,                       -- sha256 of the Tasks.md bytes at the latest import
  migration_id text,
  switched_at timestamptz
);
INSERT INTO authority (domain, mode) VALUES ('todos', 'legacy');
