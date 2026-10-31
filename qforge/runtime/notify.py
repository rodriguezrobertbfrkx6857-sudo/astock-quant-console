# -*- coding: utf-8 -*-
"""Underlying notification and alerting system for QForgeLab.

Provides an extensible, privacy-safe notification dispatcher supporting:
- Local console / log notifications (default, zero config)
- Database persistent alerts (lab.db alerts table)
- Webhook / Feishu / WeChat Work / DingTalk adapters (user-configurable without hardcoded secrets)

Design rules:
- Privacy-safe: No hardcoded credentials. All external endpoints read from settings / env vars.
- Fault-tolerant: Notification errors never crash trading or research pipelines.
"""
from __future__ import annotations

import json
from typing import Any
import urllib.request
import urllib.error

from .. import db
from ..config import get_settings
from ..util import get_logger, now_ts

log = get_logger("qforge.notify")


class BaseNotifier:
    def send(self, title: str, content: str, level: str = "INFO",
             meta: dict[str, Any] | None = None) -> bool:
        raise NotImplementedError


class ConsoleNotifier(BaseNotifier):
    def send(self, title: str, content: str, level: str = "INFO",
             meta: dict[str, Any] | None = None) -> bool:
        log.info("[NOTIFY-%s] %s: %s", level, title, content)
        return True


class DatabaseNotifier(BaseNotifier):
    def send(self, title: str, content: str, level: str = "INFO",
             meta: dict[str, Any] | None = None) -> bool:
        try:
            lab = db.connect_lab()
            try:
                msg = f"{title}: {content}" if content else title
                lab.execute("INSERT INTO alerts (ts, level, source, message) VALUES (?,?,?,?)",
                            (now_ts(), level, "notify", msg[:2000]))
            finally:
                lab.close()
            return True
        except Exception as e:
            log.warning("failed to persist alert to lab.db: %s", e)
            return False


class WebhookNotifier(BaseNotifier):
    def __init__(self, webhook_url: str, channel_type: str = "generic"):
        self.webhook_url = webhook_url
        self.channel_type = channel_type

    def send(self, title: str, content: str, level: str = "INFO",
             meta: dict[str, Any] | None = None) -> bool:
        if not self.webhook_url or not self.webhook_url.startswith(("http://", "https://")):
            return False
        payload = self._format_payload(title, content, level, meta)
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json; charset=utf-8"}
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return 200 <= resp.status < 300
        except Exception as e:
            log.warning("webhook dispatch to %s failed: %s", self.channel_type, e)
            return False

    def _format_payload(self, title: str, content: str, level: str,
                        meta: dict[str, Any] | None) -> dict:
        text = f"[{level}] {title}\n{content}"
        if self.channel_type == "feishu":
            return {"msg_type": "text", "content": {"text": text}}
        if self.channel_type in ("wecom", "wechat"):
            return {"msgtype": "text", "text": {"content": text}}
        if self.channel_type == "dingtalk":
            return {"msgtype": "text", "text": {"content": text}}
        return {"title": title, "content": content, "level": level, "meta": meta or {}, "ts": now_ts()}


class NotifierHub:
    def __init__(self):
        self.backends: list[BaseNotifier] = [ConsoleNotifier(), DatabaseNotifier()]
        self._load_configured_backends()

    def _load_configured_backends(self) -> None:
        s = get_settings()
        extra = getattr(s, "extra", {}) or {}
        notify_cfg = extra.get("notify", {})
        webhook = notify_cfg.get("webhook_url")
        channel_type = notify_cfg.get("channel_type", "generic")
        if webhook and isinstance(webhook, str) and webhook.startswith("http"):
            self.backends.append(WebhookNotifier(webhook, channel_type=channel_type))

    def dispatch(self, title: str, content: str, level: str = "INFO",
                 meta: dict[str, Any] | None = None) -> dict[str, bool]:
        results = {}
        for backend in self.backends:
            name = backend.__class__.__name__
            try:
                results[name] = backend.send(title, content, level=level, meta=meta)
            except Exception as e:
                log.warning("notifier %s error: %s", name, e)
                results[name] = False
        return results


_HUB: NotifierHub | None = None


def get_hub() -> NotifierHub:
    global _HUB
    if _HUB is None:
        _HUB = NotifierHub()
    return _HUB


def send_notification(title: str, content: str, level: str = "INFO",
                      meta: dict[str, Any] | None = None) -> dict[str, bool]:
    return get_hub().dispatch(title, content, level=level, meta=meta)


def notify_eod_report(bdate: str, summary: dict) -> dict[str, bool]:
    title = f"QForgeLab EOD 报告 [{bdate}]"
    lines = [f"交易日: {bdate}"]
    if "jobs" in summary:
        status_items = [f"{k}: {v.get('status')}" for k, v in summary["jobs"].items()]
        lines.append(f"任务状态: {', '.join(status_items)}")
    if "signals" in summary:
        lines.append(f"调仓信号数量: {len(summary['signals'])}")
        for sig in summary["signals"][:5]:
            lines.append(f"  - {sig.get('strategy_id')}: {sig.get('action')} {sig.get('ts_code')} (目标仓位: {sig.get('target_weight')})")
    content = "\n".join(lines)
    return send_notification(title, content, level="INFO", meta={"bdate": bdate, "summary": summary})
