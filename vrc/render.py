"""深色主题的图片卡片渲染层。

模板为 HTML + Jinja2 字符串，交给 AstrBot 的 ``html_render``（Star.html_render）
渲染为图片。所有来自 VRChat 的用户内容在进入模板前都会先做 HTML 转义。

命名约定（重要）：
- ``avatar_*``  : 虚拟形象（Avatar），即玩家在世界中使用的 3D 角色/模型；
- ``profile_pic``: 资料头像（Profile Picture），即个人主页展示图。
两者是完全不同的概念，变量与文案均严格区分。
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Card:
    """一张待发送的卡片。

    ``needs_image`` 决定发送方式：
    - ``True``：交给 ``html_render`` 出图（资料页、世界详情、带缩略图的列表等）；
    - ``False``：直接用 ``fallback`` 发纯文本，跳过 t2i（绑定成功、解绑、
      好友请求回执、错误提示等轻量通知），避免为了一行字等一次远程生图。
    """

    tmpl: str
    """Jinja2 模板（``_wrap`` 处理后的完整 HTML 文档）。"""

    data: dict[str, Any]
    """模板数据。"""

    fallback: str
    """渲染失败或不需要图片时发送的纯文本。"""

    needs_image: bool = True
    """是否需要渲染成图片。"""


# ----------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------


def esc(value: Any) -> str:
    """HTML 转义；None 转为空串。"""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def fmt_num(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def fmt_time(value: Any) -> str:
    """ISO 时间（2024-05-01T12:34:56.789Z）-> 2024-05-01 12:34 UTC。"""
    if not value or not isinstance(value, str):
        return "—"
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # noqa: UP017
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def short(text: Any, limit: int = 400) -> str:
    if not text:
        return ""
    text = str(text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


STATUS_META: dict[str, tuple[str, str]] = {
    "active": ("在线", "green"),
    "join me": ("可加入", "blue"),
    "ask me": ("询问我", "purple"),
    "busy": ("忙碌", "orange"),
    "offline": ("离线", "grey"),
}

# 信任等级阶梯（按 VRChat 游戏内历史偏移修正）：高等级优先命中，都没有则视为游客
TRUST_LADDER: tuple[tuple[str, str, str], ...] = (
    ("system_trust_veteran", "资深玩家", "purple"),
    ("system_trust_trusted", "长期玩家", "orange"),
    ("system_trust_known", "玩家", "green"),
    ("system_trust_basic", "新用户", "blue"),
)

# 非信任等级类的 tag -> (文案, 配色)
TAG_LABELS: dict[str, tuple[str, str]] = {
    "system_supporter": ("VRChat+", "purple"),
    "system_supporter_tier_0": ("VRChat+ 赞助", "purple"),
    "system_supporter_tier_1": ("VRChat+ 赞助+", "purple"),
    "system_supporter_tier_2": ("VRChat+ 传奇", "purple"),
    "system_avatar_imped": ("已启用 Avatar Imped", "grey"),
    "system_probable_troll": ("低信任标记", "red"),
}


def status_meta(status: Any) -> tuple[str, str]:
    return STATUS_META.get(
        str(status or "offline").lower(), (str(status or "未知"), "grey")
    )


NOTIFICATION_LABELS: dict[str, str] = {
    "friendRequest": "好友请求",
    "invite": "邀请",
    "requestInvite": "请求邀请",
    "inviteResponse": "邀请回应",
    "requestInviteResponse": "请求邀请回应",
    "message": "私信",
    "boop": "Boop",
    "votetokick": "投票踢人",
}
"""通知类型 -> 中文标签（通知卡片与订阅推送共用）。"""

PLATFORM_LABELS: dict[str, str] = {
    "standalonewindows": "PC",
    "standalonewindows64": "PC",
    "android": "Quest",
    "ios": "iOS",
    "linux": "Linux",
    "web": "WEB",
}
"""VRChat ``platform`` 字段 -> 简短平台标签。"""


def platform_label(source: dict[str, Any]) -> str:
    """把 ``platform`` / ``last_platform`` 归一成 PC / Quest / WEB 这类短标签。"""
    raw = str(source.get("platform") or source.get("last_platform") or "").strip()
    if not raw:
        return ""
    return PLATFORM_LABELS.get(raw.lower(), raw.upper())


def trust_badge(tags: Any) -> dict[str, str]:
    """把 ``system_trust_*`` 映射成游戏内信任等级徽章。

    顺序为 资深玩家 > 长期玩家 > 玩家 > 新用户；一个都没有则显示「游客」。
    """
    tag_set = {str(tag) for tag in (tags or [])}
    for tag, label, tone in TRUST_LADDER:
        if tag in tag_set:
            return {"text": label, "tone": tone}
    return {"text": "游客", "tone": "grey"}


def badges_from_tags(tags: Any, limit: int = 8) -> list[dict[str, str]]:
    """把 VRChat 的 tag 映射成带配色的徽章。

    - ``system_trust_*`` 交给 :func:`trust_badge` 统一呈现；
    - ``language_*``（如 ``language_zho``）是界面语言偏好，不是玩家标签，直接丢弃；
    - 其余未知的 ``system_*`` 直接隐藏，普通 tag 原样显示。
    """
    result: list[dict[str, str]] = []
    for tag in tags or []:
        tag = str(tag)
        if tag.startswith("system_trust"):
            continue  # 信任等级统一由 trust_badge 呈现，避免重复
        if tag.startswith("language_"):
            continue  # 过滤 language_zho / language_eng 之类的界面语言标签
        if tag in TAG_LABELS:
            label, tone = TAG_LABELS[tag]
        elif tag.startswith("system_"):
            continue
        else:
            label, tone = tag, "soft"
        result.append({"text": label, "tone": tone})
        if len(result) >= limit:
            break
    return result


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


# ----------------------------------------------------------------------
# 样式（VRChat 深色玻璃拟态 UI）
# ----------------------------------------------------------------------

PAGE_STYLE = """
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  display: flex;
  justify-content: center;
  align-items: flex-start;
  margin: 0;
  background: #070b12;
  color: #e9f0fa;
  font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif;
}
.page {
  width: 880px; max-width: 100%; margin: 20px 0; box-sizing: border-box;
  padding: 30px; border-radius: 26px;
  background:
    radial-gradient(900px 420px at 88% -12%, rgba(45,110,240,0.30), transparent 62%),
    radial-gradient(760px 420px at -8% 112%, rgba(126,72,255,0.22), transparent 58%),
    linear-gradient(158deg, #0d1421 0%, #121c2c 52%, #0c1420 100%);
}
.brand { display: flex; align-items: center; gap: 14px; margin-bottom: 22px; }
.brand .logo {
  width: 46px; height: 46px; border-radius: 13px;
  background: linear-gradient(135deg, #2d6ef0, #8b5cf6);
  display: flex; align-items: center; justify-content: center;
  font-weight: 800; color: #fff; font-size: 15px; letter-spacing: .5px;
  box-shadow: 0 8px 22px rgba(45,110,240,.35);
}
.brand .name { font-size: 19px; font-weight: 700; letter-spacing: .4px; }
.brand .sub { font-size: 12px; color: #8aa0bd; margin-top: 3px; }
.card {
  background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.09);
  border-radius: 20px; padding: 24px; margin-bottom: 18px;
  box-shadow: 0 12px 30px rgba(0,0,0,.22);
}
.hero { display: flex; gap: 22px; align-items: flex-start; }
.hero-main { flex: 1; min-width: 0; }
.hero-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 14px; }
.avatar-wrap { position: relative; width: 180px; height: 180px; flex: none; }
.avatar {
  width: 180px; height: 180px; border-radius: 18px; object-fit: cover;
  border: 1px solid rgba(255,255,255,.14); background: #17222f;
}
.avatar.placeholder, .thumb.placeholder {
  display: flex; align-items: center; justify-content: center;
  color: #5f748e; font-weight: 700; background: linear-gradient(135deg,#1a2635,#223247);
}
.pfp {
  position: absolute; right: -8px; bottom: -8px; width: 62px; height: 62px;
  border-radius: 50%; object-fit: cover; border: 3px solid #16202e;
  box-shadow: 0 6px 16px rgba(0,0,0,.4);
}
.title { font-size: 28px; font-weight: 700; line-height: 1.25; word-break: break-word; }
.subtitle { color: #93a7c1; font-size: 14px; margin-top: 6px; line-height: 1.6; word-break: break-all; }
.mono, .idtag { font-family: Consolas, "Cascadia Mono", monospace; }
.idtag { color: #7287a2; font-size: 11.5px; margin-top: 5px; word-break: break-all; }
.badge-row { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  display: inline-flex; align-items: center; padding: 5px 13px; border-radius: 999px;
  font-size: 12.5px; font-weight: 600; letter-spacing: .2px;
  background: rgba(255,255,255,.09); color: #d6e2f2; border: 1px solid rgba(255,255,255,.10);
}
.chip.green { background: rgba(46,204,113,.16); color: #7ee2a8; border-color: rgba(46,204,113,.30); }
.chip.blue { background: rgba(45,110,240,.18); color: #9dc0ff; border-color: rgba(45,110,240,.34); }
.chip.purple { background: rgba(139,92,246,.18); color: #c7b4ff; border-color: rgba(139,92,246,.34); }
.chip.orange { background: rgba(245,166,35,.16); color: #ffd08a; border-color: rgba(245,166,35,.32); }
.chip.red { background: rgba(231,76,60,.16); color: #ff9d92; border-color: rgba(231,76,60,.32); }
.chip.grey { background: rgba(148,163,184,.14); color: #aab8c9; border-color: rgba(148,163,184,.26); }
.chip.soft { background: rgba(45,110,240,.12); color: #a9c4ee; border-color: rgba(45,110,240,.20); }
.chip.link { background: rgba(46,204,113,.10); color: #8fdcb4; border-color: rgba(46,204,113,.22); }
.desc { color: #c6d3e4; line-height: 1.8; font-size: 14.5px; white-space: pre-wrap; word-break: break-word; margin-top: 12px; }
.stats { display: flex; gap: 12px; margin: 6px 0 16px; flex-wrap: wrap; }
.stat { flex: 1; min-width: 120px; background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.07); border-radius: 15px; padding: 13px 16px; }
.stat .num { font-size: 21px; font-weight: 700; }
.stat .lab { color: #8fa4bd; font-size: 12px; margin-top: 4px; }
.section-title { font-size: 15px; font-weight: 700; color: #cfe0f5; margin: 4px 0 12px; letter-spacing: .3px; }
.row { display: flex; justify-content: space-between; gap: 18px; padding: 11px 2px; border-bottom: 1px solid rgba(255,255,255,.06); font-size: 14px; }
.row:last-child { border-bottom: none; }
.row .k { color: #8fa4bd; flex: none; }
.row .v { color: #eaf1fb; text-align: right; word-break: break-all; }
.list-item {
  display: flex; gap: 15px; padding: 13px; border-radius: 15px; margin-bottom: 11px;
  background: rgba(255,255,255,.045); border: 1px solid rgba(255,255,255,.07);
}
.thumb { width: 60px; height: 60px; border-radius: 12px; object-fit: cover; flex: none; background: #17222f; }
.item-main { flex: 1; min-width: 0; }
.item-title { font-size: 16px; font-weight: 650; word-break: break-word; }
.item-sub { color: #93a7c1; font-size: 12.8px; margin-top: 4px; line-height: 1.65; word-break: break-word; }
.item-hint { color: #6d84a3; font-size: 11.5px; margin-top: 5px; }
.empty { color: #8fa4bd; font-size: 14px; padding: 22px 4px; text-align: center; }
.note {
  margin-top: 14px; padding: 12px 15px; border-radius: 13px;
  background: rgba(45,110,240,.10); border: 1px solid rgba(45,110,240,.18);
  color: #a9c4ee; font-size: 12.5px; line-height: 1.8;
}
.avatar-block { display: flex; gap: 16px; align-items: center; }
.avatar-sm { width: 84px; height: 84px; border-radius: 15px; object-fit: cover; border: 1px solid rgba(255,255,255,.12); }
.avatar-info { min-width: 0; }
.world-thumb { width: 300px; height: 180px; border-radius: 16px; object-fit: cover; flex: none; border: 1px solid rgba(255,255,255,.12); }
.banner { width: 100%; height: 150px; object-fit: cover; border-radius: 15px; margin-bottom: 16px; border: 1px solid rgba(255,255,255,.10); }
.result-head { display: flex; gap: 16px; align-items: center; }
.result-icon {
  width: 54px; height: 54px; border-radius: 16px; flex: none;
  display: flex; align-items: center; justify-content: center;
  font-size: 27px; font-weight: 800; color: #fff;
}
.result-icon.ok { background: linear-gradient(135deg, #22c55e, #10b981); box-shadow: 0 8px 20px rgba(34,197,94,.32); }
.result-icon.warn { background: linear-gradient(135deg, #f59e0b, #f97316); box-shadow: 0 8px 20px rgba(245,158,11,.30); }
.result-icon.err { background: linear-gradient(135deg, #ef4444, #e11d48); box-shadow: 0 8px 20px rgba(239,68,68,.30); }
.result-body { margin-top: 14px; }
.cmd-row { display: flex; justify-content: space-between; gap: 18px; padding: 10px 2px; border-bottom: 1px solid rgba(255,255,255,.06); font-size: 13.5px; }
.cmd-row:last-child { border-bottom: none; }
.cmd { color: #9dc0ff; font-family: Consolas, "Cascadia Mono", monospace; flex: none; }
.cmd-desc { color: #8fa4bd; text-align: right; }
.footer { margin-top: 18px; text-align: center; color: #62789a; font-size: 11.5px; line-height: 1.9; }
</style>
"""

_BRAND = (
    '<div class="brand">'
    '<div class="logo">VRC</div>'
    "<div>"
    '<div class="name">VRChat Assistant</div>'
    '<div class="sub">数据来自 VRChat 官方 API · 只读查询 60s 缓存</div>'
    "</div></div>"
)


def _wrap(inner: str) -> str:
    """把卡片片段包装成**完整 HTML 文档**。

    t2i 渲染端（Playwright / astrbot t2i-service）只把 ``<head>`` 里的 ``<style>``
    当作样式表处理；若只提交 ``<style>`` + ``<div>`` 的片段，部分渲染策略会把
    ``<style>`` 当普通文本节点丢弃，表现为「有图无 UI」（白底黑字、没有卡片、
    徽章和配色）。因此这里必须输出带 ``<!DOCTYPE html>`` 的完整文档。
    """
    footer = (
        f"生成于 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())} · "
        "请遵守 VRChat 服务条款 · 仅展示公开信息"
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{PAGE_STYLE}</head>\n<body>\n"
        '<div class="page">'
        + _BRAND
        + inner
        + f'<div class="footer">{esc(footer)}</div>'
        + "</div>\n</body>\n</html>"
    )


# ----------------------------------------------------------------------
# 模板
# ----------------------------------------------------------------------

PROFILE_TMPL = """
<div class="card">
  <div class="hero">
    <div class="avatar-wrap">
      {% if avatar_image %}<img class="avatar" src="{{ avatar_image }}" />{% else %}<div class="avatar placeholder">VRC</div>{% endif %}
      {% if profile_pic %}<img class="pfp" src="{{ profile_pic }}" title="资料头像 Profile Picture" />{% endif %}
    </div>
    <div class="hero-main">
      <div class="hero-head">
        <div>
          <div class="title">{{ display_name }}</div>
          <div class="subtitle">{{ handle_line }}</div>
        </div>
        <div class="chip {{ status_tone }}">{{ status_text }}</div>
      </div>
      {% if badges %}<div class="badge-row">{% for b in badges %}<span class="chip {{ b.tone }}">{{ b.text }}</span>{% endfor %}</div>{% endif %}
      {% if bio %}<div class="desc">{{ bio }}</div>{% endif %}
      {% if bio_links %}<div class="badge-row">{% for l in bio_links %}<span class="chip link">{{ l }}</span>{% endfor %}</div>{% endif %}
    </div>
  </div>
</div>

<div class="card">
  {% if stats %}<div class="stats">
    {% for s in stats %}<div class="stat"><div class="num">{{ s.num }}</div><div class="lab">{{ s.lab }}</div></div>{% endfor %}
  </div>{% endif %}
  <div class="section-title">资料信息</div>
  {% for r in rows %}<div class="row"><div class="k">{{ r.k }}</div><div class="v">{{ r.v }}</div></div>{% endfor %}
</div>

<div class="card">
  <div class="section-title">虚拟形象（Avatar）</div>
  <div class="avatar-block">
    {% if avatar_image %}<img class="avatar-sm" src="{{ avatar_image }}" />{% endif %}
    <div class="avatar-info">
      <div class="item-title">{{ avatar_name }}</div>
      <div class="item-sub">{{ avatar_caption }}</div>
      {% if avatar_id %}<div class="idtag">{{ avatar_id }}</div>{% endif %}
    </div>
  </div>
  <div class="note">资料头像（Profile Picture）是个人主页展示图（右下角圆形图），虚拟形象（Avatar）是玩家在世界中使用的 3D 角色/模型，两者是不同的概念。</div>
</div>
"""

WORLD_TMPL = """
<div class="card">
  <div class="hero">
    {% if thumbnail %}<img class="world-thumb" src="{{ thumbnail }}" />{% endif %}
    <div class="hero-main">
      <div class="title">{{ name }}</div>
      <div class="subtitle">{{ author_line }}</div>
      {% if chips %}<div class="badge-row">{% for c in chips %}<span class="chip {{ c.tone }}">{{ c.text }}</span>{% endfor %}</div>{% endif %}
      <div class="stats">
        {% for s in stats %}<div class="stat"><div class="num">{{ s.num }}</div><div class="lab">{{ s.lab }}</div></div>{% endfor %}
      </div>
    </div>
  </div>
</div>
<div class="card">
  <div class="section-title">世界简介</div>
  <div class="desc">{{ description }}</div>
  {% if tags %}<div class="badge-row">{% for t in tags %}<span class="chip soft">{{ t }}</span>{% endfor %}</div>{% endif %}
  <div class="section-title">详细信息</div>
  {% for r in rows %}<div class="row"><div class="k">{{ r.k }}</div><div class="v">{{ r.v }}</div></div>{% endfor %}
</div>
"""

GROUP_TMPL = """
<div class="card">
  {% if banner %}<img class="banner" src="{{ banner }}" />{% endif %}
  <div class="hero">
    {% if icon %}<img class="avatar-sm" src="{{ icon }}" />{% endif %}
    <div class="hero-main">
      <div class="title">{{ name }}</div>
      <div class="subtitle">{{ subtitle }}</div>
      {% if chips %}<div class="badge-row">{% for c in chips %}<span class="chip {{ c.tone }}">{{ c.text }}</span>{% endfor %}</div>{% endif %}
    </div>
  </div>
  {% if description %}<div class="desc">{{ description }}</div>{% endif %}
  <div class="stats">
    {% for s in stats %}<div class="stat"><div class="num">{{ s.num }}</div><div class="lab">{{ s.lab }}</div></div>{% endfor %}
  </div>
  {% for r in rows %}<div class="row"><div class="k">{{ r.k }}</div><div class="v">{{ r.v }}</div></div>{% endfor %}
</div>
<div class="card">
  <div class="section-title">群组规则</div>
  <div class="desc">{{ rules }}</div>
  {% if tags %}<div class="badge-row">{% for t in tags %}<span class="chip soft">{{ t }}</span>{% endfor %}</div>{% endif %}
</div>
"""

LIST_TMPL = """
<div class="card">
  <div class="section-title">{{ title }}</div>
  {% if subtitle %}<div class="subtitle">{{ subtitle }}</div>{% endif %}
  {% if items %}
    {% for it in items %}
    <div class="list-item">
      {% if it.thumb %}<img class="thumb" src="{{ it.thumb }}" />{% else %}<div class="thumb placeholder">VRC</div>{% endif %}
      <div class="item-main">
        <div class="item-title">{{ it.title }}</div>
        {% if it.sub %}<div class="item-sub">{{ it.sub }}</div>{% endif %}
        {% if it.chips %}<div class="badge-row">{% for c in it.chips %}<span class="chip {{ c.tone }}">{{ c.text }}</span>{% endfor %}</div>{% endif %}
        {% if it.id_text %}<div class="idtag">{{ it.id_text }}</div>{% endif %}
        {% if it.hint %}<div class="item-hint">{{ it.hint }}</div>{% endif %}
      </div>
    </div>
    {% endfor %}
  {% else %}
    <div class="empty">{{ empty }}</div>
  {% endif %}
  {% if hint %}<div class="note">{{ hint }}</div>{% endif %}
</div>
"""

RESULT_TMPL = """
<div class="card">
  <div class="result-head">
    <div class="result-icon {{ tone }}">{{ icon }}</div>
    <div>
      <div class="title">{{ title }}</div>
      {% if subtitle %}<div class="subtitle">{{ subtitle }}</div>{% endif %}
    </div>
  </div>
  {% if lines %}<div class="result-body">
    {% for line in lines %}<div class="row"><div class="v">{{ line }}</div></div>{% endfor %}
  </div>{% endif %}
  {% if hint %}<div class="note">{{ hint }}</div>{% endif %}
</div>
"""

HELP_TMPL = """
{% for s in sections %}
<div class="card">
  <div class="section-title">{{ s.title }}</div>
  {% for c in s.rows %}<div class="cmd-row"><div class="cmd">{{ c.cmd }}</div><div class="cmd-desc">{{ c.desc }}</div></div>{% endfor %}
</div>
{% endfor %}
<div class="card">
  <div class="note">参数可用引号包裹以包含空格，例如 /vrc 用户搜索 "vr chat"；ID 必须带对应前缀（usr_/wrld_/grp_/avtr_/fvrt_/frq_）。绑定与解绑请在私聊中进行以保护账号安全。</div>
</div>
"""


# ----------------------------------------------------------------------
# 视图构建
# ----------------------------------------------------------------------


class CardRenderer:
    """把 VRChat 返回的原始数据构建成渲染卡片。"""

    # ---------------- 通用 ----------------

    def notice(
        self,
        title: str,
        lines: list[str] | None = None,
        *,
        ok: bool = True,
        subtitle: str = "",
        hint: str = "",
    ) -> Card:
        data = {
            "title": esc(title),
            "subtitle": esc(subtitle),
            "lines": [esc(line) for line in (lines or [])],
            "hint": esc(hint),
            "tone": "ok" if ok else "err",
            "icon": "✓" if ok else "!",
        }
        fallback = "\n".join([title] + list(lines or []) + ([hint] if hint else []))
        return Card(_wrap(RESULT_TMPL), data, fallback, needs_image=False)

    def help_card(self) -> Card:
        sections = [
            {
                "title": "账号",
                "rows": [
                    {"cmd": "/vrc 绑定", "desc": "绑定 VRChat 账号（建议私聊使用）"},
                    {"cmd": "/vrc 解绑", "desc": "解绑并登出，清除本地加密凭据"},
                ],
            },
            {
                "title": "我与用户",
                "rows": [
                    {"cmd": "/vrc 关于我", "desc": "查看自己的 VRChat 资料"},
                    {"cmd": '/vrc 用户 "usr_"', "desc": "查看指定用户资料"},
                    {
                        "cmd": '/vrc 用户搜索 "关键词"',
                        "desc": "按昵称搜索用户（支持空格）",
                    },
                ],
            },
            {
                "title": "世界",
                "rows": [
                    {"cmd": "/vrc 世界", "desc": "浏览热门世界"},
                    {"cmd": '/vrc 世界 "wrld_"', "desc": "查看世界详情"},
                    {"cmd": '/vrc 世界搜索 "关键词"', "desc": "搜索世界（支持空格）"},
                ],
            },
            {
                "title": "好友",
                "rows": [
                    {"cmd": "/vrc 好友", "desc": "好友列表（在线优先）"},
                    {"cmd": "/vrc 好友 全部", "desc": "全部好友（A-Z 字母排序）"},
                    {"cmd": '/vrc 好友 状态 "usr_"', "desc": "查询好友关系状态"},
                    {"cmd": '/vrc 好友 添加好友 "usr_"', "desc": "发送好友请求"},
                    {
                        "cmd": '/vrc 好友 取消请求 "usr_"',
                        "desc": "取消已发送的好友请求",
                    },
                    {"cmd": '/vrc 好友 删除好友 "usr_"', "desc": "删除好友"},
                    {
                        "cmd": '/vrc 好友 接受请求 "frq_"',
                        "desc": "按通知 ID 接受好友请求",
                    },
                ],
            },
            {
                "title": "通知",
                "rows": [{"cmd": "/vrc 通知", "desc": "查看通知（含 frq_ 请求 ID）"}],
            },
            {
                "title": "群组",
                "rows": [
                    {"cmd": '/vrc 群组 "grp_"', "desc": "查看群组详情"},
                    {"cmd": '/vrc 群组 成员 "grp_"', "desc": "查看群组成员"},
                ],
            },
            {
                "title": "收藏",
                "rows": [
                    {"cmd": "/vrc 收藏", "desc": "我的收藏（默认看世界）"},
                    {"cmd": "/vrc 收藏 角色", "desc": "我的收藏（虚拟形象/Avatar）"},
                    {
                        "cmd": '/vrc 收藏 添加世界 "wrld_"',
                        "desc": "收藏世界（可跟收藏组名）",
                    },
                    {"cmd": '/vrc 收藏 移除 "fvrt_"', "desc": "按收藏 ID 移除收藏"},
                ],
            },
            {
                "title": "订阅与推送",
                "rows": [
                    {
                        "cmd": '/vrc 订阅 上线 "名字或 usr_"',
                        "desc": "好友上线提醒（支持名字模糊匹配）",
                    },
                    {
                        "cmd": '/vrc 取消订阅 上线 "名字或 usr_"',
                        "desc": "取消某个好友的上线提醒",
                    },
                    {"cmd": "/vrc 订阅 通知", "desc": "开/关全局通知推送"},
                    {"cmd": "/vrc 订阅 列表", "desc": "查看当前订阅状态"},
                ],
            },
            {
                "title": "虚拟形象（Avatar）",
                "rows": [
                    {"cmd": "/vrc 角色", "desc": "我的虚拟形象列表"},
                    {"cmd": '/vrc 角色 搜索 "关键词"', "desc": "搜索公开虚拟形象"},
                    {"cmd": '/vrc 角色 选择 "avtr_"', "desc": "换上指定虚拟形象"},
                ],
            },
        ]
        data = {"sections": sections}
        return Card(_wrap(HELP_TMPL), data, "请发送 /vrc 帮助 查看完整指令。")

    # ---------------- 用户 ----------------

    def profile_card(
        self,
        user: dict[str, Any],
        profile: dict[str, Any] | None = None,
        *,
        is_self: bool = False,
        friend_status: dict[str, Any] | None = None,
        avatar_name: str = "",
    ) -> Card:
        profile = profile or {}
        merged: dict[str, Any] = {**profile, **user}

        display_name = str(
            merged.get("displayName") or merged.get("displayNameKana") or "未知用户"
        )
        user_id = str(merged.get("id") or "")
        username = str(merged.get("username") or "")
        status, tone = status_meta(merged.get("status"))
        status_text = f"{status}" + (
            f" · {merged.get('statusDescription')}"
            if merged.get("statusDescription")
            else ""
        )
        tags = merged.get("tags")
        badges = [trust_badge(tags), *badges_from_tags(tags)]
        bio = short(_first(profile.get("bio"), merged.get("bio"), ""), 500)

        # 资料头像（Profile Picture）：个人主页右下角的圆形图
        profile_pic = _first(
            merged.get("profilePicImageUrl"),
            profile.get("iconUrl"),
            merged.get("iconUrl"),
            profile.get("userIcon"),
        )
        # 虚拟形象（Avatar）图：缩略图优先（几乎必有），最后用资料图兜底，
        # 避免 currentAvatarImageUrl 为空时出现「黑块」占位。
        avatar_image = _first(
            merged.get("currentAvatarThumbnailImageUrl"),
            merged.get("currentAvatarImageUrl"),
            profile.get("currentAvatarThumbnailImageUrl"),
            profile.get("currentAvatarImageUrl"),
            merged.get("iconUrl"),
        )
        avatar_id = str(merged.get("currentAvatar") or "")

        # 身份行只拼接一次 user_id，避免出现「用户 · usr_x · usr_x」
        identity = f"@{username}" if username else ("自己" if is_self else "用户")
        handle_line = esc(f"{identity} · {user_id}" if user_id else identity)

        rows: list[dict[str, str]] = []
        if username:
            rows.append({"k": "用户名（Username）", "v": esc(username)})
        rows.append({"k": "用户 ID", "v": esc(user_id)})
        pronouns = merged.get("pronouns")
        if pronouns:
            rows.append({"k": "代词", "v": esc(pronouns)})
        rows.append({"k": "状态", "v": esc(status)})
        if merged.get("statusDescription"):
            rows.append({"k": "个性签名", "v": esc(merged.get("statusDescription"))})
        if friend_status and not is_self:
            relation = (
                "已是好友"
                if friend_status.get("isFriend")
                else "已发送好友请求"
                if friend_status.get("outgoingRequest")
                else "对方请求加你为好友"
                if friend_status.get("incomingRequest")
                else "尚无好友关系"
            )
            rows.append({"k": "好友关系", "v": esc(relation)})
        if merged.get("date_joined"):
            rows.append(
                {"k": "加入时间", "v": esc(fmt_time(merged.get("date_joined")))}
            )
        if merged.get("last_login"):
            rows.append({"k": "最近登录", "v": esc(fmt_time(merged.get("last_login")))})
        if merged.get("last_platform"):
            rows.append({"k": "最近平台", "v": esc(merged.get("last_platform"))})
        if "allowAvatarCopying" in merged:
            rows.append(
                {
                    "k": "允许复制形象",
                    "v": "允许" if merged.get("allowAvatarCopying") else "不允许",
                }
            )
        if is_self and "twoFactorAuthEnabled" in merged:
            rows.append(
                {
                    "k": "两步验证（2FA）",
                    "v": "已开启" if merged.get("twoFactorAuthEnabled") else "未开启",
                }
            )

        stats: list[dict[str, str]] = []
        if is_self:
            friends = merged.get("friends") or []
            active = merged.get("activeFriends") or merged.get("onlineFriends") or []
            stats.append({"num": fmt_num(len(friends)), "lab": "好友"})
            stats.append({"num": fmt_num(len(active)), "lab": "在线好友"})
        stats.append({"num": fmt_num(len(badges)), "lab": "徽章"})

        data = {
            "display_name": esc(display_name),
            "handle_line": handle_line,
            "status_text": esc(status_text),
            "status_tone": tone,
            "badges": [{"text": esc(b["text"]), "tone": b["tone"]} for b in badges],
            "bio": esc(bio),
            "bio_links": [esc(link) for link in (profile.get("bioLinks") or [])[:6]],
            "profile_pic": esc(profile_pic or ""),
            "avatar_image": esc(avatar_image or ""),
            "avatar_id": esc(avatar_id),
            "avatar_name": esc(avatar_name or "（未获取到形象名称）"),
            "avatar_caption": esc(
                f"当前佩戴的虚拟形象（Avatar）{'：' + avatar_name if avatar_name else ''}"
            ),
            "stats": stats,
            "rows": rows,
        }
        fallback = f"{display_name}（{user_id}）\n状态：{status_text}"
        return Card(_wrap(PROFILE_TMPL), data, fallback)

    # ---------------- 世界 ----------------

    def world_card(self, world: dict[str, Any]) -> Card:
        name = str(world.get("name") or "未命名世界")
        world_id = str(world.get("id") or "")
        author = str(world.get("authorName") or "未知作者")
        release = str(world.get("releaseStatus") or "")
        chips = []
        if release:
            chips.append(
                {
                    "text": {
                        "public": "公开发布",
                        "private": "私有",
                        "hidden": "隐藏",
                    }.get(release, release),
                    "tone": "green" if release == "public" else "grey",
                }
            )
        if world.get("featured"):
            chips.append({"text": "官方推荐", "tone": "purple"})
        if world.get("labsPublicationDate"):
            chips.append({"text": "实验性发布", "tone": "orange"})

        stats = [
            {"num": fmt_num(world.get("capacity")), "lab": "容量"},
            {"num": fmt_num(world.get("recommendedCapacity")), "lab": "推荐人数"},
            {"num": fmt_num(world.get("visits")), "lab": "访问量"},
            {"num": fmt_num(world.get("favorites")), "lab": "收藏数"},
        ]
        rows = [
            {"k": "作者", "v": esc(author)},
            {"k": "世界 ID", "v": esc(world_id)},
            {"k": "当前在线", "v": fmt_num(world.get("occupants"))},
            {
                "k": "热度 / 人气",
                "v": f"{fmt_num(world.get('heat'))} / {fmt_num(world.get('popularity'))}",
            },
            {"k": "创建时间", "v": esc(fmt_time(world.get("created_at")))},
            {"k": "更新时间", "v": esc(fmt_time(world.get("updated_at")))},
            {"k": "发布时间", "v": esc(fmt_time(world.get("publicationDate")))},
            {"k": "版本", "v": esc(world.get("version") or "—")},
        ]
        data = {
            "name": esc(name),
            "author_line": esc(f"作者：{author} · {world_id}"),
            "thumbnail": esc(
                world.get("thumbnailImageUrl") or world.get("imageUrl") or ""
            ),
            "description": esc(
                short(world.get("description"), 700) or "（作者未填写简介）"
            ),
            "chips": chips,
            "stats": stats,
            "tags": [esc(t) for t in (world.get("tags") or [])[:10]],
            "rows": rows,
        }
        fallback = f"{name} - 作者 {author}（{world_id}）"
        return Card(_wrap(WORLD_TMPL), data, fallback)

    # ---------------- 群组 ----------------

    def group_card(self, group: dict[str, Any]) -> Card:
        name = str(group.get("name") or "未命名群组")
        group_id = str(group.get("groupId") or group.get("id") or "")
        short_code = str(group.get("shortCode") or "")
        discriminator = str(group.get("discriminator") or "")
        join_state = {
            "open": "开放加入",
            "request": "需申请",
            "invite": "仅限邀请",
            "closed": "已关闭",
        }.get(str(group.get("joinState") or ""), str(group.get("joinState") or "—"))

        chips = []
        if group.get("isVerified"):
            chips.append({"text": "已验证群组", "tone": "blue"})
        membership = group.get("membershipStatus")
        if membership:
            chips.append({"text": f"我的状态：{membership}", "tone": "green"})

        stats = [
            {"num": fmt_num(group.get("memberCount")), "lab": "成员数"},
            {"num": fmt_num(group.get("onlineMemberCount")), "lab": "在线成员"},
        ]
        rows = [
            {"k": "群组 ID", "v": esc(group_id)},
            {
                "k": "短代码",
                "v": esc(f"{short_code}#{discriminator}" if short_code else "—"),
            },
            {"k": "加入状态", "v": esc(join_state)},
            {"k": "所有者 ID", "v": esc(group.get("ownerId") or "—")},
            {"k": "创建时间", "v": esc(fmt_time(group.get("createdAt")))},
        ]
        data = {
            "name": esc(name),
            "subtitle": esc(
                f"{short_code}#{discriminator} · {group_id}" if short_code else group_id
            ),
            "banner": esc(group.get("bannerUrl") or ""),
            "icon": esc(group.get("iconUrl") or ""),
            "description": esc(
                short(group.get("description"), 500) or "（未填写简介）"
            ),
            "rules": esc(short(group.get("rules"), 700) or "（未填写规则）"),
            "chips": chips,
            "stats": stats,
            "tags": [esc(t) for t in (group.get("tags") or [])[:10]],
            "rows": rows,
        }
        return Card(_wrap(GROUP_TMPL), data, f"{name}（{group_id}）")

    # ---------------- 列表 ----------------

    def list_card(
        self,
        title: str,
        items: list[dict[str, Any]],
        *,
        subtitle: str = "",
        empty: str = "没有找到结果。",
        hint: str = "",
        needs_image: bool = True,
    ) -> Card:
        prepared: list[dict[str, Any]] = []
        for item in items:
            prepared.append(
                {
                    "thumb": esc(item.get("thumb") or ""),
                    "title": esc(item.get("title") or "（无标题）"),
                    "sub": esc(item.get("sub") or ""),
                    "chips": [
                        {"text": esc(c.get("text", "")), "tone": c.get("tone", "soft")}
                        for c in (item.get("chips") or [])
                    ],
                    "id_text": esc(item.get("id_text") or ""),
                    "hint": esc(item.get("hint") or ""),
                }
            )
        data = {
            "title": esc(title),
            "subtitle": esc(subtitle),
            "items": prepared,
            "empty": esc(empty),
            "hint": esc(hint),
        }
        fallback = f"{title}\n" + "\n".join(
            f"- {item.get('title', '')} {item.get('id_text', '')}" for item in items
        )
        return Card(_wrap(LIST_TMPL), data, fallback, needs_image=needs_image)
