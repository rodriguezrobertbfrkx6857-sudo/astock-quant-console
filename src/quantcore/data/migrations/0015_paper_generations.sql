CREATE TABLE paper_generations (
  generation_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL UNIQUE,
  strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(strategy_version_id),
  qualification_id TEXT NOT NULL REFERENCES paper_qualifications(qualification_id),
  data_profile TEXT NOT NULL CHECK(data_profile IN ('ETF3_CONTINUOUS')),
  start_session TEXT NOT NULL,
  initial_cash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(strategy_version_id,qualification_id,data_profile,start_session)
);

CREATE TABLE paper_generation_events (
  event_id TEXT PRIMARY KEY,
  generation_id TEXT NOT NULL REFERENCES paper_generations(generation_id),
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','PAUSED','CLOSED')),
  action TEXT NOT NULL CHECK(action IN ('CREATE','PAUSE','RESUME','CLOSE')),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE paper_generation_operation_receipts (
  operation_id TEXT PRIMARY KEY,
  generation_id TEXT NOT NULL REFERENCES paper_generations(generation_id),
  account_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('STEP','STEP_SPAN','RECONCILE')),
  request_hash TEXT NOT NULL,
  trade_date TEXT,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(generation_id,action,request_hash)
);

CREATE INDEX paper_generation_events_latest_idx
  ON paper_generation_events(generation_id,created_at);
CREATE INDEX paper_generation_receipts_date_idx
  ON paper_generation_operation_receipts(generation_id,trade_date);

CREATE TRIGGER paper_generations_no_update BEFORE UPDATE ON paper_generations BEGIN SELECT RAISE(ABORT, 'paper generations are append-only'); END;
CREATE TRIGGER paper_generations_no_delete BEFORE DELETE ON paper_generations BEGIN SELECT RAISE(ABORT, 'paper generations are append-only'); END;
CREATE TRIGGER paper_generation_events_no_update BEFORE UPDATE ON paper_generation_events BEGIN SELECT RAISE(ABORT, 'paper generation events are append-only'); END;
CREATE TRIGGER paper_generation_events_no_delete BEFORE DELETE ON paper_generation_events BEGIN SELECT RAISE(ABORT, 'paper generation events are append-only'); END;
CREATE TRIGGER paper_generation_receipts_no_update BEFORE UPDATE ON paper_generation_operation_receipts BEGIN SELECT RAISE(ABORT, 'paper generation receipts are append-only'); END;
CREATE TRIGGER paper_generation_receipts_no_delete BEFORE DELETE ON paper_generation_operation_receipts BEGIN SELECT RAISE(ABORT, 'paper generation receipts are append-only'); END;
