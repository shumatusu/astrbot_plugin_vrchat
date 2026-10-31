"""绑定凭据的加密持久化（Fernet + AstrBot 插件 KV 存储）。

- 每个 QQ 用户（``event.get_sender_id()``）一条记录，键为 ``account:<sender_id>``；
- 记录整体序列化为 JSON 后经 **Fernet 对称加密**，KV 中只保存密文；
- Fernet 密钥优先取插件配置 ``encryption_key``；未配置时在首次使用时随机
  生成并持久化到 KV（``crypto_key``）。用户可把密钥放到配置中自行保管，
  实现「密钥与密文分离」的更高安全级别。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from astrbot.api import logger
from cryptography.fernet import Fernet, InvalidToken

from .errors import VrcError

ACCOUNT_KEY_PREFIX = "account:"
ACCOUNT_INDEX_KEY = "account_index"
CRYPTO_KEY = "crypto_key"


class KVBackend(Protocol):
    """AstrBot 插件 KV 存储接口（Star 自带 put/get/delete_kv_data）。"""

    async def put_kv_data(self, key: str, value: Any) -> None: ...

    async def get_kv_data(self, key: str, default: Any) -> Any: ...

    async def delete_kv_data(self, key: str) -> None: ...


class CredentialStore:
    """按 sender_id 加密读写 VRChat 绑定凭据。"""

    def __init__(self, kv: KVBackend, configured_key: str = "") -> None:
        self._kv = kv
        self._configured_key = (configured_key or "").strip()
        self._fernet: Fernet | None = None

    # ---------- Fernet 密钥管理 ----------

    async def _get_fernet(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet

        if self._configured_key:
            try:
                self._fernet = Fernet(self._configured_key.encode("utf-8"))
            except Exception as exc:  # 配置错误需给出可读提示
                raise VrcError(
                    "插件配置中的加密密钥（encryption_key）无效，"
                    "应为 Fernet 格式（urlsafe base64 编码的 32 字节密钥）。"
                ) from exc
            return self._fernet

        raw = await self._kv.get_kv_data(CRYPTO_KEY, None)
        if isinstance(raw, str) and raw:
            try:
                self._fernet = Fernet(raw.encode("utf-8"))
                return self._fernet
            except Exception:  # noqa: BLE001
                logger.warning(
                    "KV 中的 Fernet 密钥损坏，将重新生成；旧绑定将无法解密。"
                )

        key = Fernet.generate_key()
        await self._kv.put_kv_data(CRYPTO_KEY, key.decode("utf-8"))
        self._fernet = Fernet(key)
        return self._fernet

    # ---------- 记录读写 ----------

    async def save(self, sender_id: str, record: dict[str, Any]) -> None:
        """加密保存一条绑定记录，并维护索引。"""
        fernet = await self._get_fernet()
        payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
        token = fernet.encrypt(payload).decode("utf-8")
        await self._kv.put_kv_data(ACCOUNT_KEY_PREFIX + sender_id, token)
        await self._add_index(sender_id)

    async def load(self, sender_id: str) -> dict[str, Any] | None:
        """读取并解密一条绑定记录；不存在或无法解密时返回 None。"""
        token = await self._kv.get_kv_data(ACCOUNT_KEY_PREFIX + sender_id, None)
        if not token or not isinstance(token, str):
            return None
        fernet = await self._get_fernet()
        try:
            raw = fernet.decrypt(token.encode("utf-8"))
        except InvalidToken:
            # 加密密钥被更换或密文损坏：清理后要求用户重新绑定。
            logger.warning("用户 %s 的绑定记录无法解密（密钥可能已更换）。", sender_id)
            await self.delete(sender_id)
            return None
        try:
            record = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            await self.delete(sender_id)
            return None
        return record if isinstance(record, dict) else None

    async def delete(self, sender_id: str) -> None:
        """删除绑定记录并从索引中移除。"""
        await self._kv.delete_kv_data(ACCOUNT_KEY_PREFIX + sender_id)
        await self._remove_index(sender_id)

    async def list_ids(self) -> list[str]:
        """返回全部已绑定的 sender_id（用于启动预热）。"""
        index = await self._kv.get_kv_data(ACCOUNT_INDEX_KEY, [])
        if not isinstance(index, list):
            return []
        return [str(item) for item in index if item]

    # ---------- 索引维护 ----------

    async def _add_index(self, sender_id: str) -> None:
        index = await self.list_ids()
        if sender_id not in index:
            index.append(sender_id)
            await self._kv.put_kv_data(ACCOUNT_INDEX_KEY, index)

    async def _remove_index(self, sender_id: str) -> None:
        index = await self.list_ids()
        if sender_id in index:
            index = [item for item in index if item != sender_id]
            await self._kv.put_kv_data(ACCOUNT_INDEX_KEY, index)
