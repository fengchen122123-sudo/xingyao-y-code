# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBSITE_DIR = Path(os.environ.get("YCODE_WEBSITE_DIR") or (PROJECT_ROOT / "website"))
STATE_DIR = Path(os.environ.get("YCODE_STATE_DIR") or (PROJECT_ROOT / "site_admin"))
ADMIN_HTML = STATE_DIR / "admin.html"

HOST = "127.0.0.1"
PORT = int(os.environ.get("SITE_ADMIN_PORT") or "8930")
SESSION_TTL = 12 * 60 * 60
BODY_LIMIT = 256 * 1024
PBKDF2_ITERATIONS = 200_000
MAX_NOTICES = 100
LOGIN_LIMIT = 8
LOGIN_LOCK_SECONDS = 60
DEFAULT_USERNAME = "mou"
DEFAULT_PASSWORD = "1qaz.@WSX"
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

DEFAULT_DOWNLOAD = {
    "mode": "gitee",
    "version": "1.3.9",
    "windowsUrl": (
        "https://gitee.com/feng-chenhao/xingyao-y-code/releases/download/"
        "v1.3.9/XingYaoCode-1.3.9.exe"
    ),
    "fileName": "XingYaoCode-1.3.9.exe",
}

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HTTPS_RE = re.compile(r"^https://\S+$", re.IGNORECASE)
_EXE_RE = re.compile(r"^[\w.\-]+\.exe$", re.IGNORECASE)


class AdminError(Exception):
    """用户能看懂的校验或发布错误。"""


def _clean_text(value, limit: int, field: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if any(ord(ch) < 32 and ch != "\n" for ch in text):
        raise AdminError(f"{field}里不能有控制字符")
    if len(text) > limit:
        raise AdminError(f"{field}最多 {limit} 个字")
    return text


def _https_url(value, field: str) -> str:
    url = _clean_text(value, 500, field).replace("\n", "")
    if not _HTTPS_RE.match(url):
        raise AdminError(f"{field}必须是 https:// 开头的链接")
    return url


def _file_name_from_url(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    name = path.rsplit("/", 1)[-1]
    if _EXE_RE.match(name):
        return name
    return ""


def validate_content(payload) -> dict:
    """把后台提交整理成可保存的结构，不合法就抛 AdminError。"""
    if not isinstance(payload, dict):
        raise AdminError("提交的数据不是对象")
    raw_download = payload.get("download")
    if not isinstance(raw_download, dict):
        raise AdminError("缺少下载设置")
    mode = raw_download.get("mode")
    if mode not in ("gitee", "manual"):
        raise AdminError("下载方式只能是跟随 Gitee，或手动指定")
    version = _clean_text(raw_download.get("version"), 32, "版本号").replace("\n", "")
    if not _VERSION_RE.match(version):
        raise AdminError("版本号只能包含数字、字母、点、下划线和连字符")
    windows_url = _https_url(raw_download.get("windowsUrl"), "下载地址")
    file_name = _clean_text(raw_download.get("fileName"), 80, "文件名").replace("\n", "")
    if not file_name:
        file_name = _file_name_from_url(windows_url)
    if not _EXE_RE.match(file_name):
        raise AdminError("文件名需要是 Windows 安装包，例如 XingYaoCode-1.3.9.exe")

    raw_notices = payload.get("notices")
    if not isinstance(raw_notices, list):
        raise AdminError("公告列表格式不对")
    if len(raw_notices) > MAX_NOTICES:
        raise AdminError(f"公告最多 {MAX_NOTICES} 条")

    notices = []
    seen = set()
    for item in raw_notices:
        if not isinstance(item, dict):
            raise AdminError("有一条公告格式不对")
        notice_id = str(item.get("id") or "").strip()
        if not _ID_RE.match(notice_id):
            notice_id = "n-" + secrets.token_hex(6)
        if notice_id in seen:
            raise AdminError("有两条公告用了同一个编号")
        seen.add(notice_id)
        title = _clean_text(item.get("title"), 80, "公告标题")
        if not title:
            raise AdminError("公告标题不能为空")
        note = _clean_text(item.get("note"), 400, "公告说明")
        href = _https_url(item.get("href"), "领取链接")
        guide_raw = str(item.get("guide") or "").strip()
        guide = _https_url(guide_raw, "领取流程") if guide_raw else ""
        date = _clean_text(item.get("date"), 10, "日期").replace("\n", "")
        if date:
            if not _DATE_RE.match(date):
                raise AdminError("日期要写成 2026-11-15 这样")
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError as exc:
                raise AdminError("日期不是真实的一天") from exc
        if not isinstance(item.get("published"), bool):
            raise AdminError("上架状态必须是是或否")
        notices.append(
            {
                "id": notice_id,
                "title": title,
                "note": note,
                "href": href,
                "guide": guide,
                "date": date,
                "published": item["published"],
            }
        )
    return {
        "download": {
            "mode": mode,
            "version": version,
            "windowsUrl": windows_url,
            "fileName": file_name,
        },
        "notices": notices,
    }


def project_public(data: dict) -> dict:
    """访客文件只保留已上架公告。"""
    return {
        "download": dict(data["download"]),
        "notices": [dict(item) for item in data["notices"] if item["published"]],
    }


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _hash_password(password: str, salt: bytes | None = None) -> dict:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return {
        "salt": salt.hex(),
        "hash": digest.hex(),
        "iterations": PBKDF2_ITERATIONS,
    }


def _password_matches(password: str, record: dict) -> bool:
    salt = bytes.fromhex(record["salt"])
    iterations = int(record.get("iterations") or PBKDF2_ITERATIONS)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(digest.hex(), record["hash"])


def deploy_is_ready() -> bool:
    needed = ("DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_REMOTE_ROOT", "YA_SSH_PWD")
    return all(str(os.environ.get(name) or "").strip() for name in needed)


def publish_site_json(local_path: Path) -> str:
    """把公开的 site.json 上传到现有部署配置指向的站点目录。"""
    missing = [
        name
        for name in ("DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_REMOTE_ROOT", "YA_SSH_PWD")
        if not str(os.environ.get(name) or "").strip()
    ]
    if missing:
        raise AdminError("还不能发布到线上，缺少 " + "、".join(missing))
    try:
        import paramiko
    except ImportError as exc:
        raise AdminError("本机没有 paramiko，无法上传") from exc

    host = os.environ["DEPLOY_HOST"].strip()
    user = os.environ["DEPLOY_USER"].strip()
    root = os.environ["DEPLOY_REMOTE_ROOT"].strip().rstrip("/")
    password = os.environ["YA_SSH_PWD"]
    port = int(os.environ.get("DEPLOY_PORT") or "22")
    remote_dir = f"{root}/data"
    remote_path = f"{remote_dir}/site.json"

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            timeout=20,
        )
        with ssh.open_sftp() as sftp:
            _ensure_remote_dir(sftp, remote_dir)
            sftp.put(str(local_path), remote_path)
    except Exception as exc:
        raise AdminError(f"上传到线上失败：{exc}") from exc
    finally:
        ssh.close()
    return remote_path


def _ensure_remote_dir(sftp, remote_dir: str) -> None:
    if remote_dir in ("", "/"):
        return
    try:
        sftp.stat(remote_dir)
        return
    except IOError:
        pass
    parent = remote_dir.rsplit("/", 1)[0]
    _ensure_remote_dir(sftp, parent)
    try:
        sftp.mkdir(remote_dir)
    except IOError:
        pass


class SiteAdmin:
    def __init__(
        self,
        website_dir: Path,
        state_dir: Path,
        password: str | None = None,
        username: str | None = None,
    ):
        self.website_dir = Path(website_dir)
        self.state_dir = Path(state_dir)
        self.public_path = self.website_dir / "data" / "site.json"
        self.store_path = self.state_dir / "store.json"
        self.db_path = self.state_dir / "admin.db"
        self.admin_html = self.state_dir / "admin.html"
        self.sessions: dict[str, dict] = {}
        self.login_failures: dict[str, tuple[int, float]] = {}
        self.lock = threading.Lock()
        self._ensure_accounts(username, password)
        self._ensure_store()

    @contextmanager
    def _connect(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_accounts(self, username: str | None, password: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_user (
                    username TEXT PRIMARY KEY,
                    salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    iterations INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            count = conn.execute("SELECT COUNT(*) AS n FROM admin_user").fetchone()["n"]
            if count:
                return
            name = (username or DEFAULT_USERNAME).strip()
            secret = password if password else DEFAULT_PASSWORD
            self._insert_user(conn, name, secret)

    def _insert_user(self, conn: sqlite3.Connection, username: str, password: str) -> None:
        if not _USERNAME_RE.match(username):
            raise AdminError("账号只能包含字母、数字、下划线和连字符")
        if len(password) < 8:
            raise AdminError("密码至少 8 位")
        record = _hash_password(password)
        conn.execute(
            """
            INSERT INTO admin_user (username, salt, password_hash, iterations, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                username,
                record["salt"],
                record["hash"],
                record["iterations"],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    def authenticate(self, username, password) -> str:
        name = str(username or "").strip()
        secret = password if isinstance(password, str) else ""
        if not name or not secret:
            return ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username, salt, password_hash, iterations FROM admin_user WHERE username = ?",
                (name,),
            ).fetchone()
        if row is None:
            return ""
        matched = _password_matches(
            secret,
            {
                "salt": row["salt"],
                "hash": row["password_hash"],
                "iterations": row["iterations"],
            },
        )
        return row["username"] if matched else ""

    def change_password(self, username: str, current: str, new_password: str) -> None:
        if not self.authenticate(username, current):
            raise AdminError("当前密码不对")
        updated = str(new_password or "")
        if len(updated) < 8:
            raise AdminError("新密码至少 8 位")
        if updated == current:
            raise AdminError("新密码要和当前密码不一样")
        record = _hash_password(updated)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE admin_user
                SET salt = ?, password_hash = ?, iterations = ?, updated_at = ?
                WHERE username = ?
                """,
                (
                    record["salt"],
                    record["hash"],
                    record["iterations"],
                    datetime.now().isoformat(timespec="seconds"),
                    username,
                ),
            )

    def _ensure_store(self) -> None:
        if self.store_path.is_file():
            data = validate_content(_read_json(self.store_path))
            _atomic_write(self.public_path, project_public(data))
            return
        if self.public_path.is_file():
            raw = _read_json(self.public_path)
            notices = []
            for item in raw.get("notices") or []:
                if isinstance(item, dict) and "published" not in item:
                    item = dict(item)
                    item["published"] = True
                notices.append(item)
            raw["notices"] = notices
            if "download" not in raw:
                raw["download"] = dict(DEFAULT_DOWNLOAD)
            data = validate_content(raw)
        else:
            data = validate_content({"download": dict(DEFAULT_DOWNLOAD), "notices": []})
        _atomic_write(self.store_path, data)
        _atomic_write(self.public_path, project_public(data))

    def load(self) -> dict:
        with self.lock:
            return validate_content(_read_json(self.store_path))

    def save(self, payload) -> dict:
        data = validate_content(payload)
        with self.lock:
            _atomic_write(self.store_path, data)
            _atomic_write(self.public_path, project_public(data))
        return data

    def login_blocked(self, ip: str, now: float) -> bool:
        with self.lock:
            fails, until = self.login_failures.get(ip, (0, 0.0))
            return fails >= LOGIN_LIMIT and now < until

    def record_login(self, ip: str, ok: bool, now: float) -> None:
        with self.lock:
            if ok:
                self.login_failures.pop(ip, None)
                return
            fails, until = self.login_failures.get(ip, (0, 0.0))
            if now >= until:
                fails = 0
            fails += 1
            lock_until = now + LOGIN_LOCK_SECONDS if fails >= LOGIN_LIMIT else now
            self.login_failures[ip] = (fails, lock_until)

    def open_session(self, username: str, now: float) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            self._drop_expired(now)
            self.sessions[token] = {"expiry": now + SESSION_TTL, "username": username}
        return token

    def session_username(self, token: str, now: float) -> str:
        if not token:
            return ""
        with self.lock:
            item = self.sessions.get(token)
            if not item or item["expiry"] < now:
                self.sessions.pop(token, None)
                return ""
            item["expiry"] = now + SESSION_TTL
            return item["username"]

    def session_ok(self, token: str, now: float) -> bool:
        return bool(self.session_username(token, now))

    def close_session(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)

    def _drop_expired(self, now: float) -> None:
        expired = [token for token, item in self.sessions.items() if item["expiry"] < now]
        for token in expired:
            self.sessions.pop(token, None)

    def resolve_static(self, url_path: str) -> Path | None:
        path = urllib.parse.unquote(url_path.split("?", 1)[0])
        if path in ("", "/"):
            path = "/index.html"
        relative = path.lstrip("/")
        if not relative or relative.endswith("/"):
            return None
        root = self.website_dir.resolve()
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            return None
        if not candidate.is_file():
            return None
        return candidate


def _cookie_token(header: str) -> str:
    if not header:
        return ""
    cookie = SimpleCookie()
    cookie.load(header)
    morsel = cookie.get("site_admin")
    return morsel.value if morsel else ""


def _allowed_hosts() -> set[str]:
    hosts = {"127.0.0.1", "localhost", "xingyaocode.cn", "www.xingyaocode.cn"}
    extra = os.environ.get("SITE_ADMIN_HOSTS") or ""
    for item in extra.split(","):
        item = item.strip()
        if item:
            hosts.add(item)
    return hosts


def _origin_ok(handler: BaseHTTPRequestHandler) -> bool:
    host = (handler.headers.get("Host") or "").split(":")[0]
    allowed = _allowed_hosts()
    if host not in allowed:
        return False
    if (handler.headers.get("Sec-Fetch-Site") or "") == "same-origin":
        return True
    origin = handler.headers.get("Origin") or handler.headers.get("Referer") or ""
    if not origin:
        return False
    return urllib.parse.urlparse(origin).hostname in allowed


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    return handler.client_address[0] if handler.client_address else ""


class AdminServer(ThreadingHTTPServer):
    def __init__(self, address, admin: SiteAdmin):
        self.admin = admin
        super().__init__(address, self._make_handler())
        self.daemon_threads = True

    def _make_handler(self):
        admin = self.admin

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def do_PUT(self):
                self._dispatch("PUT")

            def do_DELETE(self):
                self._send_json(405, {"error": "不支持这个操作"})

            def log_message(self, fmt, *args):
                return

            def _dispatch(self, method: str) -> None:
                try:
                    path = urllib.parse.urlparse(self.path).path
                    if path == "/api/login" and method == "POST":
                        self._login()
                        return
                    if path == "/api/logout" and method == "POST":
                        self._logout()
                        return
                    if path == "/api/password" and method == "POST":
                        self._change_password()
                        return
                    if path == "/api/session" and method == "GET":
                        self._session()
                        return
                    if path == "/api/content" and method == "GET":
                        self._read_content()
                        return
                    if path == "/api/content" and method == "PUT":
                        self._write_content()
                        return
                    if path == "/api/publish" and method == "POST":
                        self._publish()
                        return
                    if method == "GET" and path in ("/admin", "/admin/"):
                        self._send_file(admin.admin_html, "text/html; charset=utf-8")
                        return
                    if method != "GET":
                        self._send_json(405, {"error": "不支持这个操作"})
                        return
                    static = admin.resolve_static(path)
                    if static is None:
                        self._send_json(404, {"error": "没有这个页面"})
                        return
                    mime = mimetypes.guess_type(static.name)[0] or "application/octet-stream"
                    if mime.startswith("text/") or mime == "application/json":
                        mime += "; charset=utf-8"
                    self._send_file(static, mime, no_store=static.name == "site.json")
                except AdminError as exc:
                    self._send_json(400, {"error": str(exc)})
                except (json.JSONDecodeError, UnicodeError, ValueError):
                    self._send_json(400, {"error": "请求内容无法读取"})
                except Exception:
                    self._send_json(500, {"error": "后台内部出错"})

            def _authorized(self) -> bool:
                token = _cookie_token(self.headers.get("Cookie", ""))
                return admin.session_ok(token, self._now())

            def _require_auth(self) -> bool:
                if not _origin_ok(self):
                    self._send_json(403, {"error": "请求来源不被接受"})
                    return False
                if not self._authorized():
                    self._send_json(401, {"error": "请先登录"})
                    return False
                return True

            def _username(self) -> str:
                token = _cookie_token(self.headers.get("Cookie", ""))
                return admin.session_username(token, self._now())

            def _login(self) -> None:
                if not _origin_ok(self):
                    self._send_json(403, {"error": "请求来源不被接受"})
                    return
                now = self._now()
                ip = _client_ip(self)
                if admin.login_blocked(ip, now):
                    self._send_json(429, {"error": "试得太多了，请一分钟后再试"})
                    return
                body = self._read_body()
                username = body.get("username") if isinstance(body, dict) else None
                password = body.get("password") if isinstance(body, dict) else None
                name = admin.authenticate(username, password)
                if not name:
                    admin.record_login(ip, False, now)
                    self._send_json(401, {"error": "账号或密码不对"})
                    return
                admin.record_login(ip, True, now)
                token = admin.open_session(name, now)
                self._send_json(
                    200,
                    {"ok": True, "username": name},
                    extra={"Set-Cookie": _session_cookie(token, SESSION_TTL)},
                )

            def _change_password(self) -> None:
                if not self._require_auth():
                    return
                body = self._read_body()
                current = body.get("currentPassword") if isinstance(body, dict) else None
                new_password = body.get("newPassword") if isinstance(body, dict) else None
                if not isinstance(current, str) or not isinstance(new_password, str):
                    self._send_json(400, {"error": "请填写当前密码和新密码"})
                    return
                admin.change_password(self._username(), current, new_password)
                self._send_json(200, {"ok": True})

            def _logout(self) -> None:
                if not _origin_ok(self):
                    self._send_json(403, {"error": "请求来源不被接受"})
                    return
                admin.close_session(_cookie_token(self.headers.get("Cookie", "")))
                self._send_json(
                    200,
                    {"ok": True},
                    extra={"Set-Cookie": _session_cookie("", 0)},
                )

            def _session(self) -> None:
                username = self._username()
                self._send_json(200, {"ok": bool(username), "username": username})

            def _read_content(self) -> None:
                if not self._require_auth():
                    return
                data = admin.load()
                data["deployReady"] = deploy_is_ready()
                self._send_json(200, data)

            def _write_content(self) -> None:
                if not self._require_auth():
                    return
                saved = admin.save(self._read_body())
                saved["deployReady"] = deploy_is_ready()
                self._send_json(200, saved)

            def _publish(self) -> None:
                if not self._require_auth():
                    return
                remote = publish_site_json(admin.public_path)
                self._send_json(200, {"ok": True, "remote": remote})

            def _read_body(self):
                length = int(self.headers.get("Content-Length") or "0")
                if length < 0 or length > BODY_LIMIT:
                    raise AdminError("提交的内容太大")
                raw = self.rfile.read(length) if length else b""
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))

            def _now(self) -> float:
                import time

                return time.time()

            def _send_json(self, status: int, payload: dict, extra: dict | None = None) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send_bytes(
                    status,
                    body,
                    "application/json; charset=utf-8",
                    no_store=True,
                    extra=extra,
                )

            def _send_file(self, path: Path, content_type: str, no_store: bool = False) -> None:
                if not path.is_file():
                    self._send_json(404, {"error": "没有这个页面"})
                    return
                self._send_bytes(200, path.read_bytes(), content_type, no_store=no_store)

            def _send_bytes(
                self,
                status: int,
                body: bytes,
                content_type: str,
                no_store: bool = False,
                extra: dict | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                if no_store:
                    self.send_header("Cache-Control", "no-store")
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        return Handler


def _session_cookie(token: str, max_age: int) -> str:
    return (
        f"site_admin={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"
    )


def main() -> None:
    username = os.environ.get("SITE_ADMIN_USER") or None
    password = os.environ.get("SITE_ADMIN_PASSWORD") or None
    admin = SiteAdmin(WEBSITE_DIR, STATE_DIR, password=password, username=username)
    if not ADMIN_HTML.is_file():
        raise SystemExit(f"找不到后台页面: {ADMIN_HTML}")
    server = AdminServer((HOST, PORT), admin)
    print(f"星瑶站点后台已启动：http://{HOST}:{PORT}/admin", flush=True)
    print(f"本地预览首页：http://{HOST}:{PORT}/", flush=True)
    print("使用账号和密码登录。密码保存在 site_admin/admin.db，可在后台修改。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n后台已关闭。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
