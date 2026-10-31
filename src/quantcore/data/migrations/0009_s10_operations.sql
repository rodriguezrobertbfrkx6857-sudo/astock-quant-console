CREATE TABLE shadow_acceptance_runs (
  shadow_run_id TEXT PRIMARY KEY,
  target_sessions INTEGER NOT NULL CHECK(target_sessions >= 20),
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','COMPLETE','FAILED')),
  started_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE shadow_acceptance_days (
  shadow_run_id TEXT NOT NULL REFERENCES shadow_acceptance_runs(shadow_run_id),
  business_date TEXT NOT NULL,
  trading_session_evidence TEXT NOT NULL,
  reconciliation_status TEXT NOT NULL CHECK(reconciliation_status IN ('PASS','FAIL')),
  duplicate_fills INTEGER NOT NULL CHECK(duplicate_fills >= 0),
  permission_violations INTEGER NOT NULL CHECK(permission_violations >= 0),
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(shadow_run_id,business_date)
);

CREATE TABLE recovery_exercises (
  recovery_id TEXT PRIMARY KEY,
  backup_manifest_hash TEXT NOT NULL,
  restored_control_hash TEXT NOT NULL,
  restored_accounting_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PASS','FAIL')),
  evidence_ref TEXT NOT NULL,
  completed_at TEXT NOT NULL
);
