"""Public distribution: credentials are explicit environment variables only."""
import os


def ensure_relay_credential_from_approved_project_files() -> str:
    """Compatibility entry point; never reads or searches local files."""
    return "PROCESS_ENV" if os.environ.get("TUSHARE_RELAY_KEY", "").strip() else "ABSENT"
