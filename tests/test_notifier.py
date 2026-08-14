"""邮件通知测试（mock smtplib，不发送真实邮件）"""
import smtplib

import pytest

from email import message_from_string
from email.header import decode_header

from baas_plus.config import EmailConfig
from baas_plus.notifier import EmailNotifier


class FakeSMTP:
    """模拟 SMTP 连接（记录调用，可注入失败）"""

    fail_at = None  # 第几次实例化时抛 SMTPServerDisconnected
    calls = 0
    sent = {}

    def __init__(self, *a, **kw):
        type(self).calls += 1
        if type(self).fail_at and type(self).calls == type(self).fail_at:
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, user, pwd):
        type(self).sent["login"] = (user, pwd)

    def sendmail(self, from_, to, msg):
        type(self).sent["mail"] = (from_, to, msg)

    def quit(self):
        type(self).sent["quit"] = True

    def close(self):
        pass


def _notifier(**kw):
    cfg = EmailConfig(
        username=kw.pop("username", "u@qq.com"),
        password=kw.pop("password", "authcode"),
        from_addr=kw.pop("from_addr", "u@qq.com"),
        to_addrs=kw.pop("to_addrs", ["v@qq.com"]),
        **kw,
    )
    return EmailNotifier(cfg)


def test_disabled_when_incomplete():
    n = EmailNotifier(EmailConfig(username="", password="", to_addrs=[]))
    assert not n.enabled


def test_send_success(monkeypatch):
    FakeSMTP.fail_on = None
    FakeSMTP.calls = 0
    FakeSMTP.sent = {}
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    n = _notifier()
    assert n.send("主题", "内容") is True
    assert FakeSMTP.sent["login"] == ("u@qq.com", "authcode")
    assert FakeSMTP.sent["mail"][1] == ["v@qq.com"]
    assert FakeSMTP.sent["quit"] is True  # 必须显式 quit（网易邮箱坑）
    msg = message_from_string(FakeSMTP.sent["mail"][2])
    subject, enc = decode_header(msg["Subject"])[0]
    assert subject.decode(enc or "utf-8") == "主题"
    assert "内容" in msg.get_payload(decode=True).decode("utf-8")


def test_connection_reset_retries_once(monkeypatch):
    """网易等邮箱偶发 connection reset：首次失败后自动重试一次"""
    FakeSMTP.fail_at = 1
    FakeSMTP.calls = 0
    FakeSMTP.sent = {}
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    n = _notifier()
    assert n.send("主题", "内容") is True  # 重试后成功
    assert FakeSMTP.calls == 2


def test_retry_exhausted_reports_error(monkeypatch):
    FakeSMTP.fail_at = None
    FakeSMTP.calls = 0
    FakeSMTP.sent = {}

    class AlwaysFail(FakeSMTP):
        def __init__(self, *a, **kw):
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    monkeypatch.setattr(smtplib, "SMTP_SSL", AlwaysFail)
    n = _notifier()
    assert n.send("主题", "内容") is False
    assert "SMTPServerDisconnected" in n.last_error
    assert "授权码" in n.last_error  # 提示使用客户端授权码


def test_auth_error_hints_authcode(monkeypatch):
    def boom(*a, **kw):
        raise smtplib.SMTPAuthenticationError(535, b"auth failed")

    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
    n = _notifier()
    assert n.send("主题", "内容") is False
    assert "授权码" in n.last_error


def test_send_failure_logs(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
    n = _notifier()
    assert n.send("主题", "内容") is False
    assert "smtp down" in n.last_error
