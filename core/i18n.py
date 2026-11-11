# -*- coding: utf-8 -*-
"""显示语言（v1.3.1 国际服英文）。

- 语言只有两种：``zh``（默认，即 v1.3.0 以前的全部行为）与 ``en``。
- 区服 ``region`` 目前固定为 ``intl``，只为以后扩展留位置，不对外提供设置。
- 当前语言放在 contextvar 里：一条消息从分发到出图都在同一个任务上下文中，
  ``asyncio.to_thread`` 会复制上下文，所以解析线程里也能读到正确的语言。
- 文案就地双语：``L("突击", "Sortie")``。英文缺省时回落中文，保证不会出现空文案。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

LANGS = ("zh", "en")
DEFAULT_LANG = "zh"
REGION = "intl"

_LANG: ContextVar[str] = ContextVar("sdjk_lang", default=DEFAULT_LANG)

# 用户可见的语言写法 -> 规范码。只收明确的写法，避免把普通词误判成语言。
_LANG_ALIASES = {
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "cn": "zh",
    "chinese": "zh",
    "中文": "zh",
    "简中": "zh",
    "简体中文": "zh",
    "en": "en",
    "eng": "en",
    "english": "en",
    "英文": "en",
    "英语": "en",
}


def normalize_lang(word: object) -> Optional[str]:
    """把用户输入的语言写法归一成 ``zh`` / ``en``；认不出返回 None。"""
    if not isinstance(word, str):
        return None
    return _LANG_ALIASES.get(word.strip().lower())


def coerce_lang(value: object) -> str:
    """存档、配置里读出的值：合法就用（配置写 cn / en，也接受 zh），否则回落默认中文。"""
    return normalize_lang(value) or DEFAULT_LANG


def current_lang() -> str:
    return _LANG.get()


def is_en() -> bool:
    return _LANG.get() == "en"


@contextmanager
def use_lang(lang: object) -> Iterator[str]:
    """在 with 块内切换当前语言，退出时恢复。"""
    token = _LANG.set(coerce_lang(lang))
    try:
        yield _LANG.get()
    finally:
        _LANG.reset(token)


def set_lang(lang: object):
    """不能用 with 的地方（异步生成器跨 yield）用它，配合 ``reset_lang`` 成对使用。"""
    return _LANG.set(coerce_lang(lang))


def reset_lang(token) -> None:
    _LANG.reset(token)


def L(zh: str, en: Optional[str] = None) -> str:  # noqa: N802 - 短名便于就地双语
    """就地双语文案：英文模式且给了英文时返回英文，否则返回中文。"""
    if en is not None and _LANG.get() == "en":
        return en
    return zh


def lang_suffix(lang: object) -> str:
    """面向用户的语言后缀写法：中文写 cn，英文写 en。"""
    return "en" if coerce_lang(lang) == "en" else "cn"


def lang_label(lang: object) -> str:
    """语言的展示名（用当前语言写）。"""
    lang = coerce_lang(lang)
    if _LANG.get() == "en":
        return "English" if lang == "en" else "Chinese"
    return "英文" if lang == "en" else "中文"
