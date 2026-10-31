"""Optional AI explanation layer, isolated from Quant Core truth.

The default is disabled.  AI responses are advisory text only: this module
does not write canonical data, research results, qualification records,
orders, fills, or portfolio state.
"""

from __future__ import annotations

import json
import os
import ssl
from datetime import date, datetime
from pathlib import Path
from threading import Lock
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


_LOCAL_AI_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "v2" / "ai.local.json"
)


def _read_local_ai_config() -> dict[str, object]:
    try:
        payload = json.loads(_LOCAL_AI_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _text_setting(
    local: dict[str, object], env_name: str, local_name: str, default: str
) -> str:
    env_value = os.environ.get(env_name)
    if env_value is not None and env_value.strip():
        return env_value.strip()
    local_value = local.get(local_name)
    if isinstance(local_value, str) and local_value.strip():
        return local_value.strip()
    return default


def deepseek_api_key_present() -> bool:
    """Report only whether a non-empty local key is configured."""
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key is not None and env_key.strip():
        return True
    local_key = _read_local_ai_config().get("api_key")
    return isinstance(local_key, str) and bool(local_key.strip())


class AIStatus(StrEnum):
    PASS = "PASS"
    NOT_ENABLED = "NOT_ENABLED"
    PENDING_KEY = "PENDING_KEY"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class AIConfig:
    enabled: bool = False
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    min_severity: str = "WARN"
    max_calls_per_minute: int = 5
    max_calls_per_day: int = 100
    max_tokens_per_event: int = 512
    event_cooldown_seconds: int = 300
    daily_budget: int = 100000

    @classmethod
    def from_environment(cls) -> "AIConfig":
        local = _read_local_ai_config()

        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(os.environ.get(name, str(default)))
            except ValueError:
                return default
            return max(minimum, min(maximum, value))

        raw_enabled: object = os.environ.get("AI_ENABLED")
        if raw_enabled is None or not str(raw_enabled).strip():
            raw_enabled = local.get("enabled", False)
        return cls(
            enabled=str(raw_enabled).strip().lower() in {"1", "true", "yes", "on"},
            provider=_text_setting(local, "AI_PROVIDER", "provider", "deepseek").lower(),
            model=_text_setting(
                local, "DEEPSEEK_MODEL", "model", "deepseek-v4-flash"
            ),
            min_severity=os.environ.get("AI_MIN_SEVERITY", "WARN").strip().upper()
            or "WARN",
            max_calls_per_minute=integer("MAX_AI_CALLS_PER_MINUTE", 5, 1, 60),
            max_calls_per_day=integer("MAX_AI_CALLS_PER_DAY", 100, 1, 10000),
            max_tokens_per_event=integer("MAX_AI_TOKENS_PER_EVENT", 512, 32, 4096),
            event_cooldown_seconds=integer(
                "AI_COOLDOWN",
                integer("AI_EVENT_COOLDOWN", 300, 0, 86400),
                0,
                86400,
            ),
            daily_budget=integer("AI_DAILY_BUDGET", 100000, 1, 10000000),
        )


@dataclass(frozen=True)
class AIResult:
    status: AIStatus
    text: str
    provider: str
    reason: str


class AIIntelligenceProvider(Protocol):
    def explain(self, prompt: str, *, max_tokens: int) -> AIResult:
        ...


class DisabledAdapter:
    def explain(self, prompt: str, *, max_tokens: int) -> AIResult:
        del prompt, max_tokens
        return AIResult(AIStatus.NOT_ENABLED, "", "disabled", "AI_ENABLED=false")


class DeepSeekAdapter:
    """Explicit DeepSeek adapter; the key and default model are local-only."""

    endpoint = "https://api.deepseek.com/chat/completions"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 20.0,
    ):
        self._api_key = api_key
        self._model = model
        self.timeout_seconds = timeout_seconds

    def explain(self, prompt: str, *, max_tokens: int) -> AIResult:
        local = _read_local_ai_config()
        raw_key = (
            self._api_key
            if self._api_key is not None
            else os.environ.get("DEEPSEEK_API_KEY")
        )
        if not str(raw_key or "").strip():
            local_key = local.get("api_key")
            raw_key = local_key if isinstance(local_key, str) else None
        key = str(raw_key or "").strip()
        if not key:
            return AIResult(
                AIStatus.PENDING_KEY,
                "",
                "deepseek",
                "DEEPSEEK_API_KEY absent",
            )
        raw_model = (
            self._model
            if self._model is not None
            else _text_setting(local, "DEEPSEEK_MODEL", "model", "deepseek-v4-flash")
        )
        model = str(raw_model or "deepseek-v4-flash").strip() or "deepseek-v4-flash"
        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            context = ssl.create_default_context()
            # Some local Python distributions do not ship a CA bundle.  Use an
            # explicit user override first, then the common Git-for-Windows
            # bundle; verification is never disabled.
            ca_candidates = [
                os.environ.get("AI_CA_BUNDLE", ""),
                r"C:\Program Files\Git\mingw64\etc\ssl\certs\ca-bundle.crt",
                r"C:\Program Files\Git\mingw64\ssl\certs\ca-bundle.crt",
            ]
            for candidate in ca_candidates:
                if candidate and Path(candidate).is_file():
                    context = ssl.create_default_context(cafile=candidate)
                    break
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds, context=context
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
            choices = body.get("choices") if isinstance(body, dict) else None
            message = choices[0].get("message") if isinstance(choices, list) and choices else None
            text = message.get("content") if isinstance(message, dict) else None
            if not isinstance(text, str):
                return AIResult(AIStatus.DEGRADED, "", "deepseek", "SCHEMA_MISMATCH")
            return AIResult(AIStatus.PASS, text, "deepseek", "advisory response")
        except urllib.error.HTTPError as exc:
            return AIResult(
                AIStatus.DEGRADED,
                "",
                "deepseek",
                f"HTTP_{exc.code}",
            )
        except urllib.error.URLError as exc:
            return AIResult(
                AIStatus.DEGRADED,
                "",
                "deepseek",
                f"NETWORK_{type(exc.reason).__name__}",
            )
        except TimeoutError:
            return AIResult(AIStatus.DEGRADED, "", "deepseek", "TIMEOUT")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return AIResult(
                AIStatus.DEGRADED,
                "",
                "deepseek",
                "INVALID_RESPONSE_ENCODING",
            )


class AIService:
    """Local severity/cooldown/budget gate around an advisory provider."""

    _severity = {"INFO": 1, "WARN": 2, "CRITICAL": 3}

    def __init__(
        self,
        config: AIConfig | None = None,
        provider: AIIntelligenceProvider | None = None,
        state_path: str | Path | None = None,
    ):
        self.config = config or AIConfig.from_environment()
        self.provider = provider or (
            DeepSeekAdapter(model=self.config.model)
            if self.config.provider == "deepseek"
            else DisabledAdapter()
        )
        self._calls: list[float] = []
        self._day_calls = 0
        self._day_tokens = 0
        self._last_event: dict[str, float] = {}
        self._lock = Lock()
        self._state_path = Path(state_path) if state_path is not None else Path(__file__).resolve().parents[3] / "state" / "v2" / "ai_budget.json"
        self._load_budget()

    @property
    def _business_date(self) -> str:
        return date.today().isoformat()

    def _load_budget(self) -> None:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("business_date") != self._business_date:
            return
        raw_calls = payload.get("calls")
        self._calls = [float(x) for x in raw_calls] if isinstance(raw_calls, list) else []
        self._day_calls = int(payload.get("day_calls", 0) or 0)
        self._day_tokens = int(payload.get("day_tokens", 0) or 0)
        raw_events = payload.get("last_event")
        self._last_event = {
            str(key): float(value)
            for key, value in raw_events.items()
        } if isinstance(raw_events, dict) else {}

    def _persist_budget(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "business_date": self._business_date,
            "calls": self._calls[-self.config.max_calls_per_minute * 2 :],
            "day_calls": self._day_calls,
            "day_tokens": self._day_tokens,
            "last_event": self._last_event,
            "updated_at": datetime.now().astimezone().isoformat(),
            "limits": {
                "max_calls_per_minute": self.config.max_calls_per_minute,
                "max_calls_per_day": self.config.max_calls_per_day,
                "daily_budget": self.config.daily_budget,
            },
        }
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self._state_path)

    def budget_status(self) -> dict[str, object]:
        with self._lock:
            now = time.time()
            self._calls = [stamp for stamp in self._calls if now - stamp < 60]
            return {
                "business_date": self._business_date,
                "calls_last_minute": len(self._calls),
                "calls_today": self._day_calls,
                "tokens_today": self._day_tokens,
                "max_calls_per_minute": self.config.max_calls_per_minute,
                "max_calls_per_day": self.config.max_calls_per_day,
                "daily_budget": self.config.daily_budget,
                "remaining_calls_today": max(0, self.config.max_calls_per_day - self._day_calls),
                "remaining_tokens": max(0, self.config.daily_budget - self._day_tokens),
                "state_path": str(self._state_path),
            }

    def explain(self, event_key: str, severity: str, prompt: str) -> AIResult:
        if not self.config.enabled:
            return DisabledAdapter().explain(
                prompt, max_tokens=self.config.max_tokens_per_event
            )
        if self._severity.get(severity.upper(), 0) < self._severity.get(
            self.config.min_severity, 2
        ):
            return AIResult(
                AIStatus.NOT_ENABLED,
                "",
                self.config.provider,
                "below AI_MIN_SEVERITY",
            )
        with self._lock:
            now = time.time()
            self._calls = [stamp for stamp in self._calls if now - stamp < 60]
            if (
                len(self._calls) >= self.config.max_calls_per_minute
                or self._day_calls >= self.config.max_calls_per_day
            ):
                return AIResult(AIStatus.DEGRADED, "", self.config.provider, "local rate limit")
            last = self._last_event.get(event_key)
            if last is not None and now - last < self.config.event_cooldown_seconds:
                return AIResult(AIStatus.DEGRADED, "", self.config.provider, "event cooldown")
            if self._day_tokens + self.config.max_tokens_per_event > self.config.daily_budget:
                return AIResult(AIStatus.DEGRADED, "", self.config.provider, "daily token budget")
            self._calls.append(now)
            self._day_calls += 1
            self._day_tokens += self.config.max_tokens_per_event
            self._last_event[event_key] = now
            self._persist_budget()
        return self.provider.explain(prompt, max_tokens=self.config.max_tokens_per_event)

    def explain_anomaly(self, event_key: str, severity: str, prompt: str) -> AIResult:
        return self.explain(event_key, severity, prompt)

    def summarize_event(self, event_key: str, severity: str, prompt: str) -> AIResult:
        return self.explain(event_key, severity, prompt)

    def daily_summary(self, event_key: str, prompt: str) -> AIResult:
        return self.explain(event_key, "WARN", prompt)

    def explain_research(self, event_key: str, prompt: str) -> AIResult:
        return self.explain(event_key, "WARN", prompt)
