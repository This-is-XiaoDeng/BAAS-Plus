"""邮件通知测试（mock smtplib，不发送真实邮件）"""
import smtplib

import pytest

from email import message_from_string
from email.header import decode_header

from baas_plus.config import EmailConfig
from baas_plus.notifier import EmailNotifier


def test_disabled_when_incomplete():
    n = EmailNotifier(EmailConfig(username="", password="", to_addrs=[]))
    assert not n.enabled


def test_send_success(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, pwd):
            sent["login"] = (user, pwd)

        def sendmail(self, from_, to, msg):
            sent["mail"] = (from_, to, msg)

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    n = EmailNotifier(
        EmailConfig(username="u@qq.com", password="authcode", from_addr="u@qq.com", to_addrs=["v@qq.com"])
    )
    assert n.send("主题", "内容") is True
    assert sent["login"] == ("u@qq.com", "authcode")
    assert sent["mail"][1] == ["v@qq.com"]
    msg = message_from_string(sent["mail"][2])
    subject, enc = decode_header(msg["Subject"])[0]
    assert subject.decode(enc or "utf-8") == "主题"
    assert "内容" in msg.get_payload(decode=True).decode("utf-8")


def test_send_failure_logs(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
    n = EmailNotifier(
        EmailConfig(username="u@qq.com", password="authcode", to_addrs=["v@qq.com"])
    )
    assert n.send("主题", "内容") is False
