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


# ---- HTML + 内联图片（汇总邮件嵌游戏截图） ----

def _tiny_png(tmp_path, name="shot.png"):
    """最小可读 PNG（发送时会读字节）"""
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 32)
    return p


def _parse_mail():
    """按 policy.default 解析捕获的邮件（支持 get_content / 遍历附件）"""
    from email import policy
    from email.parser import BytesParser

    raw = FakeSMTP.sent["mail"][2]
    return BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8"))


def test_send_html_with_inline_image(monkeypatch, tmp_path):
    """send_html：multipart/related，HTML 以 cid 引用、图片内联嵌入"""
    FakeSMTP.calls = 0
    FakeSMTP.sent = {}
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    png = _tiny_png(tmp_path)
    n = _notifier()
    assert n.send_html(
        "汇总（附截图）",
        "<html><body><p>执行完成</p><img src='cid:ba_x'></body></html>",
        text="执行完成",
        images=[("ba_x", png)],
    ) is True

    msg = _parse_mail()
    assert msg.get_content_type() == "multipart/related"
    html = [p for p in msg.walk() if p.get_content_type() == "text/html"][0]
    assert "cid:ba_x" in html.get_content()
    text = [p for p in msg.walk() if p.get_content_type() == "text/plain"][0]
    assert text.get_content() == "执行完成"
    imgs = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert len(imgs) == 1
    assert imgs[0].get("Content-ID") == "<ba_x>"
    assert "inline" in imgs[0].get("Content-Disposition", "")


def test_send_html_without_images_is_alternative(monkeypatch):
    """send_html 无图片：multipart/alternative（text + html）"""
    FakeSMTP.calls = 0
    FakeSMTP.sent = {}
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    n = _notifier()
    assert n.send_html("标题", "<b>html</b>", text="纯文本") is True
    msg = _parse_mail()
    assert msg.get_content_type() == "multipart/alternative"
    types = {p.get_content_type() for p in msg.walk()}
    assert types == {"text/plain", "text/html", "multipart/alternative"}


def test_send_html_missing_image_skips_but_sends(monkeypatch, tmp_path):
    """内联图片文件缺失：跳过该图并记录日志，邮件仍正常发送"""
    FakeSMTP.calls = 0
    FakeSMTP.sent = {}
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    n = _notifier()
    ok = n.send_html("标题", "<p>x</p>", images=[("ba_x", tmp_path / "nope.png")])
    assert ok is True  # 缺失图片不阻断发送
    assert n.last_error == ""  # 发送成功清空错误位
    msg = _parse_mail()
    assert not any(p.get_content_type() == "image/png" for p in msg.walk())
