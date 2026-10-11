-- Milestone 4: curated memories. Created from the worker's extracted facts/decisions/ideas;
-- afterwards the server is authoritative and extraction replays never touch them.
ALTER TABLE change_log ALTER COLUMN entity_id TYPE text;  -- memory ids are router event ids

CREATE TABLE memories (
  id text PRIMARY KEY,                -- the worker item id (router event id)
  kind text NOT NULL CHECK (kind IN ('fact', 'decision', 'idea')),
  text text NOT NULL,                 -- current, possibly user-corrected
  state text NOT NULL CHECK (state IN ('proposed', 'confirmed', 'archived')),
  original jsonb NOT NULL,            -- the extraction as received, never changed
  evidence jsonb,                     -- source excerpt and archive reference, copied at import
  revision integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);
-- Revision history is change_log: one full snapshot per committed revision.
