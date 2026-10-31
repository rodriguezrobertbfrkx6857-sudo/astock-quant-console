CREATE TABLE stage10_runs (
  result_id TEXT PRIMARY KEY,
  research_run_id TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  formal_hash TEXT NOT NULL,
  reference_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PASS','FAIL','INSUFFICIENT_DATA','NOT_COMPARABLE')),
  evidence_valid INTEGER NOT NULL CHECK (evidence_valid IN (0,1)),
  created_at TEXT NOT NULL,
  CHECK ((status = 'PASS' AND evidence_valid = 1) OR (status <> 'PASS' AND evidence_valid = 0)),
  UNIQUE(research_run_id, input_hash, formal_hash, reference_hash)
);

CREATE TABLE stage10_differences (
  result_id TEXT NOT NULL REFERENCES stage10_runs(result_id),
  difference_no INTEGER NOT NULL,
  on_date TEXT NOT NULL,
  category TEXT NOT NULL,
  comparison_key TEXT NOT NULL,
  formal_json TEXT NOT NULL,
  reference_json TEXT NOT NULL,
  PRIMARY KEY(result_id, difference_no)
);

CREATE TRIGGER stage10_runs_no_update BEFORE UPDATE ON stage10_runs BEGIN SELECT RAISE(ABORT, 'stage10 is append-only'); END;
CREATE TRIGGER stage10_runs_no_delete BEFORE DELETE ON stage10_runs BEGIN SELECT RAISE(ABORT, 'stage10 is append-only'); END;
CREATE TRIGGER stage10_differences_no_update BEFORE UPDATE ON stage10_differences BEGIN SELECT RAISE(ABORT, 'stage10 differences are append-only'); END;
CREATE TRIGGER stage10_differences_no_delete BEFORE DELETE ON stage10_differences BEGIN SELECT RAISE(ABORT, 'stage10 differences are append-only'); END;
