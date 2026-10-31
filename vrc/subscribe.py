"""好友上线提醒与全局通知推送（后台轮询）。

设计要点：

- **持久化**：每个 QQ 用户一条记录，键 ``subscribe:<sender_id>``，存 AstrBot 插件 KV；
  另用 ``subscribe_index`` 维护 sender_id 列表（与 :mod:`vrc.crypto` 的索引一致），
  避免每次轮询都要全表扫描。
- **防封**：轮询间隔下限硬编码为 :data:`POLL_INTERVAL`（300 秒），配置只能调大；
  启动后先等 :data:`FIRST_POLL_DELAY` 再首轮，错开登录态预热。
- **去重**：上线提醒依赖 ``last_online_status`` 保存的上一次状态做边沿检测
  （offline → online 才推送）；通知推送依赖 ``seen_notification_ids``（只留最近
  :data:`SEEN_LIMIT` 个）。
- **隔离**：每个订阅者的整段处理包在 ``try/except`` 里，用户 A 掉线/限流不会影响
  用户 B 的提醒与通知（模块 7）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.event import MessageChain

from .account import VrcAccount
from .client import VrcClient
from .errors import VrcError
from .manager import AccountManager
from .render import NOTIFICATION_LABELS, fmt_time, platform_label

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from astrbot.api.star import Star

POLL_INTERVAL = 300
"""轮询间隔下限（秒）。VRChat 对高频请求非常敏感，不允许配置得比这更小。"""

FIRST_POLL_DELAY = 45
"""启动后到首轮轮询的等待秒数，避开插件 initialize() 的登录态预热。"""

SEEN_LIMIT = 50
"""``seen_notification_ids`` 最多保留的条数。"""

SUBSCRIBE_KEY_PREFIX = "subscribe:"
SUBSCRIBE_INDEX_KEY = "subscribe_index"

ONLINE_STATUS_ON = "online"
ONLINE_STATUS_OFF = "offline"


# ----------------------------------------------------------------------
# 数据模型
# ----------------------------------------------------------------------


@dataclass
class Subscription:
    """一个用户的订阅状态（整条记录序列化后直接进 KV）。"""

    umo: str = ""
    """推送目标会话（``event.unified_msg_origin``）。"""

    online_alerts: list[str] = field(default_factory=list)
    """订阅了上线提醒的好友 ``usr_`` ID 列表。"""

    notify_enabled: bool = False
    """是否推送全局通知（好友请求 / 邀请 / 私信等）。"""

    last_online_status: dict[str, str] = field(default_factory=dict)
    """``usr_`` ID -> ``"online"`` / ``"offline"``，用于边沿检测。"""

    seen_notification_ids: list[str] = field(default_factory=list)
    """已经推送过的通知 ID（保留最近 :data:`SEEN_LIMIT` 个）。"""

    @property
    def is_empty(self) -> bool:
        return not self.online_alerts and not self.notify_enabled

    @classmethod
    def from_record(cls, record: Any) -> Subscription:
        """从 KV 里读出的 dict 还原；脏数据一律降级为空订阅而不是抛异常。"""
        if not isinstance(record, dict):
            return cls()

        alerts = record.get("online_alerts")
        statuses = record.get("last_online_status")
        seen = record.get("seen_notification_ids")
        return cls(
            umo=str(record.get("umo") or ""),
            online_alerts=[
                str(item) for item in (alerts or []) if str(item).startswith("usr_")
            ],
            notify_enabled=bool(record.get("notify_enabled", False)),
            last_online_status={
                str(k): ("online" if str(v) == "online" else "offline")
                for k, v in dict(statuses or {}).items()
            },
            seen_notification_ids=[
                str(item) for item in (seen or []) if str(item).strip()
            ][:SEEN_LIMIT],
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "umo": self.umo,
            "online_alerts": list(self.online_alerts),
            "notify_enabled": self.notify_enabled,
            "last_online_status": dict(self.last_online_status),
            "seen_notification_ids": list(self.seen_notification_ids[:SEEN_LIMIT]),
        }


# ----------------------------------------------------------------------
# 展示用文本
# ----------------------------------------------------------------------


def is_online(friend: dict[str, Any]) -> bool:
    """在线判定：``location != "offline"``，或网页端（``platform == "web"``）在线。

    与 ``main._is_online`` 保持同一口径，否则会出现「好友列表显示在线、但订阅
    永远不推送上线提醒」的割裂。
    """
    location = str(friend.get("location") or "").strip().lower()
    platform = str(friend.get("platform") or "").strip().lower()
    if platform == "web":
        return True
    return bool(location) and location != ONLINE_STATUS_OFF


def online_alert_text(friend: dict[str, Any]) -> str:
    """生成一条「好友上线」提醒文本。"""
    friend_id = str(friend.get("id") or "")
    name = str(friend.get("displayName") or friend_id or "未知好友")
    platform = platform_label(friend)
    platform_part = f"（{platform}）" if platform else ""
    status = str(friend.get("status") or "")
    status_part = f" · {status}" if status and status.lower() != "active" else ""
    world = str(friend.get("location") or "")
    world_part = ""
    if world and world != ONLINE_STATUS_OFF and not world.startswith("private"):
        world_part = f"\n世界：{world.split(':')[0]}"
    return f"🟢 好友上线：{name}{platform_part}{status_part}{world_part}\nID：{friend_id}"


def notification_text(notif: dict[str, Any]) -> str:
    """生成一条「新通知」摘要。"""
    notif_type = str(notif.get("type") or "notification")
    label = NOTIFICATION_LABELS.get(notif_type, notif_type)
    message = str(notif.get("message") or "").strip() or label
    sender = str(notif.get("senderUsername") or notif.get("senderUserId") or "").strip()
    sender_part = f"（来自 {sender}）" if sender else ""
    notif_id = str(notif.get("id") or "")
    created = fmt_time(notif.get("created_at"))
    created_part = f" · {created}" if created and created != "—" else ""
    return f"· [{label}] {message}{sender_part}{created_part}\n  ID：{notif_id}"


# ----------------------------------------------------------------------
# 订阅管理
# ----------------------------------------------------------------------


class SubscriptionManager:
    """订阅记录的读写 + 后台轮询推送。"""

    def __init__(
        self,
        plugin: Star,
        client: VrcClient,
        accounts: AccountManager,
        *,
        interval: int = POLL_INTERVAL,
    ) -> None:
        self._plugin = plugin
        self.client = client
        self.accounts = accounts
        self.interval = max(POLL_INTERVAL, int(interval))
        self._task: asyncio.Task | None = None

    # ---------------- 生命周期 ----------------

    def start(self) -> asyncio.Task:
        """挂载后台轮询任务（幂等）。"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever())
            logger.info(
                "VRChat 订阅轮询已启动，间隔 %s 秒（下限 %s 秒）。",
                self.interval,
                POLL_INTERVAL,
            )
        return self._task

    async def stop(self) -> None:
        """取消后台轮询任务；可重复调用。"""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ---------------- 持久化 ----------------

    @staticmethod
    def _key(sender_id: str) -> str:
        return SUBSCRIBE_KEY_PREFIX + sender_id

    async def load(self, sender_id: str) -> Subscription | None:
        raw = await self._plugin.get_kv_data(self._key(sender_id), None)
        if raw is None:
            return None
        return Subscription.from_record(raw)

    async def save(self, sender_id: str, sub: Subscription) -> None:
        await self._plugin.put_kv_data(self._key(sender_id), sub.to_record())
        await self._add_index(sender_id)

    async def delete(self, sender_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._plugin.delete_kv_data(self._key(sender_id))
        await self._remove_index(sender_id)

    async def sender_ids(self) -> list[str]:
        index = await self._plugin.get_kv_data(SUBSCRIBE_INDEX_KEY, [])
        if not isinstance(index, list):
            return []
        return [str(item) for item in index if item]

    async def entries(self) -> list[tuple[str, Subscription]]:
        """读出全部非空订阅（索引与记录不一致时自动修正）。"""
        result: list[tuple[str, Subscription]] = []
        stale: list[str] = []
        for sender_id in await self.sender_ids():
            sub = await self.load(sender_id)
            if sub is None or sub.is_empty:
                stale.append(sender_id)
                continue
            result.append((sender_id, sub))
        for sender_id in stale:
            await self._remove_index(sender_id)
        return result

    async def _add_index(self, sender_id: str) -> None:
        index = await self.sender_ids()
        if sender_id not in index:
            index.append(sender_id)
            await self._plugin.put_kv_data(SUBSCRIBE_INDEX_KEY, index)

    async def _remove_index(self, sender_id: str) -> None:
        index = await self.sender_ids()
        if sender_id in index:
            await self._plugin.put_kv_data(
                SUBSCRIBE_INDEX_KEY, [item for item in index if item != sender_id]
            )

    # ---------------- 命令侧辅助 ----------------

    async def baseline_notifications(
        self, account: VrcAccount, sub: Subscription
    ) -> int:
        """把「当前已有通知」标记为已读，避免一开启通知就把历史消息刷屏。

        返回被标记的历史通知条数。
        """
        notifications = await self.client.list_notifications(account, n=20)
        ids = [str(item.get("id") or "") for item in notifications]
        sub.seen_notification_ids = [item for item in ids if item][:SEEN_LIMIT]
        return len(sub.seen_notification_ids)

    # ---------------- 轮询 ----------------

    async def run_forever(self) -> None:
        """后台常驻循环：异常只记日志，绝不让任务退出。"""
        try:
            await asyncio.sleep(FIRST_POLL_DELAY)
            while True:
                started = time.monotonic()
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 轮询任务必须活下去
                    logger.exception("VRChat 订阅轮询整体异常，将在下一轮重试。")
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(5.0, self.interval - elapsed))
        except asyncio.CancelledError:
            logger.info("VRChat 订阅轮询任务已取消。")
            raise

    async def poll_once(self) -> None:
        """遍历所有订阅者拉一轮；单个用户失败不影响其他人（模块 7）。"""
        entries = await self.entries()
        for sender_id, sub in entries:
            try:
                await self.poll_subscriber(sender_id, sub)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 隔离单个用户
                logger.warning(
                    "VRChat 订阅轮询跳过用户 %s：%s", sender_id, exc
                )

    async def poll_subscriber(self, sender_id: str, sub: Subscription) -> None:
        """处理单个订阅者：恢复登录态 → 上线提醒 → 新通知 → 推送。"""
        umo = sub.umo.strip()
        if not umo:
            logger.debug("订阅 %s 缺少目标会话，跳过。", sender_id)
            return

        try:
            account, _ = await self.accounts.ensure_session(sender_id)
        except VrcError as exc:
            logger.info("订阅 %s 恢复登录态失败：%s", sender_id, exc)
            return

        messages: list[str] = []
        if sub.online_alerts:
            messages.extend(await self._collect_online_alerts(account, sub))
        if sub.notify_enabled:
            fresh = await self._collect_new_notifications(account, sub)
            if fresh:
                messages.append(fresh)

        if messages:
            await self.push(umo, "\n\n".join(messages))
        await self.save(sender_id, sub)

    async def _collect_online_alerts(
        self, account: VrcAccount, sub: Subscription
    ) -> list[str]:
        """比对好友在线状态，返回本轮的上线提醒文本列表。"""
        # 必须同时拉 offline=false / true：否则好友离线后会从结果里消失，
        # last_online_status 永远停在 online，下一次上线就不会再提醒。
        friends = await self.client.list_friends(
            account, n=100, include_offline=True
        )
        by_id = {str(item.get("id") or ""): item for item in friends}
        alerts: list[str] = []

        for friend_id in list(sub.online_alerts):
            friend = by_id.get(friend_id)
            if friend is None:
                continue
            online = is_online(friend)
            previous = sub.last_online_status.get(friend_id)
            if previous is None:
                # 首次见到这个订阅对象：只记录基线，不推送，避免刚订阅就误报
                sub.last_online_status[friend_id] = (
                    ONLINE_STATUS_ON if online else ONLINE_STATUS_OFF
                )
                continue
            if previous == ONLINE_STATUS_OFF and online:
                alerts.append(online_alert_text(friend))
            sub.last_online_status[friend_id] = (
                ONLINE_STATUS_ON if online else ONLINE_STATUS_OFF
            )

        # 清理已不在订阅列表里的状态，防止记录无限膨胀
        for friend_id in set(sub.last_online_status) - set(sub.online_alerts):
            sub.last_online_status.pop(friend_id, None)

        return alerts

    async def _collect_new_notifications(
        self, account: VrcAccount, sub: Subscription
    ) -> str:
        """拉取通知列表，返回合并后的新通知文本（没有新通知则返回空串）。"""
        notifications = await self.client.list_notifications(account, n=20)
        seen = set(sub.seen_notification_ids)

        if not seen and notifications:
            # 首次开启通知：把当前已有的当作基线，不刷屏
            sub.seen_notification_ids = [
                str(item.get("id") or "") for item in notifications
            ][:SEEN_LIMIT]
            return ""

        fresh = [
            item
            for item in notifications
            if str(item.get("id") or "") and str(item.get("id")) not in seen
        ]
        if not fresh:
            return ""

        # 新通知按时间倒序展示，最多 10 条，避免单条消息过长
        lines = [notification_text(item) for item in fresh[:10]]
        header = f"🔔 VRChat 新通知 {len(fresh)} 条"
        if len(fresh) > len(lines):
            lines.append(f"……另有 {len(fresh) - len(lines)} 条，发送 /vrc 通知 查看")

        new_ids = [str(item.get("id") or "") for item in fresh]
        sub.seen_notification_ids = (new_ids + sub.seen_notification_ids)[:SEEN_LIMIT]
        return header + "\n" + "\n".join(lines)

    async def push(self, umo: str, text: str) -> None:
        """主动向目标会话推送一条纯文本消息。"""
        try:
            await self._plugin.context.send_message(umo, MessageChain().message(text))
        except Exception:  # noqa: BLE001 - 推送失败不影响后续轮询
            logger.exception("VRChat 订阅推送失败：umo=%s", umo)
