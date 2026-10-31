"""绑定管理：登录态持久化、恢复校验、2FA（TOTP）自动验证。

核心行为（对应需求）：
- 登录成功后提取 ``auth`` / ``twoFactorAuth`` 两个 Cookie 加密存入 AstrBot KV；
- 每次调用/启动恢复登录态后直接调用 ``get_current_user()`` 验证，
  **仅在抛出 UnauthorizedException(401) 时**回退密码登录；
- TOTP 账号使用加密存储的 secret 配合 ``pyotp`` 自动生成验证码完成
  ``verify2_fa``，全程无需人工输入。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import pyotp

from astrbot.api import logger

from .account import VrcAccount
from .client import VrcClient
from .crypto import CredentialStore
from .errors import (
    BindError,
    NotBoundError,
    TwoFactorRequiredError,
    VrcApiError,
    VrcError,
    VrcUnauthorizedError,
)

CodeProvider = Callable[[str], Awaitable[str]]
"""交互式验证码提供者：入参为验证方式（totp/emailOtp/otp），返回用户输入的验证码。"""

TWO_FACTOR_LABELS = {
    "totp": "验证器（TOTP）",
    "otp": "恢复代码",
    "emailOtp": "邮箱验证码",
}


def describe_method(method: str) -> str:
    return TWO_FACTOR_LABELS.get(method, method)


class AccountManager:
    """负责账号绑定、登录态恢复与凭据持久化。"""

    def __init__(self, store: CredentialStore, client: VrcClient) -> None:
        self.store = store
        self.client = client

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def is_bound(self, sender_id: str) -> bool:
        return await self.store.load(sender_id) is not None

    async def bind(
        self,
        sender_id: str,
        username: str,
        password: str,
        totp_secret: str = "",
        code_provider: CodeProvider | None = None,
    ) -> tuple[VrcAccount, dict]:
        """绑定并登录 VRChat 账号。

        若账号开启 2FA 且没有可用的 TOTP secret，会抛出
        :class:`TwoFactorRequiredError`（携带 account 上下文），调用方收集到
        验证码后可继续调用 :meth:`complete_2fa` 完成绑定。
        """
        account = VrcAccount(
            sender_id=sender_id,
            username=username,
            password=password,
            totp_secret=totp_secret or "",
            bound_at=time.time(),
        )
        try:
            return await self._login_with_password(account, code_provider)
        except TwoFactorRequiredError:
            raise
        except VrcUnauthorizedError as exc:
            raise BindError("VRChat 账号或密码不正确，请检查后重试。") from exc
        except VrcApiError as exc:
            if exc.status_code == 403:
                raise BindError(
                    "登录被 VRChat 拒绝（403）：请确认账号状态正常，"
                    "或稍后重试（不要频繁尝试登录）。"
                ) from exc
            raise

    async def complete_2fa(self, account: VrcAccount, method: str, code: str) -> dict:
        """使用人工输入的验证码完成 2FA 并保存登录态。"""
        await self.client.verify_2fa(account, method, code)
        me = await self.client.get_current_user(account, use_cache=False)
        await self._finalize(account, me)
        return me

    async def ensure_session(
        self,
        sender_id: str,
        code_provider: CodeProvider | None = None,
    ) -> tuple[VrcAccount, dict]:
        """恢复登录态并校验；401 时先静默重试，再回退密码登录。

        顺序（尽量避免触发 VRChat 的登录风控）：
        1. 用 KV 里的 Cookie 调 ``get_current_user()``；
        2. 401 → **不提示用户**，用同一批 Cookie 再试一次（服务端偶发抖动/边缘节点
           不同步时，第二次往往就成功）；
        3. 仍 401 → 静默用密码重新登录（有 TOTP secret 会自动完成 2FA）。

        :raises NotBoundError: 用户未绑定。
        :raises VrcError: 其他网络/业务错误。
        """
        record = await self.store.load(sender_id)
        if record is None:
            raise NotBoundError()

        account = VrcAccount.from_record(sender_id, record)
        try:
            me = await self._verify(account)
            return account, me
        except VrcUnauthorizedError:
            logger.info(
                "用户 %s 的 VRChat 登录态疑似失效，先用现有 Cookie 静默重试一次。",
                sender_id,
            )

        try:
            me = await self._verify(account)
            logger.info("用户 %s 用同一批 Cookie 重试成功，登录态仍然有效。", sender_id)
            return account, me
        except VrcUnauthorizedError:
            logger.info(
                "用户 %s 二次校验仍为 401，静默改用密码重新登录。", sender_id
            )

        return await self._login_with_password(account, code_provider)

    async def _verify(self, account: VrcAccount) -> dict:
        """校验一次登录态，并在 Cookie 被服务端滚动更新时回写。"""
        me = await self.client.get_current_user(account, use_cache=False)
        if account.dirty:
            await self.persist(account)
        return me

    async def unbind(self, sender_id: str) -> bool:
        """解绑：登出（尽力而为）并删除加密凭据。"""
        record = await self.store.load(sender_id)
        if record is None:
            return False
        account = VrcAccount.from_record(sender_id, record)
        if account.has_login_cookies():
            await self.client.logout(account)
        await self.store.delete(sender_id)
        self.client.invalidate_user(account)
        return True

    async def persist(self, account: VrcAccount) -> None:
        await self.store.save(account.sender_id, account.to_record())
        account.dirty = False

    async def warm_up(self, concurrency: int = 3) -> None:
        """启动时预热：恢复并校验全部已绑定账号的登录态。"""
        sender_ids = await self.store.list_ids()
        if not sender_ids:
            return
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _warm(sender_id: str) -> None:
            async with semaphore:
                try:
                    await self.ensure_session(sender_id)
                    logger.info("VRChat 登录态预热成功：%s", sender_id)
                except TwoFactorRequiredError:
                    logger.warning(
                        "用户 %s 的 VRChat 账号需要 2FA 验证码，登录态未能自动恢复；"
                        "请重新执行绑定并提供 TOTP 密钥。",
                        sender_id,
                    )
                except VrcError as exc:
                    logger.warning("VRChat 登录态预热失败（%s）：%s", sender_id, exc)
                # 预热不允许中断加载
                except Exception as exc:  # noqa: BLE001
                    logger.warning("VRChat 登录态预热异常（%s）：%s", sender_id, exc)

        await asyncio.gather(*(_warm(sid) for sid in sender_ids))

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_method(methods: list[str]) -> str:
        for preferred in ("totp", "otp", "emailOtp"):
            if preferred in methods:
                return preferred
        return methods[0] if methods else "totp"

    def _totp_code(self, account: VrcAccount) -> str | None:
        if not account.totp_secret:
            return None
        try:
            return pyotp.TOTP(account.totp_secret.strip().replace(" ", "")).now()
        # secret 损坏时走人工输入
        except Exception:  # noqa: BLE001
            logger.warning(
                "用户 %s 的 TOTP 密钥无效，转为人工输入验证码。", account.sender_id
            )
            return None

    async def _login_with_password(
        self,
        account: VrcAccount,
        code_provider: CodeProvider | None = None,
    ) -> tuple[VrcAccount, dict]:
        outcome = await self.client.login(account, account.username, account.password)

        if outcome.need_two_factor:
            method = self._pick_method(outcome.requires_2fa)
            code: str | None = None
            if method == "totp":
                code = self._totp_code(account)
            if code is None and code_provider is not None:
                code = (await code_provider(method)).strip() or None
            if not code:
                # 没有 secret 也拿不到人工验证码：把 account 带出去供后续补验证码
                raise TwoFactorRequiredError(outcome.requires_2fa, account)
            await self.client.verify_2fa(account, method, code)

        me = await self.client.get_current_user(account, use_cache=False)
        await self._finalize(account, me)
        return account, me

    async def _finalize(self, account: VrcAccount, me: dict) -> None:
        account.user_id = str(me.get("id") or account.user_id or "")
        account.display_name = str(me.get("displayName") or account.display_name or "")
        await self.persist(account)

    # 便于调试：绝不打印密码/Cookie/密钥
    def __repr__(self) -> str:  # pragma: no cover
        return "<AccountManager>"
