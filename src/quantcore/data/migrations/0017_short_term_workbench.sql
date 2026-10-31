PRAGMA foreign_keys=ON;

CREATE TABLE manual_holding_revisions (
  revision_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  shares INTEGER NOT NULL CHECK(shares >= 0),
  sellable_shares INTEGER NOT NULL CHECK(sellable_shares >= 0 AND sellable_shares <= shares),
  today_bought_shares INTEGER NOT NULL DEFAULT 0 CHECK(today_bought_shares >= 0 AND sellable_shares + today_bought_shares <= shares),
  cost_price TEXT NOT NULL CHECK(CAST(cost_price AS REAL) > 0),
  cash_budget TEXT CHECK(cash_budget IS NULL OR CAST(cash_budget AS REAL) >= 0),
  risk_profile TEXT NOT NULL CHECK(risk_profile IN ('CONSERVATIVE','BALANCED','AGGRESSIVE')),
  as_of TEXT NOT NULL,
  active INTEGER NOT NULL CHECK(active IN (0,1)),
  source TEXT NOT NULL CHECK(source IN ('MANUAL','CSV','ISOLATED_SAMPLE')),
  request_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(asset_id,request_hash)
);

CREATE TABLE short_term_scan_runs (
  scan_id TEXT PRIMARY KEY,
  target_session TEXT NOT NULL,
  data_date TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  coverage_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(target_session,snapshot_hash,engine_version)
);

CREATE TABLE short_term_plans (
  plan_id TEXT PRIMARY KEY,
  plan_type TEXT NOT NULL CHECK(plan_type IN ('CANDIDATE','HOLDING','BASE_T')),
  parent_plan_id TEXT REFERENCES short_term_plans(plan_id),
  asset_id TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  target_session TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  input_available_at TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  rules_json TEXT NOT NULL,
  entry_low TEXT,
  entry_high TEXT,
  target_low TEXT,
  target_high TEXT,
  stop_price TEXT,
  trigger_json TEXT NOT NULL,
  invalidation_json TEXT NOT NULL,
  validation_status TEXT NOT NULL,
  status TEXT NOT NULL,
  settlement_json TEXT,
  settled_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(plan_type,asset_id,strategy_version,target_session,snapshot_hash)
);

CREATE TABLE short_term_candidates (
  scan_id TEXT NOT NULL REFERENCES short_term_scan_runs(scan_id),
  plan_id TEXT NOT NULL REFERENCES short_term_plans(plan_id),
  rank INTEGER NOT NULL CHECK(rank > 0),
  score TEXT NOT NULL,
  reason_json TEXT NOT NULL,
  PRIMARY KEY(scan_id,plan_id),
  UNIQUE(scan_id,rank)
);

CREATE TABLE short_term_plan_events (
  event_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES short_term_plans(plan_id),
  event_type TEXT NOT NULL CHECK(event_type IN ('CREATED','SETTLED','INVALIDATED','EXPIRED')),
  event_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(plan_id,event_type,created_at)
);

CREATE TABLE watch_signal_receipts (
  dedupe_key TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES short_term_plans(plan_id),
  asset_id TEXT NOT NULL,
  trigger_type TEXT NOT NULL,
  source_timestamp TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  price TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('OBSERVED','BLOCKED_STALE','BLOCKED_OUT_OF_ORDER','BLOCKED_UNVERIFIED')),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX holding_asset_created_idx ON manual_holding_revisions(asset_id,created_at);
CREATE INDEX short_term_plan_target_idx ON short_term_plans(target_session,plan_type,status);
CREATE INDEX watch_receipt_asset_time_idx ON watch_signal_receipts(asset_id,source_timestamp);

CREATE VIEW current_manual_holdings AS
SELECT h.* FROM manual_holding_revisions h
JOIN (
  SELECT asset_id,MAX(created_at) AS created_at
  FROM manual_holding_revisions
  GROUP BY asset_id
) latest ON latest.asset_id=h.asset_id AND latest.created_at=h.created_at
WHERE h.active=1;

CREATE TRIGGER manual_holding_revisions_no_update BEFORE UPDATE ON manual_holding_revisions BEGIN SELECT RAISE(ABORT, 'holding revisions are append-only'); END;
CREATE TRIGGER manual_holding_revisions_no_delete BEFORE DELETE ON manual_holding_revisions BEGIN SELECT RAISE(ABORT, 'holding revisions are append-only'); END;
CREATE TRIGGER short_term_scan_runs_no_update BEFORE UPDATE ON short_term_scan_runs BEGIN SELECT RAISE(ABORT, 'scan runs are append-only'); END;
CREATE TRIGGER short_term_scan_runs_no_delete BEFORE DELETE ON short_term_scan_runs BEGIN SELECT RAISE(ABORT, 'scan runs are append-only'); END;
CREATE TRIGGER short_term_plans_no_update BEFORE UPDATE ON short_term_plans BEGIN SELECT RAISE(ABORT, 'plans are immutable; create a linked version'); END;
CREATE TRIGGER short_term_plans_no_delete BEFORE DELETE ON short_term_plans BEGIN SELECT RAISE(ABORT, 'plans are append-only'); END;
CREATE TRIGGER short_term_candidates_no_update BEFORE UPDATE ON short_term_candidates BEGIN SELECT RAISE(ABORT, 'candidate ranks are append-only'); END;
CREATE TRIGGER short_term_candidates_no_delete BEFORE DELETE ON short_term_candidates BEGIN SELECT RAISE(ABORT, 'candidate ranks are append-only'); END;
CREATE TRIGGER short_term_plan_events_no_update BEFORE UPDATE ON short_term_plan_events BEGIN SELECT RAISE(ABORT, 'plan events are append-only'); END;
CREATE TRIGGER short_term_plan_events_no_delete BEFORE DELETE ON short_term_plan_events BEGIN SELECT RAISE(ABORT, 'plan events are append-only'); END;
CREATE TRIGGER watch_signal_receipts_no_update BEFORE UPDATE ON watch_signal_receipts BEGIN SELECT RAISE(ABORT, 'watch receipts are append-only'); END;
CREATE TRIGGER watch_signal_receipts_no_delete BEFORE DELETE ON watch_signal_receipts BEGIN SELECT RAISE(ABORT, 'watch receipts are append-only'); END;
