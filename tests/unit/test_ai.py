from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import src.quantcore.intelligence.ai as ai_module
from src.quantcore.intelligence.ai import (
    AIConfig,
    AIStatus,
    DeepSeekAdapter,
    deepseek_api_key_present,
)


def test_deepseek_v4_flash_is_the_default_model() -> None:
    with patch.dict(os.environ, {}, clear=True):
        config = AIConfig.from_environment()
    assert config.model == "deepseek-v4-flash"


def test_local_ai_file_supplies_configuration_without_environment_variables() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "ai.local.json"
        path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "api_key": "test-key",
                }
            ),
            encoding="utf-8",
        )
        with patch.object(ai_module, "_LOCAL_AI_CONFIG_PATH", path), patch.dict(
            os.environ, {}, clear=True
        ):
            config = AIConfig.from_environment()
            assert config.enabled is True
            assert config.model == "deepseek-v4-flash"
            assert deepseek_api_key_present() is True


def test_deepseek_model_can_be_configured_without_exposing_the_key() -> None:
    with patch.dict(
        os.environ,
        {
            "AI_ENABLED": "true",
            "DEEPSEEK_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_API_KEY": "test-key",
        },
        clear=True,
    ):
        config = AIConfig.from_environment()
        assert config.enabled is True
        assert config.model == "deepseek-v4-flash"

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": "ok"}}]}
                ).encode("utf-8")

        with patch(
            "src.quantcore.intelligence.ai.urllib.request.urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            result = DeepSeekAdapter(model=config.model).explain("test", max_tokens=32)

    assert result.status is AIStatus.PASS
    request = urlopen.call_args.args[0]
    assert json.loads(request.data.decode("utf-8"))["model"] == "deepseek-v4-flash"
    assert request.headers["Authorization"] == "Bearer test-key"
