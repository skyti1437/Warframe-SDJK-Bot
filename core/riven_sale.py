"""按配置网址读取紫卡出售参考，缓存仅驻留内存。"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from . import matching
from .api_client import community_static_urls, private_url_reason
from .i18n import L, is_en
from .parser import RIVEN_STAT_ZH

SNAPSHOT_NAME = "riven_sale.json"
_RAW, DEFAULT_URL, _MIRROR = community_static_urls(SNAPSHOT_NAME)
_TAB = {
    "primary": "primary",
    "secondary": "secondary",
    "melee": "melee",
    "archgun": "archgun",
    "sentinel": "robotic",
    "sentinelweapon": "robotic",
}
_MAX_BYTES = 512 * 1024
_TTL = 3600
_RETRY = 60


def source_url(value: str) -> str:
    """后台管理员可填 CSV 或 JSON 快照的 HTTP(S) 读取地址。"""
    url = value.strip() or DEFAULT_URL
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.port == 0
            or any(c.isspace() or ord(c) < 32 or c == "\\" for c in url)
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            L(
                "请填写有效的 HTTP(S) CSV/JSON 快照网址",
                "Use a valid HTTP(S) CSV/JSON snapshot URL",
            )
        ) from None
    return url


@dataclass(frozen=True)
class SaleRow:
    weapon: str
    positive: str
    negative: str
    notes: str
    category: str = ""


@dataclass(frozen=True)
class SaleResult:
    url: str
    status: str
    row: SaleRow | None = None


@dataclass(frozen=True)
class _Cache:
    rows: tuple[SaleRow, ...] | None
    expires: float
    source: str
    stale: bool = False


def _field(value):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:500]


def _parse(body: bytes) -> tuple[SaleRow, ...]:
    data = body.decode("utf-8-sig").strip()
    if data.startswith(("[", "{")):
        payload = json.loads(data)
        rows = payload.get("weapons") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("JSON 缺少 weapons 行列表")
    else:
        reader = csv.DictReader(io.StringIO(data), strict=True)
        header = {name.strip().rstrip(":").casefold(): name for name in reader.fieldnames or ()}
        columns = {
            "weapon": ("weapon", "武器"),
            "positive": ("positive", "positive stats", "正词条"),
            "negative": ("negative", "negative stats", "负词条"),
            "notes": ("notes", "备注"),
            "category": ("category", "类别"),
        }
        mapped = {
            key: next((header[n] for n in names if n in header), None)
            for key, names in columns.items()
        }
        if not all(mapped[key] for key in ("weapon", "positive", "negative")):
            raise ValueError("CSV 缺少武器、正词条或负词条列")
        rows = [{key: row.get(col) for key, col in mapped.items()} for row in reader]
    parsed = tuple(
        SaleRow(
            *(
                _field(row.get(key))
                for key in ("weapon", "positive", "negative", "notes", "category")
            )
        )
        for row in rows
        if isinstance(row, dict) and _field(row.get("weapon"))
    )
    if not any(row.positive or row.negative for row in parsed):
        raise ValueError("出售词条表没有可用记录")
    return parsed


async def _download(url: str, proxy: str | None = None) -> tuple[SaleRow, ...]:
    # 地址来自后台配置。禁止跨地址跳转，聊天输入仅在取得的表中匹配。
    async with httpx.AsyncClient(
        timeout=3, trust_env=False, follow_redirects=False, proxy=proxy
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > _MAX_BYTES:
                    raise ValueError("出售词条表超出读取上限")
                data.extend(chunk)
    return _parse(data)


def _choose(rows, weapon, tab):
    rows = [
        row
        for row in rows
        if not row.category or not tab or matching.normalize(row.category) == tab
    ]
    zh = weapon.get("zh")
    weapon = weapon["name"]
    exact = {matching.normalize(name) for name in (weapon, zh) if name}
    hits = [row for row in rows if matching.normalize(row.weapon) in exact]
    if hits:
        return hits[0] if len(hits) == 1 else None
    family = matching.family_key(weapon)
    hits = [row for row in rows if family and matching.family_key(row.weapon) == family]
    return hits[0] if len(hits) == 1 else None


class RivenSaleSource:
    """同一网址和工作表的请求合并，刷新失败时可显示有标记的旧参考。"""

    def __init__(self):
        self._cache: dict[tuple[str, str | None, bool], _Cache] = {}
        self._lock = asyncio.Lock()

    async def lookup(
        self, configured: str, weapon: dict, *, proxy=None, allow_private=False
    ) -> SaleResult:
        try:
            url = source_url(configured)
        except ValueError:
            return SaleResult("", "invalid")
        tab = _TAB.get(matching.normalize(weapon.get("category") or ""), "")
        # 换地址立即读新源。聊天输出不回显自填查询参数。
        label = urlsplit(url)._replace(query="", fragment="").geturl()
        key = (url, proxy, allow_private)
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None or time.monotonic() >= entry.expires:
                try:
                    rows, reached = await self._refresh(url, label, proxy, allow_private)
                except (httpx.HTTPError, ValueError, csv.Error, ImportError, asyncio.TimeoutError):
                    rows = entry.rows if entry else None
                    entry = _Cache(
                        rows,
                        time.monotonic() + _RETRY,
                        entry.source if entry else label,
                        stale=rows is not None,
                    )
                else:
                    entry = _Cache(rows, time.monotonic() + _TTL, reached)
                self._cache.pop(key, None)
                self._cache[key] = entry
                if len(self._cache) > 8:
                    self._cache.pop(next(iter(self._cache)))
        if entry.rows is None:
            return SaleResult(label, "unavailable")
        row = _choose(entry.rows, weapon, tab)
        return SaleResult(
            entry.source, ("stale" if entry.stale else "ok") if row else "missing", row
        )

    async def _refresh(self, url, label, proxy, allow_private):
        if url == DEFAULT_URL:
            for mirror in (DEFAULT_URL, _MIRROR, _RAW):
                try:
                    rows = await asyncio.wait_for(_download(mirror, proxy), timeout=2)
                    return rows, mirror
                except (httpx.HTTPError, ValueError, csv.Error, asyncio.TimeoutError):
                    continue
            raise ValueError("社区出售参考快照不可用")
        if not allow_private:
            if await asyncio.wait_for(private_url_reason(url), timeout=2):
                raise ValueError("出售参考网址指向私网")
        return await asyncio.wait_for(_download(url, proxy), timeout=4), label


_STAT = {
    "CD": "crit_damage",
    "CC": "crit_chance",
    "DMG": "melee_damage",
    "MS": "multishot",
    "FR": "fire_rate",
    "AS": "attack_speed",
    "RLS": "reload_speed",
    "SC": "status_chance",
    "SD": "status_duration",
    "SDMG": "status_damage",
    "CORRO": "corrosive_damage",
    "VIRAL": "viral_damage",
    "WEAKDMG": "weakpoint_damage",
    "PT": "punch_through",
    "PFS": "projectile_speed",
    "ELEC": "electric_damage",
    "TOX": "toxin_damage",
    "HEAT": "heat_damage",
    "COLD": "cold_damage",
    "EFF": "heavy_attack_efficiency",
    "SLIDE": "slide_crit",
    "FIN": "finisher_damage",
    "IC": "initial_combo",
    "RANGE": "range",
    "PUNC": "puncture_damage",
    "IMP": "impact_damage",
    "MAG": "magazine_capacity",
    "ZOOM": "zoom",
    "REC": "recoil",
}
_ZH = {
    **{short: RIVEN_STAT_ZH.get(sid, short) for short, sid in _STAT.items()},
    "DMG": "基伤",
    "CORRO": "腐蚀",
    "VIRAL": "病毒",
    "OR": "或",
}


def lines(result: SaleResult) -> list[str]:
    out = [L("◆ 出售参考", "◆ Selling reference")]
    if not result.row:
        text = {
            "invalid": L(
                "请在后台填写有效的出售参考网址", "Configure a valid selling reference URL"
            ),
            "unavailable": L(
                "出售参考源暂时无法读取", "Selling reference is temporarily unavailable"
            ),
            "missing": L("出售参考表未列出该武器", "Weapon not listed by the selling reference"),
        }
        return [*out, f"· {text[result.status]}"]

    def traits(value, *, negative=False):
        if is_en() or not value:
            return value or L("未列", "Not listed")
        return re.sub(
            r"\b[A-Za-z]+\b",
            lambda m: (
                "加后坐力"
                if negative and m.group(0).upper() == "REC"
                else _ZH.get(m.group(0).upper(), m.group(0))
            ),
            value,
        )

    row = result.row
    out.extend(
        [
            f"· {L('正词条', 'Positive traits')} {traits(row.positive)}",
            f"· {L('负词条', 'Negative traits')} {traits(row.negative, negative=True)}",
        ]
    )
    if row.notes:
        out.append(f"· {L('原表备注', 'Source note')} {row.notes}")
    if result.status == "stale":
        out.append(L("· 暂用上次读取的资料", "· Showing the last available reference"))
    return out
