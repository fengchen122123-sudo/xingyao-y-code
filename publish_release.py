"""
publish_release.py
==================
把 build_release.py 产出的 dist/release_parts/ 分卷附件一键发布到 Gitee Releases。

流程：
  1. 解析 Gitee 认证令牌（GITEE_TOKEN 环境变量 > git credential 存储的 HTTPS 凭据）
  2. 读取 core/version.py 版本号与 dist/release_parts/RELEASE_NOTES.txt 中的 SHA256
  3. 从 assets/CHANGELOG.md 提取本版本条目组装发布说明（保留 SHA256 校验行，
     自动更新端 core/updater.py 依赖它做安装包完整性校验，格式勿改动）
  4. 创建 Release（tag v{版本}，指向 dev 分支最新提交）
  5. 逐个流式上传分卷附件（multipart，避免 95MB 整体读入内存）

用法：
  venv\\Scripts\\python publish_release.py --check      验证令牌并查看现有 Release
  venv\\Scripts\\python publish_release.py              正式发布（tag 已存在时报错退出）
  venv\\Scripts\\python publish_release.py --force     tag 已存在时删除重建
  venv\\Scripts\\python publish_release.py --dry-run   只打印将要发布的内容，不调写接口

令牌准备（git 凭据不可用时）：
  https://gitee.com/profile/personal_access_tokens 创建私人令牌（勾选 projects），
  然后 set GITEE_TOKEN=xxx 或临时 $env:GITEE_TOKEN='xxx' 后运行本脚本。
"""
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

# 与 core/updater.py 的 GITEE_REPO 保持一致
GITEE_REPO = "feng-chenhao/xingyao-y-code"
API_BASE = "https://gitee.com/api/v5"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
RELEASE_TARGET_BRANCH = "dev"


# ============================================================================
# 认证与 API 基础封装
# ============================================================================

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
    """统一 API 调用：GET 走 query，写操作走 form-encoded body。

    data 为 dict 时 form 编码并追加 access_token；为自定义流式对象时
    原样透传（大文件 multipart 上传用）。
    """
    url = f"{API_BASE}{path}"
    if method == "GET":
        # GET 请求统一把 access_token 拼进 query（params 为空也必须带上）
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
    """流式 multipart 请求体：字段前缀 + 文件内容 + 结尾边界。

    实现 read() 供 http.client 分块发送，95MB 分卷不必整体读入内存。
    """

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
        self._phase = 0  # 0=前缀 1=文件 2=后缀 3=完成

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
                    # 文件读尽：关文件进 phase 2，循环继续取结尾边界
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


# ============================================================================
# 发布内容组装
# ============================================================================

def read_project_version():
    path = os.path.join(PROJECT_DIR, "core", "version.py")
    with open(path, encoding="utf-8") as f:
        m = re.search(r"__version__\s*=\s*[\"']([^\"']+)", f.read())
    if not m:
        print("❌ 无法从 core/version.py 解析版本号")
        sys.exit(1)
    return m.group(1)


def changelog_section(version):
    """提取 CHANGELOG.md 中本版本小节（## {version} ... 到下一个 ## 之间）。"""
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
        lines.append("下载同一安装包的全部分卷放到同一目录后执行合并：")
        lines.append("")
        for base, parts in split_groups.items():
            joined = "+".join(parts)
            lines.append(f"```")
            lines.append(f"copy /b {joined} {base}")
            lines.append(f"```")
            lines.append("")

    return "\n".join(lines).strip()


# ============================================================================
# 发布流程
# ============================================================================

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
                    continue  # 字段名不对，换下一个再试
                raise
            except Exception as e:
                # 网络抖动（远端断连/超时）：重试
                last_err = e
                break
        if attempt < retries:
            print(f"   ↻ 第 {attempt} 次失败（{last_err}），重试...", flush=True)
            import time
            time.sleep(3 * attempt)
    raise last_err


def fmt_size_mb(path):
    return os.path.getsize(path) / 1024 / 1024


def parse_version_from_tag(tag):
    """从 tag 提取版本号元组供比较（'v1.2.8' -> (1,2,8)）。"""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(map(int, m.groups())) if m else (0, 0, 0)


def cleanup_old_releases(token, current_tag, releases, keep_last_n=2):
    """自动清理旧 Release：保留最近 keep_last_n 个版本，删除更早的。

    用于控制 Gitee 仓库附件总容量（免费版约 500MB）。例如当前 v1.2.8，
    keep_last_n=2 时保留 v1.2.8 + v1.2.7，删除 v1.2.6 及更早版本。
    """
    if not releases:
        return

    # 按版本号降序排序（新 -> 旧）
    sorted_releases = sorted(
        releases,
        key=lambda r: parse_version_from_tag(r.get("tag_name", "")),
        reverse=True,
    )

    # 保留前 keep_last_n 个，删除其余的
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
    args = parser.parse_args()

    token, source = resolve_token()
    if not token and not args.dry_run:
        print("❌ 未取得 Gitee 令牌：既没有 GITEE_TOKEN 环境变量，git credential 里也没有 gitee.com 凭据。")
        print("   请到 https://gitee.com/profile/personal_access_tokens 创建私人令牌（勾选 projects），")
        print("   然后 $env:GITEE_TOKEN='令牌' 后重新运行。")
        sys.exit(1)

    # 验证令牌（只打印登录名，不回显令牌）
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

        # 列出现有 Release
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

    # 待上传附件 = 目录下除指引外的全部文件（分卷 + 未切分的单 exe）
    part_files = sorted(
        f for f in os.listdir(parts_dir)
        if f != "RELEASE_NOTES.txt" and os.path.isfile(os.path.join(parts_dir, f))
    )
    if not part_files:
        print("❌ dist/release_parts/ 下没有可上传的附件")
        sys.exit(1)

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

    # 补传模式：Release 已存在（上次上传部分失败），只传缺失附件
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
                upload_attachment(token, release_id, path)
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
        print(f"👉 https://gitee.com/{GITEE_REPO}/releases/tag/{tag}")
        return

    # tag 冲突处理
    existing = next((r for r in releases if r.get("tag_name") == tag), None) if releases else None
    if existing:
        if not args.force:
            print(f"❌ Release {tag} 已存在 (id={existing.get('id')})。确认要覆盖时加 --force 重新运行。")
            sys.exit(1)
        print(f"⚠ --force：删除旧 Release {tag} (id={existing.get('id')}) ...")
        delete_release(token, existing["id"])
        print("✔ 旧 Release 已删除")

    # 创建 Release
    print(f"\n🚀 创建 Release {tag} ...")
    release = create_release(token, tag, f"Y-CODE v{version}", body)
    release_id = release.get("id")
    if not release_id:
        print(f"❌ 创建失败：{release}")
        sys.exit(1)
    print(f"✔ Release 已创建 (id={release_id})")

    # 逐个上传附件
    ok, failed = 0, []
    total = len(part_files)
    for i, name in enumerate(part_files, 1):
        path = os.path.join(parts_dir, name)
        print(f"⬆ [{i}/{total}] 上传 {name} ({fmt_size_mb(path):.1f} MB) ...", flush=True)
        try:
            upload_attachment(token, release_id, path)
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

    # 发布成功后自动清理旧版本附件（保留最近 2 个版本：当前 + 上一个）
    # 避免 Gitee 仓库附件总容量超限（免费版约 500MB）
    if not args.dry_run and releases:
        try:
            # 重新拉取最新 Release 列表（包含刚发布的）
            updated_releases = list_releases(token)
            cleanup_old_releases(token, tag, updated_releases, keep_last_n=2)
        except Exception as e:
            print(f"⚠ 自动清理旧 Release 失败: {e}（不影响本次发布，可手动清理）")


if __name__ == "__main__":
    main()
