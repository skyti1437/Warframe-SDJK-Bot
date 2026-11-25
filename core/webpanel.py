# -*- coding: utf-8 -*-
"""插件页面「SDJKBOT 运维面板」的后端（AstrBot 插件 Pages：``pages/sdjk/index.html``）。

四块功能（2026-10-16 维护者选定）：
  · 数据新鲜度   —— 各快照的时间戳与是否过期（只读）
  · 蹲订阅 / 推送 —— 订阅总览与推送守护状态（只读）
  · 手动刷新快照 —— 从公开仓 bot-data 拉社区快照（效价 / 言录使货单），**不走 FlareSolverr**
  · 连通性测试   —— FlareSolverr 探活（sessions.list，不做真实求解）/ 兼容主渠道（vision_provider_id）解析与真调；
                    「真调一次模型」单列为 vision_live（会花 token，只在点按钮时跑）

口径（AstrBot 4.28.1 源码核对，2026-10-16）：
  · 页面目录 ``pages/<名>/index.html`` 自动发现，metadata.yaml **不用**声明 pages；
  · 路由必须以插件名开头：``/<PLUGIN_NAME>/<端点>``，前端 ``bridge.apiGet("<端点>")``；
  · 鉴权走 Dashboard 登录（不带 token 一律 401，ZCode 线上实测）；
  · ★ AstrBot **没有注销路由的 API**：插件停用后旧路由仍会应答（直到重启 AstrBot）
    ⇒ ``close()`` 打标记，之后写操作 / 外呼一律拒绝；重载时同路由 + 同 methods 会被新实例替换。
  · ★ 返回值顶层**别用 ``data`` 键**：bridge 取 ``response.data?.data ?? response.data``，
    顶层有 data 会被当成信封剥掉。

隐私：订阅里的 umo / created_by 是聊天平台给的会话 / 用户标识（OneBot 下是群号、QQ 号，
QQ 官方机器人下是群 / 成员 OpenID）—— 默认打码，``reveal=1`` 才给原值
（只有登录后台的管理员看得到这个页面）。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import nullcontext
from functools import partial
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional
from urllib.parse import urlsplit

from . import baro, damage_engine, data_registry, display_time, i18n, paths, riven_sale
from .api_client import (
    ACRITHIS_WEEK_NAME,
    RANKS_NAME,
    RIVEN_RT_NAME,
    RIVEN_WEEKLY_NAME,
    WarframeAPIError,
    WarframeClient,
    font_url_transport,
    parse_de_worldstate_urls,
    private_url_reason,
    public_error_text,
    redact_error_text,
)
from .commands.base import validate_numeric_setting
from .commands.dun import _EV_EN
from .commands.riven import riven_read_selection
from .commands.vision import _prov_id, original_vision_prompt
from .logging_compat import logger, mask_id, mask_umo  # noqa: F401 - 面板与测试沿用 W.mask_*
from .parser import COMMAND_ALIASES, COMMAND_ALIASES_EN
from .vision_compare import prepare_image, run_comparison
from .vision_prompt_presets import preset_catalog, preset_detail
from .vision_prompts import MAX_PROMPT_LENGTH, TASKS, PromptConflict

# 状态四档：ok 新鲜 / warn 该刷了但不影响功能 / stale 已过期（卡面会受影响）/ info 静态快照仅供参考
OK, WARN, STALE, INFO = "ok", "warn", "stale", "info"


def _png_data_url(size: int = 32) -> str:
    """纯色方图 PNG（data URL），不含任何用户数据。

    ★ 2026-10-16 线上：1×1 图被 qwen-vl-ocr 以「宽高须 >10」400 拒绝 ⇒ 用 32×32。
    """
    import base64
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = b"".join(b"\x00" + b"\xff\xff\xff" * size for _ in range(size))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode()


_PING_PNG = _png_data_url(32)
_LIVE_MAX_TRIES = 3  # 面板最多调用渠道 text_chat 的次数；不统计 SDK 内部 HTTP 重试
_VISION_ROUTES = {
    "compat": ("vision_provider_id", "_vision_providers"),
    "riven": ("vision_provider_id_riven", "_riven_vision_providers"),
    "line": ("vision_provider_id_line", "_line_vision_providers"),
    "scan": ("vision_provider_id_scan", "_scan_vision_providers"),
    "scan_equipment": ("vision_provider_id_scan_equipment", "_scan_equipment_providers"),
    "line_recheck": ("vision_provider_id_line_recheck", "_line_recheck_vision_providers"),
}

_VISION_CALL_AUDIT = {
    "attempted_unit": "provider_text_chat_call",
    "sent_semantics": "provider_method_invoked",
    "actual_http_requests": 0,
    "notice": "attempted 仅统计渠道 text_chat 方法调用，sent 仅表示已尝试调用该方法；"
    "不统计 SDK 内部重试，不代表实际 HTTP 请求次数或费用硬上限。调用后实际 HTTP 次数未知。",
}


_RIVEN_SWITCH_ROUTES = (
    "vision_provider_id_riven",
    "vision_provider_id_line",
    "vision_provider_id_line_recheck",
    "vision_provider_id",
)

# Operations panel labels for the built-in output contracts. Prompt text stays as authored.
_VISION_CONTRACTS_EN = {
    "riven_json": "Return one JSON object with the card’s weapon name in weapon and the complete stat lines in stat_lines. Preserve signs, decimals, units, multipliers, and unknown slots.",
    "riven_lines": "Transcribe each stat line exactly, preserving signs, values, and units. Omit numbering and explanations.",
    "scan_main": "Return one JSON object with weapon, capacity, mods, panel, and arcanes. Each mod includes name, drain, color, and polarity. Keep the panel units, thousands separators, and both values around arrows. List damage by row in damage_rows.",
    "scan_title": "Return only the text after the slash in the top-left title bar. Use one nonempty line of up to 40 characters.",
    "scan_panel": "Return panel labels and values line by line, or JSON with panel or rows. Preserve all values, units, and both sides of arrows on the same line.",
    "scan_mods": "Return one line per equipped mod as name | drain. Include the stance and omit empty slots and inventory mods. List equipped Arcanes on separate lines if needed.",
    "scan_tiles": "Return JSON with slots. Each entry includes tile, state, name, drain, and mark. Transcribe each slot’s card and drain and use visible markers to confirm empty or locked slots.",
    "scan_arcanes": "Transcribe each equipped Arcane name on its own line. Preserve Primary, Secondary, and Melee prefixes and separators.",
}


class _VisionRouteProbe:
    """在只读代理上执行真实 selector，并记录实际查找分支，不改宿主方法。

    嵌套 selector 仍绑定此代理；配置与渠道访问委托宿主。因此窄读是否回落取决于
    本次查找结果，而不是比较渠道 ID 或对象身份（同 ID 可对应不同对象）。
    """

    def __init__(self, host, cfg=None):
        self.host = host
        self.lookups = []
        if cfg is not None:
            self.cfg = cfg

    def __getattr__(self, name):
        value = getattr(self.host, name)
        if name in {entry[1] for entry in _VISION_ROUTES.values()} | {
            "_cfg_str",
            "_vision_routes_isolated",
            "_vision_route_value",
        }:
            func = getattr(value, "__func__", None)
            if func is not None:
                return func.__get__(self)
        return value

    def _try_get_provider(self, pid):
        provider = self.host._try_get_provider(pid)
        self.lookups.append((pid, bool(provider)))
        return provider


def _parse_iso(s: Any) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _age_text(d: Optional[datetime], now: datetime) -> str:
    if d is None:
        return "未知"
    sec = (now - d).total_seconds()
    if sec < 0:
        return "刚刚"
    if sec < 3600:
        return f"{int(sec // 60)} 分钟前"
    if sec < 86400 * 2:
        return f"{sec / 3600:.0f} 小时前"
    return f"{sec / 86400:.0f} 天前"


def _load_snapshot(name: str, run_dir: Path | None = None) -> tuple[Any, str]:
    """来源标签与数据共用一次路径选择，含坏 JSON 回退到种子的情况。"""
    try:
        path = paths.read_path(name, root=run_dir)
        origin = "用的是随包种子" if path == paths.PKG_DATA / name else "用的是更新过的快照"
        return paths.load_json(path), origin
    except Exception:  # noqa: BLE001
        return {}, "来源无法判定"


def _load(name: str, run_dir: Path | None = None) -> Any:
    return _load_snapshot(name, run_dir)[0]


# 「数据新鲜度」每行的用途：哪些指令 / 推送读这份数据、过期时卡面怎样（按各指令实际读取处整理）
FRESH_USED = {
    "信条元素加成": "「信条」卡的元素与加成数值；过期时卡面标「上一轮快照，数值可能已变」（换批时间按固定周期推算，不受影响）",
    "终幕元素加成": "「终幕」卡的本批元素与加成数值；过期时卡面标「上一轮快照，数值可能已变」（换批时间按固定周期推算，不受影响）",
    "言录使本周货单": "「言录使」卡的本周货单；过期后不再展示本周货单",
    "紫卡周报（DE 官方）": "「紫卡排行」周报卡的成交排行",
    "紫卡 WM 现价（手动拉取）": "「紫卡排行」卡末尾的「WM挂单 / 变化」两列（仅 PC）",
    "WM 价格排行": "「排行」及「甲 / 卡 / 部件 / 赋能 / 主武 / 副武 / 近战 / 遗物排行」",
    "奸商到访历史": "「奸商」卡的历史到访与「奸商 预测」",
    "仲裁评级（社区实测）": "「仲裁」「仲裁表」的评级列，「时效」里的仲裁，「蹲 仲裁」按评级筛选",
    "掉落表": "「遗物」「部件」的掉落与出库判断，wiki 简介卡的获取途径",
}


def _row(item: str, status: str, snapshot: str, detail: str) -> dict:
    return {
        "item": item,
        "status": status,
        "snapshot": snapshot,
        "detail": detail,
        "used": FRESH_USED.get(item, ""),
    }


def freshness(now: Optional[datetime] = None, *, run_dir: Path | None = None) -> list[dict]:
    """各数据快照的新鲜度（纯读盘，不联网）。"""
    now = now or datetime.now(timezone.utc)
    rows: list[dict] = []

    def add(label, name, data, fields, describe, *, section=""):
        # 缺文件时 _load 返回 {}，沿用原来的「尚未获取」口径。存在但类型错误才报警。
        path = section or "根节点"
        if section and isinstance(data, dict):
            data = data.get(section, {})
        if not isinstance(data, dict):
            rows.append(_row(label, WARN, "", f"{name} 的 {path} 结构异常，应为对象"))
            return
        bad = [key for key, types in fields.items() if key in data and type(data[key]) not in types]
        if not bad:
            try:
                rows.append(_row(label, *describe(data)))
                return
            except (ArithmeticError, AttributeError, OSError, ValueError, TypeError, LookupError):
                # 例如类型正确但超过日期范围的时间戳。只报告字段名，不回显运行数据。
                bad = list(fields)
        names = "、".join(f"{section}.{key}" if section else key for key in bad)
        rows.append(_row(label, WARN, "", f"{name} 的 {names} 结构异常"))

    def valence(d):
        snap = d.get("valence_snapshot") or ""
        stale = not d.get("epoch") or WarframeClient.valence_is_stale(d, now)
        same = not stale and WarframeClient.valence_values_stale(d, now)
        src = d.get("valence_source") or ""
        return (
            STALE if stale or same else OK,
            snap,
            (
                "已过本轮窗口，卡面会提示过期；"
                if stale
                else "已换轮但数值疑似仍是上一轮（wiki 未完整更新）；"
                if same
                else "在本轮窗口内；"
            )
            + (f"来源 {src}" if src else "来源未记录"),
        )

    rot = _load("rotations.json", run_dir)
    for sec, label in (("tenet", "信条元素加成"), ("coda", "终幕元素加成")):
        add(
            label,
            "rotations.json",
            rot,
            {
                "epoch": (str,),
                "period_hours": (int, str),
                "valence_snapshot": (str,),
                "valence_source": (str,),
                "batches": (list,),
                "batch_values_since": (list,),
                "valence_values_since": (str,),
                "anchor_idx": (int, str),
            },
            valence,
            section=sec,
        )

    def acrithis(acw):
        exp = _parse_iso(acw.get("expiry"))
        return (
            OK if exp and exp > now else STALE,
            str(acw.get("expiry") or ""),
            (f"观测于 {acw.get('observed')}；" if acw.get("observed") else "")
            + ("未到轮换时间" if exp and exp > now else "已过轮换时间，卡面不再展示本周货单"),
        )

    add(
        "言录使本周货单",
        ACRITHIS_WEEK_NAME,
        _load(ACRITHIS_WEEK_NAME, run_dir),
        {"expiry": (str,), "observed": (str,)},
        acrithis,
    )

    def weekly(rw):
        stamp = rw.get("fetched")
        at = datetime.fromtimestamp(float(stamp), timezone.utc) if stamp is not None else None
        return (
            INFO,
            at.isoformat(timespec="seconds") if at else "",
            (f"{_age_text(at, now)}；" if at else "还没查询过；") + "超过 6 小时下次查询时自动刷新",
        )

    add(
        "紫卡周报（DE 官方）",
        RIVEN_WEEKLY_NAME,
        _load(RIVEN_WEEKLY_NAME, run_dir),
        {"fetched": (int, float, str)},
        weekly,
    )

    def rank(data, *, riven=False):
        ts = data.get("ts") or ""
        at = _parse_iso(ts)
        cov = f"{float(data['coverage']):.0%}" if data.get("coverage") is not None else ""
        retry = len(data.get("retry", []))
        retry_note = f"待补 {retry} 项（下次先补）；" if retry else ""
        if riven:
            n = len(data.get("rows", {}))
            return (
                INFO,
                ts,
                (
                    f"{_age_text(at, now)}；已收录 {n}/{data.get('total', '?')} 把；"
                    if at
                    else f"还没拉完过一整轮（已收录 {n} 把）；"
                )
                + (f"上轮覆盖率 {cov}（< 80% 不刷新时间戳）；" if cov else "")
                + retry_note
                + "不自动抓：大更后由管理员「紫卡排行 刷新」或点上方按钮手动拉；重抓保护 10 分钟，耗时看进度",
            )
        low = data.get("low_coverage")
        return (
            WARN if low or (at and now - at > timedelta(hours=60)) else OK if at else INFO,
            ts,
            (
                f"上轮覆盖率不足（{cov} < 80%），时间未刷新，旧行照常展示；"
                if low
                else f"上轮覆盖率 {cov}；"
                if cov
                else ""
            )
            + retry_note
            + (
                f"{_age_text(at, now)}；进度 {data.get('done', '?')}/{data.get('total', '?')}；"
                if at
                else "还没跑完过一整轮；"
            )
            + "约每两天在凌晨 2–7 点空闲时段重爬（也可点上方按钮立即抓）",
        )

    rank_fields = {
        "rows": (dict,),
        "retry": (list,),
        "ts": (str, type(None)),
        "coverage": (int, float, str, type(None)),
        "total": (int,),
    }
    add(
        "紫卡 WM 现价（手动拉取）",
        RIVEN_RT_NAME,
        _load(RIVEN_RT_NAME, run_dir),
        rank_fields,
        partial(rank, riven=True),
    )
    add(
        "WM 价格排行",
        RANKS_NAME,
        _load(RANKS_NAME, run_dir),
        {**rank_fields, "done": (int,), "low_coverage": (bool,)},
        rank,
    )

    history, baro_origin = _load_snapshot("baro_history.json", run_dir)

    def baro_history(history):
        missed = baro.missed_visits(now, grace_hours=baro.STALE_GRACE_HOURS, history=history)
        if missed is None:
            detail = "末次到访日期缺失或无效，无法判定漏访次数"
        elif missed:
            detail = f"快照后已有 {missed} 次到访超过 3 天仍未收录"
        elif baro.missed_visits(now, history=history):
            detail = "本次到访进行中或刚结束，wiki 通常 1–3 天内收录，过 3 天仍未收录再提示"
        else:
            detail = "已含最近一次到访"
        return (
            INFO if missed == 0 else WARN,
            str(history.get("fetched") or ""),
            detail + f"；{baro_origin}；每 3 小时检查社区快照",
        )

    add(
        "奸商到访历史",
        "baro_history.json",
        history,
        {"visits": (list,), "fetched": (str,), "period_days": (int, str)},
        baro_history,
    )
    ratings, ratings_origin = _load_snapshot("arb_ratings.json", run_dir)
    add(
        "仲裁评级（社区实测）",
        "arb_ratings.json",
        ratings,
        {"_updated": (str,)},
        lambda d: (
            INFO,
            d.get("_updated", ""),
            f"{ratings_origin}；每 3 小时检查社区快照",
        ),
    )
    add(
        "掉落表",
        "drops.json",
        _load("drops.json", run_dir),
        {"updated": (str,)},
        lambda d: (INFO, d.get("updated", ""), "官方掉落表快照，随版本更新"),
    )
    return rows


def registry_rows(run_dir: Path | None = None) -> list[dict]:
    """数据登记表判定（``core/data_registry.evaluate``；出错不拖垮整页）。

    运行期刷新 / 社区快照类看实际生效的那份（``paths.read_path``）；``tracked_files=()``：
    面板不做「未登记」扫描（不起 git 子进程），完整性由测试与打包闸门把关。"""
    try:
        return data_registry.evaluate(
            tracked_files=(), resolve=partial(paths.read_path, root=run_dir)
        )
    except Exception:  # noqa: BLE001
        return []


_LOADED_AT = time.time()  # 插件（本模块）加载时刻：「关于」页的运行时长

PUBLIC_REPO = "https://github.com/skyti1437/Warframe-SDJK-Bot"
# 「关于」页的数据来源与许可（与 README「数据来源」一节同口径；只列主要来源）
ABOUT_SOURCES = [
    ("世界状态", "DE 官方 api.warframe.com", "", ["https://api.warframe.com/cdn/worldState.php"]),
    (
        "中文译名 / 紫卡词条模板 / 武器面板",
        "DE 官方 Public Export",
        "",
        ["https://wiki.warframe.com/w/Public_Export"],
    ),
    (
        "紫卡词条正负与限制、武器兼容标签",
        "warframe-public-export-plus",
        "MIT",
        ["https://github.com/calamity-inc/warframe-public-export-plus"],
    ),
    (
        "武器攻击形态 / 灵化形态与进化 / CO 分类 / Archgun 面板 / 回响加成 / 紫卡倾向",
        "英文 wiki（wiki.warframe.com）",
        "CC BY-NC-SA 3.0",
        ["https://wiki.warframe.com/"],
    ),
    (
        "掉落表、武器基础面板",
        "WFCD warframe-drop-data / warframe-items",
        "MIT",
        ["https://github.com/WFCD/warframe-drop-data", "https://github.com/WFCD/warframe-items"],
    ),
    (
        "MOD 等级与逐级数值",
        "DE 官方 Public Export",
        "",
        ["https://wiki.warframe.com/w/Public_Export"],
    ),
    (
        "WFCD 未收录的武器（新武器 / 空战枪 / 守护武器）",
        "英文 wiki + DE 官方 Public Export",
        "CC BY-NC-SA 3.0",
        ["https://wiki.warframe.com/", "https://wiki.warframe.com/w/Public_Export"],
    ),
    ("市场价 / 玄骸拍卖 / 紫卡词条抽样", "warframe.market", "", ["https://warframe.market/"]),
    (
        "MOD 互斥 / 条件叠层 / 不兼容武器",
        "英文 wiki + DE 官方 Public Export + warframe-public-export-plus",
        "CC BY-NC-SA 3.0 / MIT",
        [
            "https://wiki.warframe.com/",
            "https://wiki.warframe.com/w/Public_Export",
            "https://github.com/calamity-inc/warframe-public-export-plus",
        ],
    ),
    (
        "常用敌人",
        "英文 wiki + warframe-public-export-plus",
        "CC BY-NC-SA 3.0 / MIT",
        [
            "https://wiki.warframe.com/",
            "https://github.com/calamity-inc/warframe-public-export-plus",
        ],
    ),
    ("随包字体", "Noto Sans CJK SC 子集", "SIL OFL 1.1", ["https://github.com/notofonts/noto-cjk"]),
    ("插件图标", "Warframe 官方 Fan Kit", "", []),
]


def _meta_of(name: str, *keys: str) -> str:
    """随包数据文件 ``_meta`` / 顶层里的某个字段（取不到返回空串）。"""
    try:
        doc = _load(name)
    except Exception:  # noqa: BLE001
        return ""
    for k in keys:
        cur: Any = doc
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur:
            return str(cur)
    return ""


def _astrbot_version() -> str:
    try:
        from astrbot.core.config.default import VERSION  # type: ignore

        return str(VERSION)
    except Exception:  # noqa: BLE001
        return ""


def about_info(now: Optional[float] = None) -> dict:
    """「关于」页：插件 / 运行环境 / 数据版本 / 数据来源与许可。只读、不含路径与主机名。"""
    import platform

    now = time.time() if now is None else now
    try:
        from . import __version__ as ver
    except Exception:  # noqa: BLE001
        ver = ""
    try:
        from .parser import COMMAND_ALIASES

        n_cmd = len(COMMAND_ALIASES)
    except Exception:  # noqa: BLE001
        n_cmd = 0
    reg = registry_rows()
    counts: dict[str, int] = {}
    for r in reg:
        st = str(r.get("status") or "")
        counts[st] = counts.get(st, 0) + 1
    up = max(0, int(now - _LOADED_AT))
    return {
        "plugin": {
            "name": "Warframe SDJKBOT",
            "version": ver,
            "author": "SDJK",
            "license": "GPL-3.0",
            "repo": PUBLIC_REPO,
            "commands": n_cmd,
        },
        "runtime": {
            "python": platform.python_version(),
            "astrbot": _astrbot_version(),
            "uptime_s": up,
        },
        # ★ 2026-10-27（线上实测）：**顶层不能叫 `data`** —— AstrBot 的插件页桥在拿到
        #   响应后执行 `r.data.data ?? r.data`，本键会把整包（plugin / runtime / sources）
        #   挤掉，页面只剩这 5 个字段 ⇒ 关于页全显示「未知 / 空表」。
        #   （P3 首版就踩了，只是当时用桩渲染页面、没走真实桥，没暴露。）
        "data_versions": {
            "damage_wiki_snapshot": _meta_of("weapon_attacks.json", "_meta.wiki_snapshot"),
            "riven_pools_generated": _meta_of("de/riven_pools.json", "_generated_at"),
            "riven_survey_date": _meta_of("de/riven_pools_survey.json", "_surveyed"),
            "registry_total": len(reg),
            "registry_status": counts,
        },
        "sources": [
            {"item": a, "source": b, "license": c, "links": list(u)} for a, b, c, u in ABOUT_SOURCES
        ],
    }


def ledger_rows(run_dir: Path | None = None) -> list[dict]:
    """有效数据账本（``core/data_registry.ledger``；出错不拖垮整页）。

    逐份对照「运行目录副本 / 随包种子」，说明插件实际读到哪一份；**只读**（不改名 /
    不删除 / 不写任何数据文件）。运行目录未注入（== 包内目录）时 ``ledger()`` 返回 ``[]``。"""
    try:
        return data_registry.ledger(run_dir=run_dir)
    except Exception:  # noqa: BLE001
        return []


def session_key(umo: str) -> str:
    """会话的不透明键（订阅总览默认打码，操作接口按它回查真实 umo）。"""
    import hashlib

    return hashlib.sha1(str(umo).encode("utf-8")).hexdigest()[:12]


def pause_info(groups, umo: str) -> Optional[dict]:
    """该会话的「连续失败暂停」记录（面板展示用）；没有 ⇒ None。

    ★ reason 只取推送层写入的异常短文本（≤120 字），绝不拼 umo —— 面板默认打码会话标识，
      暂停原因里也不能把原始标识带出去。
    """
    if groups is None:
        return None
    try:
        info = groups.pause_of(umo)
    except Exception:  # noqa: BLE001 - 读不到按未暂停处理
        return None
    if not isinstance(info, dict):
        return None
    reason = str(info.get("reason") or "")[:120]
    return {
        "since": info.get("since"),
        "next_try": info.get("next_try"),
        "reason": reason,
        "fails": info.get("fails"),
    }


def subscriptions(store, groups=None, reveal: bool = False, now: Optional[float] = None) -> dict:
    """蹲订阅总览：按会话分组；默认打码。"""
    now = time.time() if now is None else now
    hide = (lambda s: s) if reveal else mask_umo
    hide_user = (lambda s: s) if reveal else mask_id
    try:
        subs = list(store.all())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"读取订阅失败：{exc}", "total": 0, "sessions": []}

    def _group(umo: str) -> dict:
        # ★ 走 GroupStore.get：会补默认值（push 默认关）并归一化历史脏键；直接读 all() 会把「没记录」当成开
        try:
            return dict(groups.get(umo)) if groups is not None else {}
        except Exception:  # noqa: BLE001
            return {}

    by: dict[str, list] = {}
    for s in subs:
        by.setdefault(s.umo, []).append(s)
    sessions = []
    events: dict[str, int] = {}
    for umo, lst in sorted(by.items()):
        g = _group(umo)
        items = []
        for s in sorted(lst, key=lambda x: x.created_at):
            events[s.event] = events.get(s.event, 0) + 1
            window = s.time_window()
            items.append(
                {
                    "sid": s.sid,
                    "event": s.event,
                    "rule": s.rule,
                    "platform": s.platform,
                    "until": s.until,
                    "window": {"zh": window.describe(), "en": window.describe("en")},
                    "once": s.once,
                    "hits_left": s.hits_left,
                    "created_at": s.created_at,
                    "created_by": hide_user(s.created_by),
                    "expired": bool(s.expired(now)),
                }
            )
        sessions.append(
            {
                "umo": hide(umo),
                "key": session_key(umo),  # 打码后前端拿不到原 umo，操作按这个不透明键回查
                "push": bool(g.get("push")) if groups is not None else None,
                "quiet": g.get("quiet"),
                # 连续失败被暂停（2026-10-17）：None = 未暂停
                "paused": pause_info(groups, umo),
                "subs": items,
            }
        )
    return {
        "total": len(subs),
        "events": events,
        "event_names_en": {key: _EV_EN[key][1] for key in events if key in _EV_EN},
        "sessions": sessions,
    }


def paused_session_count(push) -> int:
    """被「连续失败暂停」且**仍有订阅**的会话数（与推送页列表同口径）。

    暂停记录存在 GroupStore；订阅被删光后记录可能残留，那种会话推送页列不出来，也不该计数。
    """
    groups = getattr(push, "groups", None)
    store = getattr(push, "store", None)
    if groups is None:
        mem = getattr(push, "_pause", None)
        return len([u for u, v in mem.items() if v]) if isinstance(mem, dict) else 0
    try:
        live = {s.umo for s in store.all()} if store is not None else None
        return sum(
            1
            for umo, rec in (groups.all() or {}).items()
            if isinstance(rec, dict) and rec.get("push_pause") and (live is None or umo in live)
        )
    except Exception:  # noqa: BLE001
        return 0


def push_state(push) -> dict:
    """推送守护状态（只读属性；**不调用** tick/start/stop —— 那些会真的发消息）。"""
    task = getattr(push, "task", None)
    alive = bool(task) and not task.done()
    # ★ 用内存里的 last_tick_at：零订阅时 tick 不落盘基线，基线文件 ts 会停在旧时刻（ZCode 10-16 线上实测）
    last_ts = getattr(push, "last_tick_at", None)
    completed = getattr(push, "last_tick_completed_at", None)
    try:
        nxt = float(push.next_delay())
    except Exception:  # noqa: BLE001
        nxt = None
    return {
        "alive": alive,
        "interval": getattr(push, "interval", None),
        "ticks": getattr(push, "_tick_no", 0),
        # _tick_ok / _tick_fail 每轮清零：是「最近一轮」的数据源成败，不是累计
        "last_round_ok": getattr(push, "_tick_ok", 0),
        "last_round_fail": getattr(push, "_tick_fail", 0),
        "idle": bool(getattr(push, "last_tick_idle", False)),
        "fail_streak": getattr(push, "_fail_streak", 0),
        "next_delay": nxt,
        "pending": len(getattr(push, "_pending", []) or []),
        "failing_sessions": sum(1 for v in (getattr(push, "_fail_count", {}) or {}).values() if v),
        "paused_sessions": paused_session_count(push),
        "last_tick": last_ts,
        "last_tick_completed_at": completed,
        "last_tick_completed_age_secs": max(0, int(time.time() - completed))
        if completed is not None
        else None,
    }


# ---------------------------------------------------------------------------
# 依赖与资源（2026-10-16 维护者要求：面板里能下载需要的依赖）
# ---------------------------------------------------------------------------
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = PLUGIN_ROOT / "requirements.txt"
# 完整中文字库（Noto Sans CJK，Regular + Bold ≈ 40 MB；市场包 16 MB 上限放不下，只随包带 GB2312 子集）。
# 源与 scripts/fetch_font.py 一致（scripts/ 不进市场包，市场用户跑不到那个脚本 ⇒ 面板里给按钮）。
FULL_FONTS = {
    "NotoSansCJK-Regular.ttc": [
        "https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@main/Sans/OTC/NotoSansCJK-Regular.ttc",
        "https://fastly.jsdelivr.net/gh/notofonts/noto-cjk@main/Sans/OTC/NotoSansCJK-Regular.ttc",
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTC/NotoSansCJK-Regular.ttc",
    ],
    "NotoSansCJK-Bold.ttc": [
        "https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@main/Sans/OTC/NotoSansCJK-Bold.ttc",
        "https://fastly.jsdelivr.net/gh/notofonts/noto-cjk@main/Sans/OTC/NotoSansCJK-Bold.ttc",
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTC/NotoSansCJK-Bold.ttc",
    ],
}
_MIN_FONT_BYTES = 8 * 1024 * 1024  # 完整字库每个约 19–20 MB；小于 8 MB 一定是错误页 / 截断
# 用户导入字体（URL / 上传）：小于 64 KB 不可能含中文；大于 64 MB 拒收（防误传）
_IMPORT_MIN_BYTES = 64 * 1024
_FONT_REPAIR_UNSET = object()  # 区分直接下载调用与明确「起初没有可修复旧文件」。
_IMPORT_MAX_BYTES = 64 * 1024 * 1024
_FONT_SUFFIXES = (".ttf", ".otf", ".ttc")


def safe_font_name(raw: str) -> str:
    """把 URL / 上传文件名清洗成安全的字体文件名；后缀不是 ttf/otf/ttc 返回空串。"""
    from urllib.parse import unquote

    name = Path(unquote(str(raw or "")).split("?", 1)[0].split("#", 1)[0]).name
    stem, dot, suf = name.rpartition(".")
    if not dot or "." + suf.lower() not in _FONT_SUFFIXES:
        return ""
    stem = re.sub(r"[^\w.\-]+", "_", stem).strip("._")[:80] or "font"
    return f"{stem}.{suf.lower()}"


def render_font_sample(path) -> str:
    """用指定字体画一张小样张，返回 PNG data URL。"""
    import base64
    import io

    from PIL import Image, ImageDraw, ImageFont

    from .render import pick_cjk_face

    face = pick_cjk_face(path) or {"index": 0}
    big = ImageFont.truetype(str(path), 34, index=face["index"])
    small = ImageFont.truetype(str(path), 24, index=face["index"])
    img = Image.new("RGB", (900, 150), (19, 24, 35))
    d = ImageDraw.Draw(img)
    d.text((24, 18), "Warframe 仲裁 · 遗物 赏金 紫卡 ◆※★", font=big, fill=(240, 215, 154))
    d.text(
        (24, 74),
        # 后三个生僻字随包子集没有：样张就是要让人看到缺字。写成转义，因为
        # tests/test_render_overflow.py 把 core/*.py 的字符当卡片语料、要求子集全覆盖
        "生僻字：犽 罅 魇 \u5f67 \u9f98 \U00020bb7　数字 0123456789 ABCxyz",
        font=small,
        fill=(236, 232, 222),
    )
    d.text((24, 110), "缺字会显示成方块 □", font=small, fill=(168, 176, 190))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---- 主题（外观）----
THEME_PRESETS = [
    {"key": "orokin", "name": "Orokin（默认）", "colors": {}},
    {
        "key": "kuva",
        "name": "赤毒",
        "colors": {
            "bg": "#140c0d",
            "panel": "#22171a",
            "line": "#4a2a26",
            "accent": "#d0584a",
            "highlight": "#f2a08a",
        },
    },
    {
        "key": "void",
        "name": "虚空",
        "colors": {
            "bg": "#070d14",
            "panel": "#0f1a26",
            "line": "#1f3442",
            "accent": "#7fc8d8",
            "highlight": "#c9f1ff",
        },
    },
    {
        "key": "ivory",
        "name": "象牙（浅色）",
        "colors": {
            "bg": "#ece6d8",
            "panel": "#f8f4ea",
            "line": "#d9c9a3",
            "accent": "#7b5b23",
            "highlight": "#614918",
            "ink": "#2a2620",
            "ink_dim": "#5a5448",
            "warn": "#87530c",
            # 浅底使用深类别色；不回落暗色主题的亮色。
            "cyan": "#0e6376",
            "violet": "#7044a0",
            "red": "#a1322a",
            "green": "#2a683f",
            "amber": "#87530c",
            "blue": "#315f91",
            "magenta": "#933d73",
            "teal": "#17695d",
            "steel": "#3d5c7c",
            "blueprint": "#7e4a0f",
            "rarity_gold": "#8a6a00",
            "rarity_silver": "#4a5b71",
            "rarity_bronze": "#a3501f",
        },
    },
]
# 预览样卡：用正式版式展示分区、列表、遗物档位、注释、警示、页码和页脚。
PREVIEW_TITLE = "外观预览（第1/2页）"
PREVIEW_LINES = [
    "◆ 当前仲裁",
    "◆ 节点：Zeugma（火卫一）　生存　I系",
    "◆ 剩余：3小时12分",
    "◆ 遗物与部件",
    "· [古纪] Lith A1　⟦g⟧Gyre Prime 系统蓝图⟦/g⟧　100杜卡德",
    "· [前纪] Meso K10　⟦s⟧Protea Prime 头部⟦/s⟧　45杜卡德",
    "· [中纪] Neo V8　⟦b⟧Akbolto Prime 枪管⟦/b⟧　15杜卡德",
    "· [后纪] Axi A2　满级 ★　▲12%　▼3%",
    "※ 注释面板：数据来自社区实测，仅供参考",
    "※ ⚠ 警示面板：排期表即将到期",
]
PREVIEW_FOOT = "平台：PC · 来源 外观预览"
PREVIEW_EN = (
    "Appearance Preview (Page 1/2)",
    [
        "◆ Current Arbitration",
        "◆ Node: Zeugma (Phobos)　Survival　Infested",
        "◆ Time: 3h 12m left",
        "◆ Relics & Parts",
        "· [Lith] Lith A1　⟦g⟧Gyre Prime Systems Blueprint⟦/g⟧　100 Ducats",
        "· [Meso] Meso K10　⟦s⟧Protea Prime Neuroptics⟦/s⟧　45 Ducats",
        "· [Neo] Neo V8　⟦b⟧Akbolto Prime Barrel⟦/b⟧　15 Ducats",
        "· [Axi] Axi A2　Max Rank ★　▲12%　▼3%",
        "※ Notes: Community test data for reference",
        "※ ⚠ Warning: The rotation schedule is about to expire",
    ],
    "Platform: PC · Source: Appearance Preview",
)
_IMG_MAX_BYTES = 15 * 1024 * 1024
_IMG_MAX_SIDE = {"bg": 2400, "logo": 512}
_COMPARE_TTL = 600


def font_id(path) -> str:
    import hashlib

    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]


def _ver_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def _satisfies(installed: str, op: str, want: str) -> bool:
    try:
        from packaging.version import Version

        a, b = Version(installed), Version(want)
    except Exception:  # noqa: BLE001 - 没有 packaging 时退化为数字元组比较
        a, b = _ver_tuple(installed), _ver_tuple(want)
    return {
        ">=": a >= b,
        ">": a > b,
        "==": a == b,
        "<=": a <= b,
        "<": a < b,
        "!=": a != b,
        "~=": a >= b,
    }.get(op, True)


def deps_status(req_path: Path = REQUIREMENTS) -> list[dict]:
    """requirements.txt 里每个包：要求 / 已装版本 / 是否满足。"""
    from importlib import metadata

    rows: list[dict] = []
    try:
        lines = req_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(?:(>=|==|<=|~=|!=|>|<)\s*([\w.]+))?", line)
        if not m:
            continue
        name, op, want = m.group(1), m.group(2) or "", m.group(3) or ""
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            have = ""
        ok = bool(have) and (not op or _satisfies(have, op, want))
        rows.append(
            {
                "name": name,
                "spec": f"{op}{want}" if op else "任意版本",
                "installed": have,
                "ok": ok,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 设置（读 _conf_schema.json；写回 AstrBot 的插件配置对象）
# ---------------------------------------------------------------------------
SCHEMA_PATH = PLUGIN_ROOT / "_conf_schema.json"
# 分组只是展示用；schema 里新增的键没列进来会落到「其他」，不会丢
SETTINGS_GROUPS = [
    (
        "数据源",
        [
            "worldsource",
            "de_worldstate_url",
            "proxy",
            "http_timeout",
            "flaresolverr_enabled",
            "flaresolverr_urls",
            "community_data_url",
            "wfhelper_valence",
        ],
    ),
    (
        "输出",
        [
            "render_mode",
            "page_size",
            "default_language",
            "display_timezone",
            "default_platform",
            "wiki_intro",
            "wiki_link_source",
        ],
    ),
    ("QQ", ["qq_markdown_cards", "qq_merge_image_actions"]),
    ("Telegram / Discord / Slack / KOOK", ["text_chunk_limit"]),
    ("推送", ["push_interval", "digest_timezone"]),
    # 视觉模型按读图角色分组；装备文字渠道留空时继承识卡主读。
    ("视觉模型 · 兼容主渠道", ["vision_provider_id"]),
    (
        "视觉模型 · 紫卡识别",
        ["vision_provider_id_riven", "vision_provider_id_line", "vision_provider_id_line_recheck"],
    ),
    (
        "视觉模型 · 配卡识别（识卡）",
        ["vision_provider_id_scan", "vision_provider_id_scan_equipment"],
    ),
    # 冷却与并发对紫卡截图和配卡截图都生效；详情模式只管识卡
    ("识图行为", ["scan_cooldown", "scan_max_concurrent", "scan_detail_mode"]),
    (
        "伤害引擎",
        [
            "damage_engine_mode",
            "damage_engine_url",
            "damage_engine_portable_port",
            "damage_engine_portable_bundle_sha256",
            "damage_engine_token",
        ],
    ),
    ("紫卡出售参考", ["riven_sale_sheet_url"]),
    ("权限", ["super_admins"]),
]
# 只对部分聊天平台起作用的设置：设置页按「当前在用的平台」显示，其余默认收起（值照常保存与生效）
PLATFORM_LABELS = {
    "aiocqhttp": "QQ（OneBot / NapCat）",
    "qq_official": "QQ 官方机器人",
    "qq_official_webhook": "QQ 官方机器人（Webhook）",
    "telegram": "Telegram",
    "discord": "Discord",
    "slack": "Slack",
    "kook": "KOOK",
}
FIELD_PLATFORMS = {
    "qq_markdown_cards": ["qq_official"],
    "qq_merge_image_actions": ["qq_official"],
    "text_chunk_limit": ["telegram", "discord", "slack", "kook"],
}
# 面板内部流程管理的键，不作为普通设置项列出。
PANEL_LANGUAGE = "panel_language"
PANEL_LANGUAGE_INITIALIZED = "panel_language_initialized"
PANEL_ONLY_KEYS = frozenset(
    {
        "panel_platforms",
        "vision_routes_isolated",
        "disabled_commands",
        PANEL_LANGUAGE,
        PANEL_LANGUAGE_INITIALIZED,
    }
)
# 改了要重载插件才生效的键（在 __init__ 里构造进 WarframeClient / GroupStore 的）；其余运行时直接读 cfg，保存即生效
RELOAD_KEYS = frozenset(
    {
        "worldsource",
        "de_worldstate_url",
        "http_timeout",
        "proxy",
        "flaresolverr_enabled",
        "flaresolverr_urls",
        "default_platform",
    }
)

GLOBAL_PROTECTED_COMMANDS = frozenset({"help", "status", "wm", "bounty"})


class _Command(NamedTuple):
    key: str
    name: str
    name_en: str
    description: str
    description_en: str
    dependencies: tuple[str, ...] = ()
    experimental: bool = False


# 组名和说明用于控制台，触发词沿用解析器词表。依赖标签也标出可选能力。
_COMMAND_GROUPS = {
    ("核心入口", "Core"): (
        _Command("help", "帮助", "Help", "查看可用指令与用法", "List available commands and usage"),
        _Command(
            "status",
            "状态",
            "Status",
            "查看插件版本与订阅状态",
            "Check the version and subscriptions",
        ),
    ),
    ("世界状态与周期", "World State and Cycles"): (
        _Command("timers", "时效", "Timers", "汇总各周期剩余时间", "See time left in each cycle"),
        _Command(
            "cetus",
            "夜灵",
            "Open World Cycles",
            "查看开放世界昼夜与周期",
            "Check open-world cycles",
        ),
        _Command(
            "bounty", "赏金", "Bounties", "查各地区赏金与奖励", "Find regional bounties and rewards"
        ),
        _Command(
            "fissures", "裂隙", "Void Fissures", "筛选当前虚空裂隙", "Filter active Void Fissures"
        ),
        _Command("sortie", "突击", "Sortie", "查看每日突击任务", "See today's Sortie missions"),
        _Command(
            "archon", "执刑官", "Archon Hunt", "查看本周执刑官猎杀", "See this week's Archon Hunt"
        ),
        _Command(
            "arbitration",
            "仲裁",
            "Arbitration",
            "查看当前与后续仲裁",
            "See active and upcoming Arbitrations",
        ),
        _Command(
            "arbtable",
            "仲裁表",
            "Arbitration Schedule",
            "查整周仲裁排期",
            "Browse the weekly Arbitration schedule",
        ),
        _Command("alerts", "警报", "Alerts", "查当前警报与奖励", "Find active alerts and rewards"),
        _Command(
            "invasions",
            "入侵",
            "Invasions",
            "查入侵进度与报酬",
            "Check Invasion progress and rewards",
        ),
        _Command("nightwave", "电波", "Nightwave", "查看午夜电波挑战", "See Nightwave challenges"),
        _Command("events", "活动", "Events", "查看限时活动", "See limited-time events"),
        _Command("conclave", "武形秘仪", "Conclave", "查看 PvP 挑战", "See PvP challenges"),
    ),
    ("周常与轮换", "Weekly Activities and Rotations"): (
        _Command(
            "deeparchimedea",
            "深层科研",
            "Deep Archimedea",
            "查本周任务与修正",
            "See this week's missions and modifiers",
        ),
        _Command(
            "temporalarchimedea",
            "时光科研",
            "Temporal Archimedea",
            "查本周任务与修正",
            "See this week's missions and modifiers",
        ),
        _Command(
            "descendia",
            "沉沦之地",
            "Descendia",
            "查看本周楼层与复杂化",
            "See this week's floors and complications",
        ),
        _Command("calendar", "日历", "1999 Calendar", "查看 1999 日历", "Browse the 1999 calendar"),
        _Command(
            "incursions",
            "侵袭",
            "Steel Path Incursions",
            "查看钢路每日侵袭节点",
            "See daily Steel Path Incursion nodes",
        ),
        _Command(
            "voidstorms",
            "九重天",
            "Void Storms",
            "查看航道星舰虚空风暴",
            "See active Railjack Void Storms",
        ),
        _Command(
            "incarnon",
            "灵化轮换",
            "Incarnon Rotation",
            "查看灵化之源轮换",
            "See the Incarnon Genesis rotation",
        ),
        _Command(
            "tenet",
            "信条",
            "Tenet Rotation",
            "查轮换信条武器与元素加成",
            "See rotating Tenet weapons and element bonuses",
        ),
        _Command(
            "coda",
            "终幕",
            "Coda Rotation",
            "查轮换终幕武器与元素加成",
            "See rotating Coda weapons and element bonuses",
        ),
        _Command(
            "construction",
            "舰队进度",
            "Fleet Construction",
            "查看舰队建造进度",
            "Check fleet construction progress",
        ),
        _Command(
            "clanrewards",
            "氏族奖励",
            "Clan Rewards",
            "查看每周氏族研究奖励",
            "See weekly clan research rewards",
        ),
    ),
    ("商店与兑换", "Shops and Exchanges"): (
        _Command(
            "voidtrader",
            "奸商",
            "Void Trader",
            "查 Baro 到访与库存",
            "Check Baro's visits and stock",
        ),
        _Command(
            "dailydeals", "每日特惠", "Daily Deals", "查看 Darvo 每日特惠", "See Darvo's daily deal"
        ),
        _Command(
            "steelpath",
            "钢铁之路",
            "Steel Path Rewards",
            "查看钢铁精华商店轮换",
            "See Teshin's rotating rewards",
        ),
        _Command(
            "slivershop",
            "碎银兑换",
            "Riven Sliver Shop",
            "查看 Palladino 的碎银兑换",
            "See Palladino's Sliver exchanges",
        ),
        _Command(
            "acrichis",
            "言录使",
            "Acrithis",
            "查看言录使周常货单",
            "See Acrithis's weekly wares",
            ("Wiki",),
        ),
        _Command(
            "primevault",
            "阿耶",
            "Prime Vault",
            "查看 Prime 出库与阿耶兑换",
            "Browse vaulted Prime items and Aya exchanges",
        ),
        _Command(
            "flashsales",
            "商城折扣",
            "Flash Sales",
            "查看商城限时礼包",
            "See limited-time Market bundles",
        ),
    ),
    ("市场与查价", "Market and Prices"): (
        _Command(
            "wm",
            "市场查价",
            "Market Orders",
            "查物品在售与收购挂单",
            "Look up item sell and buy orders",
            ("WM",),
        ),
        _Command(
            "wr",
            "紫卡",
            "Riven Auctions",
            "查指定武器或未开紫卡的拍卖",
            "Find weapon or unrolled Riven auctions",
            ("WM",),
        ),
        _Command(
            "rm",
            "源紫卡",
            "Veiled Riven Prices",
            "按类别查源紫卡挂单价格",
            "Look up veiled Riven prices by category",
            ("WM",),
        ),
        _Command(
            "xh",
            "玄骸",
            "Adversary Auctions",
            "查玄骸武器拍卖",
            "Look up adversary weapon auctions",
            ("WM",),
        ),
        _Command(
            "rank",
            "排行",
            "Rankings",
            "查交易价格排行与紫卡周报",
            "See market rankings and weekly Riven trends",
            ("WM",),
        ),
        _Command(
            "trend",
            "趋势",
            "Price History",
            "查询物品近期与长期价格走势",
            "See short- and long-term item price trends",
            ("WM",),
        ),
    ),
    ("遗物与刷取", "Relics and Farming"): (
        _Command(
            "relic",
            "遗物",
            "Relics",
            "查遗物奖励、来源与入库状态",
            "Find Relic rewards, sources, and vault status",
        ),
        _Command(
            "openrelic",
            "开核桃",
            "Relic Runs",
            "根据当前裂隙推荐开遗物",
            "Find Relic runs in active Fissures",
        ),
        _Command(
            "parts",
            "部件",
            "Prime Parts",
            "查 Prime 部件和蓝图来源",
            "Find Prime part and blueprint sources",
        ),
        _Command(
            "farm",
            "刷",
            "Farming Plan",
            "规划整套部件的遗物与刷取节点",
            "Plan Relics and missions for a full set",
        ),
        _Command(
            "ducats",
            "金垃圾",
            "Ducats",
            "按杜卡德价值查询 Prime 部件",
            "List Prime parts by Ducat value",
        ),
    ),
    ("紫卡与伤害配卡", "Rivens, Damage, and Builds"): (
        _Command(
            "damage",
            "伤害",
            "Damage",
            "计算单次伤害、面板与 DPS",
            "Calculate hit damage, stats, and DPS",
            ("独立引擎",),
        ),
        _Command(
            "scan",
            "识卡",
            "Build Scan",
            "读取配卡截图并校验面板。目前尚未完善，识别较慢且易受遮挡或改名影响，默认关闭。",
            "Read and verify a build screenshot. Recognition is incomplete, slow, and sensitive to hidden slots or renamed weapons. Disabled by default.",
            ("视觉模型",),
            experimental=True,
        ),
        _Command(
            "scandamage",
            "识卡伤害",
            "Build Scan Damage",
            "复用识卡结果复算伤害。识卡尚未完善，识别较慢且易受遮挡或改名影响，默认关闭。",
            "Recalculate damage from a build scan. Recognition is incomplete, slow, and sensitive to hidden slots or renamed weapons. Disabled by default.",
            ("独立引擎",),
            experimental=True,
        ),
        _Command(
            "loadoutrecommend",
            "配卡推荐",
            "Build Recommendations",
            "按武器搜索配装，可按实卡重配",
            "Search for a weapon build, with or without a Riven",
            ("视觉模型", "独立引擎"),
        ),
        _Command(
            "analysis",
            "紫卡分析",
            "Riven Analysis",
            "分析紫卡词条、融合与报价",
            "Analyze Riven stats, splices, and prices",
            ("视觉模型", "独立引擎", "WM"),
        ),
        _Command(
            "riventraits",
            "词条推荐",
            "Riven Reroll Suggestions",
            "看洗卡词条建议与出售参考",
            "See suggested stats and selling references",
            ("独立引擎",),
        ),
        _Command(
            "ranges",
            "词条范围",
            "Riven Stat Ranges",
            "查看可刷出的紫卡词条区间",
            "Look up possible Riven stat ranges",
        ),
        _Command(
            "splice",
            "紫卡融合",
            "Riven Splice",
            "查融合词条的组成配方",
            "Find the stats that make up a splice",
        ),
        _Command(
            "disposition",
            "倾向",
            "Riven Disposition",
            "查看原倾向与统一后的缩放",
            "See original and normalized dispositions",
        ),
        _Command(
            "valence",
            "武器融合",
            "Valence Fusion",
            "计算融合后的元素加成",
            "Calculate the resulting elemental bonus",
        ),
    ),
    ("资料与推送", "Reference and Push"): (
        _Command(
            "wiki", "维基", "Wiki", "按术语直达维基条目", "Find a matching Wiki page", ("Wiki",)
        ),
        _Command(
            "translate",
            "翻译",
            "Translate",
            "查游戏名称中英对照",
            "Look up Chinese and English game terms",
        ),
        _Command(
            "news", "新闻", "News", "查看最新公告与更新", "Read recent official news and updates"
        ),
        _Command(
            "synthtargets",
            "结合目标",
            "Synthesis Targets",
            "查 Simaris 结合目标",
            "Find Simaris Synthesis Targets",
        ),
        _Command(
            "dun",
            "蹲",
            "Subscriptions",
            "订阅或取消变化推送",
            "Manage subscriptions to change alerts",
            ("推送",),
        ),
    ),
}

COMMAND_CATALOG = {
    command.key: command for commands in _COMMAND_GROUPS.values() for command in commands
}
COMMAND_DESCRIPTIONS = {
    key: (command.description, command.description_en) for key, command in COMMAND_CATALOG.items()
}


def _global_disabled_commands(cfg: dict | None) -> frozenset[str]:
    if cfg is None or "disabled_commands" not in cfg:
        return DEFAULT_DISABLED_COMMANDS
    return _disabled_command_keys(cfg["disabled_commands"])


def _disabled_command_keys(values: Any) -> frozenset[str]:
    if not isinstance(values, list):
        return frozenset()
    return (
        frozenset(key for key in values if isinstance(key, str) and key in COMMAND_ALIASES)
        - GLOBAL_PROTECTED_COMMANDS
    )


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


DEFAULT_DISABLED_COMMANDS = _disabled_command_keys(
    load_schema().get("disabled_commands", {}).get("default")
)


def coerce_setting(spec: dict, value: Any, *, key: str = "") -> Any:
    """按 schema 类型校验 / 转换一个值；不合法抛 ValueError（带中文原因）。"""
    t = spec.get("type", "string")
    if t == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("应为开 / 关")
    if t == "int":
        if isinstance(value, bool):
            raise ValueError("应为整数")
        try:
            v = int(str(value).strip())
        except ValueError:
            raise ValueError("应为整数") from None
        if v < 0:
            raise ValueError("不能为负数")
        validate_numeric_setting(key, v)
        return v
    if t == "float":
        try:
            v = float(str(value).strip())
        except ValueError:
            raise ValueError("应为数字") from None
        validate_numeric_setting(key, v)
        return v
    if t == "list":
        if isinstance(value, str):
            value = [x for x in re.split(r"[,，\s]+", value) if x]
        if not isinstance(value, list):
            raise ValueError("应为列表")
        return [str(x).strip() for x in value if str(x).strip()]
    if key == "de_worldstate_url":
        # 面板复用逐行输入控件，宿主配置仍存字符串，兼容原来的单地址配置。
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            value = "\n".join(value)
        if not isinstance(value, str) or len(value) > 2000:
            raise ValueError("请输入由逗号或换行分隔的 HTTP(S) 地址")
        urls = parse_de_worldstate_urls(value)
        for url in urls:
            try:
                parts = urlsplit(url)
                if (
                    parts.scheme not in ("http", "https")
                    or not parts.hostname
                    or parts.port == 0
                    or parts.username is not None
                    or any(c.isspace() for c in url)
                ):
                    raise ValueError
            except ValueError:
                raise ValueError("每个 DE 地址必须是合法的 HTTP(S) 地址，且不带账号密码") from None
        return ", ".join(urls)
    v = "" if value is None else str(value)
    opts = spec.get("options")
    if opts and v not in opts:
        raise ValueError(f"只能是 {' / '.join(map(str, opts))}")
    if len(v) > 2000:
        raise ValueError("太长")
    if key == "damage_engine_url":
        return damage_engine.engine_url(v or damage_engine.DEFAULT_URL)
    if key == "riven_sale_sheet_url":
        return riven_sale.source_url(v)
    if key == "damage_engine_token" and (
        not v.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in v)
    ):
        raise ValueError("令牌只接受可见 ASCII 字符")
    return v


def _write_config_changes(conf, changes: dict) -> None:
    """保存失败时回滚宿主配置对象中的本次改动。"""
    previous = {key: conf[key] for key in changes if key in conf}
    try:
        conf.update(changes)
        conf.save_config()
    except Exception:
        for key in changes:
            if key in previous:
                conf[key] = previous[key]
            else:
                conf.pop(key, None)
        raise


class _PanelClosed(Exception):
    """排队期间面板已停用，渲染操作尚未开工。"""


class WebPanel:
    """把面板路由挂到 AstrBot（``context.register_web_api``）。"""

    def __init__(self, plugin, plugin_name: str):
        self.p = plugin
        self.name = plugin_name
        self.closed = False
        self._overview_scans: dict[Path, asyncio.Task] = {}
        self._refresh_lock = asyncio.Lock()
        self._test_lock = asyncio.Lock()
        self._data_test_cache: dict[str, tuple[float, tuple, dict]] = {}
        self._compare_jobs: dict[str, dict] = {}
        self._compare_results: dict[str, dict] = {}
        self._compare_task: Optional[asyncio.Task] = None
        self._deps_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._command_history: dict[str, frozenset[str]] = {}
        self._legacy_command_form = None
        self._legacy_ambiguous_keys = set()
        self._migration_previews: dict[str, dict] = {}
        self._appearance_lock = threading.RLock()
        self._theme_drafts: dict[str, tuple[str, bytes]] = {}
        self._render_tasks: set[asyncio.Task] = set()
        self._font_task: Optional[asyncio.Task] = None
        self._font_info: dict = {}
        self._preview_r = None  # 预览专用渲染器（独立缓存目录，免得清理时误删正要发出的卡片）  # (路径, mtime, 大小) → 覆盖探测结果（ttc 逐面探测，缓存免得每次刷新重算）
        self.font_job: dict = {"state": "idle"}  # idle / running / done / failed
        self._http_factory = None  # 测试注入（httpx.MockTransport）；None = httpx.AsyncClient

    def routes(self) -> list[tuple[str, Any, list[str], str]]:
        n = self.name
        return [
            (f"/{n}/overview", self.overview, ["GET"], "SDJKBOT 面板：新鲜度 / 订阅 / 推送"),
            (f"/{n}/command_stats", self.command_stats, ["GET"], "SDJKBOT 面板：指令统计"),
            (f"/{n}/refresh", self.refresh, ["POST"], "SDJKBOT 面板：刷新社区快照"),
            (f"/{n}/pull", self.pull, ["POST"], "SDJKBOT 面板：从源头重抓"),
            (f"/{n}/crawl", self.crawl, ["POST"], "SDJKBOT 面板：立即抓排行"),
            (f"/{n}/conntest", self.conntest, ["POST"], "SDJKBOT 面板：连通性测试"),
            (f"/{n}/resources", self.resources, ["GET"], "SDJKBOT 面板：依赖与字库状态"),
            (f"/{n}/deps_install", self.deps_install, ["POST"], "SDJKBOT 面板：安装依赖"),
            (f"/{n}/font_download", self.font_download, ["POST"], "SDJKBOT 面板：下载完整字库"),
            (
                f"/{n}/font_import_url",
                self.font_import_url,
                ["POST"],
                "SDJKBOT 面板：从 URL 导入字体",
            ),
            (f"/{n}/font_upload", self.font_upload, ["POST"], "SDJKBOT 面板：上传字体"),
            (f"/{n}/font_select", self.font_select, ["POST"], "SDJKBOT 面板：选择正文 / 粗体字体"),
            (f"/{n}/font_delete", self.font_delete, ["POST"], "SDJKBOT 面板：删除导入的字体"),
            (f"/{n}/font_sample", self.font_sample, ["GET"], "SDJKBOT 面板：字体样张"),
            (f"/{n}/theme", self.theme, ["GET"], "SDJKBOT 面板：读取主题"),
            (f"/{n}/theme_preview", self.theme_preview, ["POST"], "SDJKBOT 面板：主题预览"),
            (f"/{n}/theme_save", self.theme_save, ["POST"], "SDJKBOT 面板：保存主题"),
            (f"/{n}/theme_upload_bg", self.theme_upload_bg, ["POST"], "SDJKBOT 面板：上传背景图"),
            (f"/{n}/theme_upload_logo", self.theme_upload_logo, ["POST"], "SDJKBOT 面板：上传角标"),
            (
                f"/{n}/theme_draft_release",
                self.theme_draft_release,
                ["POST"],
                "SDJKBOT 面板：释放草稿图片",
            ),
            (
                f"/{n}/theme_image_delete",
                self.theme_image_delete,
                ["POST"],
                "SDJKBOT 面板：删除主题图片",
            ),
            (f"/{n}/settings", self.settings, ["GET"], "SDJKBOT 面板：读取设置"),
            (f"/{n}/commands_config", self.commands_config, ["GET"], "SDJKBOT 面板：读取指令开关"),
            (
                f"/{n}/commands_config_save",
                self.commands_config_save,
                ["POST"],
                "SDJKBOT 面板：保存指令开关",
            ),
            (f"/{n}/riven_ocr_status", self.riven_ocr_status, ["GET"], "本地紫卡 OCR 状态"),
            (f"/{n}/riven_ocr_check", self.riven_ocr_check, ["POST"], "检查本地 OCR 路径"),
            (f"/{n}/riven_ocr_save", self.riven_ocr_save, ["POST"], "保存本地 OCR 路径"),
            (f"/{n}/riven_mode_switch", self.riven_mode_switch, ["POST"], "切换紫卡实际读取方式"),
            (f"/{n}/riven_ocr_enable", self.riven_ocr_enable, ["POST"], "主动启停本地 OCR"),
            (f"/{n}/riven_ocr_try", self.riven_ocr_try, ["POST"], "主动试识别本地紫卡"),
            (
                f"/{n}/riven_ocr_install_status",
                self.riven_ocr_install_status,
                ["GET"],
                "本地 OCR 部署状态",
            ),
            (f"/{n}/riven_ocr_install", self.riven_ocr_install, ["POST"], "主动下载部署本地 OCR"),
            (
                f"/{n}/vision_prompt_presets",
                self.vision_prompt_presets,
                ["GET"],
                "SDJKBOT 面板：固定视觉方案",
            ),
            (
                f"/{n}/vision_compare_prepare",
                self.vision_compare_prepare,
                ["POST"],
                "SDJKBOT 面板：准备同图对比",
            ),
            (
                f"/{n}/vision_compare_run",
                self.vision_compare_run,
                ["POST"],
                "SDJKBOT 面板：执行同图对比",
            ),
            (
                f"/{n}/vision_compare_status",
                self.vision_compare_status,
                ["GET"],
                "SDJKBOT 面板：查询同图对比状态",
            ),
            (
                f"/{n}/vision_compare_release",
                self.vision_compare_release,
                ["POST"],
                "SDJKBOT 面板：释放未执行的对比",
            ),
            (f"/{n}/vision_prompts", self.vision_prompts, ["GET"], "SDJKBOT 面板：视觉提示词方案"),
            (
                f"/{n}/vision_prompt_update",
                self.vision_prompt_update,
                ["POST"],
                "SDJKBOT 面板：保存、启用或恢复视觉提示词",
            ),
            (f"/{n}/settings_save", self.settings_save, ["POST"], "SDJKBOT 面板：保存设置"),
            (f"/{n}/damage_engine_status", self.damage_engine_status, ["GET"], "伤害引擎部署状态"),
            (
                f"/{n}/damage_engine_action",
                self.damage_engine_action,
                ["POST"],
                "部署或检查伤害引擎",
            ),
            (f"/{n}/panel_language", self.panel_language, ["GET"], "SDJKBOT 面板：界面语言"),
            (
                f"/{n}/vision_migration_status",
                self.vision_migration_status,
                ["GET"],
                "视觉任务迁移状态",
            ),
            (
                f"/{n}/vision_migration_prepare",
                self.vision_migration_prepare,
                ["POST"],
                "预览视觉任务迁移",
            ),
            (
                f"/{n}/vision_migration_release",
                self.vision_migration_release,
                ["POST"],
                "释放视觉任务迁移预览",
            ),
            (
                f"/{n}/vision_migration_apply",
                self.vision_migration_apply,
                ["POST"],
                "主动确认视觉任务迁移",
            ),
            (f"/{n}/sub_remove", self.sub_remove, ["POST"], "SDJKBOT 面板：删除订阅"),
            (f"/{n}/group_push", self.group_push, ["POST"], "SDJKBOT 面板：群推送开关"),
            (f"/{n}/push_resume", self.push_resume, ["POST"], "SDJKBOT 面板：恢复被暂停的群推送"),
            (f"/{n}/about", self.about, ["GET"], "SDJKBOT 面板：关于（版本 / 环境 / 数据来源）"),
        ]

    def register(self, context) -> bool:
        """注册路由；旧版 AstrBot 没有该能力或注册失败只记日志，不影响插件其余功能。"""
        reg = getattr(context, "register_web_api", None)
        if not callable(reg):
            logger.info("[sdjk] 当前 AstrBot 不支持插件页面接口，运维面板不可用")
            return False
        try:
            for route, fn, methods, desc in self.routes():
                reg(route, fn, methods, desc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sdjk] 运维面板路由注册失败（不影响指令）：%s", exc)
            return False
        return True

    def close(self) -> Optional[asyncio.Task]:
        """立即停用路由并取消下载；返回任务供宿主有界等待清理，重复调用不打断清理。"""
        if not self.closed:
            self.closed = True
            self._data_test_cache.clear()
            self._migration_previews.clear()
            self._theme_drafts.clear()
            self._compare_results.clear()
            for token, job in list(self._compare_jobs.items()):
                if job["state"] != "running":
                    self._drop_compare_snapshot(token, job)
            if self._compare_task and not self._compare_task.done():
                self._compare_task.cancel()
            if self._font_task and not self._font_task.done():
                self._font_task.cancel()
        return self._font_task

    # ---- 请求参数（新旧两种 request 代理都兼容；测试里可直接覆盖这两个方法） ----
    @staticmethod
    def _query(key: str, default: str = "") -> str:
        try:
            from astrbot.api.web import request
        except ImportError:  # 旧版 AstrBot：Quart 风格
            from quart import request
        try:
            return str(request.query.get(key, default))
        except AttributeError:
            return str(request.args.get(key, default))

    @staticmethod
    async def _body() -> dict:
        try:
            from astrbot.api.web import request

            data = await request.json(default={})
        except ImportError:
            from quart import request

            data = await request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def _gone(self) -> Optional[dict]:
        if self.closed:
            return {"status": "error", "message": "插件已停用或正在重载，请刷新页面"}
        return None

    # ---- 路由 ----
    def _ocr_service(self):
        if self.closed:
            return None
        return getattr(self.p, "local_ocr", None)

    @staticmethod
    def _ocr_unavailable():
        return {
            "ok": False,
            "implemented": False,
            "code": "unavailable",
            "message": "本地 OCR 服务不可用或正在重载",
        }

    async def riven_ocr_status(self):
        service = self._ocr_service()
        return service.status() if service is not None else self._ocr_unavailable()

    async def _ocr_action(self, action):
        service = self._ocr_service()
        if service is None:
            return self._ocr_unavailable()
        from .local_ocr import OCRConflict

        body = await self._body()
        try:
            if action == "save":
                return await service.save(body)
            if action == "check":
                result = await service.check(
                    body.get("engine"), body.get("python"), body.get("model_dir")
                )
                # check 是已完成的检查请求；check_ok 表达检查结论，不用HTTP错误吞掉详情。
                return {
                    **result,
                    "ok": True,
                    "check_ok": result.get("ok") is True,
                    "findings": [str(result.get("message") or "检查完成")],
                }
            return await service.enable(body.get("enabled"), body.get("revision"))
        except OCRConflict as exc:
            return {"ok": False, "code": "revision_conflict", "message": str(exc)}
        except (ValueError, OSError) as exc:
            return {"ok": False, "code": "invalid_config", "message": str(exc)}

    async def riven_ocr_check(self):
        return await self._ocr_action("check")

    async def riven_ocr_save(self):
        return await self._ocr_action("save")

    async def riven_ocr_enable(self):
        return await self._ocr_action("enable")

    async def riven_mode_switch(self):
        body = await self._body()
        if self.closed:
            return self._riven_switch_error("closed", "插件已停用或正在重载，请刷新页面")
        mode, revision = body.get("mode"), body.get("revision")
        if mode not in ("local", "single", "dual"):
            return self._riven_switch_error("invalid_mode", "请选择有效的紫卡读取方式")
        if body.get("confirmed") is not True:
            return self._riven_switch_error("confirmation_required", "请确认切换紫卡读取方式")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
            return self._riven_switch_error("invalid_revision", "请先刷新并确认当前读取方式")
        if (
            self._save_lock.locked()
            or self._test_lock.locked()
            or getattr(self.p, "_ocr_busy", None)
        ):
            return self._riven_switch_error("busy", "设置正在保存或识图正在进行，请稍后重试")
        async with self._save_lock:
            local = getattr(self.p, "local_ocr", None)
            try:
                with local._locked() if local is not None else nullcontext():
                    return self._commit_riven_mode(mode, revision, local)
            except Exception:  # noqa: BLE001 - 不公开配置、渠道或文件路径的异常细节
                return self._riven_switch_error("invalid_config", "读取配置失败，请检查后重试")

    @staticmethod
    def _riven_switch_error(code, message):
        return {"ok": False, "code": code, "message": message}

    def _commit_riven_mode(self, mode, revision, local):
        error = self._riven_switch_error
        if self.closed:
            return error("closed", "插件已停用或正在重载，请刷新页面")
        running = getattr(self.p, "cfg", None)
        if not isinstance(running, dict):
            return error("invalid_config", "插件运行配置不可用")
        cfg = copy.deepcopy(running)
        status = local.status() if local is not None else None
        snapshot = self._riven_effective_snapshot(cfg, local, status)
        if snapshot["revision"] != revision:
            return error("revision_conflict", "读取方式或渠道配置已变化，请刷新后重新确认")
        if local is not None:
            if status.get("ok") is not True:
                return error("invalid_config", "请先修复本地 OCR 配置")
            if status.get("closed"):
                return error("closed", "本地 OCR 管理器已关闭，请刷新页面")
            if local._switch_busy():
                return error("busy", "本地 OCR 正在使用，请稍后重试")
        enable_local = mode == "local"
        if enable_local:
            if local is None:
                return error("local_unavailable", "本地 OCR 管理器不可用")
            try:
                local._check_enable(status)
            except ValueError:
                return error("local_unavailable", "请先保存本地 OCR 配置并检查通过")
            python = Path(status["python"])
            if not python.is_file() or not os.access(python, os.X_OK):
                return error("local_unavailable", "本地 OCR 解释器不存在或不可执行")
            if status.get("models_found") is not True:
                return error("local_unavailable", "本地 OCR 模型文件不齐，请检查模型目录")
        else:
            if type(cfg.get("vision_routes_isolated", False)) is not bool:
                return error("invalid_config", "视觉路由隔离状态异常，请检查已保存配置")
            frozen = _VisionRouteProbe(self.p, cfg=cfg)
            available = (
                self._riven_single_available(frozen)
                if mode == "single"
                else any(
                    self._resolve_vision_route(route, host=frozen)[0]["available"]
                    for route in ("riven", "line")
                )
            )
            if not available:
                return error("route_unavailable", "目标读取方式没有可用渠道，请检查渠道配置")
        if self.closed:
            return error("closed", "插件已停用或正在重载，请刷新页面")
        if (
            running is not self.p.cfg
            or running != cfg
            or local is not getattr(self.p, "local_ocr", None)
        ):
            return error("revision_conflict", "读取方式或渠道配置已变化，请刷新后重新确认")
        change_local = local is not None and status["enabled"] != enable_local
        data = None
        if change_local:
            from .local_ocr import OCRConflict

            data = local._read()
            try:
                local._cas(data, status["revision"])
            except OCRConflict:
                return error("revision_conflict", "本地 OCR 配置已变化，请刷新后重新确认")
        clean = {} if enable_local else {"riven_single_ocr": mode == "single"}
        conf = getattr(self.p, "_config", None)
        persisted = False
        previous = {}
        if clean:
            if conf is None or not callable(getattr(conf, "save_config", None)):
                return error("invalid_config", "插件持久配置不可用")
            defaults = dict.fromkeys(_RIVEN_SWITCH_ROUTES, "")
            defaults.update(vision_routes_isolated=False, riven_single_ocr=False)
            for key, default in defaults.items():
                saved, live = conf.get(key, default), cfg.get(key, default)
                if type(saved) is not type(live) or saved != live:
                    return error(
                        "revision_conflict", "已保存配置与运行配置不一致，请刷新后重新确认"
                    )
            previous = {key: conf[key] for key in clean if key in conf}
        try:
            if clean != previous:
                persisted = True
                conf.update(clean)
                conf.save_config()
            if change_local:
                local._write_enabled(data, enable_local)
        except Exception:  # noqa: BLE001 - 在释放 OCR 锁前恢复宿主配置
            if persisted:
                for key in clean:
                    if key in previous:
                        conf[key] = previous[key]
                    else:
                        conf.pop(key, None)
                try:
                    conf.save_config()
                except Exception:  # noqa: BLE001
                    return error("rollback_failed", "切换失败且持久配置恢复失败，请检查已保存配置")
            return error("save_failed", "切换保存失败，原有读取方式保留")
        self._apply_live(clean)
        self._migration_previews.clear()
        return {"ok": True, "mode": mode}

    async def riven_ocr_try(self):
        service = self._ocr_service()
        if service is None:
            return self._ocr_unavailable()
        body = await self._body()
        if body.get("confirmed") is not True:
            return {
                "ok": False,
                "code": "confirmation_required",
                "message": "请确认执行一次本地识别",
            }
        revision = body.get("revision")
        if type(revision) is not int or not 0 <= revision <= 2**53 - 1:
            return {
                "ok": False,
                "code": "invalid_revision",
                "message": "请重新读取本地 OCR 配置，并携带有效修订号确认试识别",
            }
        from .local_ocr import decode_image_data_url

        try:
            image = decode_image_data_url(body.get("image_data_url"))
        except ValueError as exc:
            return {"ok": False, "code": "invalid_image", "message": str(exc)}
        return await service.recognize(image, expected_revision=revision)

    async def riven_ocr_install_status(self):
        installer = getattr(self.p, "local_ocr_installer", None) if not self.closed else None
        return installer.status() if installer is not None else self._ocr_unavailable()

    async def riven_ocr_install(self):
        installer = getattr(self.p, "local_ocr_installer", None) if not self.closed else None
        if installer is None:
            return self._ocr_unavailable()
        body = await self._body()
        if self.closed:
            return self._ocr_unavailable()
        return await installer.start(
            body.get("confirmed") is True, expected_job_id=body.get("job_id")
        )

    async def damage_engine_status(self):
        if (g := self._gone()) is not None:
            return g
        service = getattr(self.p, "damage_engine", None)
        if service is None:
            return {"ok": False}
        cfg = self.p.cfg or {}
        status = service.status(cfg) if damage_engine.portable_port(cfg) else service.status()
        return {"ok": True, **status}

    async def damage_engine_action(self):
        if (g := self._gone()) is not None:
            return g
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        service = getattr(self.p, "damage_engine", None)
        if service is None:
            return {"ok": False, "message": "伤害引擎管理器不可用"}
        if body.get("action") == "install" and body.get("confirmed") is True:
            return {"ok": True, **service.install(self.p.cfg or {})}
        if body.get("action") == "check":
            try:
                return await service.check(dict(self.p.cfg or {}))
            except damage_engine.EngineError as exc:
                return {"ok": False, "message": str(exc)}
        return {"ok": False, "message": "请选择检查，或确认下载部署"}

    async def about(self):
        if (g := self._gone()) is not None:
            return g
        return about_info()

    def background_rows(self) -> list[dict]:
        """只读任务状态；实际 Task 校正元数据，不将检查成功时间当作数据更新时间。"""
        records = getattr(self.p, "_background_status", {})
        records = records if isinstance(records, dict) else {}
        specs = [
            ("rank", "自动价格榜", self.p, "_auto_task", True),
            ("valence", "轮换检查", self.p, "_valence_task", True),
            ("revision", "文档更新检查", self.p, "_revision_task", True),
            ("community", "社区检查", self.p, "_community_task", True),
            ("boot", "启动预热", self.p, "_boot_task", False),
            ("warmup", "本地预热", self.p, "_warm_task", False),
            ("manual_rank", "手动价格榜", self.p.client, "_rank_crawl_task", False),
            ("manual_riven", "手动紫卡现价", self.p.client, "_riven_rt_crawl_task", False),
            ("font", "字体下载 / 导入", self, "_font_task", False),
        ]

        def epoch(value):
            try:
                value = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return value if math.isfinite(value) and value > 0 else None

        rows = []
        for key, label, owner, attr, permanent in specs:
            meta = records.get(key) or {}
            meta = meta if isinstance(meta, dict) else {}
            task = getattr(owner, attr, None)
            task = task if isinstance(task, asyncio.Task) else None
            if key.startswith("manual_") and task is not None:
                # 每次手动任务自己的结果；自动抓取或后续轮次不能改写历史结果。
                current = getattr(task, "_wf_crawl_status", None)
                if isinstance(current, dict):
                    meta = current
            alive = task is not None and not task.done()
            state = str(meta.get("state") or "unknown")
            result = meta.get("result")
            raw_detail = str(meta.get("detail") or "").strip()
            detail = public_error_text(raw_detail)[:240] if raw_detail else ""
            # 元数据可能保存异常消息；概览只展示类型，绝不透出凭据或连接地址。
            error = ""
            if meta.get("last_error"):
                match = re.match(
                    r"^([A-Za-z_]\w*(?:Error|Exception|Timeout))(?::|$)", str(meta["last_error"])
                )
                error = match.group(1) if match else "有异常记录"
            status = STALE if result == "error" else WARN if result == "degraded" else INFO
            if alive:
                state = "waiting" if state == "waiting" else "running"
                if getattr(task, "cancelling", lambda: 0)():
                    detail = "正在取消并释放资源"
                if result == "ok":
                    status = OK
            elif task is not None:
                if task.cancelled():
                    state, status = "cancelled", WARN
                else:
                    exc = task.exception()
                    if exc is not None:
                        state, status, error = "failed", STALE, type(exc).__name__
                    elif permanent:
                        state, status, detail = "failed", STALE, "常驻任务已结束"
                    else:
                        state = "failed" if result == "error" else "completed"
                        if result not in ("error", "degraded"):
                            status = OK
            elif key.startswith("manual_") or key == "font":
                state, status = "idle", INFO
            elif permanent and meta:
                state, status, detail = "failed", WARN, "常驻任务未运行"
            if key == "font" and task is not None and task.done() and not task.cancelled():
                if self.font_job.get("state") == "failed":
                    state, status, result = "failed", STALE, "error"
                    detail = "下载或导入失败，详情见外观页"
            rows.append(
                {
                    "key": key,
                    "label": label,
                    "alive": alive,
                    "state": state,
                    "status": status,
                    "result": result,
                    "detail": detail,
                    "last_error": error,
                    **{
                        field: epoch(meta.get(field))
                        for field in (
                            "last_attempt",
                            "last_finished",
                            "last_success",
                            "last_error_at",
                        )
                    },
                    "next_run": epoch(meta.get("next_run")) if alive else None,
                }
            )
        return rows

    async def overview(self):
        if (g := self._gone()) is not None:
            return g
        reveal = self._query("reveal", "0") == "1"
        return await self._overview_snapshot(reveal)

    @staticmethod
    def _overview_disk(run_dir: Path) -> dict:
        try:
            fresh = freshness(run_dir=run_dir)
        except (AttributeError, TypeError, ValueError, LookupError, ArithmeticError) as exc:
            # 各文件通常由 freshness 逐行诊断；汇总自身出错时仍须读取独立账本。
            logger.warning("[sdjk] 总览新鲜度汇总失败（%s）", type(exc).__name__)
            fresh = [
                _row("数据新鲜度", WARN, "", "overview.freshness 结构异常，请查看有效数据账本")
            ]
        return {
            "freshness": fresh,
            "registry": registry_rows(run_dir),
            "ledger": ledger_rows(run_dir),
        }

    async def _overview_snapshot(self, reveal: bool):
        run_dir = paths.run_dir()
        task = self._overview_scans.get(run_dir)
        if task is None:
            task = asyncio.create_task(asyncio.to_thread(self._overview_disk, run_dir))
            self._overview_scans[run_dir] = task

            def finished(done):
                self._overview_scans.pop(run_dir, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        disk = await asyncio.shield(task)
        if (g := self._gone()) is not None:
            return g
        try:
            from . import __version__ as ver
        except Exception:  # noqa: BLE001
            ver = ""
        payload = {
            "version": ver,
            "now": time.time(),
            "display_timezone": display_time.panel_timezone(
                (getattr(self.p, "cfg", None) or {}).get("display_timezone")
            ),
            "background": self.background_rows(),
            **disk,
            "disabled_commands": sorted(_global_disabled_commands(getattr(self.p, "cfg", None))),
            "push": push_state(self.p.push),
            "subs": subscriptions(self.p.subs, getattr(self.p, "groups", None), reveal=reveal),
            "flare_enabled": bool(getattr(self.p.client, "flare_enabled", False)),
            # 社区快照采用记录（批次 B 全链路健康记录·插件侧）：这台机器最近一次检查每份快照的结论
            "community": (
                self.p.client.community_report()
                if hasattr(self.p.client, "community_report")
                else []
            ),
            "community_base": str(getattr(self.p.client, "community_base", "") or ""),
        }
        return payload

    async def refresh(self):
        """从公开仓 bot-data 拉社区快照（效价 + 言录使货单）。只在本地已过期时才会写盘。"""
        if (g := self._gone()) is not None:
            return g
        if self._refresh_lock.locked():
            return {"ok": False, "code": "refresh_busy", "message": "上一次刷新还没结束"}
        out = {}
        async with self._refresh_lock:
            for key, fn in (
                ("valence", self.p.client.refresh_valence),
                ("acrithis", self.p.client.refresh_acrichis_week),
            ):
                try:
                    out[key] = str(await asyncio.wait_for(fn(community_only=True), 60))
                except Exception as exc:  # noqa: BLE001
                    out[key] = f"失败：{type(exc).__name__}: {exc}"[:200]
            static = getattr(self.p.client, "refresh_community_static", None)
            if static is not None:
                try:
                    out["static"] = await asyncio.wait_for(static(), 90)
                except Exception as exc:  # noqa: BLE001
                    out["static"] = {"*": f"失败：{type(exc).__name__}: {exc}"[:200]}
        logger.info("[sdjk] 面板手动刷新快照：%s", out)
        return {"results": out, "freshness": freshness()}

    async def pull(self):
        """从源头重抓（2026-10-18 维护者定：不能只靠定时）：不管本地新不新鲜，立即从 wiki 源页重抓
        元素加成、言录使货单。校验照旧（数值未更新 / 货单目录），校验不过不落盘；
        源页抓不到时各自按原兜底链回落。"""
        if (g := self._gone()) is not None:
            return g
        if self._refresh_lock.locked():
            return {"ok": False, "code": "refresh_busy", "message": "上一次刷新还没结束"}
        c = self.p.client
        out = {}
        async with self._refresh_lock:
            for key, fn in (
                ("valence", lambda: c.refresh_valence(force=True)),
                ("acrithis", lambda: c.refresh_acrichis_week(force=True)),
            ):
                try:
                    out[key] = str(await asyncio.wait_for(fn(), 150))
                except Exception as exc:  # noqa: BLE001
                    out[key] = f"失败：{type(exc).__name__}: {exc}"[:200]
        logger.info("[sdjk] 面板从源头重抓：%s", out)
        return {"results": out, "freshness": freshness()}

    async def crawl(self):
        """立即抓排行（2026-10-24 维护者定：加面板按钮）：``target`` = ``riven_rt``（紫卡实时排行）/
        ``wm_ranks``（WM 价格排行）。只负责在后台启动，不等抓完；进度看新鲜度表。
        与群里的「紫卡排行 刷新」/「排行 刷新」同一入口，已在抓时不会重复启动；
        启动和结束后的保护冷却同样适用于面板。"""
        if (g := self._gone()) is not None:
            return g
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        target = str(body.get("target") or "")
        c = self.p.client
        try:
            if target == "riven_rt":
                state, done, total = c.start_riven_rt_crawl(force=False)
                if state == "cooldown":
                    return {
                        "status": "error",
                        "message": "紫卡 WM 现价抓取仍在保护冷却中，请稍后再试",
                    }
            elif target == "wm_ranks":
                started, done, total = c.start_rank_crawl()
                state = "started" if started else "running"
            else:
                return {"status": "error", "message": f"未知排行：{target}"}
        except WarframeAPIError as exc:
            return {"status": "error", "message": public_error_text(exc)}
        logger.info("[sdjk] 面板立即抓排行 %s：%s（%s/%s）", target, state, done, total)
        return {
            "target": target,
            "state": state,
            "done": done,
            "total": total,
            "freshness": freshness(),
        }

    async def conntest(self):
        if (g := self._gone()) is not None:
            return g
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        target = str(body.get("target") or "")
        if target not in (
            "flare",
            "vision",
            "vision_routes",
            "vision_live",
            "worldstate",
            "community",
            "wiki",
            "market",
        ):
            return {"status": "error", "message": f"未知测试项：{target}"}
        route = body.get("route")
        if route is None or route == "":
            route = "compat"
        if target == "vision_live" and (not isinstance(route, str) or route not in _VISION_ROUTES):
            return {
                "status": "error",
                "route": None,
                "message": "未知视觉任务：请选择 compat / riven / line / scan",
                "attempted": 0,
                **_VISION_CALL_AUDIT,
                "sent": False,
                "not_sent_reason": "invalid_route",
            }
        if target in ("vision", "vision_routes"):
            t0 = time.perf_counter()
            res = (
                self._test_vision_routes()
                if target == "vision_routes"
                else self._test_vision_resolve()
            )
            return {**res, "ms": int((time.perf_counter() - t0) * 1000), "target": target}
        if self._test_lock.locked():
            result = {"status": "error", "message": "上一项测试还没结束"}
            if target == "vision_live":
                result.update(
                    route=route,
                    attempted=0,
                    sent=False,
                    not_sent_reason="test_busy",
                    **_VISION_CALL_AUDIT,
                )
            return result
        async with self._test_lock:
            if (g := self._gone()) is not None:
                return g
            if target in ("worldstate", "community", "wiki", "market"):
                return await self._test_data_source(target)
            t0 = time.perf_counter()
            if target == "flare":
                res = await self._test_flare()
            else:
                res = await self._test_vision_live(route)
            res["ms"] = int((time.perf_counter() - t0) * 1000)
            res["target"] = target
            return res

    def _data_test_key(self) -> tuple:
        client = self.p.client
        http = getattr(client, "http", None)
        cfg = getattr(self.p, "cfg", {})
        return (
            id(client),
            id(http),
            str(getattr(http, "timeout", "")),
            tuple(getattr(http, "headers", {}).items()),
            cfg.get("default_platform", "pc"),
            getattr(client, "worldsource", "de"),
            cfg.get("worldsource", "de"),
            self._running_de_urls(),
            tuple(parse_de_worldstate_urls(cfg["de_worldstate_url"]))
            if "de_worldstate_url" in cfg
            else None,
            getattr(client, "worldstate_base", ""),
            getattr(client, "community_base", ""),
            getattr(client, "allow_private_urls", False),
            getattr(client, "wm_v2_base", ""),
            getattr(client, "flare_enabled", False),
            tuple(getattr(client, "_flare_urls", ())),
        )

    def _running_de_urls(self) -> tuple[str, ...]:
        client = self.p.client
        candidates = getattr(client, "_de_worldstate_urls", None)
        if candidates is not None:
            return tuple(candidates())
        return tuple(parse_de_worldstate_urls(getattr(client, "de_worldstate_url", "")))

    async def _test_data_source(self, target: str) -> dict:
        key = self._data_test_key()
        hit = self._data_test_cache.get(target)
        if hit is not None and time.monotonic() < hit[0] and key == hit[1]:
            return {**copy.deepcopy(hit[2]), "cached": True}
        started = time.perf_counter()
        cfg = getattr(self.p, "cfg", {})
        # 按保存的来源和地址测，重载前仍保留运行中的配置。
        saved_ws = cfg.get("worldsource", "de")
        running_ws = getattr(self.p.client, "worldsource", "de")
        saved_de_url = cfg.get("de_worldstate_url")
        probe_options = {}
        pending_de_urls = False
        if target == "worldstate":
            probe_options["worldsource"] = saved_ws
            if saved_ws == "de" and saved_de_url is not None:
                probe_options["de_worldstate_url"] = saved_de_url
                pending_de_urls = (
                    tuple(parse_de_worldstate_urls(saved_de_url)) != self._running_de_urls()
                )
        try:
            result = await self.p.client.probe_data_source(
                target,
                platform=cfg.get("default_platform", "pc"),
                **probe_options,
            )
        except Exception as exc:  # noqa: BLE001 - 原异常可能包含凭据与主机路径
            result = {
                "ok": False,
                "detail": f"探测失败（{type(exc).__name__}），请稍后重试",
                "attempts": [],
                "skipped_reason": "",
            }
        if (g := self._gone()) is not None:
            return g
        if target == "worldstate" and saved_ws != running_ws:
            names = {"de": "DE 世界状态", "warframestat": "warframestat.us"}
            result = {
                **result,
                "detail": (
                    f"{result.get('detail') or ''}（按设置测的是 {names.get(saved_ws, saved_ws)}；"
                    f"该项需重载插件才生效，当前运行中仍是 {names.get(running_ws, running_ws)}）"
                ),
            }
        elif pending_de_urls:
            result = {
                **result,
                "detail": f"{result.get('detail') or ''}（已按保存的 DE 地址探测，需重载插件后生效）",
            }
        result = {
            **result,
            "target": target,
            "checked_at": time.time() if result.get("attempts") else None,
            "ms": int((time.perf_counter() - started) * 1000),
            "cached": False,
        }
        # 未执行的额度检查不占30秒缓存，额度恢复后可立即重测。
        if not result.get("skipped_reason") and key == self._data_test_key():
            self._data_test_cache[target] = (
                time.monotonic() + 30,
                key,
                copy.deepcopy(result),
            )
        return result

    async def _test_flare(self) -> dict:
        c = self.p.client
        if not getattr(c, "flare_enabled", False):
            return {
                "ok": False,
                "detail": "配置里已关闭 FlareSolverr（flaresolverr_enabled）",
                "skipped_reason": "disabled",
            }
        try:
            ok = await c.flare_reachable(timeout=6.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"探测异常：{exc}"}
        url = getattr(c, "_flare_url", "") or ""
        if ok:
            return {"ok": True, "detail": f"可连：{url}"}
        tried = "、".join(getattr(c, "_flare_urls", []) or [])
        return {"ok": False, "detail": f"候选地址都连不上：{tried or '（无）'}"}

    _pid = staticmethod(_prov_id)

    def _test_vision_resolve(self) -> dict:
        """只解析渠道，不调模型（零成本）。"""
        try:
            main = [self._pid(p) for p in self.p._vision_providers()]
            line = [self._pid(p) for p in self.p._line_vision_providers()]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"解析渠道异常：{self._vision_failure(exc)}"}
        if not main:
            return {
                "ok": False,
                "detail": "没有可用的多模态渠道：请在配置 vision_provider_id 填一个",
            }
        return {
            "ok": True,
            "detail": f"兼容主渠道（按优先级）：{'、'.join(main)}；紫卡窄读：{'、'.join(line) or '同主渠道'}",
        }

    def _resolve_vision_route(self, route: str, *, host=None) -> tuple[dict, list]:
        """解析与真调共用真实 selector；返回公开元数据及不对外序列化的渠道对象。"""
        host = self.p if host is None else host
        key, selector = _VISION_ROUTES[route]
        cfg = getattr(host, "_cfg_str", None)
        configured = cfg(key) if callable(cfg) else ""
        probe = _VisionRouteProbe(host)
        try:
            invalid_type = not isinstance(getattr(host, "cfg", {}).get(key, ""), str)
        except Exception:  # noqa: BLE001
            invalid_type = True
        error = "渠道配置类型异常" if invalid_type else ""
        try:
            providers = [p for p in getattr(probe, selector)() if p is not None]
        except Exception as exc:  # noqa: BLE001
            providers = []
            error = f"解析渠道异常：{type(exc).__name__}"
        isolated = bool(getattr(host, "_vision_routes_isolated", lambda: False)())
        fallback = False
        inherited_from = None
        if route == "line" and configured:
            # 真实 _line_vision_providers 的第一次查找就是显式窄读渠道。
            # 不用输出 ID 猜：显式选中与主读同一对象，也仍是显式配置。
            lookup = next((ok for pid, ok in probe.lookups if pid == configured), None)
            if lookup is False and isolated:
                source_mode = "explicit_unavailable"
            elif lookup is False:
                source_mode, inherited_from, fallback = "fallback_riven", "riven", True
            elif lookup is True:
                source_mode = "explicit"
            else:
                source_mode = "unknown"  # 自定义 selector 不暴露查找分支时不臆断
        elif configured:
            source_mode = "explicit" if providers else "explicit_unavailable"
        elif isolated and route in ("riven", "scan"):
            source_mode = "unconfigured"
        elif route == "line_recheck":
            source_mode = "disabled"
        elif route == "compat":
            source_mode = "automatic"
        else:
            inherited_from = {"line": "riven", "scan_equipment": "scan"}.get(route, "compat")
            source_mode = f"inherit_{inherited_from}"
        cap = {
            "compat": None,
            "riven": 1,
            "line": 1,
            "scan": 4,
            "scan_equipment": 1,
            "line_recheck": 1,
        }[route]
        selected = providers if cap is None else providers[:cap]
        live_limit = min(_LIVE_MAX_TRIES, len(selected))
        limits = {
            "runtime_provider_cap": cap,
            "runtime_selected_count": len(selected),
            "live_max_tries": live_limit,
            "live_provider_call_cap": _LIVE_MAX_TRIES,
            "live_count_unit": "provider_text_chat_call",
        }
        if route == "scan":
            limits.update(
                focus_provider_cap=1,
                focus_selected_count=min(1, len(providers)),
                title_provider_cap=2,
                title_selected_count=min(2, len(providers)),
                legacy_damage_rows_provider_cap=2,
                legacy_damage_rows_active=False,
            )
        info = {
            "route": route,
            "isolated": isolated,
            "config_key": key,
            "configured_id": public_error_text(configured)
            if configured and not invalid_type
            else "",
            "source_mode": source_mode,
            "inherited_from": inherited_from,
            "fallback": fallback,
            "available": bool(selected),
            "provider_ids": [public_error_text(self._pid(p)) for p in providers],
            "selected_provider_ids": [public_error_text(self._pid(p)) for p in selected],
            "limits": limits,
            "error": error,
        }
        return info, selected

    def _riven_effective_snapshot(self, cfg: dict, local, status) -> dict:
        """仅白名单派生模式与修订；不公开本地配置、路径、凭据或异常原文。"""
        flag = cfg.get("riven_single_ocr")
        isolated_flag = cfg.get("vision_routes_isolated", False)
        isolated = isolated_flag if type(isolated_flag) is bool else True
        basis = {
            "local_manager": local is not None,
            "local_state": "absent" if local is None else "unavailable",
            "local_revision": None,
            "single_ocr": flag is True,
            "isolated": isolated,
        }
        result = {
            "mode": "unknown",
            "revision": "",
            "basis": basis,
            "blocked": True,
            "reason": "本地状态暂不可读，请刷新确认",
            "reason_code": "local_unreadable",
        }
        if local is not None:
            if not isinstance(status, dict):
                return result
            revision = status.get("revision")
            if type(revision) is int and revision >= 0:
                basis["local_revision"] = revision
            if status.get("ok") is not True:
                basis["local_state"] = "invalid"
            elif type(status.get("enabled")) is bool:
                basis["local_state"] = "enabled" if status["enabled"] else "disabled"
            else:
                basis["local_state"] = "invalid"
                result.update(
                    reason="本地状态字段异常，请检查本地配置", reason_code="local_invalid"
                )
                return result
        mode = (
            riven_read_selection(flag, status) if local is not None else riven_read_selection(flag)
        )
        result.update(
            mode=mode,
            blocked=False,
            reason_code="selected",
            reason={
                "local": "已选择本地 OCR",
                "single": "已选择单模型 OCR",
                "dual": "已选择双路模型",
            }[mode],
        )
        if basis["local_state"] == "invalid":
            reason = (
                "本地配置异常，按运行入口停止云端回落"
                if status.get("ok") is not True
                else "本地状态字段异常，请检查本地配置"
            )
            result.update(
                blocked=True,
                reason=reason,
                reason_code="local_config_invalid"
                if status.get("ok") is not True
                else "local_invalid",
            )
        elif mode == "local" and (
            status.get("closed") is True
            or status.get("models_found") is False
            or status.get("implemented") is False
        ):
            result.update(
                blocked=True,
                reason="本地 OCR 暂不可用，请检查本地配置",
                reason_code="local_unavailable",
            )
        if mode != "local" and (
            type(isolated_flag) is not bool or (flag is not None and type(flag) is not bool)
        ):
            result.update(
                mode="unknown",
                revision="",
                blocked=True,
                reason="读取方式配置异常，请检查已保存配置",
                reason_code="mode_config_invalid",
            )
            return result
        # 渠道 ID 只作为哈希输入；不把 endpoint、key、路径或整个配置放进修订。
        if mode == "single" and not isinstance(cfg.get("vision_provider_id_riven", ""), str):
            result.update(
                blocked=True,
                reason="读取方式配置异常，请检查已保存配置",
                reason_code="mode_config_invalid",
            )
        material = {
            "mode": mode,
            "basis": basis,
            "routes": {key: cfg.get(key, "") for key in _RIVEN_SWITCH_ROUTES},
        }
        # 非字符串路由只记录类型，避免将异常嵌套配置中的凭据纳入哈希。
        material["routes"] = {
            key: value if isinstance(value, str) else {"invalid": type(value).__name__}
            for key, value in material["routes"].items()
        }
        result["revision"] = hashlib.sha256(
            json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return result

    @staticmethod
    def _riven_single_available(frozen):
        pid = frozen._vision_route_value("vision_provider_id_riven")
        selected = frozen._try_get_provider(pid) if pid else None
        client = getattr(selected, "client", None)
        completions = getattr(getattr(client, "chat", None), "completions", None)
        return all(
            callable(method)
            for method in (
                getattr(selected, "get_model", None),
                getattr(client, "with_options", None),
                getattr(completions, "create", None),
            )
        )

    def _test_vision_routes(self) -> dict:
        """同一次本地状态与配置快照诊断；不识别、不调用模型、不启动 worker。"""
        try:
            before = copy.deepcopy(dict(getattr(self.p, "cfg", {}) or {}))
        except Exception:  # noqa: BLE001
            before = None
        local = getattr(self.p, "local_ocr", None)
        status = None
        if local is not None:
            try:
                status = copy.deepcopy(local.status())
            except Exception:  # noqa: BLE001 - 未知状态绝不猜测云端
                pass
        frozen = _VisionRouteProbe(self.p, cfg=before or {})
        effective = self._riven_effective_snapshot(before or {}, local, status)
        routes = (
            [self._resolve_vision_route(route, host=frozen)[0] for route in _VISION_ROUTES]
            if before is not None
            else []
        )
        single = effective["mode"] == "single"
        if effective["mode"] in {"local", "unknown"}:
            for row in routes:
                if row["route"] in {"riven", "line", "line_recheck"}:
                    row["runtime_active"] = False
        if single:
            for row in routes:
                if row["route"] in {"line", "line_recheck"}:
                    row["runtime_active"] = False
                if row["route"] == "riven":
                    row["available"] = self._riven_single_available(frozen)
                    if not row["available"]:
                        row["error"] = "请指定兼容图像对话接口的紫卡主读渠道"
        if effective["mode"] in {"single", "dual"} and not effective["blocked"]:
            active = {"riven"} if single else {"riven", "line"}
            if not any(row["available"] for row in routes if row["route"] in active):
                effective.update(
                    blocked=True,
                    reason="已选模式的渠道不可用，请检查渠道配置",
                    reason_code="channel_unavailable",
                )
        try:
            changed = before is None or before != dict(getattr(self.p, "cfg", {}) or {})
        except Exception:  # noqa: BLE001
            changed = True
        if changed or local is not getattr(self.p, "local_ocr", None) or self.closed:
            effective.update(
                mode="unknown",
                revision="",
                blocked=True,
                reason="配置正在变化或服务已停用，请刷新确认",
                reason_code="snapshot_changed",
            )
            for row in routes:
                if row["route"] in {"riven", "line", "line_recheck"}:
                    row["runtime_active"] = False
        detail = {
            "local": "紫卡使用本地 OCR，云端读取不参与。",
            "single": "紫卡使用单个主读 OCR 渠道。首读原图，必要时用同一渠道复读增强图和位置。",
            "dual": "紫卡主读与窄读分别判断可用性，各取首个渠道。",
            "unknown": "紫卡实际读取方式暂不可确认，请刷新。",
        }[effective["mode"]]
        return {
            "riven_read_mode": "single_ocr"
            if effective["mode"] == "single"
            else "configured_routes",
            "riven_effective_mode": effective,
            "ok": all(row["available"] for row in routes if row.get("runtime_active", True)),
            "detail": detail
            + "识卡主读最多4路，标题补读最多2路，装备文字可独立选1路，其它分区按需执行。",
            "routes": routes,
            "attempted": 0,
            **_VISION_CALL_AUDIT,
            "sent": False,
            "not_sent_reason": "resolve_only",
        }

    @staticmethod
    def _vision_failure(exc: Exception) -> str:
        """不把 SDK 的 key、请求头、endpoint 或响应体原文发回页面。"""
        code = re.search(r"\b[45]\d{2}\b", str(exc))
        return f"{type(exc).__name__}" + (f"（HTTP {code[0]}）" if code else "")

    async def _test_vision_live(self, route: str = "compat") -> dict:
        """只测选定任务渠道的连通性；逐个发送、成功即停，不执行识别竞速。"""
        if not isinstance(route, str) or route not in _VISION_ROUTES:
            return {
                "ok": False,
                "route": route,
                "detail": "未知视觉任务",
                "attempted": 0,
                **_VISION_CALL_AUDIT,
                "sent": False,
                "not_sent_reason": "invalid_route",
            }
        info, provs = self._resolve_vision_route(route)
        results = []
        audit = {
            "route": route,
            "route_info": info,
            "attempted": 0,
            **_VISION_CALL_AUDIT,
            "sent": False,
            "attempts": results,
            "test_mode": "sequential_connectivity",
            "notice": "仅按顺序逐个做连通性测试、成功即停；不执行截图识别或识别竞速。"
            + _VISION_CALL_AUDIT["notice"],
            "not_sent_provider_ids": list(info["selected_provider_ids"]),
            "not_sent_reason": "no_candidates",
        }
        if not provs:
            return {"ok": False, "detail": info["error"] or "没有可用的多模态渠道", **audit}
        tries = []
        for prov in provs[: info["limits"]["live_max_tries"]]:
            # 停用或等待锁期间停用后，下一路尚未发送的请求也要拒绝。
            if self._gone() is not None:
                audit["not_sent_reason"] = "panel_closed"
                return {"ok": False, "detail": "面板已停用，未继续发送", **audit}
            pid = public_error_text(self._pid(prov))
            audit["attempted"] += 1
            audit["not_sent_provider_ids"] = info["selected_provider_ids"][audit["attempted"] :]
            audit["sent"] = True
            audit["actual_http_requests"] = None
            audit["not_sent_reason"] = ""
            attempt = {"provider_id": pid, "status": "started"}
            results.append(attempt)
            try:
                resp = await asyncio.wait_for(
                    prov.text_chat(
                        prompt="这是连通性测试，请只回复 OK",
                        session_id=f"sdjk-ping-{uuid.uuid4().hex[:8]}",
                        image_urls=[_PING_PNG],
                    ),
                    30,
                )
            except asyncio.TimeoutError:
                attempt["status"] = "timeout"
                tries.append(f"{pid} 30 秒无响应")
                continue
            except Exception as exc:  # noqa: BLE001
                attempt["status"] = "error"
                attempt["error"] = self._vision_failure(exc)
                tries.append(f"{pid} 失败：{attempt['error']}")
                continue
            text = str(getattr(resp, "completion_text", "") or "").strip()
            if text:
                attempt["status"] = "ok"
                if audit["not_sent_provider_ids"]:
                    audit["not_sent_reason"] = "success_stop"
                # 仅显示固定测试期望值；其它非空内容不回显，避免模型/代理返回敏感配置。
                reply = "OK" if text.upper() == "OK" else "已收到非空响应"
                head = f"{pid} 回复：{reply}"
                return {
                    "ok": True,
                    "detail": "；".join([head, *(f"先试 {t}" for t in tries)]),
                    **audit,
                }
            attempt["status"] = "empty"
            tries.append(f"{pid} 返回空")
        if audit["not_sent_provider_ids"]:
            audit["not_sent_reason"] = "budget_limit"
        return {"ok": False, "detail": "；".join(tries)[:600], **audit}

    # ---- 依赖与资源 ----
    def _font_probe(self, path: Path) -> dict:
        from .render import pick_cjk_face

        try:
            st = path.stat()
        except OSError:
            return {}
        key = (str(path), st.st_mtime, st.st_size)
        if key not in self._font_info:
            try:
                face = pick_cjk_face(path)
            except Exception:  # noqa: BLE001
                face = None
            self._font_info[key] = {
                "size_mb": round(st.st_size / 1048576, 1),
                "family": (face or {}).get("name", ""),
                "ok": face is not None,
                "symbol_missing": "".join(
                    (face or {}).get("coverage", {}).get("symbol_missing", [])
                ),
            }
        return self._font_info[key]

    def _font_library(self, r) -> list[dict]:
        from .render import _Fonts

        fonts = getattr(r, "fonts", None)
        reg, bold = getattr(fonts, "regular", None), getattr(fonts, "bold", None)
        seen: set[str] = set()
        out = []
        groups = (
            ("bundled", [p for p in _Fonts.PACKED if p.exists()]),
            ("user", _Fonts._user_fonts([r.user_font_dir])),
            ("system", [p for p in _Fonts.SYSTEM if p.exists()]),
        )
        for origin, paths_ in groups:
            for path in paths_:
                if str(path) in seen:
                    continue
                seen.add(str(path))
                info = self._font_probe(path)
                if not info:
                    continue
                out.append(
                    {
                        "id": font_id(path),
                        "file": path.name,
                        "origin": origin,
                        **info,
                        "regular": reg is not None and str(reg) == str(path),
                        "bold": bold is not None and str(bold) == str(path),
                    }
                )
        return out

    def _font_path_of(self, r, fid: str) -> Optional[Path]:
        from .render import _Fonts

        cands = list(_Fonts.PACKED) + _Fonts._user_fonts([r.user_font_dir]) + list(_Fonts.SYSTEM)
        return next((p for p in cands if p.exists() and font_id(p) == fid), None)

    def _save_appearance(self, r, update: dict) -> None:
        import json

        # 主题提交在渲染线程内执行，字体选择仍在事件循环；合并写入必须串行。
        with self._appearance_lock:
            app = r.load_appearance()
            for k, v in update.items():
                if v is None:
                    app.pop(k, None)
                else:
                    app[k] = v
            r.appearance_path.parent.mkdir(parents=True, exist_ok=True)
            paths.atomic_write_text(
                r.appearance_path, json.dumps(app, ensure_ascii=False, indent=1)
            )

    def _fonts_state(self) -> dict:
        r = getattr(self.p, "renderer", None)
        if r is not None and hasattr(r, "reload_fonts"):
            fonts = getattr(r, "fonts", None)
            # ★ ZCode 10-16：字体文件被挪走后状态仍显示旧值 ⇒ 正在用的文件不在了就重挑
            if fonts is not None and any(
                p is not None and not Path(p).exists()
                for p in (getattr(fonts, "regular", None), getattr(fonts, "bold", None))
            ):
                r.reload_fonts()
        fonts = getattr(r, "fonts", None)
        kind = r.font_kind() if r is not None and hasattr(r, "font_kind") else "none"
        fdir = r.user_font_dir if r is not None and hasattr(r, "user_font_dir") else None
        have = {}
        for name in FULL_FONTS:
            try:
                f = fdir / name if fdir else None
                have[name] = round(f.stat().st_size / 1048576, 1) if f and f.exists() else 0
            except OSError:
                have[name] = 0
        app = r.load_appearance() if r is not None and hasattr(r, "load_appearance") else {}
        return {
            "kind": kind,
            "regular": getattr(getattr(fonts, "regular", None), "name", ""),
            "bold": getattr(getattr(fonts, "bold", None), "name", ""),
            "files": have,
            "job": dict(self.font_job),
            "library": self._font_library(r) if fdir is not None else [],
            "auto": {"regular": not app.get("font_regular"), "bold": not app.get("font_bold")},
        }

    async def resources(self):
        if (g := self._gone()) is not None:
            return g
        return {"deps": deps_status(), "fonts": self._fonts_state()}

    async def deps_install(self):
        """按 requirements.txt 安装 / 升级（走 AstrBot 自己的 pip_installer：同一镜像、同一核心版本约束）。"""
        if (g := self._gone()) is not None:
            return g
        if self._deps_lock.locked():
            return {"status": "error", "message": "上一次安装还没结束"}
        try:
            from astrbot.core import pip_installer
        except Exception:  # noqa: BLE001 - 旧版 / 非 AstrBot 环境
            return {
                "status": "error",
                "message": "当前 AstrBot 版本没有提供依赖安装接口，请在后台手动安装",
            }
        async with self._deps_lock:
            t0 = time.perf_counter()
            try:
                await asyncio.wait_for(
                    pip_installer.install(requirements_path=str(REQUIREMENTS)), 900
                )
                msg, ok = "安装完成；已加载的模块要**重载插件**后才换成新版本", True
            except Exception as exc:  # noqa: BLE001
                msg, ok = f"安装失败：{type(exc).__name__}: {str(exc)[:300]}", False
            logger.info("[sdjk] 面板安装依赖：%s", msg)
            return {
                "ok": ok,
                "detail": msg,
                "ms": int((time.perf_counter() - t0) * 1000),
                "deps": deps_status(),
            }

    async def font_download(self):
        """后台下载完整字库到 plugin_data/<插件>/fonts/；页面轮询 resources 看进度。"""
        if (g := self._gone()) is not None:
            return g
        r = getattr(self.p, "renderer", None)
        if r is None or not hasattr(r, "user_font_dir"):
            return {"status": "error", "message": "渲染器不可用（Pillow 未安装？先安装依赖）"}
        if self._font_task and not self._font_task.done():
            return {"started": False, "fonts": self._fonts_state()}
        self.font_job = {
            "state": "running",
            "kind": "full",
            "file": "",
            "got": 0,
            "total": 0,
            "log": [],
        }
        self._font_task = asyncio.create_task(self._font_worker(r))
        return {"started": True, "fonts": self._fonts_state()}

    async def font_import_url(self):
        """从用户给的 URL 下载一个字体到用户字体目录（后台任务，进度同完整字库）。"""
        if (g := self._gone()) is not None:
            return g
        r = getattr(self.p, "renderer", None)
        if r is None or not hasattr(r, "user_font_dir"):
            return {"status": "error", "message": "渲染器不可用（Pillow 未安装？先安装依赖）"}
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        url = str(body.get("url") or "").strip()
        if not re.match(r"^https?://[^\s/]+/\S+$", url):
            return {"status": "error", "message": "请填 http(s) 开头的字体文件直链"}
        # ★ 2026-10-25（Astra 评估 S4）：字体直链不许指向内网 / 本机（可在配置里显式放行）
        if not bool((getattr(self.p, "cfg", {}) or {}).get("allow_private_urls", False)):
            why = await private_url_reason(url)
            if why:
                return {"status": "error", "message": f"字体地址不可用：{why}"}
        # 读取请求体 / 校验地址会让出事件循环，期间插件可能已停用。
        if (g := self._gone()) is not None:
            return g
        name = safe_font_name(url)
        if not name:
            return {"status": "error", "message": "链接要以 .ttf / .otf / .ttc 结尾"}
        if (r.user_font_dir / name).exists():
            return {"status": "error", "message": f"已有同名字体 {name}，先删掉再导入"}
        if self._font_task and not self._font_task.done():
            return {"status": "error", "message": "已有字体下载在进行"}
        self.font_job = {
            "state": "running",
            "kind": "import",
            "file": name,
            "got": 0,
            "total": 0,
            "log": [],
        }
        self._font_task = asyncio.create_task(self._import_worker(r, name, url))
        return {"started": True, "fonts": self._fonts_state()}

    async def _import_worker(self, renderer, name: str, url: str) -> None:
        import httpx

        job = self.font_job
        try:
            renderer.user_font_dir.mkdir(parents=True, exist_ok=True)
            proxy = str((getattr(self.p, "cfg", {}) or {}).get("proxy") or "") or None
            factory = self._http_factory or httpx.AsyncClient
            allow_private = bool(
                (getattr(self.p, "cfg", {}) or {}).get("allow_private_urls", False)
            )

            async def _guard(request) -> None:
                # 每一跳（含重定向后的地址）都重新校验，防止公网地址 302 到内网
                if not allow_private:
                    why = await private_url_reason(str(request.url))
                    if why:
                        raise httpx.RequestError(f"重定向目标不可用：{why}", request=request)

            kwargs = dict(
                timeout=httpx.Timeout(30.0, read=90.0),
                follow_redirects=True,
                proxy=proxy,
                event_hooks={"request": [_guard]},
            )
            if not allow_private and self._http_factory is None:
                # 自定义 transport 自己处理代理，禁止环境 mount 绕过目标固定。
                kwargs.pop("proxy")
                kwargs.update(transport=font_url_transport(proxy), trust_env=False)
                kwargs.pop("event_hooks")  # transport 校验并使用同一次解析结果。
            async with factory(**kwargs) as http:
                ok = await self._fetch_font(
                    http,
                    url,
                    renderer.user_font_dir / name,
                    job,
                    min_bytes=_IMPORT_MIN_BYTES,
                    max_bytes=_IMPORT_MAX_BYTES,
                    need_cjk=True,
                )
            job["state"] = "done" if ok else "failed"
            if ok:
                job["log"].append(f"{name} ✓ 已导入；在下面的字体列表里设为正文 / 粗体")
            logger.info("[sdjk] 面板从 URL 导入字体 %s：%s", name, job["log"][-1:])
        except asyncio.CancelledError:
            job["state"] = "failed"
            job["log"].append("已取消（插件停用 / 重载）")
            raise
        except Exception as exc:  # noqa: BLE001
            job["state"] = "failed"
            job["log"].append(f"失败：{type(exc).__name__}: {redact_error_text(str(exc))[:200]}")

    @staticmethod
    async def _upload_file():
        try:
            from astrbot.api.web import request

            files = await request.files()
        except ImportError:  # 旧版 AstrBot：Quart 风格
            from quart import request

            files = await request.files
        return files.get("file") if files else None

    async def font_upload(self):
        """上传字体文件（bridge.upload，字段名 file）；校验含中文后放进用户字体目录。"""
        if (g := self._gone()) is not None:
            return g
        r = getattr(self.p, "renderer", None)
        if r is None or not hasattr(r, "user_font_dir"):
            return {"status": "error", "message": "渲染器不可用（Pillow 未安装？先安装依赖）"}
        up = await self._upload_file()
        if (g := self._gone()) is not None:
            return g
        if up is None:
            return {"status": "error", "message": "没有收到文件"}
        name = safe_font_name(getattr(up, "filename", "") or "")
        if not name:
            return {"status": "error", "message": "只接受 .ttf / .otf / .ttc 字体文件"}
        dst = r.user_font_dir / name
        if dst.exists():
            return {"status": "error", "message": f"已有同名字体 {name}，先删掉再上传"}
        r.user_font_dir.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.part")
        try:
            data = await up.read(_IMPORT_MAX_BYTES + 1)
            if (g := self._gone()) is not None:
                return g
            if len(data) > _IMPORT_MAX_BYTES:
                return {"status": "error", "message": "文件超过 64 MB"}
            if len(data) < _IMPORT_MIN_BYTES:
                return {"status": "error", "message": "文件太小，不像是含中文的字体"}
            tmp.write_bytes(data)
            err = self._check_cjk_font(tmp)
            if err:
                return {"status": "error", "message": err}
            try:
                os.link(tmp, dst)  # 独占发布：其它上传 / 导入先完成时，保留其成果。
            except FileExistsError:
                return {"status": "error", "message": f"已有同名字体 {name}，先删掉再上传"}
        finally:
            tmp.unlink(missing_ok=True)
        logger.info("[sdjk] 面板上传字体 %s（%.1f MB）", name, dst.stat().st_size / 1048576)
        return {"saved": name, "fonts": self._fonts_state()}

    @staticmethod
    def _check_cjk_font(path: Path) -> str:
        """能打开且常用汉字都有字形 ⇒ 返回空串；否则返回中文原因。"""
        from .render import FONT_PROBE_HANZI, pick_cjk_face

        try:
            face = pick_cjk_face(path)
        except Exception as exc:  # noqa: BLE001
            return f"打不开这个字体：{exc}"
        if face is None:
            return f"这个字体缺少常用汉字（检查字：{FONT_PROBE_HANZI}），卡片会出方块，没有导入"
        return ""

    async def font_select(self):
        """把某个字体设为正文 / 粗体（id = 列表里的 id；auto = 恢复自动挑选）。立即生效。"""
        if (g := self._gone()) is not None:
            return g
        r = getattr(self.p, "renderer", None)
        if r is None or not hasattr(r, "appearance_path"):
            return {"status": "error", "message": "渲染器不可用"}
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        slot, fid = str(body.get("slot") or ""), str(body.get("id") or "")
        if slot not in ("regular", "bold", "both"):
            return {"status": "error", "message": "slot 只能是 regular / bold / both"}
        if fid == "auto":
            path = None
        else:
            path = self._font_path_of(r, fid)
            if path is None:
                return {"status": "error", "message": "找不到这个字体"}
            err = self._check_cjk_font(path)
            if err:
                return {"status": "error", "message": err}
        slots = ("regular", "bold") if slot == "both" else (slot,)
        self._save_appearance(r, {f"font_{s_}": (str(path) if path else None) for s_ in slots})
        r.reload_fonts()
        return {"fonts": self._fonts_state()}

    async def font_delete(self):
        if (g := self._gone()) is not None:
            return g
        r = getattr(self.p, "renderer", None)
        if r is None or not hasattr(r, "user_font_dir"):
            return {"status": "error", "message": "渲染器不可用"}
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        fid = str(body.get("id") or "")
        path = self._font_path_of(r, fid)
        if path is None or path.parent.resolve() != r.user_font_dir.resolve():
            return {"status": "error", "message": "只能删除自己导入 / 下载到插件数据目录的字体"}
        with self._appearance_lock:
            app = r.load_appearance()
            previous = {k: app[k] for k in ("font_regular", "font_bold") if app.get(k) == str(path)}
            if previous:
                self._save_appearance(r, {k: None for k in previous})
            try:
                path.unlink(missing_ok=True)
            except OSError:
                if previous:
                    try:
                        self._save_appearance(r, previous)
                    finally:
                        # 回滚也可能写盘失败，内存选择以实际保存下来的配置为准。
                        r.reload_fonts()
                raise
            r.reload_fonts()
        logger.info("[sdjk] 面板删除字体 %s", path.name)
        return {"deleted": path.name, "fonts": self._fonts_state()}

    async def font_sample(self):
        """字体样张（PNG data URL）：卡片上常见的字 + 生僻字 + 符号，看清这款字体长什么样、缺不缺字。"""
        if (g := self._gone()) is not None:
            return g
        r = getattr(self.p, "renderer", None)
        path = self._font_path_of(r, self._query("id", "")) if r is not None else None
        if path is None:
            return {"status": "error", "message": "找不到这个字体"}
        return {"png": await asyncio.to_thread(render_font_sample, path)}

    async def _font_worker(self, renderer) -> None:
        import httpx

        job = self.font_job
        fdir: Path = renderer.user_font_dir
        try:
            fdir.mkdir(parents=True, exist_ok=True)
            proxy = str((getattr(self.p, "cfg", {}) or {}).get("proxy") or "") or None
            timeout = httpx.Timeout(30.0, read=90.0)
            factory = self._http_factory or httpx.AsyncClient
            async with factory(timeout=timeout, follow_redirects=True, proxy=proxy) as http:
                for name, urls in FULL_FONTS.items():
                    dst = fdir / name
                    job["file"], job["got"], job["total"] = name, 0, 0
                    if dst.exists() and dst.stat().st_size >= _MIN_FONT_BYTES:
                        job["log"].append(f"{name} 已存在，跳过")
                        continue
                    repair_identity = self._font_identity(dst)
                    for url in urls:
                        if await self._fetch_font(
                            http, url, dst, job, _repair_identity=repair_identity
                        ):
                            job["log"].append(f"{name} ✓ {dst.stat().st_size / 1048576:.1f} MB")
                            break
                    else:
                        raise RuntimeError(f"{name} 所有下载源都失败")
            renderer.reload_fonts()
            bad = [n for n in FULL_FONTS if self._check_cjk_font(fdir / n)]
            job["state"] = "failed" if bad else "done"
            if bad:
                job["log"].append(f"下载完成但字体校验未通过：{'、'.join(bad)}")
            logger.info("[sdjk] 面板下载完整字库：%s", job["log"])
        except asyncio.CancelledError:
            job["state"] = "failed"
            job["log"].append("已取消（插件停用 / 重载）")
            raise
        except Exception as exc:  # noqa: BLE001
            job["state"] = "failed"
            job["log"].append(f"失败：{type(exc).__name__}: {redact_error_text(str(exc))[:200]}")
            logger.warning("[sdjk] 面板下载完整字库失败：%s", exc)

    @staticmethod
    def _font_identity(path: Path):
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    @classmethod
    async def _fetch_font(
        cls,
        http,
        url: str,
        dst: Path,
        job: dict,
        min_bytes: Optional[int] = None,
        max_bytes: Optional[int] = None,
        need_cjk: bool = False,
        _repair_identity: Any = _FONT_REPAIR_UNSET,
    ) -> bool:
        """流式下载到 .part，校验大小 + 能被 Pillow 打开（导入时还要含中文）后再改名；失败删掉半截文件。"""
        # 重载等待超时后，新旧实例仍可能重叠；各自只清理自己的下载文件。
        tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.part")
        host = url.split("/")[2]
        try:
            if min_bytes is None:
                min_bytes = _MIN_FONT_BYTES  # 运行时取（测试会临时调小）

            # 一个字体的多源回退必须沿用起始身份，不能把后来上传的文件重新授权为旧文件。
            original = (
                cls._font_identity(dst)
                if _repair_identity is _FONT_REPAIR_UNSET
                else _repair_identity
            )
            if need_cjk:
                original = None
            repair = original if original is not None and original[2] < min_bytes else None
            async with http.stream("GET", url) as resp:
                resp.raise_for_status()
                job["total"] = int(resp.headers.get("Content-Length") or 0)
                job["got"] = 0
                with open(tmp, "wb") as fh:
                    async for chunk in resp.aiter_bytes(256 * 1024):
                        fh.write(chunk)
                        job["got"] += len(chunk)
                        if max_bytes and job["got"] > max_bytes:
                            raise ValueError(f"超过 {max_bytes // 1048576} MB 上限")
            if tmp.stat().st_size < min_bytes:
                raise ValueError(f"文件过小（{tmp.stat().st_size} 字节）")
            if need_cjk:
                err = cls._check_cjk_font(tmp)
                if err:
                    raise ValueError(err)
            else:
                from PIL import ImageFont

                ImageFont.truetype(str(tmp), 24, index=0)  # 打不开就不是字库
            # 小旧文件修复依赖插件的发布在同一事件循环、同步区间内无 await；
            # 身份比较不是外部进程写文件的 CAS。其它发布用 OS 独占创建保证不覆盖。
            if repair is not None and cls._font_identity(dst) == repair:
                tmp.replace(dst)
            else:
                os.link(tmp, dst)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            job["log"].append(
                redact_error_text(f"{dst.name} ← {host} 失败：{type(exc).__name__}: {exc}")[:300]
            )
            return False
        finally:
            tmp.unlink(missing_ok=True)

    # ---- 设置 ----
    def _provider_ids(self) -> list[str]:
        try:
            provs = self.p.context.get_all_providers() or []
        except Exception:  # noqa: BLE001
            return []
        return sorted({self._pid(p) for p in provs} - {"?"})

    async def vision_prompt_presets(self):
        if (g := self._gone()) is not None:
            return {**g, "ok": False, "attempted": 0, "actual_http_requests": 0}
        preset_id, task = self._query("preset_id"), self._query("task")
        if not preset_id and not task:
            return {"ok": True, "presets": preset_catalog()}
        try:
            return {"ok": True, **preset_detail(preset_id, task)}
        except ValueError:
            return {"ok": False, "message": "请选择有效方案与任务"}

    @staticmethod
    def _compare_error(code, message, *, entered=False, **extra):
        return {
            "ok": False,
            "code": code,
            "message": message,
            "attempted": None if entered else 0,
            "sent": None if entered else False,
            "attempted_unit": "provider_text_chat_call",
            "sent_semantics": "provider_method_invoked",
            "actual_http_requests": None if entered else 0,
            **extra,
        }

    @staticmethod
    def _cancel_compare_expiry(job):
        handle = job.pop("expiry_handle", None)
        if handle is not None:
            handle.cancel()

    def _expire_compare(self, token):
        job = self._compare_jobs.get(token)
        if job is not None and job["state"] == "ready":
            self._drop_compare_snapshot(token, job)

    def _compare_prune(self):
        now = time.monotonic()
        for token, job in list(self._compare_jobs.items()):
            if job["state"] == "ready" and job["expires"] <= now:
                self._drop_compare_snapshot(token, job)
        for token, job in list(self._compare_results.items()):
            if job["expires"] <= now:
                del self._compare_results[token]

    def _drop_compare_snapshot(self, token, job):
        self._cancel_compare_expiry(job)
        self._compare_jobs.pop(token, None)
        for key in ("prepared", "provider", "original", "candidate"):
            job.pop(key, None)

    def _compare_finish(self, token, job):
        self._drop_compare_snapshot(token, job)
        if not self.closed:
            job["expires"] = time.monotonic() + _COMPARE_TTL
            self._compare_results[token] = job
            while len(self._compare_results) > 32:
                del self._compare_results[next(iter(self._compare_results))]

    async def vision_compare_status(self):
        """读取待执行、执行中或缓存的终态；查询从不启动任务。"""
        if self.closed:
            return self._compare_error("closed", "面板已关闭")
        token = self._query("token")
        job = self._compare_jobs.get(token) or self._compare_results.get(token)
        if not job or (job["state"] != "running" and job["expires"] <= time.monotonic()):
            return self._compare_error("token_unavailable", "对比任务不存在或已过期")
        result = {
            "ok": True,
            "token": token,
            "state": job["state"],
            "attempted": 0,
            "sent": False,
            **_VISION_CALL_AUDIT,
        }
        if "result" in job:
            result["result"] = {**job["result"], "cached": True}
        return result

    async def vision_compare_release(self):
        """只释放尚未执行的快照；终态缓存避免旧令牌重新发送。"""
        if self.closed:
            return self._compare_error("closed", "面板已关闭")
        body = await self._body()
        if self.closed:
            return self._compare_error("closed", "面板已关闭")
        self._compare_prune()
        token = body.get("token")
        if not isinstance(token, str):
            return self._compare_error("token_unavailable", "对比任务不存在或已过期")
        if token in self._compare_results:
            return {**self._compare_results[token]["result"], "cached": True}
        job = self._compare_jobs.get(token)
        if job is None:
            return self._compare_error("token_unavailable", "对比任务不存在或已过期")
        if job["state"] == "running":
            return self._compare_error("busy", "对比正在执行，无法释放")
        job["state"] = "discarded"
        job["result"] = {
            **self._compare_error("discarded", "未执行的对比已释放"),
            "ok": True,
            "released": True,
            "token": token,
            "cached": False,
        }
        self._compare_finish(token, job)
        return job["result"]

    async def vision_compare_prepare(self):
        if self.closed:
            return self._compare_error("closed", "面板已关闭")
        result = await self._vision_compare_prepare()
        if self.closed:
            return self._compare_error("closed", "面板已关闭")
        if not result.get("ok"):
            return self._compare_error(
                result.get("code", "invalid_prepare"),
                result.get("message", "无法准备对比"),
                **{
                    k: v
                    for k, v in result.items()
                    if k
                    not in ("ok", "code", "message", "attempted", "sent", "actual_http_requests")
                },
            )
        return result

    async def _vision_compare_prepare(self):
        """仅冻结本次图片和指令；不保存方案、不发送模型。"""
        if (g := self._gone()) is not None:
            return {**g, "ok": False, "attempted": 0, "actual_http_requests": 0}
        body = await self._body()
        if (g := self._gone()) is not None:
            return {**g, "ok": False, "attempted": 0, "actual_http_requests": 0}
        self._compare_prune()
        if len(self._compare_jobs) >= 4:
            return self._compare_error(
                "prepare_capacity", "最多保留四个待执行对比，请执行或释放后再准备"
            )
        task, pid = body.get("task"), body.get("provider_id")
        if not isinstance(task, str) or task not in TASKS or not isinstance(pid, str):
            return {"ok": False, "message": "请选择识别任务和已加载渠道"}
        try:
            provs = [p for p in self.p.context.get_all_providers() if self._pid(p) == pid]
        except Exception:
            provs = []
        if len(provs) != 1 or pid == "?":
            return {"ok": False, "message": "渠道未加载或 ID 不唯一，请刷新"}
        source = body.get("candidate_source", "draft")
        try:
            revision = None
            preset_id = None
            if source == "draft":
                view = self.p.vision_prompts.view(pid, task)
                revision = body.get("revision")
                if type(revision) is not int or revision != view["revision"]:
                    return {
                        "ok": False,
                        "code": "revision_conflict",
                        "message": "草稿版本已变化，请重新读取",
                    }
                candidate = view["draft"]
                if not candidate:
                    return {"ok": False, "message": "请先保存非空草稿"}
            elif source == "preset":
                preset_id = body.get("preset_id")
                candidate = preset_detail(preset_id, task)["prompt"]
            else:
                return {"ok": False, "message": "候选来源应为 draft 或 preset"}
            original = original_vision_prompt(task)
            if original == candidate:
                return {"ok": False, "message": "两侧提示词相同，请修改草稿或选择有覆盖的方案"}
            prepared = await asyncio.to_thread(prepare_image, body.get("image"), task)
        except (ValueError, OSError):
            return {"ok": False, "message": "草稿、固定方案或图片无效，请检查格式和大小"}
        if (g := self._gone()) is not None:
            return {**g, "ok": False, "attempted": 0, "actual_http_requests": 0}
        self._compare_prune()
        if len(self._compare_jobs) >= 4:
            return self._compare_error("prepare_capacity", "对比任务已满，请执行或释放后重试")
        token = uuid.uuid4().hex
        info = {
            "ok": True,
            "token": token,
            "expires_in": _COMPARE_TTL,
            "provider_id": pid,
            "task": task,
            "candidate_source": source,
            "preset_id": preset_id,
            "revision": revision,
            "image": {k: v for k, v in prepared.items() if k not in ("image_url", "task")},
            "prompts": {
                "original": {"sha256": hashlib.sha256(original.encode()).hexdigest()},
                "candidate": {"sha256": hashlib.sha256(candidate.encode()).hexdigest()},
            },
            "provider_call_cap": 2,
            "attempted": 0,
            "attempted_unit": "provider_text_chat_call",
            "sent": False,
            "sent_semantics": "provider_method_invoked",
            "actual_http_requests": 0,
            "notice": "执行将把这份图片交给选定渠道，两侧各调用一次；SDK 内部 HTTP 次数与费用未统计。",
        }
        self._compare_jobs[token] = {
            "state": "ready",
            "expires": time.monotonic() + _COMPARE_TTL,
            "expiry_handle": asyncio.get_running_loop().call_later(
                _COMPARE_TTL, self._expire_compare, token
            ),
            "info": info,
            "provider": provs[0],
            "prepared": prepared,
            "original": original,
            "candidate": candidate,
        }
        return info

    async def vision_compare_run(self):
        """同一准备令牌只执行一次；重发返回缓存，取消或失败也不会重放。"""
        if self.closed:
            return self._compare_error("closed", "面板已关闭")
        body = await self._body()
        if self.closed:
            return self._compare_error("closed", "面板已关闭")
        token = body.get("token")
        if body.get("confirm") is not True:
            return self._compare_error(
                "confirmation_required", "请确认图片、渠道和两次调用后再执行"
            )
        self._compare_prune()
        if not isinstance(token, str):
            return self._compare_error("token_unavailable", "对比任务不存在或已过期，请重新准备")
        if token in self._compare_results:
            result = self._compare_results[token]["result"]
            if result.get("released"):
                return self._compare_error(
                    "token_unavailable", "对比已释放，请重新准备", token=token
                )
            return {**result, "cached": True}
        job = self._compare_jobs.get(token)
        if job is None:
            return self._compare_error("token_unavailable", "对比任务不存在或已过期，请重新准备")
        if job["state"] == "running" or self._test_lock.locked():
            return self._compare_error("busy", "已有测试正在执行")
        job["state"] = "running"
        self._cancel_compare_expiry(job)
        async with self._test_lock:
            self._compare_task = asyncio.current_task()
            entered = False
            try:
                providers = self.p.context.get_all_providers() or []
                if (
                    sum(p is job["provider"] for p in providers) != 1
                    or self._pid(job["provider"]) != job["info"]["provider_id"]
                ):
                    job["state"] = "failed"
                    job["result"] = self._compare_error(
                        "provider_changed", "准备后渠道实例已变化，请重新准备", token=token
                    )
                else:
                    entered = True
                    result = await run_comparison(
                        job["provider"],
                        job["info"]["task"],
                        job["prepared"],
                        job["original"],
                        job["candidate"],
                        allowed=lambda: not self.closed,
                    )
                    job["result"] = {
                        **result,
                        "token": token,
                        "provider_id": job["info"]["provider_id"],
                        "snapshot": job["info"],
                        "cached": False,
                    }
                    job["state"] = "done"
            except asyncio.CancelledError:
                job["state"] = "cancelled"
                job["result"] = self._compare_error(
                    "cancelled", "对比已取消", entered=entered, token=token
                )
                raise
            except Exception:
                job["state"] = "failed"
                job["result"] = self._compare_error(
                    "comparison_failed", "对比未完成，请重新准备", entered=entered, token=token
                )
            finally:
                self._compare_task = None
                self._compare_finish(token, job)
        return job["result"]

    async def vision_prompts(self):
        """无参数列出任务与渠道；指定渠道、任务时返回内置原文及该渠道的独立状态。"""
        if (g := self._gone()) is not None:
            return g
        provider_id = self._query("provider_id")
        task = self._query("task")
        if not provider_id and not task:
            providers = self._provider_ids()
            store = getattr(self.p, "vision_prompts", None)
            customized, customized_error = None, None
            if store is None:
                customized_error = "已有方案存储尚未就绪，请刷新页面"
            else:
                try:
                    loaded = set(providers)
                    customized = [
                        {**row, "loaded": row["provider_id"] in loaded} for row in store.overview()
                    ]
                except (ValueError, OSError):
                    customized_error = "已有方案读取失败，请检查运行目录中的提示词配置"
            return {
                "status": "ok",
                "providers": providers,
                "tasks": [
                    {"id": key, **info, "contract_en": _VISION_CONTRACTS_EN[key]}
                    for key, info in TASKS.items()
                ],
                "max_prompt_length": MAX_PROMPT_LENGTH,
                "customized": customized,
                "customized_error": customized_error,
            }
        if task not in TASKS or not provider_id:
            return {"status": "error", "message": "请选择渠道和识别任务"}
        try:
            view = self.p.vision_prompts.view(provider_id, task)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        except OSError:
            return {"status": "error", "message": "提示词文件读取失败，请检查运行目录"}
        return {
            "status": "ok",
            "provider_id": provider_id,
            "task": task,
            "original": original_vision_prompt(task),
            "loaded": provider_id in self._provider_ids(),
            **view,
        }

    async def vision_prompt_update(self):
        """只改指定渠道和任务；草稿、启用、恢复分开，任何操作都不调用模型。"""
        if (g := self._gone()) is not None:
            return g
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        provider_id, task, action = body.get("provider_id"), body.get("task"), body.get("action")
        if not isinstance(task, str) or task not in TASKS:
            return {"status": "error", "message": "请选择有效的识别任务"}
        if action in ("save", "activate") and provider_id not in self._provider_ids():
            return {"status": "error", "message": "渠道未加载，请刷新渠道列表"}
        try:
            view = self.p.vision_prompts.apply(
                provider_id, task, action, body.get("revision"), text=body.get("text")
            )
        except PromptConflict as exc:
            return {"status": "error", "code": "revision_conflict", "message": str(exc)}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        except OSError:
            return {"status": "error", "message": "提示词文件保存失败，请检查运行目录"}
        return {"status": "ok", "provider_id": provider_id, "task": task, **view}

    async def command_stats(self):
        if (gone := self._gone()) is not None:
            return gone
        stats = getattr(self.p, "command_stats", None)
        now = time.time()

        def named_rows(days):
            rows = []
            for row in stats.summary(days, now) if stats is not None else []:
                key = row["command"]
                command = COMMAND_CATALOG.get(key)
                rows.append(
                    {
                        **row,
                        "name_zh": command.name if command else key,
                        "name_en": command.name_en if command else key,
                    }
                )
            return rows

        return {
            "retention_days": 30,
            "timezone": "UTC",
            "latency_scope": "handler",
            "latency_unit": "ms",
            "last_7_days": named_rows(7),
            "last_30_days": named_rows(30),
        }

    def _command_snapshot(self):
        disabled = _global_disabled_commands(getattr(self.p, "cfg", None))
        revision = next(reversed(self._command_history), None)
        if revision is None or disabled != self._command_history[revision]:
            revision = uuid.uuid4().hex
            self._command_history[revision] = disabled
            if len(self._command_history) > 64:
                oldest = next(iter(self._command_history))
                del self._command_history[oldest]
        return revision, disabled

    async def commands_config(self):
        if (gone := self._gone()) is not None:
            return gone
        revision, disabled = self._command_snapshot()
        # 新版页面自行回传修订号。旧页面只回传整张开关表。
        try:
            versioned = self._query("format") == "delta"
        except (ImportError, RuntimeError):  # 直接调用，无宿主 HTTP 请求上下文
            versioned = False
        if not versioned:
            # 旧客户端不带表单 ID。不同读者看到过相反状态的键，整表无法表达
            # 它是有意改回还是陈旧字段；其余键仍可按最近一次读取的基线合并。
            if self._legacy_command_form is not None:
                self._legacy_ambiguous_keys.update(self._legacy_command_form[1] ^ disabled)
            self._legacy_command_form = revision, disabled
        groups = []
        for (name, name_en), commands in _COMMAND_GROUPS.items():
            rows = []
            for command in commands:
                key = command.key
                aliases = COMMAND_ALIASES[key]
                aliases_en = COMMAND_ALIASES_EN.get(key, set()) | {
                    alias for alias in aliases if alias.isascii()
                }
                rows.append(
                    {
                        **command._asdict(),
                        "group": name,
                        "aliases": sorted(aliases - {command.name, key}),
                        "aliases_en": sorted(aliases_en - {key}),
                        "enabled": key not in disabled,
                        "locked": key in GLOBAL_PROTECTED_COMMANDS,
                    }
                )
            groups.append({"name": name, "name_en": name_en, "commands": rows})
        total = len(COMMAND_ALIASES)
        return {
            "revision": revision,
            "groups": groups,
            "disabled": sorted(disabled),
            "default_disabled": sorted(DEFAULT_DISABLED_COMMANDS),
            "counts": {
                "total": total,
                "enabled": total - len(disabled),
                "disabled": len(disabled),
                "locked": len(GLOBAL_PROTECTED_COMMANDS),
            },
        }

    async def commands_config_save(self):
        if (gone := self._gone()) is not None:
            return gone
        body = await self._body()
        if (gone := self._gone()) is not None:
            return gone
        async with self._save_lock:
            if (gone := self._gone()) is not None:
                return gone
            revision, current = self._command_snapshot()
            source = body.get("revision")
            edits = body.get("changes")
            if "changes" in body:
                if (
                    not isinstance(source, str)
                    or not isinstance(edits, dict)
                    or any(
                        key not in COMMAND_ALIASES or type(value) is not bool
                        for key, value in edits.items()
                    )
                ):
                    return {"status": "error", "message": "请提交配置修订号与改动键的禁用状态"}
            else:
                values = body.get("disabled")
                if not isinstance(values, list) or any(
                    not isinstance(key, str) or key not in COMMAND_ALIASES for key in values
                ):
                    return {"status": "error", "message": "请提交只含有效主指令键的禁用列表"}
                if GLOBAL_PROTECTED_COMMANDS.intersection(values):
                    return {"status": "error", "message": "核心必留指令无法关闭"}
                if "revision" not in body:
                    source = self._legacy_command_form[0] if self._legacy_command_form else revision
                    if (
                        source not in self._command_history
                        or self._legacy_ambiguous_keys.intersection(current ^ set(values))
                    ):
                        return {
                            "status": "error",
                            "code": "legacy_revision_required",
                            "revision": revision,
                            "disabled": sorted(current),
                            "message": "旧版整表没有修订号，无法区分其他窗口的改动。请用新版指令开关面板重新提交。",
                        }

            if not isinstance(source, str) or source not in self._command_history:
                return {
                    "status": "error",
                    "code": "revision_unavailable",
                    "revision": revision,
                    "disabled": sorted(current),
                    "message": "无法确认表单基线，请核对最新配置",
                }
            base = self._command_history[source]
            if edits is None:
                target = frozenset(values)
                edits = {key: key in target for key in base ^ target}
            if any(key in GLOBAL_PROTECTED_COMMANDS and value for key, value in edits.items()):
                return {"status": "error", "message": "核心必留指令无法关闭"}

            # 已经达到相同目标的重复提交幂等。也识别期间被改动后又改回的键。
            newer = list(self._command_history)
            touched = set().union(
                *(base ^ self._command_history[k] for k in newer[newer.index(source) + 1 :])
            )
            clashes = [key for key in edits if key in touched and edits[key] != (key in current)]
            if clashes:
                return {
                    "status": "conflict",
                    "code": "revision_conflict",
                    "revision": revision,
                    "disabled": sorted(current),
                    "conflicts": sorted(clashes),
                    "message": "其他窗口已修改同一指令：" + "、".join(sorted(clashes)),
                }
            disabled = sorted(
                (current - edits.keys()) | {key for key, value in edits.items() if value}
            )
            conf = getattr(self.p, "_config", None)
            if conf is None or not callable(getattr(conf, "save_config", None)):
                return {"status": "error", "message": "无法取得插件配置，指令开关未保存"}
            changes = {"disabled_commands": disabled}
            try:
                if conf.get("disabled_commands") != disabled:
                    _write_config_changes(conf, changes)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[sdjk] 指令开关保存失败（%s）", type(exc).__name__)
                return {"status": "error", "message": "指令开关保存失败，请检查插件配置"}
            self._apply_live(changes)
            revision, _ = self._command_snapshot()
        logger.info("[sdjk] 面板保存指令开关：关闭 %d 条", len(disabled))
        return {"status": "ok", "disabled": disabled, "revision": revision}

    async def settings(self):
        if (g := self._gone()) is not None:
            return g
        schema = load_schema()
        cfg = getattr(self.p, "cfg", {}) or {}
        seen: set[str] = set()
        groups = []
        for title, keys in SETTINGS_GROUPS + [("其他", [k for k in schema])]:
            fields = []
            for k in keys:
                if k not in schema or k in seen or k in PANEL_ONLY_KEYS:
                    continue
                seen.add(k)
                spec = schema[k]
                secret = k == "damage_engine_token"
                kind = spec.get("type", "string")
                default = spec.get("default")
                value = cfg.get(k, default)
                if k == "de_worldstate_url":
                    kind = "list"
                    default = parse_de_worldstate_urls(default)
                    value = parse_de_worldstate_urls(value)
                fields.append(
                    {
                        "key": k,
                        "type": kind,
                        "label": spec.get("description", k),
                        "hint": spec.get("hint", ""),
                        "options": spec.get("options"),
                        "provider": spec.get("_special") == "select_provider",
                        "default": default,
                        "value": None if secret else value,
                        **({"secret": True, "configured": bool(cfg.get(k))} if secret else {}),
                        "reload": k in RELOAD_KEYS,
                        "platforms": FIELD_PLATFORMS.get(k, []),
                    }
                )
            if fields:
                groups.append({"title": title, "fields": fields})
        choice = cfg.get("panel_platforms") or []
        return {
            "groups": groups,
            "disabled_commands": sorted(_global_disabled_commands(cfg)),
            "providers": self._provider_ids(),
            "platform_info": {
                "detected": self._enabled_platforms(),
                "labels": PLATFORM_LABELS,
                "choice": [x for x in choice if x in PLATFORM_LABELS]
                if isinstance(choice, list)
                else [],
            },
        }

    def _enabled_platforms(self) -> list[str]:
        """AstrBot 里已加载的聊天平台类型（去重、按 PLATFORM_LABELS 顺序；读不到时返回空）。"""
        try:
            insts = self.p.context.platform_manager.get_insts() or []
        except Exception:  # noqa: BLE001 - 旧宿主或测试桩没有 platform_manager
            return []
        names = set()
        for inst in insts:
            try:
                names.add(str(inst.meta().name))
            except Exception:  # noqa: BLE001
                continue
        return [k for k in PLATFORM_LABELS if k in names]

    def _migration_plan(self, operation="migrate"):
        if operation not in ("migrate", "rollback"):
            raise ValueError("未知迁移动作")
        conf = getattr(self.p, "_config", None)
        if conf is None or not hasattr(conf, "save_config"):
            raise ValueError("拿不到插件配置对象")
        keys = (
            "vision_provider_id",
            "vision_provider_id_riven",
            "vision_provider_id_line",
            "vision_provider_id_scan",
        )
        values = {key: conf.get(key, "") for key in keys}
        if any(not isinstance(value, str) for value in values.values()):
            raise ValueError("渠道配置格式异常，请先检查配置")
        flag = conf.get("vision_routes_isolated", False)
        if type(flag) is not bool:
            raise ValueError("隔离状态格式异常，请先检查配置")
        cfg = getattr(self.p, "cfg", {})
        if (
            any(cfg.get(key, "") != value for key, value in values.items())
            or cfg.get("vision_routes_isolated", False) != flag
        ):
            raise ValueError("运行配置与持久配置不一致，请刷新或重载后再迁移")
        target = {key: value.strip() for key, value in values.items()}
        clean = {}
        if operation == "migrate":
            for key in ("vision_provider_id_riven", "vision_provider_id_scan"):
                if not target[key] and target["vision_provider_id"]:
                    target[key] = target["vision_provider_id"]
                    clean[key] = target[key]
            if not target["vision_provider_id_line"] and target["vision_provider_id_riven"]:
                target["vision_provider_id_line"] = target["vision_provider_id_riven"]
                clean["vision_provider_id_line"] = target["vision_provider_id_line"]
        available = {}
        resolve = getattr(self.p, "_try_get_provider", None)
        for key in keys[1:]:
            try:
                available[key] = bool(target[key] and callable(resolve) and resolve(target[key]))
            except Exception:  # noqa: BLE001 - 渠道解析失败只阻止迁移，不进行外呼
                available[key] = False
        blocked = [key for key in keys[1:] if not available[key]] if operation == "migrate" else []
        already = flag if operation == "migrate" else not flag
        if already:
            clean = {}
        elif not blocked:
            clean["vision_routes_isolated"] = operation == "migrate"
        snapshot = {
            "values": values,
            "flag": flag,
            "operation": operation,
            "available": available,
            "changes": clean,
        }
        revision = hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return {
            "ok": True,
            "operation": operation,
            "isolated": flag,
            "phase": "isolated" if flag else "legacy_pending",
            "revision": revision,
            "changes": clean,
            "current": values,
            "available": available,
            "requires_selection": blocked,
            "can_apply": not already and not blocked,
            "already_applied": already,
            "legacy_retained": True,
            "attempted": 0,
            "sent": False,
            "message": "旧兼容值保留；只复制留空的专用项，不猜测自动枚举的渠道。迁移后专用项留空就停止该任务。机器总并发预算仍共享。",
        }

    async def vision_migration_status(self):
        if self.closed:
            return {"ok": False, "code": "closed"}
        try:
            return self._migration_plan()
        except (ValueError, OSError) as exc:
            return {"ok": False, "code": "invalid_config", "message": str(exc)}

    async def vision_migration_prepare(self):
        if self.closed:
            return {"ok": False, "code": "closed"}
        body = await self._body()
        if self.closed:
            return {"ok": False, "code": "closed"}
        try:
            plan = self._migration_plan(body.get("operation", "migrate"))
        except (ValueError, OSError) as exc:
            return {"ok": False, "code": "invalid_config", "message": str(exc)}
        if not plan["can_apply"]:
            return {
                **plan,
                "code": "already_applied" if plan["already_applied"] else "selection_required",
            }
        now = time.monotonic()
        self._migration_previews = {
            token: row for token, row in self._migration_previews.items() if row["expires"] > now
        }
        if len(self._migration_previews) >= 4:
            return {"ok": False, "code": "busy", "message": "待确认迁移过多，请稍后重新预览"}
        token = uuid.uuid4().hex
        self._migration_previews[token] = {"plan": plan, "expires": now + 180}
        return {**plan, "token": token, "expires_in": 180}

    async def vision_migration_release(self):
        """幂等释放指定预览；不读写配置，也不影响其它窗口。"""
        if self.closed:
            return {"ok": False, "code": "closed"}
        body = await self._body()
        if self.closed:
            return {"ok": False, "code": "closed"}
        token = body.get("token")
        if not isinstance(token, str):
            return {"ok": False, "code": "token_unavailable"}
        released = self._migration_previews.pop(token, None) is not None
        return {"ok": True, "released": released}

    async def vision_migration_apply(self):
        if self.closed:
            return {"ok": False, "code": "closed"}
        body = await self._body()
        if self.closed:
            return {"ok": False, "code": "closed"}
        if body.get("confirmed") is not True:
            return {"ok": False, "code": "confirmation_required"}
        token = body.get("token")
        if not isinstance(token, str):
            return {"ok": False, "code": "token_unavailable"}
        pending = self._migration_previews.get(token)
        if pending is None or pending["expires"] <= time.monotonic():
            self._migration_previews.pop(token, None)
            return {"ok": False, "code": "token_unavailable"}
        if (
            self._save_lock.locked()
            or self._test_lock.locked()
            or getattr(self.p, "_ocr_busy", None)
        ):
            return {
                "ok": False,
                "code": "busy",
                "message": "设置正在保存或识图正在进行，请结束后重新确认",
            }
        async with self._save_lock:
            self._migration_previews.pop(token, None)
            expected = pending["plan"]
            try:
                plan = self._migration_plan(expected["operation"])
            except (ValueError, OSError) as exc:
                return {"ok": False, "code": "invalid_config", "message": str(exc)}
            if (
                plan["revision"] != expected["revision"]
                or body.get("revision") != expected["revision"]
            ):
                return {
                    "ok": False,
                    "code": "revision_conflict",
                    "message": "配置或渠道可用性已变化，请重新预览",
                }
            if not plan["can_apply"]:
                return {"ok": False, "code": "selection_required"}
            conf = self.p._config
            clean = plan["changes"]
            previous = {key: conf[key] for key in clean if key in conf}
            try:
                conf.update(clean)
                conf.save_config()
            except Exception:  # noqa: BLE001 - 与普通设置保存同口径，仅回滚本次内存键
                for key in clean:
                    if key in previous:
                        conf[key] = previous[key]
                    else:
                        conf.pop(key, None)
                return {
                    "ok": False,
                    "code": "save_failed",
                    "message": "配置保存失败，未改变运行状态；请检查持久配置后重新预览",
                }
            self._apply_live(clean)
            self._migration_previews.clear()
            logger.info("[sdjk] 管理员确认视觉路由%s：%s", plan["operation"], sorted(clean))
            return {
                "ok": True,
                "operation": plan["operation"],
                "isolated": clean["vision_routes_isolated"],
                "saved": sorted(clean),
                "legacy_retained": True,
                "attempted": 0,
                "sent": False,
            }

    async def panel_language(self):
        if (g := self._gone()) is not None:
            return g
        cfg = getattr(self.p, "cfg", None) or {}
        language = cfg.get(PANEL_LANGUAGE)
        return {
            "language": language if language in ("cn", "en") else "",
            "sync_card_language": cfg.get(PANEL_LANGUAGE_INITIALIZED) is not True,
        }

    async def settings_save(self):
        """校验后写回插件配置（AstrBotConfig.save_config）；能即时生效的同步到运行中的实例。"""
        if (g := self._gone()) is not None:
            return g
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        initial = "initial_language" in body
        if initial:
            language = body["initial_language"]
            if "changes" in body or language not in ("cn", "en"):
                return {"status": "error", "message": "首次语言选择无效"}
            changes = {PANEL_LANGUAGE: language}
        else:
            changes = body.get("changes")
        if not isinstance(changes, dict) or not changes:
            return {"status": "error", "message": "没有要保存的改动"}
        schema = load_schema()
        clean, errors = {}, {}
        for k, v in changes.items():
            if k == PANEL_LANGUAGE and v not in ("cn", "en"):
                errors[k] = "界面语言无效"
                continue
            if k == PANEL_LANGUAGE_INITIALIZED:
                errors[k] = "该项由首次语言选择管理"
                continue
            if k == "vision_routes_isolated":
                errors[k] = "请通过迁移预览与确认操作更改隔离状态"
                continue
            if k == "disabled_commands":
                errors[k] = "请通过指令开关页面管理"
                continue
            if k not in schema:
                errors[k] = "未知配置项"
                continue
            try:
                clean[k] = coerce_setting(schema[k], v, key=k)
            except ValueError as exc:
                errors[k] = str(exc)
        if errors:
            return {"status": "error", "message": "；".join(f"{k}：{v}" for k, v in errors.items())}
        if self._save_lock.locked():
            return {"status": "error", "message": "上一次保存还没结束"}
        async with self._save_lock:
            conf = getattr(self.p, "_config", None)
            if conf is None or not hasattr(conf, "save_config"):
                return {
                    "status": "error",
                    "message": "拿不到插件配置对象，请在 AstrBot 的插件配置页修改",
                }
            # 已开着的旧首选弹框只能读取先保存的偏好，不能覆盖另一窗口的新设置。
            prior_language = conf.get(PANEL_LANGUAGE)
            if initial and prior_language in ("cn", "en"):
                return {"saved": [], "need_reload": [], PANEL_LANGUAGE: prior_language}
            if (
                PANEL_LANGUAGE in clean
                and prior_language not in ("cn", "en")
                and conf.get(PANEL_LANGUAGE_INITIALIZED) is not True
            ):
                clean.setdefault("default_language", clean[PANEL_LANGUAGE])
            saved = sorted(clean)
            if "default_language" in clean:
                clean[PANEL_LANGUAGE_INITIALIZED] = True
            self._migration_previews.clear()
            previous_port = damage_engine.portable_port(getattr(self.p, "cfg", None))
            try:
                _write_config_changes(conf, clean)
            except Exception as exc:  # noqa: BLE001
                return {"status": "error", "message": f"写配置失败：{exc}"}
            self._apply_live(clean)
            port = damage_engine.portable_port(getattr(self.p, "cfg", None))
            if port != previous_port and (service := getattr(self.p, "damage_engine", None)):
                await service.reconfigure_portable(port)
        need = [k for k in saved if k in RELOAD_KEYS]
        logger.info("[sdjk] 面板保存设置：%s（需重载 %s）", saved, need)
        result = {"saved": saved, "need_reload": need}
        if PANEL_LANGUAGE in clean:
            result[PANEL_LANGUAGE] = clean[PANEL_LANGUAGE]
        return result

    def _apply_live(self, clean: dict) -> None:
        """同步到运行中的实例：cfg 快照 + 几个在 __init__ 里缓存成属性的值。"""
        p = self.p
        cfg = getattr(p, "cfg", None)
        if isinstance(cfg, dict):
            cfg.update(clean)
        if "render_mode" in clean:
            p.render_mode = clean["render_mode"]
        if "page_size" in clean and clean["page_size"] > 0:
            p.page_size = clean["page_size"]
        if "push_interval" in clean and getattr(p, "push", None) is not None:
            p.push.interval = max(15, clean["push_interval"])
        if "display_timezone" in clean and getattr(p, "push", None) is not None:
            p.push.display_timezone = clean["display_timezone"]
        client = getattr(p, "client", None)
        if client is None:
            return
        source_changed = False
        for key in ("allow_private_urls", "wfhelper_valence"):
            if key in clean and bool(clean[key]) != getattr(client, key, False):
                setattr(client, key, bool(clean[key]))
                source_changed = True
        if "community_data_url" in clean:
            from .api_client import normalize_community_base

            base = normalize_community_base(clean["community_data_url"])
            if base != getattr(client, "community_base", ""):
                client.community_base = base
                source_changed = True
                client._comm_good_host = None
                client._comm_cool = {}
                client.community_log = {}
                cache = getattr(client, "cache", None)
                if hasattr(cache, "drop_prefix"):
                    cache.drop_prefix("community:")
        if source_changed:
            client._community_generation = getattr(client, "_community_generation", 0) + 1

    # ---- 订阅操作 ----
    def _umo_of(self, key: str) -> Optional[str]:
        umos = {s.umo for s in self.p.subs.all()}
        try:
            groups = getattr(self.p, "groups", None)
            umos |= set(groups.all()) if groups is not None else set()
        except Exception:  # noqa: BLE001
            pass
        return next((u for u in umos if session_key(u) == key), None)

    async def sub_remove(self):
        if (g := self._gone()) is not None:
            return g
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        sid = str(body.get("sid") or "")
        if not re.fullmatch(r"[0-9a-f]{4,32}", sid):
            return {"status": "error", "message": "订阅 ID 不合法"}
        n = await self.p.subs.remove(lambda s: s.sid == sid)
        logger.info("[sdjk] 面板删除订阅 %s：%d 条", sid, n)
        if not n:
            return {"status": "error", "message": "没找到这条订阅（可能已过期被清理）"}
        return {"removed": n}

    async def group_push(self):
        if (g := self._gone()) is not None:
            return g
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        umo = self._umo_of(str(body.get("key") or ""))
        on = body.get("push")
        if umo is None or not isinstance(on, bool):
            return {"status": "error", "message": "会话或开关值不合法"}
        groups = getattr(self.p, "groups", None)
        if groups is None:
            return {"status": "error", "message": "群设置不可用"}
        await groups.set_switch(umo, "push", on)
        logger.info("[sdjk] 面板%s群推送：%s", "开启" if on else "关闭", mask_umo(umo))
        return {"push": on}

    async def push_resume(self):
        """手动恢复被「连续失败暂停」的会话推送：解除暂停并清零失败计数。"""
        if (g := self._gone()) is not None:
            return g
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        umo = self._umo_of(str(body.get("key") or ""))
        if umo is None:
            return {"status": "error", "message": "会话不合法"}
        groups = getattr(self.p, "groups", None)
        if groups is None:
            return {"status": "error", "message": "群设置不可用"}
        await groups.set_pause(umo, None)
        push = getattr(self.p, "push", None)
        mem = getattr(push, "_pause", None)
        if isinstance(mem, dict):
            mem.pop(umo, None)  # 守护内存镜像同步（下次 tick 立刻恢复发送）
        fails = getattr(push, "_fail_count", None)
        if isinstance(fails, dict):
            fails.pop(umo, None)
        logger.info("[sdjk] 面板手动恢复群推送：%s", mask_umo(umo))
        return {"resumed": True}

    # ---- 主题 ----
    def _renderer(self):
        r = getattr(self.p, "renderer", None)
        return r if r is not None and hasattr(r, "reload_theme") else None

    async def _locked(self, fn):
        """在 main 的渲染锁里跑（主题是模块级状态：预览「套用→渲染→还原」不能和正常出图交错）。"""
        cls = type(self.p)
        lock = getattr(cls, "_render_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            try:
                cls._render_lock = lock
            except Exception:  # noqa: BLE001
                pass

        async def run():
            async with lock:
                if self.closed:
                    raise _PanelClosed
                return await asyncio.to_thread(fn)

        # HTTP 请求取消不能停止工作线程；由独立任务持锁，直到主题恢复与文件清理完成。
        task = asyncio.create_task(run())
        self._render_tasks.add(task)

        def finished(done):
            self._render_tasks.discard(done)
            if (
                not done.cancelled()
                and (exc := done.exception()) is not None
                and not isinstance(exc, _PanelClosed)
            ):
                logger.warning("[sdjk] 面板渲染操作失败：%s", type(exc).__name__)

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def _theme_candidate(self, body: dict) -> dict:
        from .render import BG_FITS, BG_POSITIONS, THEME_SLOTS, _clamp_scale, parse_hex_color

        keys = {k for k, *_ in THEME_SLOTS}
        colors = {}
        for k, v in (body.get("colors") or {}).items():
            if k in keys and parse_hex_color(v):
                colors[k] = "#%02x%02x%02x" % parse_hex_color(v)

        def _f(name, lo, hi, dflt):
            try:
                return max(lo, min(hi, float(body.get(name, dflt))))
            except (TypeError, ValueError):
                return dflt

        return {
            "colors": colors,
            "bg_dim": _f("bg_dim", 0.0, 0.95, 0.55),
            "panel_alpha": _f("panel_alpha", 0.3, 1.0, 1.0),
            "bg_image": bool(body.get("use_bg")),
            "logo": bool(body.get("use_logo")),
            "bg_fit": body.get("bg_fit") if body.get("bg_fit") in BG_FITS else "cover",
            "bg_pos": body.get("bg_pos") if body.get("bg_pos") in BG_POSITIONS else "c",
            "bg_scale": _clamp_scale(body.get("bg_scale", 60)),
        }

    async def theme(self):
        if (g := self._gone()) is not None:
            return g
        from .render import THEME_SLOTS, theme_defaults

        r = self._renderer()
        if r is None:
            return {"status": "error", "message": "渲染器不可用（Pillow 未安装？先安装依赖）"}

        def snapshot():
            with self._appearance_lock:
                return (
                    r.load_appearance().get("theme") or {},
                    (r.theme_dir / "bg.png").is_file(),
                    (r.theme_dir / "logo.png").is_file(),
                )

        t, has_bg, has_logo = await asyncio.to_thread(snapshot)
        if (g := self._gone()) is not None:
            return g
        dflt = theme_defaults()
        cur = t.get("colors") or {}
        return {
            "slots": [
                {
                    "key": k,
                    "label": lab,
                    "where": where,
                    "group": grp,
                    "default": dflt[k],
                    "value": cur.get(k, dflt[k]),
                }
                for k, lab, where, _c, grp in THEME_SLOTS
            ],
            "bg_dim": t.get("bg_dim", 0.55),
            "panel_alpha": t.get("panel_alpha", 1.0),
            "use_bg": bool(t.get("bg_image")),
            "use_logo": bool(t.get("logo")),
            "bg_fit": t.get("bg_fit") or "cover",
            "bg_pos": t.get("bg_pos") or "c",
            "bg_scale": t.get("bg_scale", 60),
            "has_bg": has_bg,
            "has_logo": has_logo,
            "presets": THEME_PRESETS,
        }

    def _draft_images(self, versions) -> dict[str, bytes]:
        if not isinstance(versions, dict):
            raise ValueError("图片草稿版本无效")
        images = {}
        for kind, token in versions.items():
            draft = self._theme_drafts.get(token) if isinstance(token, str) else None
            if draft is None or draft[0] != kind:
                raise ValueError("图片草稿已失效，请重新上传")
            images[kind] = draft[1]
        return images

    async def theme_draft_release(self):
        if (g := self._gone()) is not None:
            return g
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        tokens = body.get("tokens")
        if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
            return {"status": "error", "message": "图片草稿版本无效"}
        for token in tokens:
            self._theme_drafts.pop(token, None)
        return {"released": True}

    async def theme_preview(self):
        """用候选主题渲染一张样卡（不保存），返回 PNG data URL。"""
        if (g := self._gone()) is not None:
            return g
        r = self._renderer()
        if r is None:
            return {"status": "error", "message": "渲染器不可用（Pillow 未安装？先安装依赖）"}
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        cand = self._theme_candidate(body)
        locate = body.get("locate")

        def run():
            import base64
            from io import BytesIO
            from PIL import Image

            from . import render as R

            if self._preview_r is None:
                self._preview_r = R.ImageRenderer(r.cache_dir.parent / "cards_preview")
            images = self._draft_images(body.get("image_drafts", {}))
            options = dict(cand)
            for kind, key in (("bg", "bg_image"), ("logo", "logo")):
                if cand[key] and kind in images:
                    with Image.open(BytesIO(images[kind])) as image:
                        options[key] = image.copy()
                else:
                    options[key] = r.theme_image(kind) if cand[key] else None
            colors = options.pop("colors")
            self._preview_r.fonts = r.fonts  # 预览跟正式出图用同一套字体

            def render_sample(palette):
                R.apply_theme(palette, **options)
                sample = (
                    PREVIEW_EN if i18n.is_en() else (PREVIEW_TITLE, PREVIEW_LINES, PREVIEW_FOOT)
                )
                path = self._preview_r.render(*sample)
                if not path:
                    raise RuntimeError("渲染失败")
                try:
                    return Path(path).read_bytes()
                finally:
                    Path(path).unlink(missing_ok=True)

            def locate_slot(data, slot, color):
                from io import BytesIO

                from PIL import Image, ImageChops

                marked = render_sample({**colors, slot: color})
                with Image.open(BytesIO(data)) as base, Image.open(BytesIO(marked)) as tinted:
                    changed = (
                        base.size != tinted.size
                        or ImageChops.difference(
                            base.convert("RGB"), tinted.convert("RGB")
                        ).getbbox()
                    )
                return (marked, color) if changed else (data, None)

            saved = R.theme_state()
            cfg = getattr(self.p, "cfg", {}) or {}
            result = {}
            try:
                with i18n.use_lang(cfg.get("default_language")):
                    data = render_sample(colors)
                    if isinstance(locate, str) and locate in (defaults := R.theme_defaults()):
                        # 与同一份草稿的正常样卡对拍。色槽已是品红时改用亮青，仍可看出位置。
                        color = (
                            "#00eaff"
                            if colors.get(locate, defaults[locate]) == "#ff2bd6"
                            else "#ff2bd6"
                        )
                        data, marker = locate_slot(data, locate, color)
                        result["locator"] = {"slot": locate, "color": marker}
            finally:
                R.apply_theme(**saved)
            result["png"] = "data:image/png;base64," + base64.b64encode(data).decode()
            return result

        t0 = time.perf_counter()
        try:
            result = await self._locked(run)
        except _PanelClosed:
            return self._gone()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"预览失败：{exc}"}
        return {**result, "ms": int((time.perf_counter() - t0) * 1000)}

    async def theme_save(self):
        """保存主题到 appearance.json 并立即套用（新出的卡片马上换肤，不用重载）。"""
        if (g := self._gone()) is not None:
            return g
        r = self._renderer()
        if r is None:
            return {"status": "error", "message": "渲染器不可用"}
        from .render import theme_defaults

        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        cand = self._theme_candidate(body)
        dflt = theme_defaults()
        cand["colors"] = {k: v for k, v in cand["colors"].items() if v != dflt.get(k)}

        def commit():
            versions = body.get("image_drafts", {})
            images = self._draft_images(versions)
            for kind, key in (("bg", "bg_image"), ("logo", "logo")):
                cand[key] = cand[key] and (kind in images or (r.theme_dir / f"{kind}.png").exists())
            if not images:
                self._save_appearance(r, {"theme": cand})
                r.reload_theme()
                return
            r.theme_dir.mkdir(parents=True, exist_ok=True)
            # 渲染锁内更新整组图片、配置和缓存。备份留在同一文件系统，失败可逐个原子还原。
            with tempfile.TemporaryDirectory(dir=r.theme_dir) as tmp, self._appearance_lock:
                stage = Path(tmp)
                old_theme = r.load_appearance().get("theme")
                installed = []
                config_saved = False
                try:
                    for kind, data in images.items():
                        dst = r.theme_dir / f"{kind}.png"
                        if dst.exists():
                            shutil.copyfile(dst, stage / (kind + ".old"))
                        (stage / kind).write_bytes(data)
                        (stage / kind).replace(dst)
                        installed.append(kind)
                    self._save_appearance(r, {"theme": cand})
                    config_saved = True
                    r.reload_theme()
                except Exception:
                    for kind in reversed(installed):
                        dst, old = r.theme_dir / f"{kind}.png", stage / (kind + ".old")
                        if old.exists():
                            old.replace(dst)
                        else:
                            dst.unlink()
                    if config_saved:
                        self._save_appearance(r, {"theme": old_theme})
                        r.reload_theme()
                    raise
                for token in versions.values():
                    self._theme_drafts.pop(token, None)

        try:
            await self._locked(commit)
        except _PanelClosed:
            return self._gone()
        except (ValueError, OSError) as exc:
            return {"status": "error", "message": f"主题保存失败：{exc}"}
        logger.info("[sdjk] 面板保存主题：%s", cand)
        return {"saved": cand}

    async def _theme_upload(self, kind: str):
        if (g := self._gone()) is not None:
            return g
        r = self._renderer()
        if r is None:
            return {"status": "error", "message": "渲染器不可用"}
        up = await self._upload_file()
        if (g := self._gone()) is not None:
            return g
        if up is None:
            return {"status": "error", "message": "没有收到文件"}
        data = await up.read(_IMG_MAX_BYTES + 1)
        if (g := self._gone()) is not None:
            return g
        if len(data) > _IMG_MAX_BYTES:
            return {"status": "error", "message": "图片超过 15 MB"}

        def run():
            import io

            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                if im.width * im.height > 40_000_000:
                    raise ValueError("图片像素太多")
                im.load()
                im = im.convert("RGBA" if kind == "logo" else "RGB")
            side = _IMG_MAX_SIDE[kind]
            if max(im.size) > side:
                k = side / max(im.size)
                im = im.resize(
                    (max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS
                )
            with io.BytesIO() as png:
                im.save(png, "PNG")
                return im.size, png.getvalue()

        try:
            size, png = await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"不是能识别的图片：{exc}"}
        if (g := self._gone()) is not None:
            return g
        token = uuid.uuid4().hex
        if len(self._theme_drafts) >= 8:
            self._theme_drafts.pop(next(iter(self._theme_drafts)))
        self._theme_drafts[token] = (kind, png)
        return {"kind": kind, "size": list(size), "draft": token}

    async def theme_upload_bg(self):
        return await self._theme_upload("bg")

    async def theme_upload_logo(self):
        return await self._theme_upload("logo")

    async def theme_image_delete(self):
        if (g := self._gone()) is not None:
            return g
        r = self._renderer()
        body = await self._body()
        if (g := self._gone()) is not None:
            return g
        kind = str(body.get("kind") or "")
        if r is None or kind not in ("bg", "logo"):
            return {"status": "error", "message": "kind 只能是 bg / logo"}

        def commit():
            # 先提交配置；它失败时不动原图。unlink失败则回滚本次theme改动。
            # 同实例的字体配置合并写入不能穿过提交/回滚区间；锁内仅同步短操作。
            with self._appearance_lock:
                app = r.load_appearance()
                previous = app.get("theme")
                t = dict(previous or {})
                key = "bg_image" if kind == "bg" else "logo"
                changed = bool(t.get(key))
                if changed:
                    t[key] = False
                    self._save_appearance(r, {"theme": t})
                try:
                    (r.theme_dir / f"{kind}.png").unlink(missing_ok=True)
                except OSError:
                    if changed:
                        try:
                            self._save_appearance(r, {"theme": previous})
                        finally:
                            # 回滚本身也失败时，以实际磁盘配置为准，错误仍向上传播。
                            r.reload_theme()
                    raise
                r.reload_theme()

        try:
            await self._locked(commit)
        except _PanelClosed:
            return self._gone()
        return {"deleted": kind}
