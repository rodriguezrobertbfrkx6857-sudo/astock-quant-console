CREATE TABLE business_events (
  business_event_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  event_type TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  committed_at TEXT NOT NULL
);

CREATE TABLE accounting_outbox (
  message_id TEXT PRIMARY KEY,
  business_event_id TEXT NOT NULL REFERENCES business_events(business_event_id),
  topic TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT
);

CREATE INDEX accounting_outbox_pending_idx ON accounting_outbox(delivered_at,created_at);
