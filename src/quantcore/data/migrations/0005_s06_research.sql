CREATE TABLE strategy_families (
  family_id TEXT PRIMARY KEY,
  control_only INTEGER NOT NULL CHECK (control_only IN (0,1))
);

CREATE TABLE research_protocols (
  protocol_hash TEXT PRIMARY KEY,
  protocol_id TEXT NOT NULL UNIQUE,
  family_id TEXT NOT NULL REFERENCES strategy_families(family_id),
  protocol_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE strategy_versions (
  strategy_version_id TEXT PRIMARY KEY,
  family_id TEXT NOT NULL REFERENCES strategy_families(family_id),
  protocol_hash TEXT NOT NULL REFERENCES research_protocols(protocol_hash),
  source_code_hash TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  universe_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE research_queue (
  queue_item_id TEXT PRIMARY KEY,
  strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(strategy_version_id),
  protocol_hash TEXT NOT NULL REFERENCES research_protocols(protocol_hash),
  priority INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PLANNED','BLOCKED_DATA','READY','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  attempt INTEGER NOT NULL CHECK (attempt >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE stage09_results (
  result_id TEXT PRIMARY KEY,
  queue_item_id TEXT NOT NULL REFERENCES research_queue(queue_item_id),
  protocol_hash TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  rating TEXT NOT NULL CHECK (rating IN ('INVALID','INSUFFICIENT_DATA','REJECT','RESEARCH','CANDIDATE')),
  reasons_json TEXT NOT NULL,
  primary_metric TEXT,
  effective_trades INTEGER NOT NULL CHECK (effective_trades >= 0),
  validation_blocks INTEGER NOT NULL CHECK (validation_blocks >= 0),
  created_at TEXT NOT NULL,
  UNIQUE(queue_item_id, protocol_hash, snapshot_hash)
);

CREATE TRIGGER research_protocols_no_update BEFORE UPDATE ON research_protocols BEGIN SELECT RAISE(ABORT, 'protocols are append-only'); END;
CREATE TRIGGER research_protocols_no_delete BEFORE DELETE ON research_protocols BEGIN SELECT RAISE(ABORT, 'protocols are append-only'); END;
CREATE TRIGGER strategy_versions_no_update BEFORE UPDATE ON strategy_versions BEGIN SELECT RAISE(ABORT, 'strategy versions are append-only'); END;
CREATE TRIGGER strategy_versions_no_delete BEFORE DELETE ON strategy_versions BEGIN SELECT RAISE(ABORT, 'strategy versions are append-only'); END;
CREATE TRIGGER stage09_results_no_update BEFORE UPDATE ON stage09_results BEGIN SELECT RAISE(ABORT, 'Stage09 is append-only'); END;
CREATE TRIGGER stage09_results_no_delete BEFORE DELETE ON stage09_results BEGIN SELECT RAISE(ABORT, 'Stage09 is append-only'); END;
