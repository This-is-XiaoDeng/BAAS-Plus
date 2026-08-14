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

    def _send_once(self, cfg: EmailConfig, msg: MIMEText) -> None:
        """单次发送：连接 → ehlo → 认证 → 发送 → 显式 quit

        参考网易邮箱坑（SMTPServerDisconnected: Connection unexpectedly closed）：
        - 每次发送使用独立连接
        - 发送完成必须显式 quit()（服务端约 1 小时会踢掉未正常退出的连接）
        - SSL 连接后显式 ehlo()（某些服务器环境缺 ehlo 会直接断开）
        """
        if cfg.use_ssl:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
            server.starttls()
        try:
            server.ehlo()
            server.login(cfg.username, cfg.password)
            server.sendmail(cfg.from_addr or cfg.username, cfg.to_addrs, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                try:
                    server.close()
                except Exception:  # noqa: BLE001
                    pass

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

        # 网易等邮箱偶发连接被重置，断连类错误自动重试一次
        retryable = (smtplib.SMTPServerDisconnected, ConnectionResetError, ConnectionAbortedError, TimeoutError)
        try:
            try:
                self._send_once(cfg, msg)
            except retryable:
                logger.warning("SMTP 连接被重置，重试一次")
                self._send_once(cfg, msg)
            logger.info("邮件通知发送成功: %s", subject)
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            # 网易/QQ 等免费邮箱的常见坑：必须用客户端授权码而不是登录密码
            if isinstance(exc, (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected, ConnectionResetError)):
                detail += "；提示：网易/QQ 等免费邮箱请使用「客户端授权码」作为密码" \
                          "（邮箱设置 → POP3/SMTP/IMAP → 开启服务并生成授权码），不要用登录密码"
            self.last_error = detail
            logger.error("邮件通知发送失败: %s", exc)
            return False


def send_notification(config: EmailConfig, subject: str, body: str) -> bool:
    """便捷入口"""
    return EmailNotifier(config).send(subject, body)
