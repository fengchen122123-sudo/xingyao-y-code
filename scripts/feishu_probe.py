"""飞书长连接接入探针（T1）：在 websockets 17.1 上验证代码路径，不依赖飞书官方包。

三种模式：
  --http       假凭据请求飞书长连接接入点，断言返回飞书错误码（证明接入点可达、
               请求构造正确、凭据校验路径有效）。CI 可跑，默认模式。
  --ws-mock    本地起一个 asyncio websocket echo 服务器，用 websockets.connect
               往返收发 + 关闭，验证 websockets 17.1 在当前 Python 上的传输路径。
  --real       真凭据（环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET），请求接入点，
               断言返回 code=0 并打印分配的 WS URL（完整建连验证在 T2 搬运完成后做）。

退出码：所有断言通过 = 0；任一失败 = 1。不打印任何真实凭据。
"""

import argparse
import asyncio
import json
import os
import sys
import urllib.request

END_POINT = "https://open.feishu.cn/callback/ws/endpoint"
FAKE_ID = "cli_probe_fake"
FAKE_SECRET = "probe_fake_secret"


def _post_endpoint(app_id: str, app_secret: str):
    payload = json.dumps({"AppID": app_id, "AppSecret": app_secret}).encode("utf-8")
    req = urllib.request.Request(
        END_POINT,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def http_check(args) -> int:
    if args.real:
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            print("❌ --real 需要设置环境变量 FEISHU_APP_ID 与 FEISHU_APP_SECRET")
            return 1
        print(f"真实凭据模式（app_id={app_id[:8]}…）")
    else:
        app_id, app_secret = FAKE_ID, FAKE_SECRET
        print(f"假凭据模式（app_id={FAKE_ID}）")

    try:
        status, body = _post_endpoint(app_id, app_secret)
    except Exception as e:
        print(f"❌ 接入点不可达/请求失败（网络受限时属预期）: {e}")
        return 2
    code = body.get("code")
    url = (body.get("data") or {}).get("URL") if isinstance(body.get("data"), dict) else None
    print(f"HTTP {status}，飞书 code={code}, data.URL={'<有值>' if url else '<空>'}")

    if args.real:
        ok = status == 200 and code == 0 and bool(url)
        print(("✅ 凭据有效，接入点返回 WS URL（完整建连验证放 T2）" if ok else "❌ 凭据校验未通过或返回异常"))
        return 0 if ok else 1
    else:
        ok = status == 200 and code != 0
        print(("✅ 接入点可达且对假凭据返回错误码（代码路径通）" if ok else "❌ 返回与预期不符（接入点行为变化？）"))
        return 0 if ok else 1


async def _ws_mock_client():
    """连接本地 echo 服务器，验证 websockets 17.1 的 connect/recv/send/close 路径。"""
    import websockets

    async def handler(ws):
        msg = await ws.recv()
        await ws.send(msg)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    uri = f"ws://127.0.0.1:{port}/"
    try:
        async with websockets.connect(uri) as ws:
            import websockets.version as wv

            await ws.send("飞书探针-往返测试")
            echo = await ws.recv()
            print(f"websockets 版本 {wv.version} · 本地 echo 往返: {echo}")
            if echo != "飞书探针-往返测试":
                raise AssertionError("往返内容不一致")
        print("✅ websockets 17.1 在本机 Python 上 connect/recv/send/close 均正常")
        return 0
    finally:
        server.close()
        await server.wait_closed()


def ws_mock_check(args) -> int:
    try:
        return asyncio.run(_ws_mock_client())
    except Exception as e:
        print(f"❌ websockets 传输路径异常: {e}")
        return 1


def main():
    parser = argparse.ArgumentParser(description="飞书长连接接入探针")
    parser.add_argument("--http", action="store_true", help="假凭据请求接入点（默认）")
    parser.add_argument("--real", action="store_true", help="真凭据请求接入点（需环境变量）")
    parser.add_argument("--ws-mock", action="store_true", help="本地 websockets 传输路径检查")
    args = parser.parse_args()

    if args.ws_mock:
        return ws_mock_check(args)
    return http_check(args)


if __name__ == "__main__":
    sys.exit(main())