"""The public snapshot must work without the private installation."""
import json

from fastapi.testclient import TestClient

from scripts.init_public import main as initialize
from src.quantcore.contracts.safety import PROJECT_ROOT
from src.quantcore.contracts.secrets import ensure_relay_credential_from_approved_project_files


def test_environment_only_secret_loader(monkeypatch):
    monkeypatch.delenv("TUSHARE_RELAY_KEY", raising=False)
    assert ensure_relay_credential_from_approved_project_files() == "ABSENT"


def test_empty_public_installation_supports_core_views():
    initialize()
    # A second run must preserve migration checksums and existing empty schemas.
    initialize()
    from quantweb.app import app

    client = TestClient(app)
    for path in (
        "/api/v2/overview", "/api/v2/system", "/api/v2/research/overview",
        "/api/v2/paper/portfolio", "/api/v2/watch",
    ):
        assert client.get(path).status_code == 200, path
    profile = client.get("/api/v2/overview").json()["supported_profile"]
    assert profile["profile_id"] == "PUBLIC_FRAMEWORK_EXAMPLE"
    assert profile["status"] == "FAIL"
    from src.quantcore.operations.profile import load_operational_profile, validate_operational_profile
    assert load_operational_profile()["supported_profile_status"] == "PENDING_DATA"
    assert validate_operational_profile().ready is False


def test_public_config_has_no_private_exposure_history():
    config = json.loads((PROJECT_ROOT / "config/v2/oos_known_lower_bound.json").read_text("utf-8"))
    assert config["events"] == []
