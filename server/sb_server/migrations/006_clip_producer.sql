-- Clips belong to the producer that uploaded them: one Windows worker cannot overwrite,
-- delete or serve another's clip by reusing an observation id.
ALTER TABLE clips ADD COLUMN producer_id uuid REFERENCES producers;
UPDATE clips c SET producer_id = w.producer_id
  FROM worker_records w WHERE w.kind = 'observation' AND w.id = c.observation_id;
DELETE FROM clips WHERE producer_id IS NULL;
ALTER TABLE clips ALTER COLUMN producer_id SET NOT NULL;
ALTER TABLE clips DROP CONSTRAINT clips_pkey;
ALTER TABLE clips ADD PRIMARY KEY (producer_id, observation_id, n);
