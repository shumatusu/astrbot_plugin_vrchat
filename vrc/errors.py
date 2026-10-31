"""VRChat 插件的错误类型定义。

所有面向用户的错误都继承自 :class:`VrcError`，其 ``str()`` 即为可直接
展示给用户的中文提示；内部调试信息保留在各异常的属性中，不回显给用户。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from .account import VrcAccount


class VrcError(Exception):
    """插件内所有可预期错误的基类。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CommandUsageError(VrcError):
    """用户指令用法错误（参数缺失、ID 前缀不对等）。"""

    def __init__(self, message: str, usage: str | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class NotBoundError(VrcError):
    """用户尚未绑定 VRChat 账号。"""

    def __init__(self) -> None:
        super().__init__(
            "你还没有绑定 VRChat 账号，请先私聊发送「/vrc 绑定」完成绑定。"
        )


class VrcNetworkError(VrcError):
    """网络异常（超时、连接失败等）。"""

    def __init__(self, message: str = "连接 VRChat 服务失败，请稍后再试。") -> None:
        super().__init__(message)


class VrcApiError(VrcError):
    """VRChat API 返回的业务错误。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.api_message = message


class VrcUnauthorizedError(VrcApiError):
    """401：登录态失效或凭证不正确。"""

    def __init__(self, message: str = "VRChat 登录凭证无效或已过期。") -> None:
        super().__init__(401, message)


class VrcNotFoundError(VrcApiError):
    """404：目标不存在或无权查看。"""

    def __init__(
        self, message: str = "没有找到对应的资源，它可能不存在或你无权查看。"
    ) -> None:
        super().__init__(404, message)


class VrcRateLimitError(VrcApiError):
    """429：请求过于频繁。"""

    def __init__(self, message: str = "VRChat API 请求过于频繁，请稍后再试。") -> None:
        super().__init__(429, message)


class TwoFactorRequiredError(VrcError):
    """登录需要二次验证（2FA）。"""

    def __init__(
        self,
        methods: list[str],
        account: VrcAccount | None = None,
        message: str = "该账号开启了两步验证（2FA），需要补充验证码。",
    ) -> None:
        super().__init__(message)
        self.methods = methods
        """VRChat 返回的支持验证方式，如 ["totp"] / ["emailOtp"] / ["otp"]。"""
        self.account = account
        """已经过密码验证、等待 2FA 的账号上下文，便于继续完成登录。"""

    @property
    def method(self) -> str:
        """优先推荐的验证方式（TOTP 优先）。"""
        for preferred in ("totp", "otp", "emailOtp"):
            if preferred in self.methods:
                return preferred
        return self.methods[0] if self.methods else "totp"


class TwoFactorFailedError(VrcError):
    """2FA 验证失败。"""

    def __init__(self) -> None:
        super().__init__("两步验证失败：验证码不正确或已过期，请重试。")


class BindError(VrcError):
    """绑定账号失败（账号或密码错误等）。"""
