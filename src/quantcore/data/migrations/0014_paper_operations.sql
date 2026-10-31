CREATE TABLE paper_accounts (
  account_id TEXT PRIMARY KEY,
  strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(strategy_version_id),
  qualification_id TEXT NOT NULL REFERENCES paper_qualifications(qualification_id),
  initial_cash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(strategy_version_id,qualification_id)
);

CREATE TABLE paper_account_events (
  event_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','PAUSED','CLOSED')),
  action TEXT NOT NULL CHECK(action IN ('ADMIT','PAUSE','RESUME','CLOSE')),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE paper_operation_receipts (
  operation_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
  action TEXT NOT NULL CHECK(action IN ('STEP','STEP_SPAN','RECONCILE')),
  request_hash TEXT NOT NULL,
  trade_date TEXT,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(account_id,action,request_hash)
);

CREATE INDEX paper_account_events_latest_idx
  ON paper_account_events(account_id,created_at);
CREATE INDEX paper_operation_receipts_date_idx
  ON paper_operation_receipts(account_id,trade_date);

CREATE TRIGGER paper_accounts_no_update BEFORE UPDATE ON paper_accounts BEGIN SELECT RAISE(ABORT, 'paper account bindings are append-only'); END;
CREATE TRIGGER paper_accounts_no_delete BEFORE DELETE ON paper_accounts BEGIN SELECT RAISE(ABORT, 'paper account bindings are append-only'); END;
CREATE TRIGGER paper_account_events_no_update BEFORE UPDATE ON paper_account_events BEGIN SELECT RAISE(ABORT, 'paper account events are append-only'); END;
CREATE TRIGGER paper_account_events_no_delete BEFORE DELETE ON paper_account_events BEGIN SELECT RAISE(ABORT, 'paper account events are append-only'); END;
CREATE TRIGGER paper_operation_receipts_no_update BEFORE UPDATE ON paper_operation_receipts BEGIN SELECT RAISE(ABORT, 'paper operation receipts are append-only'); END;
CREATE TRIGGER paper_operation_receipts_no_delete BEFORE DELETE ON paper_operation_receipts BEGIN SELECT RAISE(ABORT, 'paper operation receipts are append-only'); END;
