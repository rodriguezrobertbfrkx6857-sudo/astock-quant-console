CREATE TABLE event_documents (
  event_id TEXT PRIMARY KEY,
  source_url TEXT NOT NULL,
  source_name TEXT NOT NULL,
  published_at TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  raw_object_hash TEXT NOT NULL,
  provenance TEXT NOT NULL CHECK(provenance IN ('REAL','SYNTHETIC','UNKNOWN','MIXED')),
  created_at TEXT NOT NULL
);

CREATE TABLE event_revisions (
  event_id TEXT NOT NULL REFERENCES event_documents(event_id),
  revision_id TEXT NOT NULL,
  available_at TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  title TEXT NOT NULL,
  category TEXT,
  PRIMARY KEY(event_id,revision_id),
  UNIQUE(event_id,available_at,text_hash)
);

CREATE TABLE event_security_links (
  event_id TEXT NOT NULL REFERENCES event_documents(event_id),
  asset_id TEXT NOT NULL,
  available_at TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  confidence TEXT NOT NULL CHECK(confidence IN ('VERIFIED','REVIEWED','UNKNOWN')),
  PRIMARY KEY(event_id,asset_id,available_at)
);

CREATE TABLE event_study_results (
  study_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES event_documents(event_id),
  protocol_hash TEXT NOT NULL,
  snapshot_hash TEXT NOT NULL,
  window_start TEXT NOT NULL,
  window_end TEXT NOT NULL,
  raw_return TEXT,
  excess_car TEXT,
  benchmark_id TEXT,
  access_receipt_id TEXT NOT NULL REFERENCES access_receipts(receipt_id),
  status TEXT NOT NULL CHECK(status IN ('PASS','BLOCKED_DATA','INVALID')),
  created_at TEXT NOT NULL
);

CREATE TABLE queue_readiness_assessments (
  assessment_id TEXT PRIMARY KEY,
  queue_item_id TEXT NOT NULL REFERENCES research_queue(queue_item_id),
  status TEXT NOT NULL CHECK(status IN ('READY','BLOCKED_DATA')),
  reasons_json TEXT NOT NULL,
  manifest_hashes_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TRIGGER event_documents_no_update BEFORE UPDATE ON event_documents BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER event_documents_no_delete BEFORE DELETE ON event_documents BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER event_revisions_no_update BEFORE UPDATE ON event_revisions BEGIN SELECT RAISE(ABORT, 'event revisions are append-only'); END;
CREATE TRIGGER event_revisions_no_delete BEFORE DELETE ON event_revisions BEGIN SELECT RAISE(ABORT, 'event revisions are append-only'); END;
CREATE TRIGGER event_studies_no_update BEFORE UPDATE ON event_study_results BEGIN SELECT RAISE(ABORT, 'event studies are append-only'); END;
CREATE TRIGGER event_studies_no_delete BEFORE DELETE ON event_study_results BEGIN SELECT RAISE(ABORT, 'event studies are append-only'); END;
