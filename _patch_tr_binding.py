"""一次性补丁脚本：重做「登录即新建专属 Key」绑定语义。

用法：venv\\Scripts\\python.exe _patch_tr_binding.py
每个改动都要求锚点唯一命中，命中数不为 1 直接失败退出（不会写坏文件）。
跑完即可删除本文件。
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
AUTH = ROOT / "core" / "tokenrhythm_auth.py"
TESTS = ROOT / "tests" / "test_tokenrhythm_auth.py"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"[FAIL] {label}：锚点应命中 1 次，实际 {count} 次")
    return text.replace(old, new, 1)


def splice(text: str, start: str, end: str, new: str, label: str) -> str:
    if text.count(start) != 1:
        raise SystemExit(f"[FAIL] {label}：起始锚点命中 {text.count(start)} 次")
    if text.count(end) != 1:
        raise SystemExit(f"[FAIL] {label}：结束锚点命中 {text.count(end)} 次")
    start_at = text.index(start)
    end_at = text.index(end)
    if end_at <= start_at:
        raise SystemExit(f"[FAIL] {label}：结束锚点位于起始锚点之前")
    return text[:start_at] + new + text[end_at:]


# ===========================================================================
# core/tokenrhythm_auth.py
# ===========================================================================

OLD_DOC = '''3. Key 自动绑定：登录成功后若本地没有可用的基元律动 API Key，自动创建一枚
   并写入模型厂商配置，用户无感；「账号 ⇄ 厂商/Key」绑定关系加密落盘，换账号
   登录时自动删除旧账号的厂商并整体重建。'''

NEW_DOC = '''3. Key 自动绑定：登录成功后**总是**为当前账号新建一枚专属 API Key（名称「星瑶Code」）
   写入模型厂商配置，绝不复用用户在模型配置里手动填过的 Key；「账号 ⇄ 厂商/Key」
   绑定关系加密落盘，同一账号重复登录复用这枚 Key，换账号时先删掉旧账号的 Key 与
   厂商再整体重建，退出登录时也会先删掉这枚 Key。'''

OLD_LOGOUT = '''def logout() -> None:
    """清空进程内会话、缓存的 challengeId，以及本地保存的加密 cookie。

    只清本地，不强制调用服务端登出（服务端会话到期后自然失效）。
    """
    _reset_state()
    _clear_session()'''

NEW_LOGOUT = '''def logout() -> None:
    """退出登录：先删掉服务端那枚专属 Key，再清本地绑定 / 会话 / 进程内状态。

    顺序固定：``POST /api/api-keys/{id}/delete`` → 清 binding → 清会话文件与进程
    状态。删 Key 失败（网络异常 / 用户已在控制台删掉 / 会话已失效）只记 warning，
    绝不阻断退出——否则用户会被卡在登录态；日志只出现 key_id，绝不出现 Key 明文。
    """
    binding = _load_binding() or {}
    key_id = str(binding.get("key_id") or "").strip()
    if key_id:
        _delete_api_key(key_id)
    _clear_binding()
    _reset_state()
    _clear_session()'''

OLD_REQUEST_DEF = '''def _request(method: str, path: str, body: dict | None = None) -> dict:
    """调用 /api/*，成功返回完整 envelope，失败抛 TokenRhythmAuthError。"""'''

NEW_REQUEST_DEF = '''def _request(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    allow_empty_data: bool = False,
) -> dict:
    """调用 /api/*，成功返回完整 envelope，失败抛 TokenRhythmAuthError。

    allow_empty_data：允许 `data` 为 null 也算成功（删除类接口可能返回空 data）。
    """'''

OLD_REQUEST_CHECK = '''    has_data = payload.get("data") is not None
    failed = (
        status >= 400
        or _is_error_code(code)
        or (code is not None and code != 0)
        or not has_data
    )'''

NEW_REQUEST_CHECK = '''    has_data = payload.get("data") is not None
    failed = (
        status >= 400
        or _is_error_code(code)
        or (code is not None and code != 0)
        or (not has_data and not allow_empty_data)
    )'''

OLD_CREATE_TAIL = '''    key_id = ""
    if isinstance(data, dict):
        key_id = str(_first_field(data, _KEY_ID_FIELDS) or "").strip()
    return key, key_id'''

NEW_CREATE_TAIL = '''    key_id = ""
    if isinstance(data, dict):
        key_id = str(_first_field(data, _KEY_ID_FIELDS) or "").strip()
    return key, key_id


def _delete_api_key(key_id: str) -> bool:
    """删掉账号下指定 id 的 API Key（换账号 / 退出登录时清理专属 Key）。

    接口：``POST /api/api-keys/{id}/delete``（成功返回 status=deleted，data 可能为空）。
    失败只记 warning 并返回 False，绝不抛异常：清理失败不能把用户卡在登录态；
    日志只出现 key_id，绝不出现 Key 明文。
    """
    target = str(key_id or "").strip()
    if not target:
        return False
    try:
        _request(
            "POST",
            f"/api/api-keys/{target}/delete",
            allow_empty_data=True,
        )
    except TokenRhythmAuthError as error:
        logger.warning(
            "基元律动 API Key 删除失败（key=%s）：%s",
            target,
            error.server_message or error,
        )
        return False
    except Exception as error:  # noqa: BLE001 - 清理失败不能反向打断主流程
        logger.warning("基元律动 API Key 删除异常（key=%s）：%s", target, error)
        return False
    logger.info("基元律动 API Key 已删除（key=%s）", target)
    return True


def _account_key_ids() -> set[str]:
    """当前登录账号名下所有 API Key 的 id 集合（GET /api/api-keys）。

    用于判断绑定里的 key_id 是否还属于这个账号：不在就说明用户已在控制台删掉，
    必须重新创建。data 既可能是数组，也可能是 ``{"items": [...]}`` 这类分页对象；
    读取失败返回空集合（调用方据此走"重新创建"，不会误复用）。
    """
    try:
        payload = _request("GET", "/api/api-keys")
    except TokenRhythmAuthError as error:
        logger.warning(
            "基元律动 API Key 列表读取失败：%s", error.server_message or error
        )
        return set()
    data = payload.get("data")
    items: Any = data
    if isinstance(data, dict):
        items = []
        for key in ("items", "list", "records"):
            value = data.get(key)
            if isinstance(value, list):
                items = value
                break
    if not isinstance(items, list):
        return set()
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key_id = str(_first_field(item, _KEY_ID_FIELDS) or "").strip()
        if key_id:
            ids.add(key_id)
    return ids'''

BIND_END_BLOCK = '''# ---------------------------------------------------------------------------
# 模型目录同步：把基元律动提供的全部可用语言模型写入本地厂商
# ---------------------------------------------------------------------------
'''

NEW_BIND_AND_ENSURE = '''def _bind_result(
    account: dict,
    *,
    created: bool,
    provider_id: Any,
    reason: str,
    provider_name: str = "",
    model_added: bool = False,
    account_switched: bool = False,
    key_created: bool | None = None,
    old_key_deleted: bool = False,
) -> dict:
    """拼装返回值：原字段语义不变，补充 key_created / old_key_deleted。

    created 一直表示"本次是否新建了 Key"；key_created 省略时跟随 created，
    显式传 False 用于"复用已有 Key"的分支，便于上层区分。
    """
    created_key = bool(created) if key_created is None else bool(key_created)
    result: dict[str, Any] = {
        "created": bool(created),
        "key_created": created_key,
        "old_key_deleted": bool(old_key_deleted),
        "provider_id": str(provider_id or "").strip(),
        "model_added": bool(model_added),
        "reason": reason,
        "account_id": str(account.get("id") or ""),
        "account_name": str(account.get("name") or ""),
        "provider_name": str(provider_name or "").strip() or DEFAULT_PROVIDER_NAME,
    }
    if account_switched:
        result["account_switched"] = True
    return result


def _reuse_bound_key(account: dict, binding: dict, provider_name: str) -> dict | None:
    """同账号重复登录：Key 仍属于账号且本地可用时复用，否则返回 None（要求新建）。

    两个条件缺一不可：
    1. binding.key_id 出现在 GET /api/api-keys 返回的 id 列表里（用户没在控制台删掉），
       这是"这枚 Key 属于当前账号"的权威依据；
    2. 本地绑定厂商还在，且里面存的 Key 能通过 GET {base_url}/models 校验，保证本地
       配置没被改坏。

    Key 被删 / 厂商丢失 / Key 失效一律返回 None，交给调用方新建一枚覆盖，
    绝不退化成"沿用本地其它可用 Key"。
    """
    key_id = str(binding.get("key_id") or "").strip()
    if not key_id:
        return None
    if key_id not in _account_key_ids():
        logger.info("基元律动绑定 Key 已不在账号下（key=%s），将重新创建", key_id)
        return None
    provider = _find_provider(_load_providers(), binding.get("provider_id") or "")
    if provider is None or not _provider_key_usable(provider):
        logger.info("基元律动绑定厂商缺失或 Key 已失效（key=%s），将重新创建", key_id)
        return None
    provider_id = str(provider.get("id") or "").strip()
    _rename_provider(provider, provider_name)
    _remember_binding(
        account, provider_id, key_id, _as_int(binding.get("bound_at"), 0)
    )
    return _bind_result(
        account,
        created=False,
        provider_id=provider_id,
        reason="复用当前账号已绑定的 Key",
        provider_name=provider_name,
        key_created=False,
    )


def ensure_bound() -> dict:
    """确保当前登录账号拥有一枚专属 API Key，并绑定到本地模型厂商。

    产品语义（登录即新建专属 Key，绝不复用用户手动填过的 Key）：
    1. 同账号、Key 仍在账号下且本地可用：复用这一枚，不重复创建（created=False）。
    2. 同账号但 Key 已被删 / 厂商丢失 / Key 失效：新建一枚 Key，覆盖绑定过的厂商。
    3. 换账号：先删旧账号的 Key（POST /api/api-keys/{id}/delete），再删旧厂商、
       清绑定，然后新建 Key + 新建厂商（名称「基元律动（昵称）」）。
    4. 无绑定 / 绑定损坏：一定新建 Key + 新建厂商 + 写绑定（不沿用本地任何已有 Key）。

    未登录抛 TokenRhythmAuthError（请先登录基元律动账号）。
    返回：created / key_created（是否新建了 Key）、old_key_deleted（换账号时旧 Key
    是否删除成功）、provider_id、model_added、reason、account_id、account_name、
    provider_name；account_switched 只在本次发生账号切换时出现。
    日志只记 key_id / provider_id，绝不打印 Key 明文。
    """
    account = _current_account()
    provider_name = _bound_provider_name(account["name"])

    binding = _load_binding() or {}
    bound_account_id = str(binding.get("account_id") or "").strip()
    account_switched = bool(bound_account_id) and bound_account_id != account["id"]
    old_key_deleted = False

    if account_switched:
        # 旧账号的 Key 属于旧账号，不能留下：先删 Key → 再删旧厂商 → 最后清绑定
        old_key_id = str(binding.get("key_id") or "").strip()
        dropped_provider_id = str(binding.get("provider_id") or "").strip()
        old_key_deleted = _delete_api_key(old_key_id)
        _delete_provider_quietly(dropped_provider_id)
        _clear_binding()
        binding = {}
    elif binding:
        reused = _reuse_bound_key(account, binding, provider_name)
        if reused is not None:
            return reused

    # 走到这里必须新建一枚专属 Key：不复用本地任何已有 Key（含用户手动填的）
    new_key, key_id = _create_api_key()

    # 目标厂商：同账号优先覆盖绑定过的那个；换账号 / 无绑定时一律新建厂商
    target: dict | None = None
    if not account_switched and binding:
        target = _find_provider(_load_providers(), binding.get("provider_id") or "")

    if target is not None:
        provider_id = str(target.get("id") or "").strip()
        base_url = str(target.get("base_url") or API_BASE_URL).strip() or API_BASE_URL
        try:
            from core import model_manager

            model_manager.update_provider(
                provider_id,
                name=provider_name,
                base_url=base_url,
                api_key=new_key,
                protocol="openai",
            )
        except Exception as error:
            raise TokenRhythmAuthError(
                f"API Key 已创建，但写入模型厂商配置失败：{error}"
            ) from error
        _remember_binding(account, provider_id, key_id)
        return _bind_result(
            account,
            created=True,
            provider_id=provider_id,
            reason="已自动创建并绑定 Key",
            provider_name=provider_name,
            account_switched=account_switched,
            key_created=True,
            old_key_deleted=old_key_deleted,
        )

    try:
        from core import model_manager

        provider_id = str(
            model_manager.add_provider(provider_name, API_BASE_URL, new_key, "openai")
            or ""
        ).strip()
    except Exception as error:
        raise TokenRhythmAuthError(
            f"API Key 已创建，但新增模型厂商失败：{error}"
        ) from error

    model_added = False
    try:
        from core import model_manager

        model_manager.add_model_to_provider(
            provider_id,
            DEFAULT_MODEL_NAME,
            DEFAULT_CONTEXT_WINDOW,
            supports_vision=False,
            supports_function_calling=True,
        )
        model_added = True
    except Exception as error:
        # 默认模型追加失败不影响 Key 绑定本身，记 warning 即可。
        logger.warning("基元律动默认模型添加失败（provider=%s）：%s", provider_id, error)

    _remember_binding(account, provider_id, key_id)
    return _bind_result(
        account,
        created=True,
        provider_id=provider_id,
        reason="已自动创建并绑定 Key",
        provider_name=provider_name,
        model_added=model_added,
        account_switched=account_switched,
        key_created=True,
        old_key_deleted=old_key_deleted,
    )


'''


def patch_auth() -> None:
    text = AUTH.read_text(encoding="utf-8")
    before = sha(text)
    text = replace_once(text, OLD_DOC, NEW_DOC, "模块文档 3")
    text = replace_once(text, OLD_LOGOUT, NEW_LOGOUT, "logout")
    text = replace_once(text, OLD_REQUEST_DEF, NEW_REQUEST_DEF, "_request 签名")
    text = replace_once(text, OLD_REQUEST_CHECK, NEW_REQUEST_CHECK, "_request 成功判定")
    text = replace_once(text, OLD_CREATE_TAIL, NEW_CREATE_TAIL, "_delete_api_key/_account_key_ids")
    text = splice(
        text,
        "def _bind_result(\n",
        BIND_END_BLOCK,
        NEW_BIND_AND_ENSURE + BIND_END_BLOCK,
        "_bind_result + ensure_bound",
    )
    AUTH.write_text(text, encoding="utf-8", newline="\n")
    print(f"[OK] {AUTH.name}: {before[:12]} -> {sha(text)[:12]}")


# ===========================================================================
# tests/test_tokenrhythm_auth.py
# ===========================================================================

OLD_TEST_IMPORTS = '''import base64
import time'''

NEW_TEST_IMPORTS = '''import base64
import logging
import time'''

OLD_SESSION_FIXTURE = '''    path = tmp_path / "tr_session.json"
    monkeypatch.setattr(auth, "_SESSION_FILE", path)'''

NEW_SESSION_FIXTURE = '''    path = tmp_path / "tr_session.json"
    monkeypatch.setattr(auth, "_SESSION_FILE", path)
    # 绑定文件也指向 tmp_path：logout 现在会读 binding 并向服务端删 Key，
    # 绝不能读写用户真实的 ~/.ycode/secrets。
    monkeypatch.setattr(auth, "_BINDING_FILE", tmp_path / "tr_binding.json")'''

OLD_BINDING_TESTS_END = '''def test_account_status_without_session(http):'''

NEW_BINDING_TESTS = '''def _patch_existing_provider(monkeypatch, api_key="dpapi:stored", base_url="https://tokenrhythm.studio/v1"):
    import config
    from core import credentials

    monkeypatch.setattr(
        config,
        "_load",
        lambda: {
            "providers": [
                {
                    "id": "p1",
                    "name": "基元律动",
                    "base_url": base_url,
                    "api_key": api_key,
                    "protocol": "openai",
                    "models": [],
                }
            ]
        },
    )
    return credentials


def _keys_ok(*key_ids, container="list"):
    """GET /api/api-keys 的成功响应：覆盖 data 为数组 / {items:[...]} 两种形态。"""
    items = [
        {"id": key_id, "name": "星瑶Code", "maskedKey": "sk-****"}
        for key_id in key_ids
    ]
    return _FakeResponse(200, _ok(items if container == "list" else {"items": items}))


def _unexpected(name):
    """构造"绝不应被调用"的替身，断言某条旧路径已被删除。"""

    def _boom(*args, **kwargs):
        raise AssertionError(f"不应调用 {name}")

    return _boom


def _creation_recorder(model_manager, monkeypatch):
    """打桩 model_manager：记录新增厂商，禁止更新 / 删除厂商。"""
    calls = {"added": []}

    def _add_provider(name, base_url, api_key, protocol):
        calls["added"].append((name, base_url, api_key, protocol))
        return "p-new"

    monkeypatch.setattr(model_manager, "add_provider", _add_provider)
    monkeypatch.setattr(model_manager, "add_model_to_provider", lambda *a, **k: "m-new")
    monkeypatch.setattr(model_manager, "update_provider", _unexpected("update_provider"))
    monkeypatch.setattr(model_manager, "delete_provider", _unexpected("delete_provider"))
    return calls


def test_ensure_bound_without_binding_always_creates_key(http, monkeypatch):
    """无绑定首次登录：即使本地已有可用 provider + Key，也必须新建专属 Key。"""
    _patch_providers(monkeypatch, _provider("p1", "基元律动", _fake_protect("sk-manual")))
    assert auth._load_binding() is None

    from core import model_manager

    calls = _creation_recorder(model_manager, monkeypatch)
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        _FakeResponse(200, _ok({"key": "sk-dedicated", "id": "k1"})),
    )

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert result["old_key_deleted"] is False
    assert result["reason"] == "已自动创建并绑定 Key"
    assert result["provider_id"] == "p-new"
    assert result["provider_name"] == "基元律动（木偶）"
    assert calls["added"] == [
        ("基元律动（木偶）", "https://tokenrhythm.studio/v1", "sk-dedicated", "openai")
    ]
    # 无绑定就不该校验本地 Key（不请求 /v1/models），更不该沿用 sk-manual
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
    ]
    binding = auth._load_binding()
    assert binding["account_id"] == "u1"
    assert binding["provider_id"] == "p-new"
    assert binding["key_id"] == "k1"


def test_ensure_bound_never_probes_local_key_when_unbound(http, monkeypatch):
    """无绑定：连"本地 Key 是否可用"都不该校验（沿用分支已彻底删除）。"""
    _patch_providers(monkeypatch, _provider("p1", "基元律动", _fake_protect("sk-local")))
    monkeypatch.setattr(
        auth, "_provider_key_usable", _unexpected("_provider_key_usable")
    )

    from core import model_manager

    calls = _creation_recorder(model_manager, monkeypatch)
    auth._STATE["logged_in"] = True
    http.queue(_me("u1", "木偶"), _FakeResponse(200, _ok({"key": "sk-new", "id": "k1"})))

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert calls["added"] == [
        ("基元律动（木偶）", "https://tokenrhythm.studio/v1", "sk-new", "openai")
    ]


def test_ensure_bound_without_provider_creates_key_and_provider(http, monkeypatch):
    """无 provider：创建 Key 后 add_provider（名称带昵称）并追加默认模型。"""
    _patch_providers(monkeypatch)

    from core import model_manager

    calls = {}

    def _add_provider(name, base_url, api_key, protocol):
        calls["add_provider"] = (name, base_url, api_key, protocol)
        return "p-new"

    def _add_model(provider_id, name, context_window, **kwargs):
        calls["add_model"] = (provider_id, name, context_window, kwargs)
        return "m-new"

    monkeypatch.setattr(model_manager, "add_provider", _add_provider)
    monkeypatch.setattr(model_manager, "add_model_to_provider", _add_model)
    monkeypatch.setattr(
        model_manager,
        "update_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应更新厂商")),
    )
    auth._STATE["logged_in"] = True
    http.queue(_me("u1", "木偶"), _FakeResponse(200, _ok({"key": "sk-created"})))

    result = auth.ensure_bound()

    assert result == {
        "created": True,
        "key_created": True,
        "old_key_deleted": False,
        "provider_id": "p-new",
        "model_added": True,
        "reason": "已自动创建并绑定 Key",
        "account_id": "u1",
        "account_name": "木偶",
        "provider_name": "基元律动（木偶）",
    }
    create_call = _requests(http)[1]
    assert create_call["url"] == "https://tokenrhythm.studio/api/api-keys"
    assert create_call["json"] == {"name": "星瑶Code"}
    assert calls["add_provider"] == (
        "基元律动（木偶）",
        "https://tokenrhythm.studio/v1",
        "sk-created",
        "openai",
    )
    provider_id, model_name, context_window, kwargs = calls["add_model"]
    assert provider_id == "p-new"
    assert model_name == "deepseek-v4.1-flash"
    assert context_window == 128000
    assert kwargs == {"supports_vision": False, "supports_function_calling": True}


def test_ensure_bound_new_provider_when_local_key_invalid(http, monkeypatch):
    """无绑定 + 本地厂商的 Key 已失效：仍要新建 Key 与厂商，绝不打补丁复用旧条目。"""
    _patch_existing_provider(monkeypatch, api_key="enc:bad-key")

    from core import model_manager

    calls = _creation_recorder(model_manager, monkeypatch)
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        # 只给 apiKey：验证明文 Key 兜底字段（key 缺失时取 apiKey）。
        _FakeResponse(200, _ok({"apiKey": "sk-new"})),
    )

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert result["provider_id"] == "p-new"
    assert result["model_added"] is True
    assert calls["added"] == [
        ("基元律动（木偶）", "https://tokenrhythm.studio/v1", "sk-new", "openai")
    ]


def test_ensure_bound_missing_plaintext_key_raises(http, monkeypatch):
    """创建成功但响应里没有明文字段：抛明确异常。"""
    _patch_providers(monkeypatch)
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        _FakeResponse(200, _ok({"id": "k1", "maskedKey": "sk-****"})),
    )

    with pytest.raises(auth.TokenRhythmAuthError) as excinfo:
        auth.ensure_bound()

    assert "未返回明文 Key" in str(excinfo.value)


def test_ensure_bound_first_bind_creates_key_provider_and_binding(http, monkeypatch):
    """首次绑定：建 Key + add_provider（「基元律动（昵称）」）+ 写绑定关系。"""
    _patch_providers(monkeypatch)

    from core import model_manager

    calls = {}

    def _add_provider(name, base_url, api_key, protocol):
        calls["add_provider"] = (name, base_url, api_key, protocol)
        return "p-new"

    monkeypatch.setattr(model_manager, "add_provider", _add_provider)
    monkeypatch.setattr(model_manager, "add_model_to_provider", lambda *a, **k: "m-new")
    monkeypatch.setattr(
        model_manager,
        "update_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应更新厂商")),
    )
    monkeypatch.setattr(
        model_manager,
        "delete_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应删除厂商")),
    )
    auth._STATE["logged_in"] = True
    http.queue(_me("u1", "木偶"), _FakeResponse(200, _ok({"key": "sk-created", "id": "k1"})))

    result = auth.ensure_bound()

    assert result == {
        "created": True,
        "key_created": True,
        "old_key_deleted": False,
        "provider_id": "p-new",
        "model_added": True,
        "reason": "已自动创建并绑定 Key",
        "account_id": "u1",
        "account_name": "木偶",
        "provider_name": "基元律动（木偶）",
    }
    assert calls["add_provider"] == (
        "基元律动（木偶）",
        "https://tokenrhythm.studio/v1",
        "sk-created",
        "openai",
    )
    binding = auth._load_binding()
    assert binding["v"] == 1
    assert binding["account_id"] == "u1"
    assert binding["account_name"] == "木偶"
    assert binding["provider_id"] == "p-new"
    assert binding["key_id"] == "k1"
    assert isinstance(binding["bound_at"], int)
    # 无绑定不校验本地 Key：只有 /auth/me 与创建 Key 两次请求
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
    ]


def test_ensure_bound_same_account_second_call_skips_key_creation(http, monkeypatch):
    """同账号重复登录且 Key 仍在账号下：复用，不重复建 Key，created=False。"""
    _patch_providers(
        monkeypatch, _provider("p1", "基元律动（木偶）", _fake_protect("sk-good"))
    )
    auth._save_binding(
        {
            "account_id": "u1",
            "account_name": "木偶",
            "provider_id": "p1",
            "key_id": "k1",
            "bound_at": 1700000000,
        }
    )

    from core import model_manager

    monkeypatch.setattr(model_manager, "add_provider", _unexpected("add_provider"))
    monkeypatch.setattr(model_manager, "update_provider", _unexpected("update_provider"))
    monkeypatch.setattr(
        model_manager, "add_model_to_provider", _unexpected("add_model_to_provider")
    )
    monkeypatch.setattr(model_manager, "delete_provider", _unexpected("delete_provider"))
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        # data 为 {items:[...]} 形态也要能解析出 key id
        _keys_ok("k1", "k-other", container="items"),
        _FakeResponse(200, _ok({"data": []})),
    )

    result = auth.ensure_bound()

    assert result["created"] is False
    assert result["key_created"] is False
    assert result["old_key_deleted"] is False
    assert result["provider_id"] == "p1"
    assert result["provider_name"] == "基元律动（木偶）"
    assert result["reason"] == "复用当前账号已绑定的 Key"
    calls = _requests(http)
    assert [call["url"] for call in calls] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
        "https://tokenrhythm.studio/v1/models",
    ]
    assert calls[2]["headers"]["authorization"] == "Bearer sk-good"
    binding = auth._load_binding()
    assert binding["account_id"] == "u1"
    assert binding["key_id"] == "k1"
    assert binding["bound_at"] == 1700000000


def test_ensure_bound_same_account_recreates_key_when_local_key_invalid(http, monkeypatch):
    """同账号且 Key 仍在账号下，但本地 Key 校验失败：新建 Key 覆盖绑定过的厂商。"""
    _patch_providers(
        monkeypatch, _provider("p1", "基元律动（木偶）", _fake_protect("sk-broken"))
    )
    auth._save_binding(
        {
            "account_id": "u1",
            "account_name": "木偶",
            "provider_id": "p1",
            "key_id": "k1",
            "bound_at": 1700000000,
        }
    )

    from core import model_manager

    calls = {}

    def _update_provider(provider_id, **kwargs):
        calls["update"] = (provider_id, kwargs)

    monkeypatch.setattr(model_manager, "update_provider", _update_provider)
    monkeypatch.setattr(model_manager, "add_provider", _unexpected("add_provider"))
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        _keys_ok("k1"),
        _FakeResponse(401, {"code": "UNAUTHORIZED", "message": "invalid key", "data": None}),
        _FakeResponse(200, _ok({"key": "sk-new", "id": "k2"})),
    )

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert result["provider_id"] == "p1"
    assert calls["update"] == (
        "p1",
        {
            "name": "基元律动（木偶）",
            "base_url": "https://tokenrhythm.studio/v1",
            "api_key": "sk-new",
            "protocol": "openai",
        },
    )
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
        "https://tokenrhythm.studio/v1/models",
        "https://tokenrhythm.studio/api/api-keys",
    ]
    binding = auth._load_binding()
    assert binding["key_id"] == "k2"
    assert binding["bound_at"] > 1700000000


def test_ensure_bound_same_account_deleted_key_creates_new_key(http, monkeypatch):
    """同账号但 key_id 已不在账号 Key 列表（被删）：新建 Key 并更新绑定。"""
    _patch_providers(
        monkeypatch, _provider("p1", "基元律动（木偶）", _fake_protect("sk-old"))
    )
    auth._save_binding(
        {
            "account_id": "u1",
            "account_name": "木偶",
            "provider_id": "p1",
            "key_id": "k-old",
            "bound_at": 1700000000,
        }
    )

    from core import model_manager

    calls = {}

    def _update_provider(provider_id, **kwargs):
        calls["update"] = (provider_id, kwargs)

    monkeypatch.setattr(model_manager, "update_provider", _update_provider)
    monkeypatch.setattr(model_manager, "add_provider", _unexpected("add_provider"))
    monkeypatch.setattr(model_manager, "delete_provider", _unexpected("delete_provider"))
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        # 账号下只剩另一枚 Key：k-old 已被控制台删掉
        _keys_ok("k-other"),
        _FakeResponse(200, _ok({"key": "sk-new", "id": "k-new"})),
    )

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert result["old_key_deleted"] is False
    assert result["provider_id"] == "p1"
    assert calls["update"][0] == "p1"
    assert calls["update"][1]["api_key"] == "sk-new"
    # Key 不在列表里就不必再校验本地 Key，直接重建
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
        "https://tokenrhythm.studio/api/api-keys",
    ]
    binding = auth._load_binding()
    assert binding["key_id"] == "k-new"
    assert binding["bound_at"] > 1700000000


def test_ensure_bound_switches_account_rebuilds_provider(http, monkeypatch):
    """换账号：先删旧 Key（断言删除请求 id）→ 删旧厂商 → 新建 Key + 厂商 + 绑定。"""
    _patch_providers(
        monkeypatch, _provider("p-old", "基元律动（旧号）", _fake_protect("sk-old"))
    )
    auth._save_binding(
        {
            "account_id": "u1",
            "account_name": "旧号",
            "provider_id": "p-old",
            "key_id": "k-old",
        }
    )

    from core import model_manager

    calls = {"deleted": [], "added": []}

    def _delete_provider(provider_id):
        calls["deleted"].append(provider_id)
        # 记录删厂商那一刻已经发过的请求：必须已经删掉旧 Key
        calls["requests_at_delete"] = [call["url"] for call in _requests(http)]

    def _add_provider(name, base_url, api_key, protocol):
        calls["added"].append((name, base_url, api_key, protocol))
        return "p-new"

    monkeypatch.setattr(model_manager, "delete_provider", _delete_provider)
    monkeypatch.setattr(model_manager, "add_provider", _add_provider)
    monkeypatch.setattr(model_manager, "add_model_to_provider", lambda *a, **k: "m-new")
    monkeypatch.setattr(
        model_manager,
        "update_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("换账号时不应复用旧厂商")),
    )
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u2", "新号"),
        _FakeResponse(200, _ok({"status": "deleted"})),
        _FakeResponse(200, _ok({"key": "sk-new", "id": "k2"})),
    )

    result = auth.ensure_bound()

    assert result["account_switched"] is True
    assert result["old_key_deleted"] is True
    assert result["created"] is True
    assert result["key_created"] is True
    assert result["provider_id"] == "p-new"
    assert result["account_id"] == "u2"
    assert result["account_name"] == "新号"
    assert result["provider_name"] == "基元律动（新号）"
    calls_list = _requests(http)
    # 第一条业务请求就是删旧账号的 Key，且用的是绑定里的 key_id
    assert calls_list[1]["method"] == "POST"
    assert calls_list[1]["url"] == "https://tokenrhythm.studio/api/api-keys/k-old/delete"
    assert calls_list[2]["url"] == "https://tokenrhythm.studio/api/api-keys"
    assert calls_list[2]["json"] == {"name": "星瑶Code"}
    # 删厂商时旧 Key 已经删过了
    assert calls["requests_at_delete"] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys/k-old/delete",
    ]
    assert calls["deleted"] == ["p-old"]
    assert calls["added"] == [
        ("基元律动（新号）", "https://tokenrhythm.studio/v1", "sk-new", "openai")
    ]
    binding = auth._load_binding()
    assert binding["account_id"] == "u2"
    assert binding["account_name"] == "新号"
    assert binding["provider_id"] == "p-new"
    assert binding["key_id"] == "k2"
    # 换账号不复用旧 Key：/auth/me → 删旧 Key → 建新 Key，共三次请求
    assert [call["url"] for call in calls_list] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys/k-old/delete",
        "https://tokenrhythm.studio/api/api-keys",
    ]


def test_ensure_bound_switch_account_keeps_going_when_old_key_delete_fails(http, monkeypatch):
    """换账号时旧 Key 删不掉（500）：仍继续删厂商 + 建新 Key，old_key_deleted=False。"""
    _patch_providers(monkeypatch, _provider("p-old", "基元律动（旧号）", ""))
    auth._save_binding(
        {
            "account_id": "u1",
            "account_name": "旧号",
            "provider_id": "p-old",
            "key_id": "k-old",
        }
    )

    from core import model_manager

    deleted = []
    monkeypatch.setattr(model_manager, "delete_provider", lambda pid: deleted.append(pid))
    monkeypatch.setattr(model_manager, "add_provider", lambda *a, **k: "p-new")
    monkeypatch.setattr(model_manager, "add_model_to_provider", lambda *a, **k: "m-new")
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u2", "新号"),
        _FakeResponse(500, {"code": "INTERNAL_ERROR", "message": "boom", "data": None}),
        _FakeResponse(200, _ok({"key": "sk-new", "id": "k2"})),
    )

    result = auth.ensure_bound()

    assert result["account_switched"] is True
    assert result["old_key_deleted"] is False
    assert result["key_created"] is True
    assert deleted == ["p-old"]
    binding = auth._load_binding()
    assert binding["account_id"] == "u2"
    assert binding["key_id"] == "k2"


def test_ensure_bound_corrupt_binding_treated_as_unbound(http, monkeypatch):
    """绑定文件损坏 / 解密失败：按无绑定处理，不崩溃并重建绑定。"""
    _patch_providers(monkeypatch)
    auth._BINDING_FILE.write_text("这不是加密内容", encoding="utf-8")
    assert auth._load_binding() is None

    from core import model_manager

    monkeypatch.setattr(model_manager, "add_provider", lambda *a, **k: "p-new")
    monkeypatch.setattr(model_manager, "add_model_to_provider", lambda *a, **k: "m-new")
    auth._STATE["logged_in"] = True
    http.queue(_me("u1", "木偶"), _FakeResponse(200, _ok({"key": "sk-created", "id": "k1"})))

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert result["provider_id"] == "p-new"
    binding = auth._load_binding()
    assert binding["account_id"] == "u1"
    assert binding["provider_id"] == "p-new"


def test_ensure_bound_requires_login(http):
    """未登录：直接抛"请先登录基元律动账号"，且不发任何请求。"""
    auth._STATE["logged_in"] = False

    with pytest.raises(auth.TokenRhythmAuthError) as excinfo:
        auth.ensure_bound()

    assert "请先登录基元律动账号" in str(excinfo.value)
    client = http.holder["client"]
    assert client is None or client.calls == []


# ---------------------------------------------------------------------------
# 退出登录：先删服务端专属 Key，再清本地
# ---------------------------------------------------------------------------


def _spy_local_cleanup(monkeypatch, http):
    """记录清理 binding / 会话时已经发出的请求数，用于断言"先删 Key 后清本地"。"""
    order = {}
    original_clear_binding = auth._clear_binding
    original_clear_session = auth._clear_session

    def _clear_binding():
        order["clear_binding"] = len(_requests(http))
        original_clear_binding()

    def _clear_session():
        order["clear_session"] = len(_requests(http))
        original_clear_session()

    monkeypatch.setattr(auth, "_clear_binding", _clear_binding)
    monkeypatch.setattr(auth, "_clear_session", _clear_session)
    return order


def test_logout_deletes_key_before_clearing_local(http, monkeypatch):
    """退出登录：先调服务端删掉专属 Key（data 为空也算成功），再清 binding / 会话。"""
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None)))
    assert auth._SESSION_FILE.exists()
    auth._save_binding(
        {"account_id": "u1", "account_name": "木偶", "provider_id": "p1", "key_id": "k1"}
    )
    assert auth._load_binding()["key_id"] == "k1"
    auth._STATE["logged_in"] = True
    order = _spy_local_cleanup(monkeypatch, http)
    http.queue(_FakeResponse(200, _ok(None)))

    auth.logout()

    calls = _requests(http)
    assert [call["url"] for call in calls] == [
        "https://tokenrhythm.studio/api/api-keys/k1/delete"
    ]
    assert calls[0]["method"] == "POST"
    # binding 与 session 都是在删 Key 请求发出之后才清的
    assert order == {"clear_binding": 1, "clear_session": 1}
    assert auth._load_binding() is None
    assert not auth._SESSION_FILE.exists()
    assert auth._STATE["logged_in"] is False


def test_logout_clears_local_even_when_key_delete_fails(http, monkeypatch, caplog):
    """删除 Key 返回 500：仍清 binding / 会话 / 登录态，只记 warning。"""
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None)))
    auth._save_binding(
        {"account_id": "u1", "account_name": "木偶", "provider_id": "p1", "key_id": "k1"}
    )
    auth._STATE["logged_in"] = True
    http.queue(
        _FakeResponse(500, {"code": "INTERNAL_ERROR", "message": "boom", "data": None})
    )

    with caplog.at_level(logging.WARNING, logger=auth.logger.name):
        assert auth.logout() is None

    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/api-keys/k1/delete"
    ]
    assert "删除失败" in caplog.text
    assert "k1" in caplog.text
    assert auth._load_binding() is None
    assert not auth._SESSION_FILE.exists()
    assert auth._STATE["logged_in"] is False


def test_logout_without_binding_sends_no_request(http):
    """本地没有绑定（未登录 / 已退出）：logout 不该发任何请求。"""
    auth._STATE["logged_in"] = False

    auth.logout()

    client = http.holder["client"]
    assert client is None or client.calls == []


'''


def patch_tests() -> None:
    text = TESTS.read_text(encoding="utf-8")
    before = sha(text)
    text = replace_once(text, OLD_TEST_IMPORTS, NEW_TEST_IMPORTS, "测试导入 logging")
    text = replace_once(text, OLD_SESSION_FIXTURE, NEW_SESSION_FIXTURE, "session_store 隔离绑定文件")
    text = splice(
        text,
        "def _patch_existing_provider(",
        OLD_BINDING_TESTS_END,
        NEW_BINDING_TESTS,
        "ensure_bound / logout 用例整段重写",
    )
    TESTS.write_text(text, encoding="utf-8", newline="\n")
    print(f"[OK] {TESTS.name}: {before[:12]} -> {sha(text)[:12]}")


def main() -> int:
    patch_auth()
    patch_tests()
    print("[DONE] 两个文件已更新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
