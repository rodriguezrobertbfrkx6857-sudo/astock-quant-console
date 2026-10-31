CREATE TABLE paper_qualifications (
  qualification_id TEXT PRIMARY KEY,
  strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(strategy_version_id),
  stage09_result_id TEXT NOT NULL REFERENCES stage09_results(result_id),
  stage10_result_id TEXT NOT NULL REFERENCES stage10_runs(result_id),
  protocol_hash TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  UNIQUE(strategy_version_id, stage09_result_id, stage10_result_id)
);

CREATE TABLE qualification_events (
  event_id TEXT PRIMARY KEY,
  qualification_id TEXT NOT NULL REFERENCES paper_qualifications(qualification_id),
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TRIGGER paper_qualifications_no_update BEFORE UPDATE ON paper_qualifications BEGIN SELECT RAISE(ABORT, 'qualifications are immutable'); END;
CREATE TRIGGER paper_qualifications_no_delete BEFORE DELETE ON paper_qualifications BEGIN SELECT RAISE(ABORT, 'qualifications are immutable'); END;
CREATE TRIGGER qualification_events_no_update BEFORE UPDATE ON qualification_events BEGIN SELECT RAISE(ABORT, 'qualification events are append-only'); END;
CREATE TRIGGER qualification_events_no_delete BEFORE DELETE ON qualification_events BEGIN SELECT RAISE(ABORT, 'qualification events are append-only'); END;
