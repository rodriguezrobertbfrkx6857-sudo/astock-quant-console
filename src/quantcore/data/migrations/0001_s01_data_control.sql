CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_attempts (
  attempt_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  dataset TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  provenance TEXT NOT NULL CHECK (provenance IN ('REAL','SYNTHETIC','UNKNOWN','MIXED')),
  status TEXT NOT NULL CHECK (status IN ('SUCCESS','EMPTY','PARTIAL','FAILED')),
  pages_expected INTEGER,
  pages_received INTEGER NOT NULL CHECK (pages_received >= 0),
  row_count INTEGER NOT NULL CHECK (row_count >= 0),
  request_json TEXT NOT NULL,
  object_hashes_json TEXT NOT NULL,
  error_code TEXT,
  error_message TEXT
);

CREATE TABLE IF NOT EXISTS data_manifests (
  manifest_id TEXT PRIMARY KEY,
  dataset TEXT NOT NULL,
  version TEXT NOT NULL,
  layer TEXT NOT NULL CHECK (layer IN ('RAW','NORMALIZED','CURATED')),
  provenance TEXT NOT NULL CHECK (provenance IN ('REAL','SYNTHETIC','UNKNOWN','MIXED')),
  readiness TEXT NOT NULL CHECK (readiness IN ('REAL_READY','RESEARCH_ONLY','BLOCKED')),
  manifest_hash TEXT NOT NULL UNIQUE,
  manifest_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(dataset, version, layer)
);

CREATE TABLE IF NOT EXISTS data_publications (
  dataset TEXT PRIMARY KEY,
  manifest_id TEXT NOT NULL REFERENCES data_manifests(manifest_id),
  manifest_hash TEXT NOT NULL,
  published_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_reports (
  report_id TEXT PRIMARY KEY,
  manifest_id TEXT NOT NULL REFERENCES data_manifests(manifest_id),
  status TEXT NOT NULL CHECK (status IN ('PASS','FAIL')),
  input_hash TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  report_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
