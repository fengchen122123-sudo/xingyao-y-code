import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlencode

sys.stdout.reconfigure(encoding="utf-8")

GITEE_REPO = "feng-chenhao/xingyao-y-code"
API_BASE = "https://gitee.com/api/v5"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
RELEASE_TARGET_BRANCH = "dev"


def resolve_token():
    """GITEE_TOKEN 环境变量优先；否则读 git credential 里 gitee.com 的 HTTPS 凭据。

    返回 (token, 来源)。凭据来自 Windows 凭据管理器（git push 时保存），
    全程不打印令牌内容。
    """
    tok = (os.environ.get("GITEE_TOKEN") or "").strip()
    if tok:
        return tok, "环境变量 GITEE_TOKEN"
    try:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input=b"protocol=https\nhost=gitee.com\n\n",
            capture_output=True,
            env=env,
            timeout=15,
        )
    except Exception:
        return "", ""
    if proc.returncode != 0:
        return "", ""
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip(), "git credential (gitee.com)"
    return "", ""


def _api_request(path, token, method="GET", params=None, data=None,
                 headers=None, timeout=60):
    url = f"{API_BASE}{path}"
    if method == "GET":
        merged = dict(params or {})
        merged["access_token"] = token
        url += "?" + urlencode(merged)
    hdrs = {"User-Agent": "ycode-publisher"}
    if headers:
        hdrs.update(headers)
    body = data
    if isinstance(data, dict):
        merged = dict(data)
        merged["access_token"] = token
        body = urlencode(merged).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class MultipartBody:

    def __init__(self, field, filename, filepath, extra_fields=None):
        self.boundary = "----ycodepub" + uuid.uuid4().hex
        prefix = []
        for k, v in (extra_fields or {}).items():
            prefix.append(
                f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
            )
        prefix.append(
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        )
        self._prefix = "".join(prefix).encode("utf-8")
        self._suffix = f"\r\n--{self.boundary}--\r\n".encode("utf-8")
        self._filepath = filepath
        self._filesize = os.path.getsize(filepath)
        self.length = len(self._prefix) + self._filesize + len(self._suffix)
        self._file = None
        self._phase = 0

    def read(self, n=-1):
        """读取最多 n 字节，跨阶段聚合。

        关键：文件读完（phase 1 → 2）的当次调用必须接续返回结尾边界，
        绝不能返回空字节——http.client 把空返回视为请求体 EOF，
        会漏发结尾边界导致 multipart 格式错误、服务器直接断连。
        """
        if self._phase == 3:
            return b""
        limit = None if (n is None or n < 0) else n
        parts = []
        total = 0
        while self._phase < 3 and (limit is None or total < limit):
            budget = None if limit is None else limit - total
            if self._phase == 0:
                src = self._prefix
                take = len(src) if budget is None else min(len(src), budget)
                parts.append(src[:take])
                self._prefix = src[take:]
                total += take
                if not self._prefix:
                    self._phase = 1
                    self._file = open(self._filepath, "rb")
            elif self._phase == 1:
                chunk = self._file.read(budget) if budget else self._file.read()
                if chunk:
                    parts.append(chunk)
                    total += len(chunk)
                else:
                    self._file.close()
                    self._file = None
                    self._phase = 2
            else:
                src = self._suffix
                take = len(src) if budget is None else min(len(src), budget)
                parts.append(src[:take])
                self._suffix = src[take:]
                total += take
                if not self._suffix:
                    self._phase = 3
        return b"".join(parts)


def read_project_version():
    path = os.path.join(PROJECT_DIR, "core", "version.py")
    with open(path, encoding="utf-8") as f:
        m = re.search(r"__version__\s*=\s*[\"']([^\"']+)", f.read())
    if not m:
        print("❌ 无法从 core/version.py 解析版本号")
        sys.exit(1)
    return m.group(1)


def changelog_section(version):
    path = os.path.join(PROJECT_DIR, "assets", "CHANGELOG.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(
        rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", text, re.M | re.S
    )
    return m.group(1).strip() if m else ""


def read_main_sha256(notes_path):
    """从 RELEASE_NOTES.txt 提取主安装包 SHA256（第一个 SHA256 行）。"""
    with open(notes_path, encoding="utf-8") as f:
        m = re.search(r"SHA256[:：]\s*([0-9a-fA-F]{64})", f.read())
    return m.group(1).lower() if m else ""


def build_release_body(version, sha256, part_files):
    """组装发布说明：更新条目 + SHA256 校验行（格式勿动，updater 依赖）+ 手动合并命令。"""
    section = changelog_section(version) or "（见 assets/CHANGELOG.md）"
    lines = ["## 更新内容", "", section, "", f"SHA256: {sha256}", ""]

    split_groups = {}
    for name in part_files:
        m = re.match(r"(.+?)\.part\d+$", name)
        if m:
            split_groups.setdefault(m.group(1), []).append(name)
    if split_groups:
        lines.append("### 手动下载合并（未走应用内自动更新的用户）")
        lines.append("")
        lines.append("下载同一安装包的全部分卷放到同一目录后，按你的终端类型执行合并：")
        lines.append("")
        for base, parts in split_groups.items():
            joined = "+".join(parts)
            lines.append(f"**{base}**")
            lines.append("")
            lines.append("CMD 用户：")
            lines.append(f"```cmd")
            lines.append(f"copy /b {joined} {base}")
            lines.append(f"```")
            lines.append("")
            lines.append("PowerShell 用户：")
            lines.append(f"```powershell")
            lines.append(f"cmd /c \"copy /b {joined} {base}\"")
            lines.append(f"```")
            lines.append("")

    return "\n".join(lines).strip()


def list_releases(token):
    return _api_request(f"/repos/{GITEE_REPO}/releases", token,
                        params={"page": 1, "per_page": 50, "direction": "desc"})


def get_release_by_tag(token, tag):
    """按 tag 取 Release 详情（含已上传附件列表 assets）。"""
    return _api_request(f"/repos/{GITEE_REPO}/releases/tags/{tag}", token)


def create_release(token, tag, name, body):
    return _api_request(
        f"/repos/{GITEE_REPO}/releases", token, method="POST",
        data={
            "tag_name": tag,
            "name": name,
            "body": body,
            "target_commitish": RELEASE_TARGET_BRANCH,
            "prerelease": "false",
        },
    )


def delete_release(token, release_id):
    return _api_request(
        f"/repos/{GITEE_REPO}/releases/{release_id}", token, method="DELETE"
    )


def touch_release(token, release_id, tag, name, body):
    """PATCH 触碰 Release，触发 Gitee 重算 /releases/latest 指针。

    实测 Gitee 在 API 创建 Release 后该指针会滞留在旧版本，而存量客户端
    的更新器恰好只请求该接口。PATCH 必须携带 tag_name，否则 400；body
    追加换行形成真实变更（SHA256 行解析不受影响）。
    """
    return _api_request(
        f"/repos/{GITEE_REPO}/releases/{release_id}", token, method="PATCH",
        data={"tag_name": tag, "name": name, "body": body + "\n"},
    )


def upload_attachment(token, release_id, filepath, retries=3):
    """上传单个附件；Gitee 附件接口字段名为 file，失败时回退 files。

    网络抖动（远端断连等）自动重试 retries 次，每次重建请求体。
    """
    field_names = ("file", "files")
    last_err = None
    for attempt in range(1, retries + 1):
        for field in field_names:
            body = MultipartBody(
                field,
                os.path.basename(filepath),
                filepath,
                extra_fields={"access_token": token},
            )
            try:
                return _api_request(
                    f"/repos/{GITEE_REPO}/releases/{release_id}/attach_files",
                    token, method="POST",
                    data=body,
                    headers={
                        "Content-Type": f"multipart/form-data; boundary={body.boundary}",
                        "Content-Length": str(body.length),
                    },
                    timeout=1800,
                )
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (400, 422):
                    continue
                raise
            except Exception as e:
                last_err = e
                break
        if attempt < retries:
            print(f"   ↻ 第 {attempt} 次失败（{last_err}），重试...", flush=True)
            import time
            time.sleep(3 * attempt)
    raise last_err


def delete_attachment(token, release_id, attach_file_id):
    """删除单个 Release 附件。Gitee 该接口成功返回 204（无响应体）。"""
    url = (f"{API_BASE}/repos/{GITEE_REPO}/releases/{release_id}"
           f"/attach_files/{attach_file_id}?access_token={token}")
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"User-Agent": "ycode-publisher"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status in (200, 204)


def verify_remote_attachment(download_url, local_path, timeout=30.0):
    try:
        req = urllib.request.Request(
            download_url, headers={"User-Agent": "ycode-publisher-verify"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = str(resp.headers.get("Content-Type", "")).lower()
            remote_len = int(resp.headers.get("Content-Length") or 0)
            remote_head = resp.read(64)
    except Exception as e:
        return False, f"回读校验请求失败: {e}"
    if "text/html" in content_type or "text/plain" in content_type:
        return False, (f"附件被 CDN 标记为网页/文本 ({content_type})，"
                       "疑似内容嗅探篡改（如 UTF-16 BOM 注入）")
    local_size = os.path.getsize(local_path)
    if remote_len and remote_len != local_size:
        return False, (f"附件大小不一致（远端 {remote_len} != 本地 {local_size} "
                       f"字节），疑似被注入/截断")
    with open(local_path, "rb") as f:
        local_head = f.read(64)
    if remote_head != local_head:
        return False, "附件首 64 字节与本地不一致，内容已被篡改"
    return True, ""


def upload_and_verify(token, release_id, filepath):
    last_reason = ""
    for attempt in (1, 2):
        resp = upload_attachment(token, release_id, filepath)
        download_url = str((resp or {}).get("browser_download_url") or "")
        if not download_url:
            last_reason = "上传响应缺少 browser_download_url，无法回读校验"
        else:
            ok, reason = verify_remote_attachment(download_url, filepath)
            if ok:
                return resp
            last_reason = reason
        # 校验不过：删掉这片坏附件再重传（响应里没有 id 时只能放弃删除，
        # 但重传覆盖前必须先尝试，避免坏片残留）
        attach_id = (resp or {}).get("id")
        if attach_id:
            try:
                delete_attachment(token, release_id, attach_id)
            except Exception as e:
                print(f"   ⚠ 删除校验失败的附件(id={attach_id})出错: {e}")
        if attempt == 1:
            print(f"   ⚠ 上传后校验失败（{last_reason}），已删除坏附件，重传一次...",
                  flush=True)
    raise RuntimeError(
        f"{os.path.basename(filepath)} 重传后仍未通过回读校验: {last_reason}"
    )


def fmt_size_mb(path):
    return os.path.getsize(path) / 1024 / 1024


def parse_version_from_tag(tag):
    """从 tag 提取版本号元组供比较（'v1.2.8' -> (1,2,8)）。"""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(map(int, m.groups())) if m else (0, 0, 0)


def cleanup_old_releases(token, current_tag, releases, keep_last_n=2):
    if not releases:
        return

    sorted_releases = sorted(
        releases,
        key=lambda r: parse_version_from_tag(r.get("tag_name", "")),
        reverse=True,
    )

    to_delete = sorted_releases[keep_last_n:]
    if not to_delete:
        print(f"ℹ 现有 Release 共 {len(releases)} 个，无需清理（保留最近 {keep_last_n} 个）")
        return

    print(f"\n🗑️  自动清理旧 Release（保留最近 {keep_last_n} 个版本）...")
    for r in to_delete:
        tag = r.get("tag_name", "")
        rid = r.get("id")
        name = r.get("name", "")
        try:
            delete_release(token, rid)
            print(f"   ✔ 已删除 {tag}  {name}  (id={rid})")
        except Exception as e:
            print(f"   ⚠ 删除 {tag} 失败: {e}（可手动到网页端删除）")


def main():
    parser = argparse.ArgumentParser(description="一键发布 dist/release_parts/ 到 Gitee Releases")
    parser.add_argument("--check", action="store_true", help="只验证令牌并列出现有 Release")
    parser.add_argument("--dry-run", action="store_true", help="打印发布内容但不实际调用写接口")
    parser.add_argument("--force", action="store_true", help="tag 已存在时删除旧 Release 重建")
    parser.add_argument("--attach-only", action="store_true",
                        help="不创建 Release，只向已存在的 Release 补传缺失附件（断点续传）")
    parser.add_argument("--verify", action="store_true",
                        help="只回读校验当前版本 Release 的远端附件与本地分卷是否一致，不上传")
    parser.add_argument("--cleanup", action="store_true",
                        help="发布成功后清理旧 Release（保留最近 2 个版本）。默认不清理，"
                             "旧版本由维护者手动在网页端删除")
    args = parser.parse_args()

    token, source = resolve_token()
    if not token and not args.dry_run:
        print("❌ 未取得 Gitee 令牌：既没有 GITEE_TOKEN 环境变量，git credential 里也没有 gitee.com 凭据。")
        print("   请到 https://gitee.com/profile/personal_access_tokens 创建私人令牌（勾选 projects），")
        print("   然后 $env:GITEE_TOKEN='令牌' 后重新运行。")
        sys.exit(1)

    releases = []
    if token and not args.dry_run:
        try:
            me = _api_request("/user", token)
            print(f"✅ 令牌有效（来源: {source}），账号: {me.get('login') or me.get('name')}")
        except Exception as e:
            print(f"❌ 令牌无法调用 Gitee API（来源: {source}）：{e}")
            print("   git 存的凭据可能是账号密码而非私人令牌。请创建私人令牌（勾选 projects）后：")
            print("   $env:GITEE_TOKEN='令牌' 然后重试")
            sys.exit(1)

        try:
            releases = list_releases(token)
            print(f"ℹ 现有 Release {len(releases)} 个：")
            for r in releases[:10]:
                print(f"   - {r.get('tag_name')}  {r.get('name')}  (id={r.get('id')})")
        except Exception as e:
            print(f"⚠ 读取 Release 列表失败：{e}")

    if args.check:
        return

    version = read_project_version()
    tag = f"v{version}"
    parts_dir = os.path.join(PROJECT_DIR, "dist", "release_parts")
    notes_path = os.path.join(parts_dir, "RELEASE_NOTES.txt")
    if not os.path.isdir(parts_dir) or not os.path.exists(notes_path):
        print(f"❌ 未找到 {parts_dir}（先运行 build_release.py 生成分卷产物）")
        sys.exit(1)

    raw_files = [
        f for f in os.listdir(parts_dir)
        if f != "RELEASE_NOTES.txt" and os.path.isfile(os.path.join(parts_dir, f))
    ]

    def _upload_sort_key(name):
        # 上传顺序必须保证：主安装包（含其分卷组）整体排在 CLI 安装包组之前。
        # 原因：旧版客户端（1.2.7~1.2.27）的 _pick_main_installer_group 在认不出
        # 主安装包前缀时，会直接取分卷组字典里的第一组；而该字典按附件在 Release
        # 中的先后（即上传顺序）插入。因此主安装包组必须先上传，否则老用户的
        # 自动更新会挑到 CLI 组（挑错包 / SHA256 校验失败）。
        stem = re.sub(r"\.part\d+$", "", name)
        low = stem.lower()
        is_cli = "cli" in low
        is_main = (low.startswith("xingyaocode") or low.startswith("y-code")) and not is_cli
        group_rank = 0 if is_main else (1 if is_cli else 2)
        m = re.search(r"\.part(\d+)$", name)
        part_no = int(m.group(1)) if m else 0
        return (group_rank, low, part_no)

    part_files = sorted(raw_files, key=_upload_sort_key)
    if not part_files:
        print("❌ dist/release_parts/ 下没有可上传的附件")
        sys.exit(1)

    if args.verify:
        try:
            release = get_release_by_tag(token, tag)
        except Exception as e:
            print(f"❌ Release {tag} 不存在或读取失败：{e}")
            sys.exit(1)
        url_by_name = {
            str(a.get("name") or ""): str(a.get("browser_download_url") or "")
            for a in (release.get("assets") or [])
        }
        bad = 0
        for name in part_files:
            url = url_by_name.get(name)
            if not url:
                print(f"❌ {name}: Release 上缺少该附件")
                bad += 1
                continue
            ok, reason = verify_remote_attachment(
                url, os.path.join(parts_dir, name))
            if ok:
                print(f"✔ {name}")
            else:
                print(f"❌ {name}: {reason}")
                bad += 1
        if bad:
            print(f"\n⚠ {bad}/{len(part_files)} 个附件校验失败：删除坏附件后用 "
                  f"--attach-only 重传（若重传仍被篡改，需调整分卷切分边界，"
                  f"见 build_release.py 的 _safe_part_boundary）")
            sys.exit(1)
        print(f"\n🎉 全部 {len(part_files)} 个远端附件与本地一致。")
        return

    sha256 = read_main_sha256(notes_path)
    if not sha256:
        print("❌ RELEASE_NOTES.txt 里没有 SHA256 行（自动更新完整性校验依赖它，不能缺）")
        sys.exit(1)

    body = build_release_body(version, sha256, part_files)

    print(f"\n📦 即将发布: {tag} -> {GITEE_REPO} (目标分支 {RELEASE_TARGET_BRANCH})")
    for f in part_files:
        print(f"   - {f}  {fmt_size_mb(os.path.join(parts_dir, f)):.1f} MB")
    print(f"\n发布说明预览:")
    print("-" * 56)
    print(body)
    print("-" * 56)

    if args.dry_run:
        print("\n[dry-run] 未实际创建 Release。")
        return

    if args.attach_only:
        try:
            release = get_release_by_tag(token, tag)
        except Exception as e:
            print(f"❌ Release {tag} 不存在或读取失败：{e}（先不带 --attach-only 完整发布一次）")
            sys.exit(1)
        release_id = release.get("id")
        existing_names = {str(a.get("name") or "") for a in (release.get("assets") or [])}
        missing = [f for f in part_files if f not in existing_names]
        print(f"\n🔁 补传模式: Release {tag} (id={release_id})")
        print(f"   已有附件 {len(existing_names)} 个: {sorted(existing_names) or '无'}")
        if not missing:
            print("✔ 全部附件已存在，无需补传。")
            return
        ok, failed = 0, []
        total = len(missing)
        for i, name in enumerate(missing, 1):
            path = os.path.join(parts_dir, name)
            print(f"⬆ [{i}/{total}] 补传 {name} ({fmt_size_mb(path):.1f} MB) ...", flush=True)
            try:
                upload_and_verify(token, release_id, path)
                ok += 1
                print("✔ 完成")
            except Exception as e:
                failed.append((name, str(e)))
                print(f"❌ 失败: {e}")
        if failed:
            print(f"⚠ 补传完成 {ok}/{total}，失败的可重跑 --attach-only 续传：")
            for name, err in failed:
                print(f"   - {name}: {err}")
            sys.exit(1)
        print(f"🎉 补传完成，Release {tag} 附件齐全。")
        try:
            touch_release(token, release_id, tag, f"Y-CODE v{version}", body)
            print("✔ 已触碰 Release，latest 指针已刷新")
        except Exception as e:
            print(f"⚠ 触碰 Release 失败（latest 指针可能滞留旧版，可网页端手动编辑一次）：{e}")
        print(f"👉 https://gitee.com/{GITEE_REPO}/releases/tag/{tag}")
        return

    existing = next((r for r in releases if r.get("tag_name") == tag), None) if releases else None
    if existing:
        if not args.force:
            print(f"❌ Release {tag} 已存在 (id={existing.get('id')})。确认要覆盖时加 --force 重新运行。")
            sys.exit(1)
        print(f"⚠ --force：删除旧 Release {tag} (id={existing.get('id')}) ...")
        delete_release(token, existing["id"])
        print("✔ 旧 Release 已删除")

    print(f"\n🚀 创建 Release {tag} ...")
    release = create_release(token, tag, f"Y-CODE v{version}", body)
    release_id = release.get("id")
    if not release_id:
        print(f"❌ 创建失败：{release}")
        sys.exit(1)
    print(f"✔ Release 已创建 (id={release_id})")

    ok, failed = 0, []
    total = len(part_files)
    for i, name in enumerate(part_files, 1):
        path = os.path.join(parts_dir, name)
        print(f"⬆ [{i}/{total}] 上传 {name} ({fmt_size_mb(path):.1f} MB) ...", flush=True)
        try:
            upload_and_verify(token, release_id, path)
            ok += 1
            print(f"✔ 完成")
        except Exception as e:
            failed.append((name, str(e)))
            print(f"❌ 失败: {e}")

    print("\n" + "=" * 56)
    if failed:
        print(f"⚠ 发布部分完成：成功 {ok}/{total}，以下附件失败（可到网页端手动补传）：")
        for name, err in failed:
            print(f"   - {name}: {err}")
        print(f"👉 Release 页面: https://gitee.com/{GITEE_REPO}/releases/tag/{tag}")
        sys.exit(1)
    print(f"🎉 发布成功！{ok}/{total} 个附件全部上传。")
    print(f"👉 Release 页面: https://gitee.com/{GITEE_REPO}/releases/tag/{tag}")
    print("ℹ 老版本应用内『检查更新』即可自动下载分卷拼接并校验安装。")

    try:
        touch_release(token, release_id, tag, f"Y-CODE v{version}", body)
        print("✔ 已触碰 Release，latest 指针已刷新")
    except Exception as e:
        print(f"⚠ 触碰 Release 失败（latest 指针可能滞留旧版，可网页端手动编辑一次）：{e}")

    if args.cleanup and not args.dry_run and releases:
        try:
            updated_releases = list_releases(token)
            cleanup_old_releases(token, tag, updated_releases, keep_last_n=2)
        except Exception as e:
            print(f"⚠ 自动清理旧 Release 失败: {e}（不影响本次发布，可手动清理）")


if __name__ == "__main__":
    main()
