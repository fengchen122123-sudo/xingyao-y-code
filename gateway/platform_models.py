"""平台模型由管理员统一提供。受邀账号不自备 Key，开通时写入自己的卷。"""

from __future__ import annotations

import json
import time


class PlatformModelStore:
    """SQLite 只保存 keyring 引用；工人沿用 core.credentials 的引用格式读取。"""

    def __init__(self, database, keyring_backend, service: str = "Y-CODE API Keys"):
        self.database = database
        self.keyring = keyring_backend
        self.service = service

    def save(self, providers: list, active_model_id: str) -> None:
        saved = []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            copied = dict(provider)
            key = str(copied.get("api_key") or "")
            if key and not key.startswith("keyring:"):
                account = f"model:{copied.get('id') or len(saved)}"
                self.keyring.set_password(self.service, account, key)
                copied["api_key"] = "keyring:" + account
            saved.append(copied)
        save_platform_models(self.database, saved, active_model_id)

    def load(self) -> dict:
        models = load_platform_models(self.database)
        for provider in models["providers"]:
            if not isinstance(provider, dict):
                continue
            reference = str(provider.get("api_key") or "")
            if reference.startswith("keyring:"):
                secret = self.keyring.get_password(self.service, reference[len("keyring:"):])
                if secret is None:
                    raise RuntimeError("系统凭据存储中缺少平台模型 Key")
                provider["api_key"] = secret
        return models

    def references(self) -> dict:
        """给 worker 的 config.json 使用引用；只在加载配置时由 core.config 解密。"""
        return load_platform_models(self.database)

    def public(self) -> dict:
        models = load_platform_models(self.database)
        for provider in models["providers"]:
            if not isinstance(provider, dict):
                continue
            reference = str(provider.pop("api_key", "") or "")
            provider["api_key_configured"] = bool(reference)
        return models


def save_platform_models(database, providers: list, active_model_id: str) -> None:
    payload = json.dumps(providers, ensure_ascii=False)
    now = int(time.time())
    database.execute(
        """
        INSERT INTO platform_models(id, providers_json, active_model_id, updated_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          providers_json = excluded.providers_json,
          active_model_id = excluded.active_model_id,
          updated_at = excluded.updated_at
        """,
        (payload, active_model_id, now),
    )
    database.commit()


def load_platform_models(database) -> dict:
    row = database.execute("SELECT providers_json, active_model_id FROM platform_models WHERE id = 1").fetchone()
    if row is None:
        return {"providers": [], "active_model_id": ""}
    try:
        providers = json.loads(row["providers_json"] or "[]")
    except json.JSONDecodeError:
        providers = []
    if not isinstance(providers, list):
        providers = []
    return {"providers": providers, "active_model_id": str(row["active_model_id"] or "")}
