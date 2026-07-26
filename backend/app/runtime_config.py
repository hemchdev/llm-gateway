from __future__ import annotations

import json
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from redis.asyncio import Redis

from .config import Settings


DEFAULT_PROVIDER_CLASSES = ["classification", "long_form_generation"]


class ProviderStore:
    def __init__(
        self,
        redis: Redis,
        encryption_key: str | bytes | None = None,
        key_prefix: str = "llm_gateway:runtime_providers",
    ) -> None:
        raw_key = encryption_key or os.getenv("ENCRYPTION_KEY")
        if raw_key is None or not str(raw_key).strip():
            raise RuntimeError("ENCRYPTION_KEY is required for ProviderStore")
        self.redis = redis
        self.key_prefix = key_prefix
        self.fernet = Fernet(raw_key.encode("utf-8") if isinstance(raw_key, str) else raw_key)

    @classmethod
    def from_settings(cls, redis: Redis, settings: Settings) -> "ProviderStore":
        return cls(
            redis=redis,
            encryption_key=settings.encryption_key or os.getenv("ENCRYPTION_KEY"),
            key_prefix=settings.runtime_provider_key_prefix,
        )

    @staticmethod
    def is_configured(settings: Settings | None = None) -> bool:
        configured = (settings.encryption_key if settings is not None else None) or os.getenv("ENCRYPTION_KEY")
        return bool(configured and configured.strip())

    async def create(self, provider: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        payload = self._normalize(provider, kwargs, require_name=True)
        stored = self._encrypt_for_storage(payload)
        await self.redis.set(self._provider_key(stored["name"]), json.dumps(stored, sort_keys=True))
        await self.redis.sadd(self._names_key(), stored["name"])
        return self._mask_for_display(stored)

    async def list(self) -> list[dict[str, Any]]:
        providers = []
        for name in await self._provider_names():
            stored = await self._read_stored(name)
            if stored is not None:
                providers.append(self._mask_for_display(stored))
        return self._sort_for_display(providers)

    async def get_raw(self, name: str) -> dict[str, Any] | None:
        stored = await self._read_stored(name)
        if stored is None:
            return None
        return self._decrypt_for_runtime(stored)

    async def update(self, name: str, updates: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
        stored = await self._read_stored(name)
        if stored is None:
            return None
        existing = self._decrypt_for_runtime(stored)
        payload = self._normalize(updates, kwargs, require_name=False)
        payload.pop("name", None)
        existing.update(payload)
        existing["name"] = name
        updated = self._encrypt_for_storage(existing)
        await self.redis.set(self._provider_key(name), json.dumps(updated, sort_keys=True))
        await self.redis.sadd(self._names_key(), name)
        return self._mask_for_display(updated)

    async def delete(self, name: str) -> bool:
        deleted = await self.redis.delete(self._provider_key(name))
        await self.redis.srem(self._names_key(), name)
        return bool(deleted)

    async def ordered_for_class(self, request_class: str) -> list[dict[str, Any]]:
        providers: list[dict[str, Any]] = []
        for name in await self._provider_names():
            stored = await self._read_stored(name)
            if stored is None:
                continue
            provider = self._decrypt_for_runtime(stored)
            if not provider.get("enabled", True):
                continue
            classes = provider.get("request_classes") or DEFAULT_PROVIDER_CLASSES
            if request_class in classes:
                providers.append(provider)
        return self._sort_for_routing(providers)

    async def _provider_names(self) -> list[str]:
        names = await self.redis.smembers(self._names_key())
        return sorted(str(name) for name in names)

    async def _read_stored(self, name: str) -> dict[str, Any] | None:
        payload = await self.redis.get(self._provider_key(name))
        if payload is None:
            return None
        return json.loads(payload)

    def _encrypt_for_storage(self, payload: dict[str, Any]) -> dict[str, Any]:
        stored = dict(payload)
        api_key = stored.pop("api_key", None)
        encrypted_key = stored.pop("encrypted_api_key", None)
        if api_key:
            encrypted_key = self.fernet.encrypt(str(api_key).encode("utf-8")).decode("utf-8")
        if encrypted_key:
            stored["encrypted_api_key"] = encrypted_key
        return stored

    def _decrypt_for_runtime(self, stored: dict[str, Any]) -> dict[str, Any]:
        provider = dict(stored)
        encrypted_key = provider.pop("encrypted_api_key", None)
        if encrypted_key:
            try:
                provider["api_key"] = self.fernet.decrypt(str(encrypted_key).encode("utf-8")).decode("utf-8")
            except InvalidToken as exc:
                raise RuntimeError(f"Provider api_key for {provider.get('name', 'unknown')} cannot be decrypted") from exc
        return provider

    def _mask_for_display(self, stored: dict[str, Any]) -> dict[str, Any]:
        provider = self._decrypt_for_runtime(stored)
        api_key = provider.pop("api_key", None)
        provider["api_key"] = _mask_secret(api_key)
        provider["has_api_key"] = bool(api_key)
        return provider

    def _normalize(
        self,
        provider: dict[str, Any] | None,
        kwargs: dict[str, Any],
        require_name: bool,
    ) -> dict[str, Any]:
        payload = dict(provider or {})
        payload.update(kwargs)
        if require_name and not payload.get("name"):
            raise ValueError("provider name is required")
        if "name" in payload:
            payload["name"] = str(payload["name"]).strip().lower()
        payload.setdefault("enabled", True)
        payload.setdefault("priority", 100)
        payload.setdefault("request_classes", DEFAULT_PROVIDER_CLASSES)
        if isinstance(payload.get("request_classes"), str):
            payload["request_classes"] = [
                item.strip()
                for item in payload["request_classes"].split(",")
                if item.strip()
            ]
        return payload

    def _sort_for_display(self, providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(providers, key=lambda provider: (int(provider.get("priority", 100)), str(provider.get("name", ""))))

    def _sort_for_routing(self, providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(providers, key=lambda provider: (int(provider.get("priority", 100)), str(provider.get("name", ""))))

    def _names_key(self) -> str:
        return f"{self.key_prefix}:names"

    def _provider_key(self, name: str) -> str:
        return f"{self.key_prefix}:provider:{name}"


def _mask_secret(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 8:
        return f"{secret[:2]}...{secret[-2:]}"
    return f"{secret[:4]}...{secret[-4:]}"
