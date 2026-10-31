CREATE TABLE operational_status (
  component TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('PASS','PARTIAL','FAIL','BLOCKED_DATA','PENDING_VENDOR','DEGRADED','RUNNING','PENDING')),
  detail TEXT NOT NULL,
  evidence_ref TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE intraday_ticks (
  source_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  available_at TEXT NOT NULL,
  price TEXT NOT NULL,
  volume TEXT,
  provenance TEXT NOT NULL CHECK(provenance IN ('REAL','SYNTHETIC','UNKNOWN','MIXED')),
  raw_object_hash TEXT NOT NULL,
  PRIMARY KEY(source_id,asset_id,observed_at)
);

CREATE TABLE watch_alerts (
  alert_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  alert_type TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN ('INFO','WARN','CRITICAL')),
  state TEXT NOT NULL CHECK(state IN ('OPEN','ACKNOWLEDGED','CLOSED')),
  evidence_ref TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  worker_type TEXT NOT NULL CHECK(worker_type IN ('DATA','DAILY','RESEARCH','EVENT','WATCH','SCHEDULER')),
  status TEXT NOT NULL CHECK(status IN ('STARTING','IDLE','RUNNING','DEGRADED','STOPPED','FAILED')),
  process_id INTEGER,
  detail TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL
);

CREATE INDEX operational_status_updated_idx ON operational_status(updated_at);
CREATE INDEX intraday_ticks_asset_time_idx ON intraday_ticks(asset_id,observed_at);
CREATE INDEX watch_alerts_state_idx ON watch_alerts(state,created_at);

CREATE TRIGGER intraday_ticks_no_update BEFORE UPDATE ON intraday_ticks BEGIN SELECT RAISE(ABORT, 'intraday ticks are append-only'); END;
CREATE TRIGGER intraday_ticks_no_delete BEFORE DELETE ON intraday_ticks BEGIN SELECT RAISE(ABORT, 'intraday ticks are append-only'); END;
CREATE TRIGGER watch_alerts_no_update BEFORE UPDATE ON watch_alerts BEGIN SELECT RAISE(ABORT, 'watch alerts are append-only'); END;
CREATE TRIGGER watch_alerts_no_delete BEFORE DELETE ON watch_alerts BEGIN SELECT RAISE(ABORT, 'watch alerts are append-only'); END;
