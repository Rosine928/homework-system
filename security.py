"""CSRF protection and conservative attachment validation without extra dependencies."""
import hmac
import os
from pathlib import Path
from secrets import token_urlsafe

from flask import abort, request, session


def secret_key(instance_path):
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    directory = Path(instance_path)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "secret.key"
    try:
        with path.open("x", encoding="ascii") as handle:
            handle.write(token_urlsafe(48))
    except FileExistsError:
        pass
    return path.read_text(encoding="ascii").strip()


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = token_urlsafe(32)
    return session["csrf_token"]


def protect_csrf():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = session.get("csrf_token")
        provided = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8")):
            abort(400, description="表单验证已失效，请刷新页面后重试。")


def valid_attachment(file, suffix):
    prefix = file.stream.read(4096)
    file.stream.seek(0)
    if not prefix or prefix.startswith((b"MZ", b"\x7fELF")):
        return False
    signatures = {
        "pdf": (b"%PDF-",), "png": (b"\x89PNG\r\n\x1a\n",), "jpg": (b"\xff\xd8\xff",),
        "jpeg": (b"\xff\xd8\xff",), "zip": (b"PK\x03\x04", b"PK\x05\x06"),
        "docx": (b"PK\x03\x04",), "xlsx": (b"PK\x03\x04",), "pptx": (b"PK\x03\x04",),
        "rar": (b"Rar!\x1a\x07",), "doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
        "xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",), "ppt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    }
    if suffix in signatures:
        return prefix.startswith(signatures[suffix])
    return b"\x00" not in prefix
