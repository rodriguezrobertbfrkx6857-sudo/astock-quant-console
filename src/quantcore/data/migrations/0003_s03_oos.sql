CREATE TABLE IF NOT EXISTS exposure_events (
 event_id TEXT PRIMARY KEY, import_key TEXT NOT NULL UNIQUE, observed_at TEXT NOT NULL,
 imported_at TEXT NOT NULL, source_project TEXT NOT NULL, source_path TEXT NOT NULL,
 source_sha256 TEXT NOT NULL, run_id TEXT, artifact_hash TEXT,
 start_date TEXT NOT NULL, end_date TEXT NOT NULL, calendar_id TEXT NOT NULL,
 asset_scope TEXT NOT NULL, family_scope TEXT NOT NULL,
 provenance TEXT NOT NULL, evidence_kind TEXT NOT NULL, confidence TEXT NOT NULL,
 actor_scope TEXT NOT NULL, notes TEXT, supersedes_event_id TEXT REFERENCES exposure_events(event_id),
 CHECK(start_date<=end_date)
);
CREATE TABLE IF NOT EXISTS window_reservations (
 reservation_id TEXT PRIMARY KEY, protocol_hash TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
 start_date TEXT NOT NULL, end_date TEXT NOT NULL, asset_scope TEXT NOT NULL,
 family_scope TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN('RESERVED','CONSUMED','CANCELLED')),
 created_at TEXT NOT NULL, UNIQUE(protocol_hash,snapshot_hash,start_date,end_date,asset_scope,family_scope)
);
CREATE TABLE IF NOT EXISTS access_receipts (
 receipt_id TEXT PRIMARY KEY, reservation_id TEXT REFERENCES window_reservations(reservation_id),
 protocol_hash TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
 requested_start TEXT NOT NULL, requested_end TEXT NOT NULL,
 asset_scope TEXT NOT NULL, family_scope TEXT NOT NULL, purpose TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN('ALLOW_KNOWN','DENY_UNKNOWN','DENY_RESERVED')),
 observed_at TEXT NOT NULL, reason TEXT NOT NULL, released_artifact_hash TEXT
);
CREATE TRIGGER IF NOT EXISTS exposure_events_no_update BEFORE UPDATE ON exposure_events BEGIN SELECT RAISE(ABORT,'exposure events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS exposure_events_no_delete BEFORE DELETE ON exposure_events BEGIN SELECT RAISE(ABORT,'exposure events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS access_receipts_no_update BEFORE UPDATE ON access_receipts BEGIN SELECT RAISE(ABORT,'access receipts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS access_receipts_no_delete BEFORE DELETE ON access_receipts BEGIN SELECT RAISE(ABORT,'access receipts are append-only'); END;
CREATE INDEX IF NOT EXISTS ix_exposure_intersection ON exposure_events(start_date,end_date,asset_scope,family_scope);
