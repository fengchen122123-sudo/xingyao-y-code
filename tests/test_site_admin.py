# -*- coding: utf-8 -*-

import json
import threading
import urllib.error
import urllib.request

from site_admin.server import (
    AdminError,
    SiteAdmin,
    AdminServer,
    project_public,
    validate_content,
)


def _sample(**overrides):
    notice = {
        "id": "n-demo",
        "title": "示例平台",
        "note": "新用户可领。",
        "href": "https://example.com/claim",
        "date": "2026-11-15",
        "published": True,
    }
    notice.update(overrides)
    return {
        "download": {
            "mode": "manual",
            "version": "1.3.9",
            "windowsUrl": "https://example.com/XingYaoCode-1.3.9.exe",
            "fileName": "XingYaoCode-1.3.9.exe",
        },
        "notices": [notice],
    }


def test_claim_guide_is_optional_and_must_be_https():
    without = validate_content(_sample())
    assert without["notices"][0]["guide"] == ""
    with_guide = validate_content(_sample(guide="https://example.com/how"))
    assert with_guide["notices"][0]["guide"] == "https://example.com/how"
    try:
        validate_content(_sample(guide="http://example.com/how"))
    except AdminError as exc:
        assert "领取流程" in str(exc)
    else:
        raise AssertionError("http 领取流程不应通过")


def test_manual_download_rejects_plain_http():
    payload = _sample()
    payload["download"]["windowsUrl"] = "http://example.com/XingYaoCode-1.3.9.exe"
    try:
        validate_content(payload)
    except AdminError as exc:
        assert "https://" in str(exc)
    else:
        raise AssertionError("http 下载地址不应通过")


def test_unpublished_notice_is_left_out_of_the_public_file():
    data = validate_content(_sample(published=False))
    public = project_public(data)
    assert public["notices"] == []
    assert public["download"]["version"] == "1.3.9"


def test_login_required_to_change_content(tmp_path):
    website = tmp_path / "website"
    state = tmp_path / "state"
    website.mkdir()
    (website / "index.html").write_text("<html>星瑶Code</html>", encoding="utf-8")
    admin = SiteAdmin(website, state, password="secret-pass")
    server = AdminServer(("127.0.0.1", 0), admin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        status, _, body = _request(port, "PUT", "/api/content", _sample())
        assert status == 401
        assert "登录" in body["error"]

        status, headers, _ = _request(
            port, "POST", "/api/login", {"username": "mou", "password": "wrong"}
        )
        assert status == 401

        status, headers, _ = _request(
            port, "POST", "/api/login", {"username": "mou", "password": "secret-pass"}
        )
        assert status == 200
        set_cookie = next(
            value for key, value in headers.items() if key.lower() == "set-cookie"
        )
        cookie = set_cookie.split(";", 1)[0]

        hidden = _sample(published=False, title="已下架")
        visible = _sample()
        visible["notices"].append(hidden["notices"][0])
        visible["notices"][1]["id"] = "n-hidden"
        status, _, saved = _request(port, "PUT", "/api/content", visible, cookie=cookie)
        assert status == 200
        assert [item["id"] for item in saved["notices"]] == ["n-demo", "n-hidden"]

        public = json.loads((website / "data" / "site.json").read_text(encoding="utf-8"))
        assert [item["id"] for item in public["notices"]] == ["n-demo"]

        assert admin.resolve_static("/../site_admin/secret.json") is None
        assert admin.resolve_static("/index.html").name == "index.html"
        status, _, home = _request(port, "GET", "/")
        assert status == 200
        assert "星瑶Code" in home

        status, _, changed = _request(
            port,
            "POST",
            "/api/password",
            {"currentPassword": "secret-pass", "newPassword": "new-pass-9"},
            cookie=cookie,
        )
        assert status == 200
        assert changed["ok"] is True
        status, _, _ = _request(
            port, "POST", "/api/login", {"username": "mou", "password": "secret-pass"}
        )
        assert status == 401
        status, _, _ = _request(
            port, "POST", "/api/login", {"username": "mou", "password": "new-pass-9"}
        )
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


def _request(port, method, path, body=None, cookie=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Origin": f"http://127.0.0.1:{port}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            raw = res.read()
            parsed = _parse(raw, res.headers.get("Content-Type", ""))
            return res.status, dict(res.headers), parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        parsed = _parse(raw, exc.headers.get("Content-Type", ""))
        return exc.code, dict(exc.headers), parsed


def _parse(raw: bytes, content_type: str):
    text = raw.decode("utf-8")
    if "json" in content_type:
        return json.loads(text or "{}")
    return text
