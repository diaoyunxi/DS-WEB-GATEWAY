"""①服务端基础设施：告警（SMTP 邮件 + 日志，按 kind 冷却，不阻塞主流程）。"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import time
from email.message import EmailMessage
from email.utils import formatdate

logger = logging.getLogger("server.alerts")


class AlertManager:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._last_sent: dict[str, float] = {}
        self._history: list[dict] = []

    @property
    def smtp_configured(self) -> bool:
        return bool(self._settings.smtp_host and self._settings.smtp_to)

    async def alert(self, kind: str, subject: str, body: str) -> bool:
        """发送告警（同 kind 在冷却期内只记日志）。返回是否真正发出邮件。"""
        now = time.time()
        self._history.append({"ts": now, "kind": kind, "subject": subject, "body": body[:2000]})
        self._history[:] = self._history[-200:]
        logger.warning("[告警:%s] %s | %s", kind, subject, body[:500])
        if not self.smtp_configured:
            return False
        if now - self._last_sent.get(kind, 0.0) < self._settings.alert_cooldown:
            return False
        self._last_sent[kind] = now
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self._send_mail, f"[DS-Web-Gateway][{kind}] {subject}", body
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("告警邮件发送失败: %s", exc)
            return False

    def _send_mail(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._settings.smtp_from or self._settings.smtp_user
        msg["To"] = self._settings.smtp_to
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(body)
        with smtplib.SMTP(self._settings.smtp_host, self._settings.smtp_port, timeout=15) as smtp:
            if self._settings.smtp_starttls:
                smtp.starttls()
            if self._settings.smtp_user:
                smtp.login(self._settings.smtp_user, self._settings.smtp_password)
            smtp.send_message(msg)

    def recent(self, n: int = 50) -> list[dict]:
        return self._history[-n:]
