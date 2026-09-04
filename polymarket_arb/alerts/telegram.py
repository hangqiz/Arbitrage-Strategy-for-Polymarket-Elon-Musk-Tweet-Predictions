"""Telegram 告警（可选）。未配置 token 时静默跳过。"""
from __future__ import annotations

from requests import Session

from ..config import AppConfig


class TelegramNotifier:
    def __init__(self, cfg: AppConfig):
        self.token = cfg.telegram_bot_token
        self.chat_id = cfg.telegram_chat_id
        self.enabled = bool(self.token and self.chat_id)
        self._sess = Session()

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            resp = self._sess.post(url, json={"chat_id": self.chat_id, "text": text}, timeout=10)
            return resp.ok
        except Exception:  # noqa: BLE001 - 告警失败不应中断该策略
            return False

    def notify_signal(self, market: str, action: str, detail: str) -> None:
        self.send(f"[{action}] {market}\n{detail}")