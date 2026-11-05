-- Where each new task was said: a short clip per router task item, cut on Windows before the
-- recording is deleted. Kept while its todo is open (app.worker_events prunes the rest).
CREATE TABLE task_clips (
  producer_id uuid NOT NULL REFERENCES producers,
  item_id text NOT NULL,  -- router event id = worker_records item id = todos.legacy_id
  sha256 text NOT NULL,
  bytes bytea NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (producer_id, item_id)
);
