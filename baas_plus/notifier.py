"""通知模块：邮件推送（smtplib，标准库实现，支持 SSL / STARTTLS）"""
from __future__ import annotations

import logging
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from .config import EmailConfig

logger = logging.getLogger(__name__)


class EmailNotifier:
    def __init__(self, config: EmailConfig) -> None:
        self.config = config
        self.last_error = ""  # 最近一次发送失败的具体原因（供 WebUI 展示）

    @property
    def enabled(self) -> bool:
        return bool(self.config.smtp_host and self.config.username and self.config.password and self.config.to_addrs)

    def send(self, subject: str, body: str) -> bool:
        """发送邮件；成功返回 True，失败返回 False（具体原因见 self.last_error）"""
        cfg = self.config
        if not self.enabled:
            self.last_error = "邮件通知未完整配置（smtp_host/username/password/to_addrs 必填）"
            logger.warning("邮件通知未完整配置（username/password/to_addrs），跳过发送")
            return False

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header("BAAS-Plus", "utf-8")), cfg.from_addr or cfg.username))
        msg["To"] = ", ".join(cfg.to_addrs)

        try:
            if cfg.use_ssl:
                with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
                    server.login(cfg.username, cfg.password)
                    server.sendmail(cfg.from_addr or cfg.username, cfg.to_addrs, msg.as_string())
            else:
                with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
                    server.starttls()
                    server.login(cfg.username, cfg.password)
                    server.sendmail(cfg.from_addr or cfg.username, cfg.to_addrs, msg.as_string())
            logger.info("邮件通知发送成功: %s", subject)
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("邮件通知发送失败: %s", exc)
            return False


def send_notification(config: EmailConfig, subject: str, body: str) -> bool:
    """便捷入口"""
    return EmailNotifier(config).send(subject, body)
