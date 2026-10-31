"""指令参数解析：引号剥离、ID 前缀校验、搜索关键词拼接。

用户在 QQ 中的输入往往带有中文引号（“”/「」）或英文引号，本模块统一
处理，保证「所有参数剥离引号」，并支持「搜索关键词包含空格」。
"""

from __future__ import annotations

import re

from .errors import CommandUsageError

# 成对引号：左引号 -> 右引号
QUOTE_PAIRS: dict[str, str] = {
    '"': '"',
    "'": "'",
    "\u201c": "\u201d",  # “ ”
    "\u2018": "\u2019",  # ‘ ’
    "\u300c": "\u300d",  # 「 」
    "\u300e": "\u300f",  # 『 』
    "\uff08": "\uff09",  # （ ）
}
_CLOSING_QUOTES = set(QUOTE_PAIRS.values())

# 支持的 ID 前缀 -> (中文名称, 示例)
ID_SPECS: dict[str, tuple[str, str]] = {
    "usr_": ("用户", "usr_01234567-89ab-cdef-0123-456789abcdef"),
    "wrld_": ("世界", "wrld_01234567-89ab-cdef-0123-456789abcdef"),
    "grp_": ("群组", "grp_01234567-89ab-cdef-0123-456789abcdef"),
    "avtr_": ("角色", "avtr_01234567-89ab-cdef-0123-456789abcdef"),
    "fvrt_": ("收藏", "fvrt_01234567-89ab-cdef-0123-456789abcdef"),
    "frq_": ("好友请求通知", "frq_01234567-89ab-cdef-0123-456789abcdef"),
}

# ID 合法字符（VRChat 的 ID 为 前缀 + UUID 形态；此处仅排除空白等垃圾字符，
# 严格的前缀校验在 check_id 中完成）
_ID_BODY_RE = re.compile(r"^[0-9A-Za-z_\-]{1,80}$")


def tokenize(text: str) -> list[str]:
    """把指令文本切成参数列表，引号内的空格会被保留，引号本身被剥离。

    支持英文引号 ``"..."`` / ``'...'``、中文弯引号 “…”、直角引号 「…」『…』
    以及全角圆括号（…）。未闭合的引号会把剩余内容整体当作一个参数。
    """
    tokens: list[str] = []
    buf: list[str] = []
    open_quote: str | None = None

    for ch in text.strip():
        if open_quote is not None:
            if ch == QUOTE_PAIRS[open_quote]:
                open_quote = None
            else:
                buf.append(ch)
            continue
        if ch in QUOTE_PAIRS:
            open_quote = ch
            continue
        if ch in _CLOSING_QUOTES:  # 孤立的右引号，直接忽略
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(ch)

    if buf:
        tokens.append("".join(buf))
    return tokens


def strip_quotes(value: str) -> str:
    """剥离字符串首尾成对的引号（兼容全角/半角）。"""
    value = value.strip()
    if len(value) >= 2:
        opener = value[0]
        if opener in QUOTE_PAIRS and value[-1] == QUOTE_PAIRS[opener]:
            return value[1:-1]
    return value


def check_id(value: str | None, prefix: str) -> str:
    """校验一个 ID 参数并返回规范化后的值。

    :param value: 用户传入的参数（可带引号，会被剥离）。
    :param prefix: 期望的前缀，如 ``usr_``。
    :raises CommandUsageError: 前缀或格式不合法时抛出，错误文案自带示例。
    """
    label, example = ID_SPECS[prefix]
    raw = strip_quotes(value or "")
    if not raw:
        raise CommandUsageError(
            f"缺少{label} ID 参数，需要以「{prefix}」开头的 ID。",
            usage=f"例如：{prefix}{example[len(prefix) :]}",
        )
    if not raw.startswith(prefix):
        raise CommandUsageError(
            f"ID 格式不正确：这是{label} ID，必须以「{prefix}」开头（你输入的是「{raw}」）。",
            usage=f'正确示例："{example}"',
        )
    body = raw[len(prefix) :]
    if not _ID_BODY_RE.match(body):
        raise CommandUsageError(
            f"ID 格式不正确：「{raw}」不是合法的{label} ID。",
            usage=f'正确示例："{example}"',
        )
    return raw


def keyword(args: list[str], usage: str) -> str:
    """把剩余参数拼成搜索关键词（支持空格），并去除首尾引号残留。"""
    words = [strip_quotes(a) for a in args if strip_quotes(a)]
    text = " ".join(words).strip()
    if not text:
        raise CommandUsageError("缺少搜索关键词。", usage=usage)
    return text


# ----------------------------------------------------------------------
# 不可见字符清洗
# ----------------------------------------------------------------------

# 零宽空格 / 方向标记 / 行段分隔符 / 窄不换行空格等复制粘贴常见残留
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2028-\u202f\u205f-\u206f]")


def clean_credential(value: str) -> str:
    """剔除零宽空格等不可见字符，并去掉首尾空白。

    QQ / 微信复制账号密码时经常夹带 U+200B 等不可见字符，直接在 VRChat
    登录接口里使用会表现为「密码明明正确却 401」，因此统一在入口清洗。
    """
    if not value:
        return ""
    return _INVISIBLE_RE.sub("", str(value)).strip()


# ----------------------------------------------------------------------
# 绑定凭据解析
# ----------------------------------------------------------------------

_CRED_KEYS = {
    "账号": "username",
    "用户名": "username",
    "account": "username",
    "user": "username",
    "email": "username",
    "密码": "password",
    "pass": "password",
    "password": "password",
    "2fa密钥": "totp",
    "密钥": "totp",
    "totp": "totp",
    "secret": "totp",
    "2fa": "totp",
}


def parse_credentials(tokens: list[str]) -> tuple[str, str, str]:
    """解析绑定凭据：支持「账号 x 密码 y 2FA密钥 z」与位置参数两种写法。

    解析前后都会经过 :func:`clean_credential`，剔除复制粘贴带入的零宽字符。

    返回 ``(username, password, totp_secret)``。
    """
    named: dict[str, str] = {}
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = clean_credential(tokens[index])
        key = _CRED_KEYS.get(token.lower()) or _CRED_KEYS.get(token)
        if key and index + 1 < len(tokens):
            named[key] = clean_credential(tokens[index + 1])
            index += 2
            continue
        if token:
            positional.append(token)
        index += 1

    for field in ("username", "password", "totp"):
        if field not in named and positional:
            named[field] = positional.pop(0)

    username = clean_credential(named.get("username", ""))
    password = clean_credential(named.get("password", ""))
    totp_secret = clean_credential(named.get("totp", ""))
    return username, password, totp_secret
