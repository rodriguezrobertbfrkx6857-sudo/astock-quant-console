CREATE TABLE IF NOT EXISTS security_identities (
  asset_id TEXT PRIMARY KEY,
  security_type TEXT NOT NULL CHECK (security_type IN ('STOCK','ETF','INDEX','UNKNOWN')),
  exchange TEXT NOT NULL,
  source_ref TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_identifier_versions (
  identifier_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES security_identities(asset_id),
  scheme TEXT NOT NULL, value TEXT NOT NULL,
  effective_from TEXT NOT NULL, effective_to TEXT,
  known_from TEXT NOT NULL, known_to TEXT,
  source_ref TEXT NOT NULL,
  confidence TEXT NOT NULL CHECK (confidence IN ('VERIFIED','INFERRED','UNKNOWN')),
  UNIQUE(scheme,value,effective_from,known_from)
);
CREATE TABLE IF NOT EXISTS security_state_versions (
  state_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES security_identities(asset_id),
  effective_from TEXT NOT NULL, effective_to TEXT,
  known_from TEXT NOT NULL, known_to TEXT,
  list_date TEXT, delist_date TEXT, name TEXT, board TEXT NOT NULL,
  risk_status TEXT NOT NULL, suspended INTEGER,
  source_ref TEXT NOT NULL, confidence TEXT NOT NULL,
  revision TEXT NOT NULL,
  CHECK (suspended IS NULL OR suspended IN (0,1)),
  UNIQUE(asset_id,effective_from,known_from,revision)
);
CREATE TABLE IF NOT EXISTS universe_membership_versions (
  membership_id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL,
  asset_id TEXT NOT NULL REFERENCES security_identities(asset_id),
  effective_from TEXT NOT NULL, effective_to TEXT,
  available_at TEXT NOT NULL, source_ref TEXT NOT NULL,
  verified INTEGER NOT NULL CHECK (verified IN (0,1)),
  UNIQUE(group_id,asset_id,effective_from,available_at)
);
CREATE TABLE IF NOT EXISTS financial_revisions (
  financial_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES security_identities(asset_id),
  statement TEXT NOT NULL, report_period TEXT NOT NULL,
  revision_id TEXT NOT NULL, available_at TEXT NOT NULL,
  observed_at TEXT NOT NULL, source_ref TEXT NOT NULL,
  source_row_hash TEXT NOT NULL, confidence TEXT NOT NULL,
  values_json TEXT NOT NULL,
  UNIQUE(asset_id,statement,report_period,revision_id)
);
CREATE INDEX IF NOT EXISTS ix_state_asof
  ON security_state_versions(asset_id,effective_from,effective_to,known_from,known_to);
CREATE INDEX IF NOT EXISTS ix_financial_asof
  ON financial_revisions(asset_id,statement,report_period,available_at);
