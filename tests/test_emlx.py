import plistlib

import pytest

from nodary.mail_store import EmlxError, read_emlx

RFC822 = (
    b"From: Ada <ada@example.com>\r\n"
    b"To: jacob@example.com\r\n"
    b"Subject: hello\r\n"
    b"\r\n"
    b"body text\r\n"
)


def make_emlx(tmp_path, name="1234.emlx", message=RFC822, plist=None, count=None):
    payload = plistlib.dumps(plist if plist is not None else {"flags": 0})
    body = str(count if count is not None else len(message)).encode() + b"\n"
    body += message + payload
    p = tmp_path / name
    p.write_bytes(body)
    return p


def test_roundtrip(tmp_path):
    msg = read_emlx(make_emlx(tmp_path))
    assert msg.rfc822 == RFC822
    assert msg.plist == {"flags": 0}
    assert not msg.partial


def test_partial_flag(tmp_path):
    msg = read_emlx(make_emlx(tmp_path, name="1234.partial.emlx"))
    assert msg.partial


def test_missing_count_line(tmp_path):
    p = tmp_path / "bad.emlx"
    p.write_bytes(b"no-count-here")
    with pytest.raises(EmlxError):
        read_emlx(p)


def test_count_exceeds_file(tmp_path):
    with pytest.raises(EmlxError):
        read_emlx(make_emlx(tmp_path, count=10_000))


def test_garbage_plist_is_tolerated(tmp_path):
    p = tmp_path / "1.emlx"
    p.write_bytes(str(len(RFC822)).encode() + b"\n" + RFC822 + b"<notaplist")
    msg = read_emlx(p)
    assert msg.rfc822 == RFC822
    assert msg.plist == {}
