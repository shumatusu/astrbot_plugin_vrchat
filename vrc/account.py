"""VRChat 账号上下文：凭据 + 登录态 Cookie。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

AUTH_COOKIE = "auth"
TWO_FACTOR_COOKIE = "twoFactorAuth"

# Cookie 名只允许 token 字符，避免把脏数据拼进请求头
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-.]+$")
# Cookie 值里出现分隔符/换行会破坏 Cookie 请求头
_COOKIE_VALUE_RE = re.compile(r"^[^\s;,\r\n]+$")


@dataclass
class VrcAccount:
    """一个 QQ 用户绑定的 VRChat 账号。

    ``cookies`` 中保存登录态（``auth`` / ``twoFactorAuth``），持久化后即可
    避免反复触发 2FA；``dirty`` 标记 cookies 是否有更新需要写回存储。
    """

    sender_id: str
    username: str
    password: str
    totp_secret: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    user_id: str = ""
    display_name: str = ""
    bound_at: float = 0.0
    dirty: bool = False

    @property
    def auth_cookie(self) -> str:
        return self.cookies.get(AUTH_COOKIE, "")

    @property
    def two_factor_cookie(self) -> str:
        return self.cookies.get(TWO_FACTOR_COOKIE, "")

    def set_cookie(self, name: str, value: str) -> bool:
        """写入一个 Cookie；值有变化时返回 True 并标记 dirty。

        不再限制只能存 ``auth`` / ``twoFactorAuth``：VRChat 会下发多个会话
        Cookie，全部保留才能最大程度避免掉登录态。
        """
        if not name or not value:
            return False
        if not _COOKIE_NAME_RE.match(name) or not _COOKIE_VALUE_RE.match(value):
            return False
        if self.cookies.get(name) == value:
            return False
        self.cookies[name] = value
        self.dirty = True
        return True

    def has_login_cookies(self) -> bool:
        return bool(self.auth_cookie)

    def to_record(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "password": self.password,
            "totp_secret": self.totp_secret,
            "cookies": dict(self.cookies),
            "user_id": self.user_id,
            "display_name": self.display_name,
            "bound_at": self.bound_at or time.time(),
        }

    @classmethod
    def from_record(cls, sender_id: str, record: dict[str, Any]) -> VrcAccount:
        cookies = record.get("cookies")
        return cls(
            sender_id=sender_id,
            username=str(record.get("username", "")),
            password=str(record.get("password", "")),
            totp_secret=str(record.get("totp_secret", "") or ""),
            cookies={str(k): str(v) for k, v in dict(cookies or {}).items()},
            user_id=str(record.get("user_id", "") or ""),
            display_name=str(record.get("display_name", "") or ""),
            bound_at=float(record.get("bound_at", 0) or 0),
        )
