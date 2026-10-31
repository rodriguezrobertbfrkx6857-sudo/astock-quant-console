CREATE TABLE reporting_publications (
  publication_id TEXT PRIMARY KEY,
  business_date TEXT NOT NULL,
  as_of TEXT NOT NULL,
  provenance TEXT NOT NULL CHECK(provenance IN ('REAL','SYNTHETIC','UNKNOWN','MIXED')),
  quality_status TEXT NOT NULL CHECK(quality_status IN ('PASS','FAIL')),
  input_hash TEXT NOT NULL,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE canonical_metrics (
  publication_id TEXT NOT NULL REFERENCES reporting_publications(publication_id),
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  metric_id TEXT NOT NULL,
  value TEXT,
  display_value TEXT NOT NULL,
  unit TEXT NOT NULL,
  convention TEXT NOT NULL,
  metric_version TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  evidence_ref TEXT NOT NULL,
  PRIMARY KEY(publication_id,entity_type,entity_id,metric_id)
);

CREATE TABLE current_reporting_publication (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  publication_id TEXT NOT NULL REFERENCES reporting_publications(publication_id),
  updated_at TEXT NOT NULL
);

CREATE TRIGGER reporting_publications_no_update BEFORE UPDATE ON reporting_publications BEGIN SELECT RAISE(ABORT, 'report publications are immutable'); END;
CREATE TRIGGER reporting_publications_no_delete BEFORE DELETE ON reporting_publications BEGIN SELECT RAISE(ABORT, 'report publications are immutable'); END;
CREATE TRIGGER canonical_metrics_no_update BEFORE UPDATE ON canonical_metrics BEGIN SELECT RAISE(ABORT, 'canonical metrics are immutable'); END;
CREATE TRIGGER canonical_metrics_no_delete BEFORE DELETE ON canonical_metrics BEGIN SELECT RAISE(ABORT, 'canonical metrics are immutable'); END;
