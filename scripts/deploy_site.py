#!/usr/bin/env python.exe
# -*- coding: utf-8 -*-
"""一键更新星瑶Code 官网静态站点。

功能概要：
  1. 通过 SSH/SFTP（paramiko）把本地 website/ 全量同步到远程静态目录；
  2. 上传后逐文件做 MD5 校验，确保内容一致；
  3. 最后做一次公网可用性验证（HTTP 状态码 + 关键字）。

密码来源优先读环境变量 YA_SSH_PWD，缺失时交互式输入，
脚本本身不包含任何明文密码。

用法示例：
    set YA_SSH_PWD=xxxxxx
    venv\\Scripts\\python.exe scripts\\deploy_site.py
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import sys
import urllib.request

import paramiko

# ---------------------------------------------------------------------------
# 默认配置（可通过命令行参数覆盖）
# ---------------------------------------------------------------------------
DEFAULT_HOST = "129.204.50.227"
DEFAULT_PORT = 22
DEFAULT_USER = "ubuntu"
DEFAULT_REMOTE_ROOT = "/var/www/xingyaocode"
DEFAULT_SITE_URL = "https://xingyaocode.cn/"
DEFAULT_SITE_KEYWORD = "星瑶Code"

# 本地源目录：基于本脚本位置定位，避免写死绝对路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOCAL_DIR = os.path.join(PROJECT_ROOT, "website")

PWD_ENV_VAR = "YA_SSH_PWD"


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    """解析命令行参数，默认值即真实生产配置。"""
    parser = argparse.ArgumentParser(
        description="一键同步星瑶Code 官网静态文件到远程服务器并校验。"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="远程主机地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="SSH 端口")
    parser.add_argument("--user", default=DEFAULT_USER, help="SSH 用户名")
    parser.add_argument(
        "--remote-root",
        default=DEFAULT_REMOTE_ROOT,
        help="远程静态站点根目录",
    )
    parser.add_argument(
        "--local-dir",
        default=DEFAULT_LOCAL_DIR,
        help="本地待上传目录（默认项目根下的 website/）",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="跳过 MD5 校验与公网可用性验证",
    )
    return parser.parse_args()


def resolve_password() -> str:
    """获取 SSH 密码：优先环境变量，其次交互式输入。"""
    pwd = os.environ.get(PWD_ENV_VAR)
    if pwd:
        info(f"已从环境变量 {PWD_ENV_VAR} 读取密码。")
        return pwd
    info(f"未检测到环境变量 {PWD_ENV_VAR}，请交互式输入密码。")
    pwd = getpass.getpass("请输入 SSH 密码: ")
    if not pwd:
        error("密码为空，无法继续。")
        sys.exit(1)
    return pwd


def collect_files(local_dir: str) -> list[str]:
    """递归收集本地目录下所有文件，返回相对于 local_dir 的路径列表。"""
    if not os.path.isdir(local_dir):
        error(f"本地目录不存在: {local_dir}")
        sys.exit(1)
    rel_paths: list[str] = []
    for root, _dirs, files in os.walk(local_dir):
        for name in files:
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, local_dir).replace(os.sep, "/")
            rel_paths.append(rel)
    if not rel_paths:
        error(f"本地目录为空，没有可上传文件: {local_dir}")
        sys.exit(1)
    rel_paths.sort()
    return rel_paths


def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    """确保远程目录存在（递归创建）。"""
    if remote_dir in ("", "/"):
        return
    try:
        sftp.stat(remote_dir)
        return
    except IOError:
        pass
    ensure_remote_dir(sftp, os.path.dirname(remote_dir.rstrip("/")))
    try:
        sftp.mkdir(remote_dir)
        info(f"已创建远程目录 {remote_dir}")
    except IOError:
        # 并发或已存在时忽略
        pass


def local_md5(path: str) -> str:
    """计算本地文件 MD5。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def remote_md5(ssh: paramiko.SSHClient, remote_path: str) -> str:
    """通过 exec_command 调用 md5sum 取远程文件 MD5。"""
    _stdin, stdout, _stderr = ssh.exec_command(f"md5sum {remote_path}")
    out = stdout.read().decode("utf-8", "ignore").strip()
    if not out:
        return ""
    return out.split()[0]


def upload_all(
    sftp: paramiko.SFTPClient, local_dir: str, remote_root: str, rel_paths: list[str]
) -> None:
    """按目录结构逐个上传文件。"""
    ensure_remote_dir(sftp, remote_root)
    for rel in rel_paths:
        local_path = os.path.join(local_dir, rel)
        remote_path = f"{remote_root}/{rel}"
        remote_dir = os.path.dirname(remote_path)
        ensure_remote_dir(sftp, remote_dir)
        size = os.path.getsize(local_path)
        info(f"正在上传 {rel} ({size} 字节)")
        sftp.put(local_path, remote_path)


def verify_md5(
    ssh: paramiko.SSHClient, local_dir: str, remote_root: str, rel_paths: list[str]
) -> bool:
    """逐文件比对本地与远程 MD5，返回是否全部通过。"""
    info("开始 MD5 校验 …")
    all_ok = True
    for rel in rel_paths:
        local_path = os.path.join(local_dir, rel)
        remote_path = f"{remote_root}/{rel}"
        l_md5 = local_md5(local_path)
        r_md5 = remote_md5(ssh, remote_path)
        if l_md5 == r_md5:
            info(f"MD5 校验通过  {rel}  {l_md5}")
        else:
            all_ok = False
            error(f"MD5 校验失败  {rel}  本地={l_md5} 远程={r_md5}")
    return all_ok


def verify_public_site(url: str, keyword: str) -> bool:
    """公网可用性验证：检查 HTTP 状态码和响应体关键字。"""
    info(f"公网可用性验证: {url}")
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001
        error(f"公网请求失败: {exc}")
        return False
    info(f"HTTP 状态码: {status}")
    if status != 200:
        error(f"HTTP 状态码异常: {status}")
        return False
    if keyword in body:
        info(f"响应体包含关键字「{keyword}」，站点正常。")
        return True
    error(f"响应体未包含关键字「{keyword}」，站点内容可能异常。")
    return False


def main() -> int:
    args = parse_args()
    password = resolve_password()
    rel_paths = collect_files(args.local_dir)

    info(f"目标主机: {args.user}@{args.host}:{args.port}")
    info(f"本地目录: {args.local_dir}")
    info(f"远程目录: {args.remote_root}")
    info(f"待上传文件数: {len(rel_paths)}")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        info("正在建立 SSH 连接 …")
        ssh.connect(
            hostname=args.host,
            port=args.port,
            username=args.user,
            password=password,
            timeout=20,
        )
        info("SSH 连接成功。")

        with ssh.open_sftp() as sftp:
            upload_all(sftp, args.local_dir, args.remote_root, rel_paths)

        if args.skip_verify:
            info("已指定 --skip-verify，跳过校验步骤。")
            print("\n===== 部署完成（未校验） =====")
            return 0

        if not verify_md5(ssh, args.local_dir, args.remote_root, rel_paths):
            error("存在 MD5 校验不一致的文件，请检查后重试。")
            return 1
    except paramiko.SSHException as exc:
        error(f"SSH/SFTP 操作失败: {exc}")
        return 1
    finally:
        ssh.close()

    if not verify_public_site(DEFAULT_SITE_URL, DEFAULT_SITE_KEYWORD):
        error("公网可用性验证未通过。")
        return 1

    print("\n===== 部署完成：文件已同步，MD5 全部一致，公网可访问 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
