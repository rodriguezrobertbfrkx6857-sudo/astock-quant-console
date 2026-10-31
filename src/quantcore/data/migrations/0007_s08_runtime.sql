CREATE TABLE runtime_jobs (
  job_key TEXT PRIMARY KEY,
  job_type TEXT NOT NULL,
  business_date TEXT NOT NULL,
  input_snapshot TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  loop_kind TEXT NOT NULL CHECK(loop_kind IN ('DAILY','RESEARCH')),
  status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','WAITING_DATA','WAITING_RETRY','SUCCEEDED','FAILED','DEGRADED','CANCELLED')),
  max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  lease_owner TEXT,
  lease_expires_at TEXT,
  fencing_token INTEGER NOT NULL DEFAULT 0,
  checkpoint_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE runtime_dependencies (
  job_key TEXT NOT NULL REFERENCES runtime_jobs(job_key),
  depends_on_job_key TEXT NOT NULL REFERENCES runtime_jobs(job_key),
  PRIMARY KEY(job_key, depends_on_job_key),
  CHECK(job_key <> depends_on_job_key)
);

CREATE TABLE runtime_attempts (
  attempt_id TEXT PRIMARY KEY,
  job_key TEXT NOT NULL REFERENCES runtime_jobs(job_key),
  attempt_no INTEGER NOT NULL,
  owner TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','FAILED','LEASE_LOST')),
  error_code TEXT,
  UNIQUE(job_key, attempt_no)
);

CREATE TABLE runtime_events (
  message_id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  published_at TEXT NOT NULL
);

CREATE INDEX runtime_ready_idx ON runtime_jobs(status,loop_kind,business_date);
