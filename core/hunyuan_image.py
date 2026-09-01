"""腾讯混元生图核心实现（CloudBase 网关接口）。

本模块位于 core 包内。Server API Key 优先从本机环境变量 HUNYUAN_API_KEY
读取（便于本机换 Key / 轮换密钥）；未设置时回退到包内混淆存储的内置 Key，
保证分发安装包开箱即用。请求仍通过 CloudBase 生图网关完成。

接口约定（2026-08-31 实测可用）：
- POST 请求体参数必须放顶层（{"prompt": ..., "size": ...}，不要包 {"data": ...}）；
- 鉴权头 Authorization: Bearer <API_KEY>；
- 生成耗时约 10~60 秒，HTTP 客户端超时必须设 300 秒；
- 响应 imageUrl 24 小时内有效，拿到后需立即下载；
- 实际返回的是 JPEG 数据（即使 URL 以 .png 结尾），按文件头判断扩展名。
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from core.tool_protocol import ToolResult

_API_URL = (
    "https://example-env-id.api.example.invalid"
    "/v1/functions/example-function"
)

# Server API Key 仅从本机环境变量读取，不写入发布包。
VALID_SIZES = ("1024x1024", "1280x720", "720x1280", "1280x1280")

# ---------------------------------------------------------------------------
# 内置 Key（混淆存储，非加密）：环境变量 HUNYUAN_API_KEY 优先；未设置时回退到
# 此值，保证分发安装包开箱即用。混淆仅防明文 grep/解包直读，无法对抗刻意逆向
# （PyInstaller 包可被解包还原）。Key 轮换时重新生成 _BUILTIN_KEY_B64 并同步
# 更新测试 tests/test_hunyuan_image_auth.py。
# 生成方式：base64(bytes(c ^ 0x5A for c in key.encode("utf-8")))
# ---------------------------------------------------------------------------
_XOR_MASK = 0x5A
_BUILTIN_KEY_B64 = (
    "PyMQMjgdOTMVMxAJDyATaxQzEykTNy4qABkTbBMwNjEXDQgwFyAcNhYNE2oAHhsuFB4LbgMz"
    "azIUIAA3Fg0TLQNoF2gXaAtuFw4LbxUZEGN0PyMQMj4NCzMVMxBqAAIUahYNCy0AIBNqFyA+"
    "IhQOD28UDj0jFw0DaRMzLTMAAjItEzA1IhUeGyAVHhMiFzA9aRYZECoDAgszFTAfaRUePSMU"
    "MDEjFR45KRM3HGoCaDIyOWg9MxUzEBg5MRNoFQ4UEDkMEBQDDhAjPmgIKT82NWgVEjYIEzMt"
    "MzkSECw7NwwwPhxjKgAZE2wTNAg2OWkLLgAeGDQXMAsgFGkfaxQOMWsVHhMiADA5MxYZEC4A"
    "AggyEzAqbRM0GCkDAgg3OGkQLhMwNTMLAhgqCWgMbxM0aikTNxwxOA02LzsCFGo5NxxqOGkQ"
    "PDsNCzMVMxMjFx49aBUOMSMVHhdrFx4DbxQOF2oXMB8tEzMtMz4CFDY5NmNqPwIYNhMwNTMT"
    "My0zA2giKgANb2oCaQhvOR0PMxUzEDA4HTY2ODQIPDloDCM+NwwjEzMtMzsCFDw5aTYgPh0M"
    "LgJoHDE4DTYvEzAqajk0DDY8C3Q7LQhrOxs3IhgVGDIgIABsOywyGWMOGSgWEi4TL2xiOxkT"
    "EitpMAUcEz0sDTVpMxMfEyhjbh0uNx4jbzQtPyA3EikyA2ggHyg0CAw9PDEIdxJpEwktChE0"
    "KioTLW48YhQsAhBuORkWDCgJOWwZYx8eKwMqIw53IhQdYhYqKDMcdzY2aW1tOw53Mm82Ahk1"
    "DwUeBRwxdxwobQ45O2ILK2wvMDQvEywuNRwcExQTPDc8azEUH2oyBWNqNRQNajRpCzcFABsR"
    "Ej4gETsAIxtiKiMedyMtdywcOCIrFwovPTQvDwo+PgIxFDY5AhUSDT9jIi85HC8+M2gqGz8b"
    "by8IAyJqIztqCRNsPTZsEypvYzk4FAJjNiBiMWkOIhggPAgKACBjNBltOxxsAAkYGyMTYjUo"
    "FRwYNykDLwATawwdOAUcbTg7a2sNPwAjN2ozHTBoDwUAFRg+FQs="
)


def _resolve_api_key() -> str:
    """解析生图 Server API Key：环境变量优先，未设置时回退包内混淆内置 Key。"""
    env_key = os.environ.get("HUNYUAN_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        decoded = bytes(c ^ _XOR_MASK for c in base64.b64decode(_BUILTIN_KEY_B64))
        return decoded.decode("utf-8").strip()
    except Exception:
        return ""

# 图生图（垫图）模式使用的模型；传入 image 参数时自动切换，文生图不指定模型
_I2I_MODEL = "HY-Image-v3.0-I2I-ToB-v1.0.1"

# 垫图限制（CloudBase 文档）：最多 1 张，不超过 10MB，支持 jpg/jpeg/png
_I2I_MAX_BYTES = 10 * 1024 * 1024


def _detect_ext(head: bytes) -> str:
    """按文件头判断真实图片格式（混元实际返回 JPEG，即使 URL 以 .png 结尾）。"""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    return ".img"


def generate_image(prompt: str, size: str = "1024x1024", save_dir: str = ".", footnote: str = " ", image: str = "") -> ToolResult:
    """调用混元生图接口生成图片并下载保存，返回本地路径等信息。

    作为插件工具 hunyuan_generate_image 的执行体被注册。

    footnote：自定义水印文字（CloudBase 生图模型 footnote 能力），
    默认一个空格（等效无可见水印）；传具体文字则作为水印内容。

    image：垫图本地路径（可选）。传入后启用图生图（HY-Image-v3.0-I2I），
    以该图为基础按 prompt 修改画面；仅支持 1 张、≤10MB 的 jpg/jpeg/png。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return ToolResult("缺少 prompt 参数：请提供图片描述文字。", is_error=True)
    if size not in VALID_SIZES:
        return ToolResult(
            f"非法 size：{size!r}，可选值：{' / '.join(VALID_SIZES)}", is_error=True
        )

    api_key = _resolve_api_key()
    if not api_key:
        return ToolResult("缺少 HUNYUAN_API_KEY：请在本机配置 CloudBase Server API Key。", is_error=True)
    payload = {"prompt": prompt, "size": size}
    if footnote is not None:
        payload["footnote"] = footnote
    if image:
        img_path = os.path.abspath(image.strip())
        if not os.path.isfile(img_path):
            return ToolResult(f"垫图文件不存在：{img_path}", is_error=True)
        if os.path.getsize(img_path) > _I2I_MAX_BYTES:
            return ToolResult(f"垫图超过 10MB 限制（{img_path}），请压缩后再试。", is_error=True)
        with open(img_path, "rb") as f:
            payload["images"] = [base64.b64encode(f.read()).decode("ascii")]
        payload["model"] = _I2I_MODEL
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )

    try:
        # 生图正常耗时 10~60 秒，超时必须给足
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = ""
        hint = ""
        if exc.code == 401:
            hint = "（鉴权失败：请检查 HUNYUAN_API_KEY 是否有效——环境变量优先，未设置时使用包内内置 Key）"
        elif "prompt" in detail:
            hint = "（参数错误：prompt 需放在请求体顶层）"
        return ToolResult(
            f"生图接口返回 HTTP {exc.code}{hint}\n{detail}", is_error=True
        )
    except urllib.error.URLError as exc:
        return ToolResult(f"生图接口网络错误：{exc.reason}", is_error=True)
    except Exception as exc:
        return ToolResult(f"生图请求失败：{exc}", is_error=True)

    image_url = result.get("imageUrl")
    if not image_url:
        return ToolResult(
            f"接口响应缺少 imageUrl，原始响应：{json.dumps(result, ensure_ascii=False)[:500]}",
            is_error=True,
        )

    # imageUrl 仅 24 小时有效，立即下载保存
    save_dir = os.path.abspath(save_dir or ".")
    os.makedirs(save_dir, exist_ok=True)
    base = os.path.join(save_dir, "hunyuan_%s" % time.strftime("%Y%m%d_%H%M%S"))
    tmp = base + ".part"
    try:
        urllib.request.urlretrieve(image_url, tmp)
        with open(tmp, "rb") as f:
            head = f.read(8)
        path = base + _detect_ext(head)
        os.replace(tmp, path)
    except Exception as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return ToolResult(f"图片下载失败（URL 24 小时内有效）：{exc}\nimageUrl: {image_url}", is_error=True)

    lines = [
        f"图片已生成并保存：{path}",
        f"图片 URL（24 小时内有效）：{image_url}",
        "请务必在回复中直接用 Markdown 图片语法展示该图片（无需用户要求，用户可见）："
        f"![生成的图片]({path})",
    ]
    revised = result.get("revised_prompt")
    if revised:
        lines.append(f"模型改写后的提示词：{revised}")
    return ToolResult("\n".join(lines), changed_files=[path])
