# Recovery

Stop the server with Ctrl+C before backing up or restoring local runtime directories. Keep private backups outside the public repository. For a new empty installation, initialize schemas with `python scripts/init_public.py`; this command does not restore lost market data, account state, exposure history or audit records. Never replace an existing research installation with an empty database to reset its OOS history.
