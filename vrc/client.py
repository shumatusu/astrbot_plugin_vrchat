"""VRChat 官方 API 的轻量异步客户端（httpx）。

设计要点：
- 全部请求显式携带 ``Cookie: auth=...; twoFactorAuth=...``，不共享 CookieJar，
  保证每个 QQ 用户只使用本人的登录态；
- 401 统一抛出 :class:`VrcUnauthorizedError`，由上层决定是否回退密码登录；
- 429 按指数退避重试（可配置次数），随后抛出 :class:`VrcRateLimitError`；
- 只读 GET 请求统一走 60 秒 TTL 缓存（缓存键含用户标识），写操作后自动
  清除该用户的缓存；
- User-Agent 按 VRChat 规范要求设置为 ``<应用名>/<版本> <联系地址>``。

端点与字段以 vrchatapi/specification（OpenAPI v1.23.x）为准，登录态复用
``auth`` / ``twoFactorAuth`` 两个 Cookie，与 vrchatapi-python 的行为一致。
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
from urllib.parse import quote

import httpx

from astrbot.api import logger

from .account import VrcAccount
from .cache import TTLCache
from .errors import (
    TwoFactorFailedError,
    VrcApiError,
    VrcNetworkError,
    VrcNotFoundError,
    VrcRateLimitError,
    VrcUnauthorizedError,
)

DEFAULT_BASE_URL = "https://api.vrchat.cloud/api/1"
DEFAULT_USER_AGENT = (
    "astrbot-plugin-vrchat/2.0.0 (+https://github.com/AstrBotDevs/AstrBot)"
)

# 2FA 验证方式 -> API 路径（注意 emailOtp 的路径是小写 emailotp）
TWO_FACTOR_PATHS = {
    "totp": "totp",
    "emailOtp": "emailotp",
    "otp": "otp",
}


class LoginOutcome:
    """GET /auth/user 的登录结果：要么拿到 CurrentUser，要么要求 2FA。"""

    def __init__(self, user: dict[str, Any] | None, requires_2fa: list[str]) -> None:
        self.user = user
        self.requires_2fa = requires_2fa

    @property
    def need_two_factor(self) -> bool:
        return bool(self.requires_2fa)


class VrcClient:
    """VRChat API 客户端。"""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        api_key: str = "",
        timeout: float = 20.0,
        max_retry_429: int = 2,
        cache: TTLCache | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.max_retry_429 = max(0, int(max_retry_429))
        self.cache = cache if cache is not None else TTLCache()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            headers={
                "User-Agent": user_agent or DEFAULT_USER_AGENT,
                "Accept": "application/json",
            },
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # 基础请求
    # ------------------------------------------------------------------

    def _cookie_header(self, account: VrcAccount | None) -> dict[str, str]:
        if account is None or not account.cookies:
            return {}
        cookie = "; ".join(f"{k}={v}" for k, v in account.cookies.items() if v)
        return {"Cookie": cookie} if cookie else {}

    @staticmethod
    def _basic_auth_header(username: str, password: str) -> dict[str, str]:
        # 与 vrchatapi-python 一致：先 URL 编码用户名/密码，再拼 Basic 凭证。
        token = f"{quote(username)}:{quote(password)}"
        encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}

    def _capture_cookies(
        self, response: httpx.Response, account: VrcAccount | None
    ) -> None:
        """把响应里的**全部** Cookie 写回账号上下文。

        只抓 ``auth`` / ``twoFactorAuth`` 会在 VRChat 轮换 Cookie（例如新增
        ``refresh_token``、``session`` 之类）时掉登录态；这里遍历整个 cookie jar
        全量落库，后续请求由 ``_cookie_header`` 原样带回。
        """
        if account is None:
            return

        captured = 0
        try:
            jar = response.cookies.jar
        except Exception:  # noqa: BLE001 - 某些自定义 transport 没有 jar
            jar = []

        for cookie in list(jar):
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
            if not name or not value:
                continue
            # 同名多域 Cookie 冲突时保留最后写入的一个（与服务端下发顺序一致）
            if account.set_cookie(name, value):
                captured += 1

        if captured:
            logger.debug(
                "已更新 %s 的 %d 个 Cookie：%s",
                account.sender_id,
                captured,
                ", ".join(sorted(account.cookies)),
            )

    def _cache_key(
        self, account: VrcAccount | None, path: str, params: dict[str, Any] | None
    ) -> str:
        owner = account.sender_id if account else "-"
        try:
            param_text = json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            param_text = str(params)
        return f"{owner}\x00{path}\x00{param_text}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        account: VrcAccount | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        basic_auth: tuple[str, str] | None = None,
        use_cache: bool | None = None,
    ) -> Any:
        """发起一次 API 请求并返回解析后的 JSON（可为 dict / list / None）。"""
        method = method.upper()
        params = dict(params or {})
        if self.api_key:
            params.setdefault("apiKey", self.api_key)

        cacheable = method == "GET" and use_cache is not False and account is not None
        cache_key = self._cache_key(account, path, params) if cacheable else None
        if cache_key is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        headers: dict[str, str] = {}
        headers.update(self._cookie_header(account))
        if basic_auth is not None:
            headers.update(self._basic_auth_header(*basic_auth))

        for attempt in range(self.max_retry_429 + 1):
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params or None,
                    json=json_body,
                    headers=headers or None,
                )
            except httpx.TimeoutException as exc:
                raise VrcNetworkError("请求 VRChat 服务超时，请稍后再试。") from exc
            except httpx.HTTPError as exc:
                raise VrcNetworkError() from exc

            self._capture_cookies(response, account)
            status = response.status_code

            if status == 429 and attempt < self.max_retry_429:
                # 规范要求 429 时退避而不是立即重试
                await asyncio.sleep(1.5 * (2**attempt))
                continue

            if status == 204 or not response.content:
                payload: Any = None
            else:
                try:
                    payload = response.json()
                except Exception:  # noqa: BLE001
                    payload = {"raw": response.text[:500]}

            if 200 <= status < 300:
                if cache_key is not None:
                    self.cache.set(cache_key, payload)
                return payload

            message = self._extract_error(payload, status)
            if status == 401:
                raise VrcUnauthorizedError(message)
            if status == 404:
                raise VrcNotFoundError(message)
            if status == 429:
                raise VrcRateLimitError()
            raise VrcApiError(status, message)

        raise VrcRateLimitError()  # pragma: no cover - 循环内必然抛出或返回

    @staticmethod
    def _extract_error(payload: Any, status: int) -> str:
        detail = ""
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            elif isinstance(payload.get("message"), str):
                detail = payload["message"]
        detail = detail.strip().strip('"')
        default = {
            400: "请求参数不正确。",
            403: "没有权限执行该操作。",
            404: "没有找到对应的资源，它可能不存在或你无权查看。",
            429: "VRChat API 请求过于频繁，请稍后再试。",
        }.get(status, f"VRChat 服务返回错误（HTTP {status}）。")
        return f"{default}（{detail}）" if detail and detail != default else default

    def invalidate_user(self, account: VrcAccount | None) -> None:
        """写操作后清除该用户的只读缓存。"""
        if account is not None:
            self.cache.clear_prefix(account.sender_id)

    # ------------------------------------------------------------------
    # 认证
    # ------------------------------------------------------------------

    async def login(
        self, account: VrcAccount, username: str, password: str
    ) -> LoginOutcome:
        """账密登录（GET /auth/user + Basic 认证）。

        未通过 2FA 时返回 ``requires_2fa`` 列表（如 ["totp"]），
        登录成功/待验证时会把服务端下发的 Cookie 写入 ``account``。
        """
        # 重新用密码登录时先丢弃旧的登录态 Cookie，避免陈旧 Cookie 干扰 Basic 认证。
        if account.cookies:
            account.cookies.clear()
            account.dirty = True
        payload = await self._request(
            "GET",
            "/auth/user",
            account=account,
            basic_auth=(username, password),
            use_cache=False,
        )
        if isinstance(payload, dict) and payload.get("requiresTwoFactorAuth"):
            methods = payload.get("requiresTwoFactorAuth") or []
            return LoginOutcome(None, [str(m) for m in methods])
        return LoginOutcome(payload if isinstance(payload, dict) else {}, [])

    async def verify_2fa(
        self, account: VrcAccount, method: str, code: str
    ) -> dict[str, Any]:
        """提交 2FA 验证码（成功后服务端下发 twoFactorAuth Cookie）。"""
        path_name = TWO_FACTOR_PATHS.get(method, method.lower())
        payload = await self._request(
            "POST",
            f"/auth/twofactorauth/{path_name}/verify",
            account=account,
            json_body={"code": str(code).strip()},
            use_cache=False,
        )
        if not isinstance(payload, dict) or not payload.get("verified", True):
            raise TwoFactorFailedError()
        return payload if isinstance(payload, dict) else {}

    async def get_current_user(
        self, account: VrcAccount, use_cache: bool = False
    ) -> dict[str, Any]:
        """获取当前登录用户（也用作登录态有效性校验）。"""
        payload = await self._request(
            "GET", "/auth/user", account=account, use_cache=use_cache
        )
        return payload if isinstance(payload, dict) else {}

    async def logout(self, account: VrcAccount) -> None:
        """登出并使已保存的登录态失效（尽力而为）。"""
        try:
            await self._request("PUT", "/logout", account=account, use_cache=False)
        # 登出失败不影响本地解绑
        except Exception as exc:  # noqa: BLE001
            logger.debug("VRChat logout failed: %s", exc)

    # ------------------------------------------------------------------
    # 用户
    # ------------------------------------------------------------------

    async def get_user(self, account: VrcAccount, user_id: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/users/{user_id}", account=account)
        return payload if isinstance(payload, dict) else {}

    async def get_profile(self, account: VrcAccount, user_id: str) -> dict[str, Any]:
        """公开资料页（bio、userIcon、currentAvatar 等在此端点）。"""
        payload = await self._request("GET", f"/profile/{user_id}", account=account)
        return payload if isinstance(payload, dict) else {}

    async def search_users(
        self, account: VrcAccount, keyword: str, n: int = 8
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            "/users",
            account=account,
            params={"search": keyword, "n": n},
        )
        return payload if isinstance(payload, list) else []

    # ------------------------------------------------------------------
    # 世界
    # ------------------------------------------------------------------

    async def get_world(self, account: VrcAccount, world_id: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/worlds/{world_id}", account=account)
        return payload if isinstance(payload, dict) else {}

    async def search_worlds(
        self, account: VrcAccount, keyword: str, n: int = 8
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            "/worlds",
            account=account,
            params={"search": keyword, "n": n, "sort": "relevance"},
        )
        return payload if isinstance(payload, list) else []

    async def list_worlds(
        self, account: VrcAccount, n: int = 8, sort: str = "popularity"
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET", "/worlds", account=account, params={"n": n, "sort": sort}
        )
        return payload if isinstance(payload, list) else []

    # ------------------------------------------------------------------
    # 好友
    # ------------------------------------------------------------------

    async def list_friends(
        self,
        account: VrcAccount,
        n: int = 60,
        include_offline: bool = False,
        *,
        page_size: int = 100,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """列出好友。

        VRChat 的 ``offline`` 参数是**互斥**的（``false``=在线，``true``=离线），
        一次请求只能拿到其中一半，因此这里显式区分两种模式：

        - ``include_offline=False``：只发一次 ``offline=false``，返回在线好友；
        - ``include_offline=True``：分别用 ``offline=false`` 与 ``offline=true``
          发起分页请求（``n=100``、``offset`` 递增），按 ``id`` 去重合并，
          在线好友排在前面，供「/vrc 好友 全部」与上线提醒使用。

        注意：``include_offline=True`` 至少 2 次请求、最多 ``2 * max_pages`` 次，
        不要在高频路径上调用。
        """
        if not include_offline:
            payload = await self._request(
                "GET",
                "/auth/user/friends",
                account=account,
                params={
                    "n": max(1, min(int(n), page_size)),
                    "offline": "false",
                },
            )
            return payload if isinstance(payload, list) else []

        merged: dict[str, dict[str, Any]] = {}

        # 先 online 后 offline，保证合并后的顺序是「在线在前」
        for offline_flag in ("false", "true"):
            offset = 0
            for _ in range(max(1, int(max_pages))):
                payload = await self._request(
                    "GET",
                    "/auth/user/friends",
                    account=account,
                    params={
                        "n": page_size,
                        "offset": offset,
                        "offline": offline_flag,
                    },
                )
                if not isinstance(payload, list) or not payload:
                    break

                added = 0
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    friend_id = str(item.get("id") or "")
                    if not friend_id or friend_id in merged:
                        continue
                    merged[friend_id] = item
                    added += 1

                # 不足一页说明到底；整页但没有任何新 ID 说明翻页重叠，也停
                if len(payload) < page_size or added == 0:
                    break
                offset += page_size

        return list(merged.values())

    async def friend_status(self, account: VrcAccount, user_id: str) -> dict[str, Any]:
        payload = await self._request(
            "GET", f"/user/{user_id}/friendStatus", account=account
        )
        return payload if isinstance(payload, dict) else {}

    async def send_friend_request(
        self, account: VrcAccount, user_id: str
    ) -> dict[str, Any]:
        payload = await self._request(
            "POST", f"/user/{user_id}/friendRequest", account=account
        )
        self.invalidate_user(account)
        return payload if isinstance(payload, dict) else {}

    async def cancel_friend_request(
        self, account: VrcAccount, user_id: str
    ) -> dict[str, Any]:
        payload = await self._request(
            "DELETE", f"/user/{user_id}/friendRequest", account=account
        )
        self.invalidate_user(account)
        return payload if isinstance(payload, dict) else {}

    async def delete_friend(self, account: VrcAccount, user_id: str) -> dict[str, Any]:
        payload = await self._request(
            "DELETE", f"/auth/user/friends/{user_id}", account=account
        )
        self.invalidate_user(account)
        return payload if isinstance(payload, dict) else {}

    async def accept_friend_request(
        self, account: VrcAccount, notification_id: str
    ) -> dict[str, Any]:
        """接受好友请求：需要 friendRequest 通知的 frq_ ID。"""
        payload = await self._request(
            "PUT",
            f"/auth/user/notifications/{notification_id}/accept",
            account=account,
        )
        self.invalidate_user(account)
        return payload if isinstance(payload, dict) else {}

    async def hide_notification(
        self, account: VrcAccount, notification_id: str
    ) -> dict[str, Any]:
        """隐藏/删除一条通知（也用于拒绝入站好友请求）。"""
        payload = await self._request(
            "PUT",
            f"/auth/user/notifications/{notification_id}/hide",
            account=account,
        )
        self.invalidate_user(account)
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------

    async def list_notifications(
        self,
        account: VrcAccount,
        n: int = 10,
        notification_type: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"n": n}
        if notification_type:
            params["type"] = notification_type
        payload = await self._request(
            "GET", "/auth/user/notifications", account=account, params=params
        )
        return payload if isinstance(payload, list) else []

    # ------------------------------------------------------------------
    # 群组
    # ------------------------------------------------------------------

    async def get_group(self, account: VrcAccount, group_id: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/groups/{group_id}", account=account)
        return payload if isinstance(payload, dict) else {}

    async def list_group_members(
        self, account: VrcAccount, group_id: str, n: int = 10
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/groups/{group_id}/members",
            account=account,
            params={"n": n, "sort": "joinedAt:desc"},
        )
        return payload if isinstance(payload, list) else []

    # ------------------------------------------------------------------
    # 收藏
    # ------------------------------------------------------------------

    async def list_favorites(
        self, account: VrcAccount, n: int = 10, favorite_type: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"n": n}
        if favorite_type:
            params["type"] = favorite_type
        payload = await self._request(
            "GET", "/favorites", account=account, params=params
        )
        return payload if isinstance(payload, list) else []

    async def add_favorite(
        self, account: VrcAccount, favorite_id: str, favorite_type: str, tags: list[str]
    ) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            "/favorites",
            account=account,
            json_body={"favoriteId": favorite_id, "type": favorite_type, "tags": tags},
        )
        self.invalidate_user(account)
        return payload if isinstance(payload, dict) else {}

    async def remove_favorite(
        self, account: VrcAccount, favorite_id: str
    ) -> dict[str, Any]:
        payload = await self._request(
            "DELETE", f"/favorites/{favorite_id}", account=account
        )
        self.invalidate_user(account)
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------
    # 虚拟形象（Avatar：玩家使用的 3D 角色/模型，不是资料头像）
    # ------------------------------------------------------------------

    async def list_avatars(
        self,
        account: VrcAccount,
        n: int = 8,
        user: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"n": n}
        if user:
            params["user"] = user
        if search:
            params["search"] = search
        try:
            payload = await self._request(
                "GET", "/avatars", account=account, params=params
            )
        except VrcApiError as exc:
            # search 参数在部分场景要求 marketplace=true，失败时降级重试一次
            if search and exc.status_code == 400:
                params["marketplace"] = "true"
                payload = await self._request(
                    "GET", "/avatars", account=account, params=params
                )
            else:
                raise
        return payload if isinstance(payload, list) else []

    async def get_avatar(self, account: VrcAccount, avatar_id: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/avatars/{avatar_id}", account=account)
        return payload if isinstance(payload, dict) else {}

    async def select_avatar(
        self, account: VrcAccount, avatar_id: str
    ) -> dict[str, Any]:
        """换上指定虚拟形象，返回最新的 CurrentUser。"""
        try:
            payload = await self._request(
                "PUT", f"/avatars/{avatar_id}/select", account=account, use_cache=False
            )
        except VrcNotFoundError:
            # 兼容旧版单数路径
            payload = await self._request(
                "PUT", f"/avatar/{avatar_id}/select", account=account, use_cache=False
            )
        self.invalidate_user(account)
        return payload if isinstance(payload, dict) else {}
