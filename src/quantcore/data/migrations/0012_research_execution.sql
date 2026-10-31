CREATE TABLE research_runs (
  research_run_id TEXT PRIMARY KEY,
  queue_item_id TEXT NOT NULL REFERENCES research_queue(queue_item_id),
  strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(strategy_version_id),
  protocol_hash TEXT NOT NULL REFERENCES research_protocols(protocol_hash),
  snapshot_hash TEXT NOT NULL,
  oos_receipt_id TEXT NOT NULL REFERENCES access_receipts(receipt_id),
  status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','FAILED','INSUFFICIENT_DATA')),
  execution_scope TEXT NOT NULL CHECK(execution_scope IN ('CONTROL','STRATEGY')),
  implementation_hash TEXT NOT NULL,
  dataset_manifest_json TEXT NOT NULL,
  window_json TEXT NOT NULL,
  cost_model TEXT NOT NULL,
  execution_rules TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  robustness_json TEXT NOT NULL,
  evidence_ref TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE stage09_evidence (
  result_id TEXT PRIMARY KEY REFERENCES stage09_results(result_id),
  research_run_id TEXT NOT NULL UNIQUE REFERENCES research_runs(research_run_id),
  strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(strategy_version_id),
  protocol_hash TEXT NOT NULL REFERENCES research_protocols(protocol_hash),
  snapshot_hash TEXT NOT NULL,
  oos_receipt_id TEXT NOT NULL REFERENCES access_receipts(receipt_id),
  dataset_manifest_json TEXT NOT NULL,
  window_json TEXT NOT NULL,
  cost_model TEXT NOT NULL,
  execution_rules TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  robustness_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX research_runs_queue_idx ON research_runs(queue_item_id,created_at);
CREATE INDEX stage09_evidence_protocol_idx ON stage09_evidence(protocol_hash,created_at);

CREATE TRIGGER research_runs_no_update BEFORE UPDATE ON research_runs BEGIN SELECT RAISE(ABORT, 'research runs are append-only'); END;
CREATE TRIGGER research_runs_no_delete BEFORE DELETE ON research_runs BEGIN SELECT RAISE(ABORT, 'research runs are append-only'); END;
CREATE TRIGGER stage09_evidence_no_update BEFORE UPDATE ON stage09_evidence BEGIN SELECT RAISE(ABORT, 'Stage09 evidence is append-only'); END;
CREATE TRIGGER stage09_evidence_no_delete BEFORE DELETE ON stage09_evidence BEGIN SELECT RAISE(ABORT, 'Stage09 evidence is append-only'); END;
