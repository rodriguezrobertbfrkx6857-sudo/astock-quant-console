CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE accounts (
  account_id TEXT PRIMARY KEY,
  cash TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE lots (
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  lot_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  shares INTEGER NOT NULL CHECK (shares > 0),
  unit_cost TEXT NOT NULL,
  acquired_on TEXT NOT NULL,
  available_on TEXT NOT NULL,
  PRIMARY KEY (account_id, lot_id)
);

CREATE TABLE receivables (
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  action_id TEXT NOT NULL,
  amount TEXT NOT NULL,
  PRIMARY KEY (account_id, action_id)
);

CREATE TABLE orders (
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  order_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
  shares INTEGER NOT NULL CHECK (shares > 0),
  created_on TEXT NOT NULL,
  board TEXT NOT NULL,
  security_type TEXT NOT NULL,
  filled_shares INTEGER NOT NULL CHECK (filled_shares >= 0 AND filled_shares <= shares),
  status TEXT NOT NULL CHECK (status IN ('PENDING','PARTIAL','FILLED','BLOCKED')),
  blocked_reason TEXT,
  PRIMARY KEY (account_id, order_id)
);

CREATE TABLE fills (
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  fill_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
  trade_on TEXT NOT NULL,
  shares INTEGER NOT NULL CHECK (shares > 0),
  price TEXT NOT NULL,
  gross TEXT NOT NULL,
  fee TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  PRIMARY KEY (account_id, fill_id),
  FOREIGN KEY (account_id, order_id) REFERENCES orders(account_id, order_id)
);

CREATE TABLE journal_entries (
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  entry_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_on TEXT NOT NULL,
  PRIMARY KEY (account_id, entry_id),
  UNIQUE (account_id, event_id)
);

CREATE TABLE journal_lines (
  account_id TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  ledger_account TEXT NOT NULL,
  debit TEXT NOT NULL,
  credit TEXT NOT NULL,
  PRIMARY KEY (account_id, entry_id, line_no),
  FOREIGN KEY (account_id, entry_id) REFERENCES journal_entries(account_id, entry_id)
);

CREATE TABLE corporate_action_receipts (
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  receipt_id TEXT NOT NULL,
  applied_at TEXT NOT NULL,
  PRIMARY KEY (account_id, receipt_id)
);

CREATE TABLE daily_nav (
  account_id TEXT NOT NULL REFERENCES accounts(account_id),
  nav_on TEXT NOT NULL,
  nav TEXT NOT NULL,
  price_snapshot_hash TEXT NOT NULL,
  run_id TEXT NOT NULL,
  PRIMARY KEY (account_id, nav_on, run_id)
);

CREATE INDEX lots_asset_available_idx ON lots(account_id, asset_id, available_on);
CREATE INDEX fills_order_idx ON fills(account_id, order_id);
