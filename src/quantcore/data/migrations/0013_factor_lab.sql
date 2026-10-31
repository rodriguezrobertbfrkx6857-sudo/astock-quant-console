CREATE TABLE factor_versions (
  factor_version_id TEXT PRIMARY KEY,
  factor_id TEXT NOT NULL,
  version TEXT NOT NULL,
  family_id TEXT NOT NULL,
  definition_json TEXT NOT NULL,
  source_code_hash TEXT NOT NULL,
  parameter_hash TEXT NOT NULL,
  dependency_json TEXT NOT NULL,
  universe_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('REGISTERED','ACTIVE','RETIRED')),
  created_at TEXT NOT NULL,
  UNIQUE(factor_id,version)
);

CREATE TABLE factor_values (
  evaluation_id TEXT NOT NULL,
  factor_version_id TEXT NOT NULL REFERENCES factor_versions(factor_version_id),
  on_date TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  value TEXT,
  forward_return TEXT,
  available_at TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  PRIMARY KEY(evaluation_id,asset_id,on_date)
);

CREATE TABLE factor_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  factor_version_id TEXT NOT NULL REFERENCES factor_versions(factor_version_id),
  snapshot_hash TEXT NOT NULL,
  oos_receipt_id TEXT NOT NULL REFERENCES access_receipts(receipt_id),
  status TEXT NOT NULL CHECK(status IN ('COMPLETED','INSUFFICIENT_DATA','REJECT','FAILED')),
  metrics_json TEXT NOT NULL,
  evidence_ref TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(factor_version_id,snapshot_hash)
);

CREATE INDEX factor_values_lookup_idx
  ON factor_values(factor_version_id,on_date,asset_id);
CREATE INDEX factor_evaluations_factor_idx
  ON factor_evaluations(factor_version_id,created_at);

CREATE TRIGGER factor_versions_no_update BEFORE UPDATE ON factor_versions BEGIN SELECT RAISE(ABORT, 'factor versions are append-only'); END;
CREATE TRIGGER factor_versions_no_delete BEFORE DELETE ON factor_versions BEGIN SELECT RAISE(ABORT, 'factor versions are append-only'); END;
CREATE TRIGGER factor_values_no_update BEFORE UPDATE ON factor_values BEGIN SELECT RAISE(ABORT, 'factor values are append-only'); END;
CREATE TRIGGER factor_values_no_delete BEFORE DELETE ON factor_values BEGIN SELECT RAISE(ABORT, 'factor values are append-only'); END;
CREATE TRIGGER factor_evaluations_no_update BEFORE UPDATE ON factor_evaluations BEGIN SELECT RAISE(ABORT, 'factor evaluations are append-only'); END;
CREATE TRIGGER factor_evaluations_no_delete BEFORE DELETE ON factor_evaluations BEGIN SELECT RAISE(ABORT, 'factor evaluations are append-only'); END;
