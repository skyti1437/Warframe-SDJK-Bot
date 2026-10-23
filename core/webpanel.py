# -*- coding: utf-8 -*-
"""插件页面「SDJKBOT 运维面板」的后端（AstrBot 插件 Pages：``pages/sdjk/index.html``）。

四块功能（2026-10-16 维护者选定）：
  · 数据新鲜度   —— 各快照的时间戳与是否过期（只读）
  · 蹲订阅 / 推送 —— 订阅总览与推送守护状态（只读）
  · 手动刷新快照 —— 从公开仓 bot-data 拉社区快照（效价 / 言录使货单），**不走 FlareSolverr**
  · 连通性测试   —— FlareSolverr 探活（sessions.list，不做真实求解）/ 识卡渠道解析；
                    「真调一次模型」单列为 vision_live（会花 token，只在点按钮时跑）

口径（AstrBot 4.28.1 源码核对，2026-10-16）：
  · 页面目录 ``pages/<名>/index.html`` 自动发现，metadata.yaml **不用**声明 pages；
  · 路由必须以插件名开头：``/<PLUGIN_NAME>/<端点>``，前端 ``bridge.apiGet("<端点>")``；
  · 鉴权走 Dashboard 登录（不带 token 一律 401，ZCode 线上实测）；
  · ★ AstrBot **没有注销路由的 API**：插件停用后旧路由仍会应答（直到重启 AstrBot）
    ⇒ ``close()`` 打标记，之后写操作 / 外呼一律拒绝；重载时同路由 + 同 methods 会被新实例替换。
  · ★ 返回值顶层**别用 ``data`` 键**：bridge 取 ``response.data?.data ?? response.data``，
    顶层有 data 会被当成信封剥掉。

隐私：订阅里的 umo / created_by 含群号、QQ 号 —— 默认打码，``reveal=1`` 才给原值
（只有登录后台的管理员看得到这个页面）。
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import baro, data_registry, paths
from .api_client import (
    ACRITHIS_WEEK_NAME,
    RANKS_NAME,
    RIVEN_RT_NAME,
    RIVEN_WEEKLY_NAME,
    WarframeClient,
)
from .logging_compat import logger

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
_LIVE_MAX_TRIES = 3  # 真调最多试前 3 个渠道（首个失败就回落，和识卡链一致）


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


def _load(name: str) -> dict:
    """读运行期覆盖优先、回落包内种子的 JSON；读不到返回 {}（面板不能因一个坏文件整页挂）。"""
    try:
        data = paths.load_json(paths.read_path(name))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _origin(name: str) -> str:
    """这份快照现在用的是哪一份：运行目录里的（社区快照 / 运行期刷新）还是包内种子。"""
    try:
        run = paths.run_dir()
        if run != paths.PKG_DATA and (run / name).exists():
            return "用的是更新过的快照"
    except Exception:  # noqa: BLE001
        pass
    return "用的是随包种子"


def _row(item: str, status: str, snapshot: str, detail: str) -> dict:
    return {"item": item, "status": status, "snapshot": snapshot, "detail": detail}


def freshness(now: Optional[datetime] = None) -> list[dict]:
    """各数据快照的新鲜度（纯读盘，不联网）。"""
    now = now or datetime.now(timezone.utc)
    rows: list[dict] = []

    rot = _load("rotations.json")
    for sec, label in (("tenet", "信条元素加成"), ("coda", "终幕元素加成")):
        d = rot.get(sec) or {}
        snap = d.get("valence_snapshot") or ""
        try:
            stale = WarframeClient.valence_is_stale(d, now)
            same = WarframeClient.valence_values_stale(d, now)
        except Exception:  # noqa: BLE001 - epoch 缺失等
            stale, same = True, False
        src = d.get("valence_source") or ""
        rows.append(
            _row(
                label,
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
        )

    acw = _load(ACRITHIS_WEEK_NAME)
    exp = _parse_iso(acw.get("expiry"))
    rows.append(
        _row(
            "言录使本周货单",
            OK if exp and exp > now else STALE,
            str(acw.get("expiry") or ""),
            (f"观测于 {acw.get('observed')}；" if acw.get("observed") else "")
            + ("未到轮换时间" if exp and exp > now else "已过轮换时间，卡面不再展示本周货单"),
        )
    )

    rw = _load(RIVEN_WEEKLY_NAME)
    try:
        rw_at = datetime.fromtimestamp(float(rw["fetched"]), timezone.utc) if rw else None
    except (KeyError, TypeError, ValueError):
        rw_at = None
    rows.append(
        _row(
            "紫卡周报（DE 官方）",
            INFO,
            rw_at.isoformat(timespec="seconds") if rw_at else "",
            (f"{_age_text(rw_at, now)}；" if rw_at else "还没查询过；")
            + "超过 6 小时下次查询时自动刷新",
        )
    )

    # 紫卡实时排行快照（WM 拍卖；2026-10-23 加）——「紫卡排行」默认视图读它。
    # 判据与 WM 价格排行同口径：跑满一轮且覆盖率 ≥80% 才刷新 ts，所以 ts 陈旧
    # 只说明「还没跑完一整轮 / 上轮成功率不足」，不是采集失败（旧行照常展示）。
    rt = _load(RIVEN_RT_NAME)
    rt_at = _parse_iso(rt.get("ts"))
    try:
        rt_cov = f"{float(rt['coverage']):.0%}" if rt.get("coverage") is not None else ""
    except (TypeError, ValueError):
        rt_cov = ""
    rt_retry = len(rt.get("retry") or [])
    rt_n = len(rt.get("rows") or {})
    rows.append(
        _row(
            "紫卡实时排行（WM 拍卖）",
            (WARN if now - rt_at > timedelta(hours=12) else OK) if rt_at else INFO,
            str(rt.get("ts") or ""),
            (
                f"{_age_text(rt_at, now)}；已收录 {rt_n}/{rt.get('total', '?')} 把；"
                if rt_at
                else f"还没跑完过一整轮（已收录 {rt_n} 把）；"
            )
            + (f"上轮覆盖率 {rt_cov}（< 80% 不刷新时间戳）；" if rt_cov else "")
            + (f"待补 {rt_retry} 项（下次先补）；" if rt_retry else "")
            + "每 6 小时在凌晨 2–7 点空闲时段重建",
        )
    )

    wm = _load(RANKS_NAME)
    wm_at = _parse_iso(wm.get("ts"))
    wm_low = bool(wm.get("low_coverage"))
    try:
        wm_cov = f"{float(wm['coverage']):.0%}" if wm.get("coverage") is not None else ""
    except (TypeError, ValueError):
        wm_cov = ""
    wm_retry = len(wm.get("retry") or [])
    rows.append(
        _row(
            "WM 价格排行",
            WARN
            if wm_low
            else ((WARN if now - wm_at > timedelta(hours=48) else OK) if wm_at else INFO),
            str(wm.get("ts") or ""),
            (
                f"上轮覆盖率不足（{wm_cov} < 80%），时间未刷新，旧行照常展示；"
                if wm_low
                else (f"上轮覆盖率 {wm_cov}；" if wm_cov else "")
            )
            + (f"待补 {wm_retry} 项（下次先补）；" if wm_retry else "")
            + (
                f"{_age_text(wm_at, now)}；进度 {wm.get('done', '?')}/{wm.get('total', '?')}；"
                if wm_at
                else "还没跑完过一整轮；"
            )
            + "每 48 小时在凌晨 2–7 点空闲时段重爬",
        )
    )

    try:
        missed = baro.missed_visits(now, grace_hours=baro.STALE_GRACE_HOURS)
        recent = baro.missed_visits(now) - missed  # 宽限期内（到访中 / 刚结束）尚未收录的次数
        bsnap = baro.snapshot_date()
    except Exception:  # noqa: BLE001
        missed, recent, bsnap = -1, 0, ""
    if missed < 0:
        bdetail = "读取失败"
    elif missed:
        bdetail = f"快照后已有 {missed} 次到访超过 3 天仍未收录"
    elif recent:
        bdetail = "本次到访进行中或刚结束，wiki 通常 1–3 天内收录，过 3 天仍未收录再提示"
    else:
        bdetail = "已含最近一次到访"
    rows.append(
        _row(
            "奸商到访历史",
            INFO if missed <= 0 else WARN,
            bsnap,
            bdetail
            if missed < 0
            else bdetail + f"；{_origin('baro_history.json')}；每 3 小时检查社区快照",
        )
    )

    arb = _load("arb_ratings.json")
    rows.append(
        _row(
            "仲裁评级（社区实测）",
            INFO,
            str(arb.get("_updated") or ""),
            f"{_origin('arb_ratings.json')}；每 3 小时检查社区快照",
        )
    )
    drops = _load("drops.json")
    rows.append(_row("掉落表", INFO, str(drops.get("updated") or ""), "官方掉落表快照，随版本更新"))
    return rows


def registry_rows() -> list[dict]:
    """数据登记表判定（``core/data_registry.evaluate``；出错不拖垮整页）。

    运行期刷新 / 社区快照类看实际生效的那份（``paths.read_path``）；``tracked_files=()``：
    面板不做「未登记」扫描（不起 git 子进程），完整性由测试与打包闸门把关。"""
    try:
        return data_registry.evaluate(tracked_files=(), resolve=paths.read_path)
    except Exception:  # noqa: BLE001
        return []


def ledger_rows() -> list[dict]:
    """有效数据账本（``core/data_registry.ledger``；出错不拖垮整页）。

    逐份对照「运行目录副本 / 随包种子」，说明插件实际读到哪一份；**只读**（不改名 /
    不删除 / 不写任何数据文件）。运行目录未注入（== 包内目录）时 ``ledger()`` 返回 ``[]``。"""
    try:
        return data_registry.ledger()
    except Exception:  # noqa: BLE001
        return []


def mask_id(s: str) -> str:
    """号码打码：保留前 2 位与后 2 位（``123456789`` → ``12*****89``）。"""
    s = str(s or "")
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def mask_umo(umo: str) -> str:
    """``平台:消息类型:会话号`` 只给会话号打码。"""
    parts = str(umo or "").split(":")
    if len(parts) >= 3:
        return ":".join(parts[:-1] + [mask_id(parts[-1])])
    return mask_id(umo)


def session_key(umo: str) -> str:
    """会话的不透明键（订阅总览默认打码，操作接口按它回查真实 umo）。"""
    import hashlib

    return hashlib.sha1(str(umo).encode("utf-8")).hexdigest()[:12]


def pause_info(groups, umo: str) -> Optional[dict]:
    """该会话的「连续失败暂停」记录（面板展示用）；没有 ⇒ None。

    ★ reason 只取推送层写入的异常短文本（≤120 字），绝不拼 umo —— 面板默认打码会话号，
      暂停原因里也不能把号码带出去。
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
            items.append(
                {
                    "sid": s.sid,
                    "event": s.event,
                    "rule": s.rule,
                    "platform": s.platform,
                    "until": s.until,
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
    return {"total": len(subs), "events": events, "sessions": sessions}


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
            "accent": "#8a6a2c",
            "highlight": "#6e531f",
            "ink": "#2a2620",
            "ink_dim": "#5a5448",
        },
    },
]
# 预览样卡：覆盖各配色槽位（KV / 分区标题 / 斑马纹列表 / 遗物档位芯片 / 杜卡德金银铜 / 计时 / 注释 / 警示 / 页码 / 页脚）
PREVIEW_TITLE = "外观预览（第1/2页）"
PREVIEW_LINES = [
    "◆ 当前仲裁",
    "◆ 节点：Zeugma（火卫一）　生存　Infested",
    "◆ 剩余：3小时12分",
    "◆ 遗物与部件",
    "· [古纪] Lith A1　⟦g⟧Gyre Prime 系统蓝图⟦/g⟧　100杜卡德",
    "· [前纪] Meso K10　⟦s⟧Protea Prime 头部⟦/s⟧　45杜卡德",
    "· [中纪] Neo V8　⟦b⟧Akbolto Prime 枪管⟦/b⟧　15杜卡德",
    "· [后纪] Axi A2　满级 ★　▲ 12%　▼ 3%",
    "※ 注释面板：数据来自社区实测，仅供参考",
    "※ ⚠ 警示面板：排期表即将到期",
]
PREVIEW_FOOT = "平台：PC · 来源 外观预览"
_IMG_MAX_BYTES = 15 * 1024 * 1024
_IMG_MAX_SIDE = {"bg": 2400, "logo": 512}


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
    ("输出", ["render_mode", "page_size", "default_platform", "strip_markdown", "wiki_intro"]),
    ("推送", ["push_interval"]),
    (
        "识卡",
        ["vision_provider_id", "vision_provider_id_line", "scan_cooldown", "scan_max_concurrent"],
    ),
    ("权限", ["super_admins"]),
]
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


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def coerce_setting(spec: dict, value: Any) -> Any:
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
        return v
    if t == "float":
        try:
            return float(str(value).strip())
        except ValueError:
            raise ValueError("应为数字") from None
    if t == "list":
        if isinstance(value, str):
            value = [x for x in re.split(r"[,，\s]+", value) if x]
        if not isinstance(value, list):
            raise ValueError("应为列表")
        return [str(x).strip() for x in value if str(x).strip()]
    v = "" if value is None else str(value)
    opts = spec.get("options")
    if opts and v not in opts:
        raise ValueError(f"只能是 {' / '.join(map(str, opts))}")
    if len(v) > 2000:
        raise ValueError("太长")
    return v


class WebPanel:
    """把面板路由挂到 AstrBot（``context.register_web_api``）。"""

    def __init__(self, plugin, plugin_name: str):
        self.p = plugin
        self.name = plugin_name
        self.closed = False
        self._refresh_lock = asyncio.Lock()
        self._test_lock = asyncio.Lock()
        self._deps_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._font_task: Optional[asyncio.Task] = None
        self._font_info: dict = {}
        self._preview_r = None  # 预览专用渲染器（独立缓存目录，免得清理时误删正要发出的卡片）  # (路径, mtime, 大小) → 覆盖探测结果（ttc 逐面探测，缓存免得每次刷新重算）
        self.font_job: dict = {"state": "idle"}  # idle / running / done / failed
        self._http_factory = None  # 测试注入（httpx.MockTransport）；None = httpx.AsyncClient

    def routes(self) -> list[tuple[str, Any, list[str], str]]:
        n = self.name
        return [
            (f"/{n}/overview", self.overview, ["GET"], "SDJKBOT 面板：新鲜度 / 订阅 / 推送"),
            (f"/{n}/refresh", self.refresh, ["POST"], "SDJKBOT 面板：刷新社区快照"),
            (f"/{n}/pull", self.pull, ["POST"], "SDJKBOT 面板：从源头重抓"),
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
                f"/{n}/theme_image_delete",
                self.theme_image_delete,
                ["POST"],
                "SDJKBOT 面板：删除主题图片",
            ),
            (f"/{n}/settings", self.settings, ["GET"], "SDJKBOT 面板：读取设置"),
            (f"/{n}/settings_save", self.settings_save, ["POST"], "SDJKBOT 面板：保存设置"),
            (f"/{n}/sub_remove", self.sub_remove, ["POST"], "SDJKBOT 面板：删除订阅"),
            (f"/{n}/group_push", self.group_push, ["POST"], "SDJKBOT 面板：群推送开关"),
            (f"/{n}/push_resume", self.push_resume, ["POST"], "SDJKBOT 面板：恢复被暂停的群推送"),
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

    def close(self) -> None:
        self.closed = True
        if self._font_task and not self._font_task.done():
            self._font_task.cancel()

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
    async def overview(self):
        if (g := self._gone()) is not None:
            return g
        reveal = self._query("reveal", "0") == "1"
        try:
            from . import __version__ as ver
        except Exception:  # noqa: BLE001
            ver = ""
        return {
            "version": ver,
            "now": time.time(),
            "freshness": freshness(),
            # 数据登记表（来源/生产器/判定方式/新鲜度结论）；与 health / 打包闸门读同一份判据
            "registry": registry_rows(),
            # 有效数据账本：逐份对照「运行目录副本 / 随包种子」（只读）
            "ledger": ledger_rows(),
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

    async def refresh(self):
        """从公开仓 bot-data 拉社区快照（效价 + 言录使货单）。只在本地已过期时才会写盘。"""
        if (g := self._gone()) is not None:
            return g
        if self._refresh_lock.locked():
            return {"status": "error", "message": "上一次刷新还没结束"}
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
        """从源头重抓（2026-10-18 用户：不能只靠定时）：不管本地新不新鲜，立即从 wiki 源页重抓
        元素加成、言录使货单。校验照旧（数值未更新 / 货单目录），校验不过不落盘；
        源页抓不到时各自按原兜底链回落。"""
        if (g := self._gone()) is not None:
            return g
        if self._refresh_lock.locked():
            return {"status": "error", "message": "上一次刷新还没结束"}
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

    async def conntest(self):
        if (g := self._gone()) is not None:
            return g
        target = str((await self._body()).get("target") or "")
        if target not in ("flare", "vision", "vision_live"):
            return {"status": "error", "message": f"未知测试项：{target}"}
        if self._test_lock.locked():
            return {"status": "error", "message": "上一项测试还没结束"}
        async with self._test_lock:
            t0 = time.perf_counter()
            if target == "flare":
                res = await self._test_flare()
            elif target == "vision":
                res = self._test_vision_resolve()
            else:
                res = await self._test_vision_live()
            res["ms"] = int((time.perf_counter() - t0) * 1000)
            res["target"] = target
            return res

    async def _test_flare(self) -> dict:
        c = self.p.client
        if not getattr(c, "flare_enabled", False):
            return {"ok": False, "detail": "配置里已关闭 FlareSolverr（flaresolverr_enabled）"}
        try:
            ok = await c.flare_reachable(timeout=6.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"探测异常：{exc}"}
        url = getattr(c, "_flare_url", "") or ""
        if ok:
            return {"ok": True, "detail": f"可连：{url}"}
        tried = "、".join(getattr(c, "_flare_urls", []) or [])
        return {"ok": False, "detail": f"候选地址都连不上：{tried or '（无）'}"}

    @staticmethod
    def _pid(prov) -> str:
        try:
            return str(prov.meta().id)
        except Exception:  # noqa: BLE001
            return "?"

    def _test_vision_resolve(self) -> dict:
        """只解析渠道，不调模型（零成本）。"""
        try:
            main = [self._pid(p) for p in self.p._vision_providers()]
            line = [self._pid(p) for p in self.p._line_vision_providers()]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"解析渠道异常：{exc}"}
        if not main:
            return {
                "ok": False,
                "detail": "没有可用的多模态渠道：请在配置 vision_provider_id 填一个",
            }
        return {
            "ok": True,
            "detail": f"识卡主渠道（按优先级）：{'、'.join(main)}；紫卡窄读：{'、'.join(line) or '同主渠道'}",
        }

    async def _test_vision_live(self) -> dict:
        """真调识卡渠道（32×32 图 + 一句话），按优先级逐个试、成功即停；会消耗少量 token。

        ★ 2026-10-16 线上：只调 provs[0] 时，首个渠道拒图就整项报失败，而后面 4 个渠道都是好的 ⇒
        改为与识卡链同样的回落口径，并逐个列出每个渠道的结果。
        """
        try:
            provs = self.p._vision_providers()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"解析渠道异常：{exc}"}
        if not provs:
            return {"ok": False, "detail": "没有可用的多模态渠道"}
        import uuid

        tries = []
        for prov in provs[:_LIVE_MAX_TRIES]:
            pid = self._pid(prov)
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
                tries.append(f"{pid} 30 秒无响应")
                continue
            except Exception as exc:  # noqa: BLE001
                tries.append(f"{pid} 失败：{str(exc)[:120]}")
                continue
            text = str(getattr(resp, "completion_text", "") or "").strip()
            if text:
                head = f"{pid} 回复：{text[:60]}"
                return {"ok": True, "detail": "；".join([head, *(f"先试 {t}" for t in tries)])}
            tries.append(f"{pid} 返回空")
        return {"ok": False, "detail": "；".join(tries)[:600]}

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

        app = r.load_appearance()
        for k, v in update.items():
            if v is None:
                app.pop(k, None)
            else:
                app[k] = v
        r.appearance_path.parent.mkdir(parents=True, exist_ok=True)
        paths.atomic_write_text(r.appearance_path, json.dumps(app, ensure_ascii=False, indent=1))

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
        url = str((await self._body()).get("url") or "").strip()
        if not re.match(r"^https?://[^\s/]+/\S+$", url):
            return {"status": "error", "message": "请填 http(s) 开头的字体文件直链"}
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
            async with factory(
                timeout=httpx.Timeout(30.0, read=90.0), follow_redirects=True, proxy=proxy
            ) as http:
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
            job["log"].append(f"失败：{type(exc).__name__}: {str(exc)[:200]}")

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
        if up is None:
            return {"status": "error", "message": "没有收到文件"}
        name = safe_font_name(getattr(up, "filename", "") or "")
        if not name:
            return {"status": "error", "message": "只接受 .ttf / .otf / .ttc 字体文件"}
        dst = r.user_font_dir / name
        if dst.exists():
            return {"status": "error", "message": f"已有同名字体 {name}，先删掉再上传"}
        r.user_font_dir.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        try:
            data = await up.read(_IMPORT_MAX_BYTES + 1)
            if len(data) > _IMPORT_MAX_BYTES:
                return {"status": "error", "message": "文件超过 64 MB"}
            if len(data) < _IMPORT_MIN_BYTES:
                return {"status": "error", "message": "文件太小，不像是含中文的字体"}
            tmp.write_bytes(data)
            err = self._check_cjk_font(tmp)
            if err:
                return {"status": "error", "message": err}
            tmp.replace(dst)
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
        fid = str((await self._body()).get("id") or "")
        path = self._font_path_of(r, fid)
        if path is None or path.parent.resolve() != r.user_font_dir.resolve():
            return {"status": "error", "message": "只能删除自己导入 / 下载到插件数据目录的字体"}
        app = r.load_appearance()
        self._save_appearance(
            r, {k: None for k in ("font_regular", "font_bold") if app.get(k) == str(path)}
        )
        path.unlink(missing_ok=True)
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
                    for url in urls:
                        if await self._fetch_font(http, url, dst, job):
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
            job["log"].append(f"失败：{type(exc).__name__}: {str(exc)[:200]}")
            logger.warning("[sdjk] 面板下载完整字库失败：%s", exc)

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
    ) -> bool:
        """流式下载到 .part，校验大小 + 能被 Pillow 打开（导入时还要含中文）后再改名；失败删掉半截文件。"""
        tmp = dst.with_suffix(dst.suffix + ".part")
        host = url.split("/")[2]
        try:
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
            if min_bytes is None:
                min_bytes = _MIN_FONT_BYTES  # 运行时取（测试会临时调小）
            if tmp.stat().st_size < min_bytes:
                raise ValueError(f"文件过小（{tmp.stat().st_size} 字节）")
            if need_cjk:
                err = cls._check_cjk_font(tmp)
                if err:
                    raise ValueError(err)
            else:
                from PIL import ImageFont

                ImageFont.truetype(str(tmp), 24, index=0)  # 打不开就不是字库
            tmp.replace(dst)
            return True
        except asyncio.CancelledError:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001
            job["log"].append(f"{dst.name} ← {host} 失败：{type(exc).__name__}: {str(exc)[:120]}")
            tmp.unlink(missing_ok=True)
            return False

    # ---- 设置 ----
    def _provider_ids(self) -> list[str]:
        try:
            provs = self.p.context.get_all_providers() or []
        except Exception:  # noqa: BLE001
            return []
        return sorted({self._pid(p) for p in provs} - {"?"})

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
                if k not in schema or k in seen:
                    continue
                seen.add(k)
                spec = schema[k]
                fields.append(
                    {
                        "key": k,
                        "type": spec.get("type", "string"),
                        "label": spec.get("description", k),
                        "hint": spec.get("hint", ""),
                        "options": spec.get("options"),
                        "provider": spec.get("_special") == "select_provider",
                        "default": spec.get("default"),
                        "value": cfg.get(k, spec.get("default")),
                        "reload": k in RELOAD_KEYS,
                    }
                )
            if fields:
                groups.append({"title": title, "fields": fields})
        return {"groups": groups, "providers": self._provider_ids()}

    async def settings_save(self):
        """校验后写回插件配置（AstrBotConfig.save_config）；能即时生效的同步到运行中的实例。"""
        if (g := self._gone()) is not None:
            return g
        changes = (await self._body()).get("changes")
        if not isinstance(changes, dict) or not changes:
            return {"status": "error", "message": "没有要保存的改动"}
        schema = load_schema()
        clean, errors = {}, {}
        for k, v in changes.items():
            if k not in schema:
                errors[k] = "未知配置项"
                continue
            try:
                clean[k] = coerce_setting(schema[k], v)
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
            try:
                conf.update(clean)
                conf.save_config()
            except Exception as exc:  # noqa: BLE001
                return {"status": "error", "message": f"写配置失败：{exc}"}
            self._apply_live(clean)
        need = sorted(k for k in clean if k in RELOAD_KEYS)
        logger.info("[sdjk] 面板保存设置：%s（需重载 %s）", sorted(clean), need)
        return {"saved": sorted(clean), "need_reload": need}

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
        if "wfhelper_valence" in clean and getattr(p, "client", None) is not None:
            p.client.wfhelper_valence = bool(clean["wfhelper_valence"])
        if "community_data_url" in clean and getattr(p, "client", None) is not None:
            from .api_client import normalize_community_base

            p.client.community_base = normalize_community_base(clean["community_data_url"])
            cache = getattr(p.client, "cache", None)
            if hasattr(cache, "drop_prefix"):
                cache.drop_prefix("community:")  # 换了地址：丢掉旧地址取回的缓存

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
        sid = str((await self._body()).get("sid") or "")
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
        async with lock:
            return await asyncio.to_thread(fn)

    def _theme_candidate(self, body: dict) -> dict:
        from .render import THEME_SLOTS, parse_hex_color

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
        }

    async def theme(self):
        if (g := self._gone()) is not None:
            return g
        from .render import THEME_SLOTS, theme_defaults

        r = self._renderer()
        if r is None:
            return {"status": "error", "message": "渲染器不可用（Pillow 未安装？先安装依赖）"}
        t = r.load_appearance().get("theme") or {}
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
            "has_bg": (r.theme_dir / "bg.png").exists(),
            "has_logo": (r.theme_dir / "logo.png").exists(),
            "presets": THEME_PRESETS,
        }

    async def theme_preview(self):
        """用候选主题渲染一张样卡（不保存），返回 PNG data URL。"""
        if (g := self._gone()) is not None:
            return g
        r = self._renderer()
        if r is None:
            return {"status": "error", "message": "渲染器不可用（Pillow 未安装？先安装依赖）"}
        cand = self._theme_candidate(await self._body())

        def run():
            import base64

            from . import render as R

            if self._preview_r is None:
                self._preview_r = R.ImageRenderer(r.cache_dir.parent / "cards_preview")
            saved = R.theme_state()
            try:
                R.apply_theme(
                    cand["colors"],
                    bg_image=r.theme_image("bg") if cand["bg_image"] else None,
                    bg_dim=cand["bg_dim"],
                    panel_alpha=cand["panel_alpha"],
                    logo=r.theme_image("logo") if cand["logo"] else None,
                )
                self._preview_r.fonts = r.fonts  # 预览跟正式出图用同一套字体
                path = self._preview_r.render(PREVIEW_TITLE, PREVIEW_LINES, PREVIEW_FOOT)
            finally:
                R.apply_theme(
                    saved["colors"],
                    bg_image=saved["bg_image"],
                    bg_dim=saved["bg_dim"],
                    panel_alpha=saved["panel_alpha"],
                    logo=saved["logo"],
                )
            if not path:
                raise RuntimeError("渲染失败")
            data = Path(path).read_bytes()
            Path(path).unlink(missing_ok=True)
            return "data:image/png;base64," + base64.b64encode(data).decode()

        t0 = time.perf_counter()
        try:
            png = await self._locked(run)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"预览失败：{exc}"}
        return {"png": png, "ms": int((time.perf_counter() - t0) * 1000)}

    async def theme_save(self):
        """保存主题到 appearance.json 并立即套用（新出的卡片马上换肤，不用重载）。"""
        if (g := self._gone()) is not None:
            return g
        r = self._renderer()
        if r is None:
            return {"status": "error", "message": "渲染器不可用"}
        from .render import theme_defaults

        cand = self._theme_candidate(await self._body())
        dflt = theme_defaults()
        cand["colors"] = {k: v for k, v in cand["colors"].items() if v != dflt.get(k)}
        cand["bg_image"] = cand["bg_image"] and (r.theme_dir / "bg.png").exists()
        cand["logo"] = cand["logo"] and (r.theme_dir / "logo.png").exists()
        self._save_appearance(r, {"theme": cand})
        await self._locked(r.reload_theme)
        logger.info("[sdjk] 面板保存主题：%s", cand)
        return {"saved": cand}

    async def _theme_upload(self, kind: str):
        if (g := self._gone()) is not None:
            return g
        r = self._renderer()
        if r is None:
            return {"status": "error", "message": "渲染器不可用"}
        up = await self._upload_file()
        if up is None:
            return {"status": "error", "message": "没有收到文件"}
        data = await up.read(_IMG_MAX_BYTES + 1)
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
            r.theme_dir.mkdir(parents=True, exist_ok=True)
            tmp = r.theme_dir / f"{kind}.png.part"
            im.save(tmp, "PNG")
            tmp.replace(r.theme_dir / f"{kind}.png")
            return im.size

        try:
            size = await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"不是能识别的图片：{exc}"}
        logger.info("[sdjk] 面板上传主题图片 %s %s", kind, size)
        return {"kind": kind, "size": list(size)}

    async def theme_upload_bg(self):
        return await self._theme_upload("bg")

    async def theme_upload_logo(self):
        return await self._theme_upload("logo")

    async def theme_image_delete(self):
        if (g := self._gone()) is not None:
            return g
        r = self._renderer()
        kind = str((await self._body()).get("kind") or "")
        if r is None or kind not in ("bg", "logo"):
            return {"status": "error", "message": "kind 只能是 bg / logo"}
        (r.theme_dir / f"{kind}.png").unlink(missing_ok=True)
        t = dict(r.load_appearance().get("theme") or {})
        if t.get("bg_image" if kind == "bg" else "logo"):
            t["bg_image" if kind == "bg" else "logo"] = False
            self._save_appearance(r, {"theme": t})
        await self._locked(r.reload_theme)
        return {"deleted": kind}
