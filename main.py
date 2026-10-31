"""VRChat 助手（AstrBot 插件）。

统一以 ``/vrc`` 单入口注册指令，在 handler 内解析 ``event.message_str`` 分发
到九个业务域：用户 / 世界 / 好友 / 通知 / 群组 / 收藏 / 角色（Avatar）/ 账号 /
订阅。

安全与合规设计：
- 每个 QQ 用户通过 ``event.get_sender_id()`` 独立绑定，所有读写只使用本人凭证；
- 凭证经 Fernet 加密后按 sender_id 存入 AstrBot KV；登录态（auth / twoFactorAuth
  等全部 Cookie）持久化，恢复后调用 ``get_current_user()`` 验证，仅 401 时回退
  密码登录（且先静默重试一次）；
- TOTP 账号使用加密存储的 secret 配合 pyotp 自动生成验证码完成 verify2_fa；
- 只读查询统一 60 秒 TTL 缓存，429 指数退避，尽量降低对 VRChat API 的压力；
- 订阅轮询间隔下限 300 秒，单用户异常隔离，避免触发 VRChat 风控。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)

from .vrc.account import VrcAccount
from .vrc.cache import TTLCache
from .vrc.client import VrcClient
from .vrc.crypto import CredentialStore
from .vrc.errors import (
    CommandUsageError,
    NotBoundError,
    TwoFactorRequiredError,
    VrcApiError,
    VrcError,
    VrcNetworkError,
    VrcRateLimitError,
    VrcUnauthorizedError,
)
from .vrc.manager import AccountManager, describe_method
from .vrc.parsing import check_id, keyword, parse_credentials, strip_quotes, tokenize
from .vrc.render import (
    NOTIFICATION_LABELS,
    Card,
    CardRenderer,
    fmt_num,
    fmt_time,
    platform_label,
    status_meta,
)
from .vrc.subscribe import Subscription, SubscriptionManager

RENDER_OPTIONS: dict[str, Any] = {"type": "png", "full_page": True}
"""t2i 渲染参数：``full_page`` 强制截整页，避免长卡片被视口高度截断。"""

FAVORITE_TYPE_LABELS = {
    "world": "世界",
    "avatar": "虚拟形象",
    "friend": "好友",
    "vrcPlusWorld": "世界（VRC+）",
}

FAVORITE_TYPE_PREFIXES = {
    "world": "wrld_",
    "avatar": "avtr_",
    "friend": "usr_",
}

FAVORITE_CATEGORY_ALIASES = {
    "世界": "world",
    "world": "world",
    "世界收藏": "world",
    "角色": "avatar",
    "形象": "avatar",
    "虚拟形象": "avatar",
    "avatar": "avatar",
    "好友": "friend",
    "friend": "friend",
}

CMD_ALIASES = {
    "帮助": "help",
    "help": "help",
    "指令": "help",
    "菜单": "help",
    "?": "help",
    "关于我": "about_me",
    "我": "about_me",
    "me": "about_me",
    "用户": "user",
    "用户搜索": "user_search",
    "世界": "world",
    "世界搜索": "world_search",
    "好友": "friends",
    "通知": "notifications",
    "notif": "notifications",
    "群组": "group",
    "收藏": "favorites",
    "角色": "avatar",
    "形象": "avatar",
    "avatar": "avatar",
    "订阅": "subscribe",
    "subscribe": "subscribe",
    "取消订阅": "unsubscribe",
    "退订": "unsubscribe",
    "绑定": "bind",
    "解绑": "unbind",
}


@dataclass
class Reply:
    """一条待发送的回复：纯文本，或一张卡片。

    ``needs_image`` 决定卡片怎么发：
    - ``True``：走 ``html_render`` 出图（资料页、世界详情、带缩略图的列表等）；
    - ``False``：直接把 ``card.fallback`` 当纯文本发（绑定成功、解绑、错误回执、
      好友请求回执等轻量通知），省掉一次远程 t2i 往返。

    默认沿用卡片自身的 ``Card.needs_image``，需要时可在调用处显式覆盖。
    """

    text: str | None = None
    card: Card | None = None
    needs_image: bool = True

    @classmethod
    def of_text(cls, text: str) -> Reply:
        return cls(text=text)

    @classmethod
    def of_card(cls, card: Card, *, needs_image: bool | None = None) -> Reply:
        return cls(
            card=card,
            needs_image=card.needs_image if needs_image is None else bool(needs_image),
        )


class SenderSessionFilter(SessionFilter):
    """以发送者 ID 作为会话标识，保证多人同时绑定互不串扰。"""

    def filter(self, event: AstrMessageEvent) -> str:
        return f"vrc-bind:{event.get_sender_id()}"


class VrcPlugin(Star):
    """🎮 VRChat 助手

    以 /vrc 为唯一入口查询 VRChat 用户 / 世界 / 好友 / 通知 / 群组 / 收藏 /
    虚拟形象（Avatar），并支持账号绑定管理与好友上线/通知订阅。

    发送「/vrc 帮助」查看全部指令。

    示例：
    - /vrc 关于我
    - /vrc 用户 "usr_xxxx"
    - /vrc 世界搜索 "club"
    - /vrc 好友 添加好友 "usr_xxxx"
    - /vrc 角色 选择 "avtr_xxxx"
    - /vrc 订阅 上线 "好友名字"
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        # AstrBot 4.14.x 的 Star 不注入 logger，这里做兼容，避免任何日志调用炸掉
        self.logger = getattr(self, "logger", None) or logger
        self.cfg = config
        self.cache = TTLCache(float(config.get("cache_ttl", 60) or 60))
        self.client = VrcClient(
            base_url=str(
                config.get("api_base_url") or "https://api.vrchat.cloud/api/1"
            ),
            user_agent=str(config.get("user_agent") or "")
            or "astrbot-plugin-vrchat/2.0.0 (+https://github.com/AstrBotDevs/AstrBot)",
            api_key=str(config.get("api_key") or ""),
            timeout=float(config.get("request_timeout", 20) or 20),
            max_retry_429=int(config.get("max_retry_429", 2) or 0),
            cache=self.cache,
        )
        self.store = CredentialStore(self, str(config.get("encryption_key") or ""))
        self.manager = AccountManager(self.store, self.client)
        self.renderer = CardRenderer()
        self.subscribe = SubscriptionManager(
            self,
            self.client,
            self.manager,
            interval=int(config.get("subscribe_interval", 300) or 300),
        )
        self._warm_task: asyncio.Task | None = None
        self._subscribe_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """插件激活时预热登录态，并挂载订阅轮询（都不阻塞加载）。"""
        if self.cfg.get("warm_up_sessions", True):
            self._warm_task = asyncio.create_task(self.manager.warm_up())
        if self.cfg.get("enable_subscribe", True):
            self._subscribe_task = self.subscribe.start()

    async def terminate(self) -> None:
        """插件卸载/重载时收尾。"""
        if self._warm_task is not None:
            self._warm_task.cancel()
            self._warm_task = None
        try:
            await self.subscribe.stop()
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("停止 VRChat 订阅轮询时出现异常：%s", exc)
        self._subscribe_task = None
        try:
            await self.client.aclose()
        except Exception as exc:  # noqa: BLE001
            # 关闭失败不影响插件卸载
            self.logger.debug("关闭 VRChat HTTP 客户端时出现异常：%s", exc)

    # ------------------------------------------------------------------
    # 单入口
    # ------------------------------------------------------------------

    @filter.command("vrc", alias={"vrchat"}, desc="VRChat 助手：/vrc 帮助 查看全部指令")
    async def vrc(self, event: AstrMessageEvent):
        """VRChat 助手。发送 /vrc 帮助 查看全部指令。"""
        event.should_call_llm(True)
        sender_id = str(event.get_sender_id() or "").strip()
        try:
            if not sender_id:
                replies = [
                    Reply.of_text("无法识别你的用户 ID，暂时不能使用 VRChat 助手。")
                ]
            elif not self._allowed(sender_id):
                replies = [
                    Reply.of_text("你没有使用 VRChat 助手的权限，请联系管理员。")
                ]
            else:
                replies = await self._dispatch(event, sender_id)
        except CommandUsageError as exc:
            usage = f"\n用法：{exc.usage}" if exc.usage else ""
            replies = [
                Reply.of_text(f"{exc.message}{usage}\n发送「/vrc 帮助」查看全部指令。")
            ]
        except NotBoundError as exc:
            replies = [Reply.of_text(str(exc))]
        except VrcRateLimitError as exc:
            replies = [Reply.of_text(str(exc))]
        except VrcNetworkError as exc:
            replies = [Reply.of_text(str(exc))]
        except VrcUnauthorizedError:
            replies = [
                Reply.of_text(
                    "VRChat 登录态失效且自动重新登录失败，请私聊重新发送「/vrc 绑定」。"
                )
            ]
        except VrcApiError as exc:
            replies = [Reply.of_text(f"VRChat API 调用失败：{exc.message}")]
        except VrcError as exc:
            replies = [Reply.of_text(str(exc))]
        except Exception:
            # 兜底：绝不让插件崩掉
            self.logger.exception("处理 VRChat 指令时出现未预期异常")
            replies = [Reply.of_text("处理指令时出现内部错误，请稍后再试。")]

        for reply in replies:
            if reply.text is not None:
                yield event.plain_result(reply.text)
                continue

            card = reply.card
            if card is None:
                continue

            # 轻量通知类回执：不生成图片，直接用卡片自带的纯文本，省掉一次远程 t2i。
            if not reply.needs_image:
                yield event.plain_result(card.fallback or "处理完成。")
                continue

            try:
                # AstrBot 4.29.1 实测签名（astrbot/core/star/base.py）：
                #   html_render(tmpl, data, return_url=True, options=None, umo=None)
                # 注意参数顺序里 return_url 在 options 之前，务必全部用关键字。
                # - umo：4.29.1 支持，用于按会话解析配置里的 t2i_endpoint；4.14.x
                #   没有该参数（会抛 TypeError），所以下面带一次无 umo 的回退。
                # - return_url=False：4.29.1 会走 download_image_by_url 落盘并返回
                #   本地路径；event.image_result(path) 内部据此调用 file_image()。
                #   当 t2i_endpoint 指向 127.0.0.1 的自建服务时，本地路径比 http
                #   URL 更可靠（平台侧抓不到回环地址）。
                try:
                    image_path = await self.html_render(
                        card.tmpl,
                        card.data,
                        return_url=False,
                        options=RENDER_OPTIONS,
                        umo=event.unified_msg_origin,
                    )
                except TypeError:
                    logger.debug("当前 AstrBot 的 html_render 不支持 umo，降级为无 umo 调用。")
                    image_path = await self.html_render(
                        card.tmpl,
                        card.data,
                        return_url=False,
                        options=RENDER_OPTIONS,
                    )
                yield event.image_result(image_path)
            except Exception:  # 渲染失败降级为纯文本
                self.logger.exception("VRChat 卡片渲染失败，降级为文本")
                yield event.plain_result(card.fallback or "（卡片渲染失败）")

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    def _allowed(self, sender_id: str) -> bool:
        allowed = self.cfg.get("allowed_user_ids") or []
        if not allowed:
            return True
        return sender_id in {str(item) for item in allowed}

    async def _dispatch(self, event: AstrMessageEvent, sender_id: str) -> list[Reply]:
        tokens = tokenize(event.message_str or "")
        if tokens:
            # 兼容 "/vrc"、"!vrc" 等带唤醒前缀的写法（正常情况下唤醒前缀已被框架剥离）
            first = tokens[0].lstrip("/!#.").lower()
            if first in ("vrc", "vrchat"):
                tokens = tokens[1:]
        domain = tokens[0] if tokens else "帮助"
        rest = tokens[1:]

        action = CMD_ALIASES.get(domain)
        if action is None:
            return [
                Reply.of_text(f"未识别的指令：「{domain}」。"),
                Reply.of_card(self.renderer.help_card()),
            ]
        if action == "help":
            return [Reply.of_card(self.renderer.help_card())]
        if action == "bind":
            return await self._cmd_bind(event, sender_id, rest)
        if action == "unbind":
            return await self._cmd_unbind(sender_id)

        # 其余指令都需要本人已绑定的 VRChat 凭证
        account, me = await self.manager.ensure_session(sender_id)

        if action == "subscribe":
            return await self._cmd_subscribe(event, sender_id, account, me, rest)
        if action == "unsubscribe":
            return await self._cmd_unsubscribe(event, sender_id, account, me, rest)

        handler = getattr(self, f"_do_{action}")
        return await handler(account, me, rest)

    # ------------------------------------------------------------------
    # 用户域
    # ------------------------------------------------------------------

    async def _do_about_me(
        self, account: VrcAccount, me: dict, rest: list[str]
    ) -> list[Reply]:
        profile = await self._safe(
            self.client.get_profile(account, str(me.get("id") or "")), {}
        )
        avatar_name = await self._avatar_name(
            account, profile.get("currentAvatar") or me.get("currentAvatar")
        )
        card = self.renderer.profile_card(
            me, profile, is_self=True, avatar_name=avatar_name
        )
        return [Reply.of_card(card)]

    async def _do_user(
        self, account: VrcAccount, me: dict, rest: list[str]
    ) -> list[Reply]:
        user_id = check_id(rest[0] if rest else None, "usr_")
        user = await self.client.get_user(account, user_id)
        profile = await self._safe(self.client.get_profile(account, user_id), {})
        friend_status = await self._safe(
            self.client.friend_status(account, user_id), {}
        )
        avatar_name = await self._avatar_name(
            account, profile.get("currentAvatar") or user.get("currentAvatar")
        )
        card = self.renderer.profile_card(
            user,
            profile,
            is_self=False,
            friend_status=friend_status,
            avatar_name=avatar_name,
        )
        return [Reply.of_card(card)]

    async def _do_user_search(
        self, account: VrcAccount, me: dict, rest: list[str]
    ) -> list[Reply]:
        kw = keyword(rest, '/vrc 用户搜索 "关键词"')
        results = await self.client.search_users(account, kw, n=self._page_size())
        items = []
        for user in results:
            user_id = str(user.get("id") or "")
            status, tone = status_meta(user.get("status"))
            chips = [{"text": status, "tone": tone}]
            if user.get("isFriend"):
                chips.append({"text": "已是好友", "tone": "green"})
            items.append(
                {
                    "thumb": user.get("iconUrl") or "",
                    "title": user.get("displayName") or user_id,
                    "sub": user.get("statusDescription") or "",
                    "chips": chips,
                    "id_text": user_id,
                    "hint": f'/vrc 用户 "{user_id}"',
                }
            )
        card = self.renderer.list_card(
            f"用户搜索：{kw}",
            items,
            subtitle=f"共 {fmt_num(len(results))} 个结果（按 VRChat 相关度排序）",
            empty="没有匹配的用户，换个关键词试试。",
            hint='将 usr_ ID 用于：/vrc 用户 "usr_" 、/vrc 好友 添加好友 "usr_" 等指令。',
        )
        return [Reply.of_card(card)]

    # ------------------------------------------------------------------
    # 世界域
    # ------------------------------------------------------------------

    async def _do_world(
        self, account: VrcAccount, me: dict, rest: list[str]
    ) -> list[Reply]:
        if not rest:
            worlds = await self.client.list_worlds(account, n=self._page_size())
            card = self.renderer.list_card(
                "热门世界",
                [self._world_item(w) for w in worlds],
                subtitle="按热度排序，60 秒缓存",
                empty="暂时没有获取到世界列表。",
                hint='查看详情：/vrc 世界 "wrld_"',
            )
            return [Reply.of_card(card)]
        world_id = check_id(rest[0], "wrld_")
        world = await self.client.get_world(account, world_id)
        return [Reply.of_card(self.renderer.world_card(world))]

    async def _do_world_search(
        self, account: VrcAccount, me: dict, rest: list[str]
    ) -> list[Reply]:
        kw = keyword(rest, '/vrc 世界搜索 "关键词"')
        worlds = await self.client.search_worlds(account, kw, n=self._page_size())
        card = self.renderer.list_card(
            f"世界搜索：{kw}",
            [self._world_item(w) for w in worlds],
            subtitle=f"共 {fmt_num(len(worlds))} 个结果",
            empty="没有匹配的世界，换个关键词试试。",
            hint='查看详情：/vrc 世界 "wrld_"',
        )
        return [Reply.of_card(card)]

    def _world_item(self, world: dict) -> dict[str, Any]:
        world_id = str(world.get("id") or "")
        return {
            "thumb": world.get("thumbnailImageUrl") or world.get("imageUrl") or "",
            "title": world.get("name") or "未命名世界",
            "sub": f"作者：{world.get('authorName') or '未知'}",
            "chips": [
                {"text": f"容量 {fmt_num(world.get('capacity'))}", "tone": "blue"},
                {"text": f"访问 {fmt_num(world.get('visits'))}", "tone": "soft"},
                {"text": f"收藏 {fmt_num(world.get('favorites'))}", "tone": "soft"},
            ],
            "id_text": world_id,
            "hint": f'/vrc 世界 "{world_id}"',
        }

    # ------------------------------------------------------------------
    # 好友域
    # ------------------------------------------------------------------

    async def _do_friends(
        self, account: VrcAccount, me: dict, rest: list[str]
    ) -> list[Reply]:
        sub = rest[0] if rest else ""
        tail = rest[1:]

        if not sub or sub in ("全部", "all"):
            full = sub in ("全部", "all")
            if full:
                # 全部好友：offline=true/false 两路分页合并去重，再按 A-Z 排序
                friends = await self.client.list_friends(
                    account, n=100, include_offline=True
                )
                ordered = sorted(
                    friends,
                    key=lambda f: str(f.get("displayName") or "").lower(),
                )
                limit = len(ordered)
            else:
                # 默认只列在线好友（offline=false 单次请求），在线优先 + 分页截断
                friends = await self.client.list_friends(account, n=100)
                ordered = sorted(friends, key=self._friend_online_rank)
                limit = max(1, int(self.cfg.get("friends_page_size", 10) or 10))

            online_count = sum(1 for f in friends if self._is_online(f))
            items = []
            for friend in ordered[:limit]:
                friend_id = str(friend.get("id") or "")
                online = self._is_online(friend)
                if online:
                    # 在线：直出 API 原始状态（在线 / 可加入 / 询问我 / 忙碌）
                    status, tone = status_meta(friend.get("status"))
                else:
                    # 离线：不要显示 API 里残留的「可加入」等在线态
                    status, tone = "离线", "grey"

                platform = platform_label(friend)
                chips = [{"text": status, "tone": tone}]
                if platform:
                    # 平台独立成一个 Chip，避免「在线 (PC)」与「PC」并存
                    chips.append({"text": platform, "tone": "soft"})

                items.append(
                    {
                        "thumb": friend.get("currentAvatarImageUrl")
                        or friend.get("iconUrl")
                        or "",
                        "title": friend.get("displayName") or friend_id,
                        "sub": self._friend_location_line(friend),
                        "chips": chips,
                        "id_text": friend_id,
                        "hint": f'/vrc 用户 "{friend_id}"',
                    }
                )

            if full:
                subtitle = (
                    f"共 {fmt_num(len(friends))} 位好友 · 在线 "
                    f"{fmt_num(online_count)} 位 · 按 A-Z 排序"
                )
            else:
                subtitle = (
                    f"在线好友 {fmt_num(len(friends))} 位"
                    f"（显示前 {len(items)} 位）· 查看离线请用 /vrc 好友 全部"
                )

            card = self.renderer.list_card(
                "全部好友（A-Z）" if full else "我的好友",
                items,
                subtitle=subtitle,
                empty="你的好友列表是空的。",
                hint=(
                    "好友操作：/vrc 好友 状态 / 添加好友 / 取消请求 / 删除好友 / "
                    '接受请求（均需对应 ID）。全部好友：/vrc 好友 全部；'
                    '上线提醒：/vrc 订阅 上线 "名字"'
                ),
            )
            return [Reply.of_card(card)]

        if sub == "状态":
            user_id = check_id(tail[0] if tail else None, "usr_")
            status = await self.client.friend_status(account, user_id)
            user = await self._safe(self.client.get_user(account, user_id), {})
            name = str(user.get("displayName") or user_id)
            relation = (
                "已是好友"
                if status.get("isFriend")
                else "你已发送好友请求（对方未接受）"
                if status.get("outgoingRequest")
                else "对方已向你发送好友请求"
                if status.get("incomingRequest")
                else "尚无好友关系"
            )
            hints = []
            if status.get("isFriend"):
                hints.append(f'/vrc 好友 删除好友 "{user_id}"')
            elif status.get("outgoingRequest"):
                hints.append(f'/vrc 好友 取消请求 "{user_id}"')
            elif status.get("incomingRequest"):
                hints.append("/vrc 好友 接受请求  → 先查看 /vrc 通知 获取 frq_ ID")
            else:
                hints.append(f'/vrc 好友 添加好友 "{user_id}"')
            return [
                Reply.of_card(
                    self.renderer.notice(
                        f"好友状态：{name}",
                        [
                            f"用户 ID：{user_id}",
                            f"好友关系：{relation}",
                            f"已发送请求：{'是' if status.get('outgoingRequest') else '否'}",
                            f"收到请求：{'是' if status.get('incomingRequest') else '否'}",
                        ],
                        ok=True,
                        subtitle="查询结果来自 VRChat 好友状态接口",
                        hint="可用操作：" + " ；".join(hints),
                    )
                )
            ]

        if sub in ("添加好友", "添加"):
            user_id = check_id(tail[0] if tail else None, "usr_")
            await self.client.send_friend_request(account, user_id)
            return [
                Reply.of_card(
                    self.renderer.notice(
                        "好友请求已发送",
                        [f"目标用户：{user_id}"],
                        ok=True,
                        subtitle="对方接受后你们将成为好友",
                        hint="VRChat 对好友请求有频率限制，请勿短时间内大量发送。",
                    )
                )
            ]

        if sub in ("取消请求", "取消"):
            user_id = check_id(tail[0] if tail else None, "usr_")
            await self.client.cancel_friend_request(account, user_id)
            return [
                Reply.of_card(
                    self.renderer.notice(
                        "已取消好友请求", [f"目标用户：{user_id}"], ok=True
                    )
                )
            ]

        if sub in ("删除好友", "删除"):
            user_id = check_id(tail[0] if tail else None, "usr_")
            await self.client.delete_friend(account, user_id)
            return [
                Reply.of_card(
                    self.renderer.notice(
                        "已删除好友", [f"目标用户：{user_id}"], ok=True
                    )
                )
            ]

        if sub in ("接受请求", "接受"):
            if not tail:
                return await self._list_friend_requests(account)
            notification_id = check_id(tail[0], "frq_")
            await self.client.accept_friend_request(account, notification_id)
            return [
                Reply.of_card(
                    self.renderer.notice(
                        "已接受好友请求",
                        [f"通知 ID：{notification_id}"],
                        ok=True,
                        hint="可在 /vrc 好友 查看最新好友列表。",
                    )
                )
            ]

        raise CommandUsageError(
            f"未知的好友子指令：「{sub}」。",
            usage='/vrc 好友 | /vrc 好友 全部 | /vrc 好友 状态 "usr_" | '
            '/vrc 好友 添加好友 "usr_" | /vrc 好友 取消请求 "usr_" | '
            '/vrc 好友 删除好友 "usr_" | /vrc 好友 接受请求 "frq_"',
        )

    async def _list_friend_requests(self, account: VrcAccount) -> list[Reply]:
        """列出待处理的好友请求（frq_ ID），供「接受请求」使用。"""
        notifications = await self.client.list_notifications(
            account, n=20, notification_type="friendRequest"
        )
        requests = [n for n in notifications if str(n.get("type")) == "friendRequest"]
        items = []
        for notif in requests:
            notif_id = str(notif.get("id") or "")
            items.append(
                {
                    "thumb": "",
                    "title": notif.get("message") or "好友请求",
                    "sub": f"来自 {notif.get('senderUsername') or notif.get('senderUserId') or '未知用户'} · {fmt_time(notif.get('created_at'))}",
                    "chips": [{"text": "friendRequest", "tone": "blue"}],
                    "id_text": notif_id,
                    "hint": f'/vrc 好友 接受请求 "{notif_id}"',
                }
            )
        card = self.renderer.list_card(
            "待处理的好友请求",
            items,
            subtitle="接受好友请求需要 frq_ 开头的通知 ID",
            empty="当前没有待处理的好友请求。",
            hint='也可以直接使用：/vrc 好友 接受请求 "frq_..."',
            needs_image=False,
        )
        return [Reply.of_card(card)]

    # ------------------------------------------------------------------
    # 通知域
    # ------------------------------------------------------------------

    async def _do_notifications(
        self, account: VrcAccount, me: dict, rest: list[str]
    ) -> list[Reply]:
        notifications = await self.client.list_notifications(
            account, n=self._page_size()
        )
        items = []
        for notif in notifications:
            notif_id = str(notif.get("id") or "")
            notif_type = str(notif.get("type") or "notification")
            label = NOTIFICATION_LABELS.get(notif_type, notif_type)
            hint = ""
            if notif_type == "friendRequest":
                hint = f'复制此 ID → /vrc 好友 接受请求 "{notif_id}"'
            items.append(
                {
                    "thumb": "",
                    "title": notif.get("message") or label,
                    "sub": f"{label} · {fmt_time(notif.get('created_at'))}",
                    "chips": [
                        {
                            "text": label,
                            "tone": "blue" if notif_type == "friendRequest" else "soft",
                        }
                    ],
                    "id_text": notif_id,
                    "hint": hint,
                }
            )
        card = self.renderer.list_card(
            "通知",
            items,
            subtitle=f"最近 {fmt_num(len(notifications))} 条通知",
            empty="暂时没有通知。",
            hint='接受好友请求请使用通知里的 frq_ ID：/vrc 好友 接受请求 "frq_..."',
            needs_image=False,
        )
        return [Reply.of_card(card)]

    # ------------------------------------------------------------------
    # 群组域
    # ------------------------------------------------------------------

    async def _do_group(
        self, account: VrcAccount, me: dict, rest: list[str]
    ) -> list[Reply]:
        if rest and rest[0] in ("成员", "members"):
            group_id = check_id(rest[1] if len(rest) > 1 else None, "grp_")
            group = await self._safe(self.client.get_group(account, group_id), {})
            members = await self.client.list_group_members(
                account, group_id, n=self._page_size()
            )
            items = []
            for member in members:
                user = member.get("user") or {}
                user_id = str(member.get("userId") or user.get("id") or "")
                items.append(
                    {
                        "thumb": user.get("iconUrl") or "",
                        "title": user.get("displayName") or user_id,
                        "sub": f"加入于 {fmt_time(member.get('joinedAt'))}",
                        "chips": [
                            {
                                "text": str(member.get("membershipStatus") or "member"),
                                "tone": "green"
                                if member.get("membershipStatus") == "member"
                                else "soft",
                            }
                        ],
                        "id_text": user_id,
                        "hint": f'/vrc 用户 "{user_id}"',
                    }
                )
            card = self.renderer.list_card(
                f"群组成员：{group.get('name') or group_id}",
                items,
                subtitle=f"成员总数 {fmt_num(group.get('memberCount'))} · 显示最近加入的 {len(items)} 位",
                empty="没有获取到成员列表（你可能不是该群组成员）。",
                hint="群组成员接口不返回你自己，你的成员信息在群组详情中。",
            )
            return [Reply.of_card(card)]

        group_id = check_id(rest[0] if rest else None, "grp_")
        group = await self.client.get_group(account, group_id)
        return [Reply.of_card(self.renderer.group_card(group))]

    # ------------------------------------------------------------------
    # 收藏域
    # ------------------------------------------------------------------

    async def _do_favorites(
        self, account: VrcAccount, me: dict, rest: list[str]
    ) -> list[Reply]:
        # 不带参数时默认看世界收藏；「角色 / 形象 / avatar」看 Avatar 收藏
        if not rest:
            return await self._list_favorites(account, "world")

        head = strip_quotes(rest[0])

        if head in FAVORITE_CATEGORY_ALIASES:
            return await self._list_favorites(account, FAVORITE_CATEGORY_ALIASES[head])

        if head in ("添加世界", "添加"):
            world_id = check_id(rest[1] if len(rest) > 1 else None, "wrld_")
            tag = (
                strip_quotes(rest[2])
                if len(rest) > 2
                else str(self.cfg.get("favorite_tag_world") or "worlds1")
            )
            favorite = await self.client.add_favorite(account, world_id, "world", [tag])
            return [
                Reply.of_card(
                    self.renderer.notice(
                        "已收藏世界",
                        [
                            f"世界 ID：{world_id}",
                            f"收藏组：{tag}",
                            f"收藏 ID：{favorite.get('id') or '（未知）'}",
                        ],
                        ok=True,
                        hint='移除收藏：/vrc 收藏 移除 "fvrt_..."',
                    )
                )
            ]

        if head in ("移除", "删除"):
            favorite_id = check_id(rest[1] if len(rest) > 1 else None, "fvrt_")
            await self.client.remove_favorite(account, favorite_id)
            return [
                Reply.of_card(
                    self.renderer.notice(
                        "已移除收藏", [f"收藏 ID：{favorite_id}"], ok=True
                    )
                )
            ]

        raise CommandUsageError(
            f"未知的收藏子指令：「{head}」。",
            usage='/vrc 收藏 | /vrc 收藏 角色 | /vrc 收藏 添加世界 "wrld_" [收藏组] '
            '| /vrc 收藏 移除 "fvrt_"',
        )

    async def _list_favorites(
        self, account: VrcAccount, favorite_type: str
    ) -> list[Reply]:
        """按类型列出收藏（世界 / 虚拟形象 / 好友）。"""
        favorites = await self.client.list_favorites(
            account, n=self._page_size(), favorite_type=favorite_type
        )
        items = []
        for favorite in favorites:
            name, thumb = await self._resolve_favorite(account, favorite)
            fav_type = str(favorite.get("type") or favorite_type)
            favorite_id = str(favorite.get("id") or "")
            target_id = str(favorite.get("favoriteId") or "")
            items.append(
                {
                    "thumb": thumb,
                    "title": name,
                    "sub": f"收藏组：{', '.join(favorite.get('tags') or []) or '（未分组）'}",
                    "chips": [
                        {
                            "text": FAVORITE_TYPE_LABELS.get(
                                fav_type, fav_type or "未知"
                            ),
                            "tone": "purple",
                        }
                    ],
                    "id_text": f"{target_id} · {favorite_id}",
                    "hint": f'/vrc 收藏 移除 "{favorite_id}"',
                }
            )
        type_label = FAVORITE_TYPE_LABELS.get(favorite_type, favorite_type)
        card = self.renderer.list_card(
            f"我的收藏（{type_label}）",
            items,
            subtitle=f"共 {fmt_num(len(favorites))} 条 {type_label} 收藏",
            empty=f"还没有收藏任何{type_label}。",
            hint=(
                f'查看另一类：/vrc 收藏 角色' if favorite_type == "world" else "/vrc 收藏"
            )
            + '；移除：/vrc 收藏 移除 "fvrt_"',
        )
        return [Reply.of_card(card)]

    async def _resolve_favorite(
        self, account: VrcAccount, favorite: dict
    ) -> tuple[str, str]:
        """解析收藏对象的名称与缩略图（失败时回退为 ID）。"""
        fav_type = str(favorite.get("type") or "")
        target_id = str(favorite.get("favoriteId") or "")
        try:
            if fav_type == "world":
                world = await self.client.get_world(account, target_id)
                return str(world.get("name") or target_id), str(
                    world.get("thumbnailImageUrl") or ""
                )
            if fav_type == "avatar":
                avatar = await self.client.get_avatar(account, target_id)
                return str(avatar.get("name") or target_id), str(
                    avatar.get("thumbnailImageUrl") or ""
                )
            if fav_type == "friend":
                user = await self.client.get_user(account, target_id)
                return str(user.get("displayName") or target_id), str(
                    user.get("iconUrl") or ""
                )
            if fav_type == "group":
                group = await self.client.get_group(account, target_id)
                return str(group.get("name") or target_id), str(
                    group.get("iconUrl") or ""
                )
        except VrcError:
            pass
        return target_id or "（未知收藏）", ""

    # ------------------------------------------------------------------
    # 角色域（Avatar：虚拟形象/模型，不是资料头像）
    # ------------------------------------------------------------------

    async def _do_avatar(
        self, account: VrcAccount, me: dict, rest: list[str]
    ) -> list[Reply]:
        if not rest:
            avatars = await self.client.list_avatars(
                account, n=self._page_size(), user="me"
            )
            title = "我的虚拟形象（Avatar）"
            empty = "你还没有上传任何虚拟形象。"
            hint = '换装：/vrc 角色 选择 "avtr_"；搜索：/vrc 角色 搜索 "关键词"'
        elif rest[0] in ("搜索", "search"):
            kw = keyword(rest[1:], '/vrc 角色 搜索 "关键词"')
            avatars = await self.client.list_avatars(
                account, n=self._page_size(), search=kw
            )
            title = f"虚拟形象搜索：{kw}"
            empty = "没有匹配的虚拟形象，换个关键词试试。"
            hint = '换装：/vrc 角色 选择 "avtr_"'
        elif rest[0] in ("选择", "换装", "select"):
            avatar_id = check_id(rest[1] if len(rest) > 1 else None, "avtr_")
            await self.client.select_avatar(account, avatar_id)
            avatar = await self._safe(self.client.get_avatar(account, avatar_id), {})
            return [
                Reply.of_card(
                    self.renderer.notice(
                        "已换上新的虚拟形象（Avatar）",
                        [
                            f"虚拟形象：{avatar.get('name') or avatar_id}",
                            f"虚拟形象 ID：{avatar_id}",
                        ],
                        ok=True,
                        subtitle="虚拟形象（Avatar）= 你在世界中使用的 3D 角色/模型，不是资料头像",
                        hint="此操作只更换 Avatar，不影响资料头像（Profile Picture）。",
                    )
                )
            ]
        else:
            raise CommandUsageError(
                f"未知的角色子指令：「{rest[0]}」。",
                usage='/vrc 角色 | /vrc 角色 搜索 "关键词" | /vrc 角色 选择 "avtr_"',
            )

        items = [self._avatar_item(a) for a in avatars]
        card = self.renderer.list_card(
            title,
            items,
            subtitle=f"共 {fmt_num(len(avatars))} 个虚拟形象（Avatar）",
            empty=empty,
            hint=hint,
        )
        return [Reply.of_card(card)]

    def _avatar_item(self, avatar: dict) -> dict[str, Any]:
        avatar_id = str(avatar.get("id") or "")
        release = str(avatar.get("releaseStatus") or "")
        return {
            "thumb": avatar.get("thumbnailImageUrl") or avatar.get("imageUrl") or "",
            "title": avatar.get("name") or "未命名形象",
            "sub": f"作者：{avatar.get('authorName') or '未知'}",
            "chips": [
                {
                    "text": {"public": "公开", "private": "私有", "hidden": "隐藏"}.get(
                        release, release or "未知"
                    ),
                    "tone": "green" if release == "public" else "grey",
                }
            ],
            "id_text": avatar_id,
            "hint": f'/vrc 角色 选择 "{avatar_id}"',
        }

    # ------------------------------------------------------------------
    # 账号域：绑定 / 解绑
    # ------------------------------------------------------------------

    async def _cmd_unbind(self, sender_id: str) -> list[Reply]:
        removed = await self.manager.unbind(sender_id)
        if not removed:
            return [Reply.of_text("你还没有绑定 VRChat 账号。")]
        return [
            Reply.of_card(
                self.renderer.notice(
                    "已解绑 VRChat 账号",
                    ["本地加密凭据与登录态已删除，远端登录会话已登出。"],
                    ok=True,
                    hint="随时可以再次使用「/vrc 绑定」重新绑定。",
                )
            )
        ]

    async def _cmd_bind(
        self, event: AstrMessageEvent, sender_id: str, rest: list[str]
    ) -> list[Reply]:
        allow_group = bool(self.cfg.get("bind_allow_group", False))
        if not event.is_private_chat() and not allow_group:
            return [
                Reply.of_text(
                    "为了保护账号安全，绑定 VRChat 账号请在【私聊】中进行。\n"
                    "请私聊机器人发送：/vrc 绑定"
                )
            ]

        username, password, totp_secret = parse_credentials(rest)
        if not (username and password):
            # 交互式绑定：等待用户在私聊中发送凭据
            text = await self._wait_input(
                event,
                "请在 120 秒内直接发送（无需命令前缀，多个值用空格分隔，含空格请加引号）：\n"
                "账号 密码 [2FA密钥]\n\n"
                "说明：\n"
                "1. 「2FA密钥」为可选的 TOTP 密钥（验证器 App 中的那串字符），"
                "提供后机器人可自动填写两步验证验证码；\n"
                '2. 也可以直接使用：/vrc 绑定 账号 "邮箱" 密码 "密码" [2FA密钥 "密钥"]；\n'
                "3. 发送「取消」退出。凭据仅加密存储于本机，请在发送后撤回该消息。",
                timeout=120,
            )
            if text is None:
                return [Reply.of_text("绑定已超时，请重新发送「/vrc 绑定」。")]
            username, password, totp_secret = parse_credentials(tokenize(text))
            if not (username and password):
                return [
                    Reply.of_text(
                        "没有解析到完整的账号信息，绑定已取消。\n"
                        '正确格式：账号 密码 [2FA密钥]，或使用 /vrc 绑定 账号 "邮箱" 密码 "密码"'
                    )
                ]

        try:
            account, me = await self.manager.bind(
                sender_id,
                username,
                password,
                totp_secret,
                code_provider=lambda method: self._ask_2fa_code(event, method),
            )
        except TwoFactorRequiredError as exc:
            # 没有 TOTP 密钥时，人工输入一次验证码补齐登录态
            if exc.account is None:
                return [Reply.of_text("两步验证上下文丢失，请重新发送「/vrc 绑定」。")]
            code = await self._ask_2fa_code(event, exc.method)
            if not code:
                return [Reply.of_text("未收到验证码，绑定已取消。")]
            try:
                me = await self.manager.complete_2fa(exc.account, exc.method, code)
                account = exc.account
            except VrcError as exc2:
                return [Reply.of_text(f"绑定失败：{exc2}")]

        display_name = str(me.get("displayName") or account.display_name or "")
        has_totp = bool(account.totp_secret)
        return [
            Reply.of_card(
                self.renderer.notice(
                    f"绑定成功：{display_name}",
                    [
                        f"VRChat 用户 ID：{me.get('id') or account.user_id}",
                        f"用户名：{me.get('username') or account.username}",
                        f"两步验证（2FA）：{'已配置 TOTP 自动验证' if has_totp else '登录态已持久化，无需重复验证'}",
                    ],
                    ok=True,
                    subtitle="登录态（auth/twoFactorAuth Cookie）已加密保存，后续查询不再重复 2FA",
                    hint="现在可以使用「/vrc 关于我」等指令。如需彻底退出请发送 /vrc 解绑。",
                )
            )
        ]

    # ------------------------------------------------------------------
    # 订阅域：好友上线提醒 / 全局通知
    # ------------------------------------------------------------------

    async def _cmd_subscribe(
        self,
        event: AstrMessageEvent,
        sender_id: str,
        account: VrcAccount,
        me: dict,
        rest: list[str],
    ) -> list[Reply]:
        """/vrc 订阅 <上线|通知|列表>。"""
        sub_cmd = strip_quotes(rest[0]) if rest else ""
        sub = await self.subscribe.load(sender_id) or Subscription()
        sub.umo = event.unified_msg_origin

        if sub_cmd in ("通知", "notify"):
            sub.notify_enabled = not sub.notify_enabled
            baselined = 0
            if sub.notify_enabled:
                try:
                    baselined = await self.subscribe.baseline_notifications(account, sub)
                except VrcError as exc:
                    self.logger.warning("标记历史通知为已读失败：%s", exc)
            await self.subscribe.save(sender_id, sub)
            lines = [f"推送会话：{sub.umo}"]
            if sub.notify_enabled:
                lines.append(f"已读基线：{fmt_num(baselined)} 条历史通知不会重复推送")
            return [
                Reply.of_card(
                    self.renderer.notice(
                        "全局通知已开启" if sub.notify_enabled else "全局通知已关闭",
                        lines,
                        ok=sub.notify_enabled,
                        subtitle="每 5 分钟轮询一次，只推送新产生的通知",
                        hint="再次发送 /vrc 订阅 通知 可切换开关。",
                    )
                )
            ]

        if sub_cmd in ("列表", "list", "状态"):
            return await self._render_subscription(account, sub)

        if sub_cmd in ("上线", "online", "好友上线"):
            friend_id, name, candidates = await self._resolve_friend(account, rest[1:])
            if candidates:
                return [Reply.of_text(self._candidates_text(candidates))]
            if not friend_id:
                return [
                    Reply.of_text(
                        "在你的好友列表中未找到该名字（仅支持自己的好友）。"
                    )
                ]
            if friend_id not in sub.online_alerts:
                sub.online_alerts.append(friend_id)
            sub.notify_enabled = True
            await self.subscribe.save(sender_id, sub)
            return [
                Reply.of_card(
                    self.renderer.notice(
                        "已订阅好友上线提醒",
                        [
                            f"好友：{name}（{friend_id}）",
                            f"当前订阅数：{fmt_num(len(sub.online_alerts))}",
                            "全局通知：已开启",
                        ],
                        ok=True,
                        subtitle="该好友从离线变为在线时，会主动推送到本会话",
                        hint=(
                            '取消：/vrc 取消订阅 上线 "名字"；'
                            "查看：/vrc 订阅 列表。"
                        ),
                    )
                )
            ]

        raise CommandUsageError(
            f"未知的订阅子指令：「{sub_cmd}」。" if sub_cmd else "缺少订阅子指令。",
            usage='/vrc 订阅 上线 "名字或 usr_" | /vrc 订阅 通知 | /vrc 订阅 列表',
        )

    async def _cmd_unsubscribe(
        self,
        event: AstrMessageEvent,
        sender_id: str,
        account: VrcAccount,
        me: dict,
        rest: list[str],
    ) -> list[Reply]:
        """/vrc 取消订阅 <上线|全部>。"""
        sub_cmd = strip_quotes(rest[0]) if rest else ""
        sub = await self.subscribe.load(sender_id)
        if sub is None or sub.is_empty:
            return [Reply.of_text("你还没有任何订阅。")]

        sub.umo = event.unified_msg_origin

        if sub_cmd in ("全部", "all"):
            await self.subscribe.delete(sender_id)
            return [
                Reply.of_card(
                    self.renderer.notice(
                        "已清空全部订阅",
                        ["上线提醒与全局通知均已关闭，历史状态已删除。"],
                        ok=True,
                        hint='重新订阅：/vrc 订阅 上线 "名字"',
                    )
                )
            ]

        if sub_cmd in ("上线", "online", "好友上线"):
            friend_id, name, candidates = await self._resolve_friend(account, rest[1:])
            if candidates:
                return [Reply.of_text(self._candidates_text(candidates))]
            if not friend_id:
                return [
                    Reply.of_text("在你的好友列表中未找到该名字（仅支持自己的好友）。")
                ]
            if friend_id not in sub.online_alerts:
                return [
                    Reply.of_text(f"你没有订阅 {name}（{friend_id}）的上线提醒。")
                ]
            sub.online_alerts.remove(friend_id)
            sub.last_online_status.pop(friend_id, None)
            if sub.is_empty:
                await self.subscribe.delete(sender_id)
            else:
                await self.subscribe.save(sender_id, sub)
            return [
                Reply.of_card(
                    self.renderer.notice(
                        "已取消好友上线提醒",
                        [
                            f"好友：{name}（{friend_id}）",
                            f"剩余订阅：{fmt_num(len(sub.online_alerts))} 位",
                        ],
                        ok=True,
                        hint="查看当前状态：/vrc 订阅 列表",
                    )
                )
            ]

        raise CommandUsageError(
            f"未知的取消订阅子指令：「{sub_cmd}」。"
            if sub_cmd
            else "缺少取消订阅子指令。",
            usage='/vrc 取消订阅 上线 "名字或 usr_" | /vrc 取消订阅 全部',
        )

    async def _render_subscription(
        self, account: VrcAccount, sub: Subscription
    ) -> list[Reply]:
        """渲染 /vrc 订阅 列表。"""
        if sub.is_empty:
            return [
                Reply.of_text(
                    '你还没有任何订阅。发送「/vrc 订阅 上线 "名字"」开始。'
                )
            ]

        names = await self._friend_name_map(account, sub.online_alerts)
        lines = [f"全局通知：{'已开启' if sub.notify_enabled else '已关闭'}"]
        if sub.online_alerts:
            lines.append(f"上线提醒（{len(sub.online_alerts)} 位）：")
            for friend_id in sub.online_alerts:
                state = (
                    "在线"
                    if sub.last_online_status.get(friend_id) == "online"
                    else "离线/待确认"
                )
                lines.append(
                    f"· {names.get(friend_id, '未知好友')}（{friend_id}）— {state}"
                )
        else:
            lines.append("上线提醒：未订阅任何好友")
        lines.append(f"推送会话：{sub.umo or '（未设置）'}")

        return [
            Reply.of_card(
                self.renderer.notice(
                    "订阅状态",
                    lines,
                    ok=True,
                    subtitle="每 5 分钟轮询一次 VRChat 接口（间隔下限 300 秒）",
                    hint=(
                        '新增：/vrc 订阅 上线 "名字"；'
                        "清空：/vrc 取消订阅 全部。"
                    ),
                )
            )
        ]

    async def _resolve_friend(
        self, account: VrcAccount, args: list[str]
    ) -> tuple[str, str, list[dict[str, str]]]:
        """把「名字或 usr_ ID」解析成好友。

        返回 ``(好友 ID, 显示名, 多个候选)``：
        - 唯一命中 → ``(usr_xxx, 名字, [])``；
        - 多个候选 → ``("", "", [{id, name}, ...])``；
        - 没有命中 → ``("", "", [])``。
        """
        token = strip_quotes(" ".join(args).strip())
        if not token:
            raise CommandUsageError(
                "请提供好友名字或 usr_ ID。",
                usage='/vrc 订阅 上线 "名字或 usr_"',
            )

        if token.lower().startswith("usr_"):
            friend_id = check_id(token, "usr_")
            user = await self._safe(self.client.get_user(account, friend_id), {})
            return friend_id, str(user.get("displayName") or friend_id), []

        # 订阅对象可能是离线好友，必须连 offline=true 一起拉
        friends = await self.client.list_friends(
            account, n=100, include_offline=True
        )
        matches = self._match_friends(friends, token)
        if not matches:
            return "", "", []
        if len(matches) == 1:
            return matches[0]["id"], matches[0]["name"], []
        return "", "", matches

    @staticmethod
    def _match_friends(
        friends: list[dict], keyword_text: str
    ) -> list[dict[str, str]]:
        """在好友列表里做「先精确、后包含」的模糊匹配。"""
        kw = keyword_text.strip().lower()
        if not kw:
            return []
        exact = [
            f for f in friends if str(f.get("displayName") or "").strip().lower() == kw
        ]
        pool = exact or [
            f for f in friends if kw in str(f.get("displayName") or "").lower()
        ]
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for friend in pool:
            friend_id = str(friend.get("id") or "")
            if not friend_id or friend_id in seen:
                continue
            seen.add(friend_id)
            result.append(
                {"id": friend_id, "name": str(friend.get("displayName") or friend_id)}
            )
        return result

    @staticmethod
    def _candidates_text(candidates: list[dict[str, str]]) -> str:
        """同名好友过多时的提示文案。"""
        shown = candidates[:10]
        lines = [
            f"找到 {len(candidates)} 个同名/相似好友，请使用具体的 usr_ ID 重新订阅："
        ]
        for item in shown:
            lines.append(f"· {item['name']}  {item['id']}")
        if len(candidates) > len(shown):
            lines.append(f"……另有 {len(candidates) - len(shown)} 个")
        return "\n".join(lines)

    async def _friend_name_map(
        self, account: VrcAccount, friend_ids: list[str]
    ) -> dict[str, str]:
        """ID -> 显示名，用于订阅列表展示。"""
        if not friend_ids:
            return {}
        friends = await self._safe(
            self.client.list_friends(account, n=100, include_offline=True), []
        )
        by_id = {
            str(item.get("id") or ""): str(item.get("displayName") or "")
            for item in friends
        }
        return {friend_id: by_id.get(friend_id) or friend_id for friend_id in friend_ids}

    # ------------------------------------------------------------------
    # 交互式输入（session_waiter）
    # ------------------------------------------------------------------

    async def _ask_2fa_code(self, event: AstrMessageEvent, method: str) -> str:
        """等待用户输入一次性验证码（仅私聊接受）。

        - ``emailOtp``：VRChat 已把 6 位数字码发到邮箱，给出明确指引并开启纯数字校验；
        - ``totp``：提示用户用验证器 App 里的 6 位数字；
        - ``otp``：恢复代码是字母数字混合，不做纯数字校验。
        """
        if method == "emailOtp":
            text = await self._wait_input(
                event,
                "VRChat 已向你的邮箱发送验证码，请在 120 秒内直接回复 6 位数字验证码"
                "（千万不要引用本条消息）：",
                timeout=120,
                numeric=True,
            )
            return (text or "").strip()

        label = describe_method(method)
        text = await self._wait_input(
            event,
            f"该账号开启了两步验证，请在 120 秒内发送【{label}】验证码：",
            timeout=120,
            numeric=(method != "otp"),  # 恢复代码（otp）不是纯数字
        )
        return (text or "").strip()

    def _extract_text(self, event: AstrMessageEvent) -> str:
        """尽力取出这条消息的纯文本。

        QQ 在用户「输入中」、仅发送图片/表情、或只引用了一条消息时，会下发
        ``message_str`` 为空的事件；此时回退到逐个 ``Plain`` 组件拼接，仍为空
        才认定为「无内容事件」，交给调用方静默忽略。
        """
        text = (event.message_str or "").strip()
        if text:
            return text
        try:
            from astrbot.api.message_components import Plain

            parts: list[str] = []
            for comp in getattr(event.message_obj, "message", None) or []:
                if isinstance(comp, Plain):
                    parts.append(str(comp.text or ""))
            return "".join(parts).strip()
        except Exception:  # noqa: BLE001 - 取不到文本时按空消息处理
            return ""

    async def _wait_input(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        timeout: int = 120,
        numeric: bool = False,
    ) -> str | None:
        """提示并等待同一用户发送下一条消息，返回其文本；超时返回 None。

        关键行为：收到空文本事件（「输入中」状态、纯图片、纯引用）时**不发送任何
        回复**，只 ``keep`` 续期并返回，静默等待下一条真正的输入；否则空消息会被
        当成「验证码格式错误」，反复提示并耗尽重试次数，把绑定流程打断。
        """
        holder: dict[str, str] = {}

        @session_waiter(timeout=timeout)
        async def _waiter(controller: SessionController, incoming: AstrMessageEvent):
            if not incoming.is_private_chat():
                await incoming.send(
                    incoming.plain_result("请在【私聊】中继续发送，避免账号信息泄露。")
                )
                controller.keep(timeout=timeout, reset_timeout=True)
                return

            text = self._extract_text(incoming)
            if not text:
                # 空消息：绝不回复、绝不结束会话，静默续期等下一次输入。
                controller.keep(timeout=timeout, reset_timeout=True)
                return

            if text in ("取消", "退出", "cancel", "q", "Q"):
                holder["cancelled"] = "1"
                controller.stop()
                return

            if numeric:
                digits = text.replace(" ", "").replace("-", "")
                if not digits.isdigit():
                    await incoming.send(
                        incoming.plain_result("验证码应为数字，请重新发送。")
                    )
                    controller.keep(timeout=timeout, reset_timeout=True)
                    return
                text = digits

            holder["text"] = text
            controller.stop()

        await event.send(event.plain_result(prompt))
        try:
            await _waiter(event, session_filter=SenderSessionFilter())
        except TimeoutError:
            return None
        if "cancelled" in holder:
            return None
        return holder.get("text")

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _is_online(friend: dict) -> bool:
        """在线判定：``location != "offline"``，或网页端（``platform == "web"``）在线。

        VRChat 的网页端用户 ``location`` 会保持 ``offline``，只看 location 会把
        他们误判成离线；反之，离线用户的 ``status`` 可能残留「可加入」等旧值。
        """
        location = str(friend.get("location") or "").strip().lower()
        platform = str(friend.get("platform") or "").strip().lower()
        if platform == "web":
            return True
        return bool(location) and location != "offline"

    @classmethod
    def _friend_online_rank(cls, friend: dict) -> int:
        """好友排序键：0=在线、1=离线。``sorted`` 稳定，同组内保留接口原顺序。"""
        return 0 if cls._is_online(friend) else 1

    @staticmethod
    def _friend_location_line(friend: dict) -> str:
        """好友列表第二行：网页端在线 / 世界中 / 本地世界 / 离线。"""
        location = str(friend.get("location") or "").strip()
        platform = str(friend.get("platform") or "").strip().lower()
        if platform == "web":
            return "网页端在线"
        if not location or location.lower() == "offline":
            return "离线"
        if location.startswith("local"):
            return "在本地世界（local）"
        return "在世界中：" + location.split("~")[0]

    def _page_size(self) -> int:
        return max(1, min(20, int(self.cfg.get("search_page_size", 8) or 8)))

    async def _safe(self, coro, default):
        """执行 API 调用，404/网络错误时返回默认值（用于可选的补充信息）。"""
        try:
            return await coro
        except (VrcApiError, VrcNetworkError):
            return default

    async def _avatar_name(self, account: VrcAccount, avatar_id: Any) -> str:
        if not avatar_id or not str(avatar_id).startswith("avtr_"):
            return ""
        avatar = await self._safe(self.client.get_avatar(account, str(avatar_id)), {})
        return str(avatar.get("name") or "")
