"""VRChat 助手插件的业务模块包。

- :mod:`vrc.client`     VRChat 官方 API 异步客户端（httpx）
- :mod:`vrc.manager`    账号绑定 / 登录态恢复 / TOTP 自动 2FA
- :mod:`vrc.crypto`     Fernet 加密 + AstrBot KV 持久化
- :mod:`vrc.cache`      只读查询 TTL 缓存
- :mod:`vrc.parsing`    指令参数解析与 ID 校验
- :mod:`vrc.render`     卡片模板（深色主题）
- :mod:`vrc.subscribe`  好友上线提醒 / 全局通知轮询
- :mod:`vrc.errors`     错误类型
"""

from __future__ import annotations

__all__ = [
    "account",
    "cache",
    "client",
    "crypto",
    "errors",
    "manager",
    "parsing",
    "render",
    "subscribe",
]
__version__ = "2.0.2"
