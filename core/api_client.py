# -*- coding: utf-8 -*-
"""Warframe SDJKBOT 异步 API 客户端（httpx.AsyncClient，全程非阻塞）。

数据源：
- 世界状态（默认）：DE 官方 worldState.php + core/de_worldstate.py 本地适配
  （无第三方中转、无 Cloudflare 风险；赤毒/仲裁/钢铁轮换不在 DE 源内，自动降级）
- 世界状态（可选）：api.warframestat.us（worldsource=warframestat 时启用）
- 市场：warframe.market v2（物品/订单，带 zh-hans 名称）+ v1 拍卖（紫卡/玄骸，v2 尚未开放）
- Wiki：本地别名词典（core/data/aliases.json）+ Fandom MediaWiki 搜索兜底

所有请求经 TTLCache 单飞缓存；WM v2 有限速（3 req/s）内置客户端节流。
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import random
import time
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

try:
    from . import matching        # core 包内正常导入
except ImportError:               # 离线脚本把 core/ 当顶层路径导入时
    import matching

try:  # 缺依赖时保持模块可导入（离线工具/测试），实例化时再给出明确提示
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from . import de_worldstate, paths
from .cache import TTLCache
from .logging_compat import logger

DATA_DIR = Path(__file__).resolve().parent / "data"   # 包内静态数据（只读）
# 运行期写盘的文件名（排行落盘 / 快照 / 缓存）：实际路径经 core.paths 解析到
# data/plugin_data/<插件名>，绝不写插件包目录（AstrBot 插件规范要求）。
RANKS_NAME = "wm_ranks.json"             # 价格排行全量落盘（后台爬取）
WIKI_DISP_NAME = "de/wiki_disp.json"     # wiki 变体倾向快照
ACRITHIS_WEEK_NAME = "de/acrichis_week.json"   # 言录使本周货单（运行期覆盖包内种子）
ACRITHIS_CURRENT_URL = ("https://wiki.warframe.com/w/Acrithis/"
                        "Current_Offerings?action=raw")  # 社区当期 5 件上报页
# Cloudflare 绕过代理（FlareSolverr）：wiki.warframe.com 等对非浏览器 403。
# 服务器上跑一个 flaresolverr 容器后自动启用；插件跑在 astrbot 容器里，
# 127.0.0.1 到不了宿主机端口，所以按候选顺序试（容器名 → docker0 网关 → 本机）。
# ★ 这些只是**默认值**：开源版用户可以在配置面板里自己填地址
#   （``flaresolverr_urls``，逗号/换行分隔），或直接关掉（``flaresolverr_enabled``）。
FLARESOLR_URLS = ([os.environ["WF_FLARESOLR"]]
                  if os.environ.get("WF_FLARESOLR") else
                  ["http://flaresolverr:8191/v1",
                   "http://172.17.0.1:8191/v1",
                   "http://127.0.0.1:8191/v1"])
TTL_FLARE = 3600


def parse_url_list(raw: str) -> list[str]:
    """把面板里填的「地址列表」文本切成候选地址。

    支持逗号 / 换行 / 分号分隔；缺 ``/v1`` 结尾时自动补上（FlareSolverr
    的 API 端点是 ``/v1``，用户常常只填 ``http://host:8191``）。
    填了非法内容返回空列表 —— 由调用方决定回退到默认值，不静默猜。
    """
    out: list[str] = []
    for tok in re.split(r"[,;\s]+", (raw or "").strip()):
        tok = tok.strip().rstrip("/")
        if not tok:
            continue
        if not tok.startswith(("http://", "https://")):
            continue
        if not tok.endswith("/v1"):
            tok += "/v1"
        if tok not in out:
            out.append(tok)
    return out
RIVEN_WEEKLY_NAME = "riven_weekly.json"  # DE 官方紫卡周报快照
DE_RIVEN_WEEKLY = "https://www-static.warframe.com/repos/weeklyRivens{plat}.json"
_DE_RIVEN_PLATFORM = {"pc": "PC", "ps4": "PS4", "xb1": "XB1", "sw": "SWITCH"}
TTL_RIVEN_WEEKLY = 6 * 3600

DEFAULT_DE_WORLDSTATE = "https://api.warframe.com/cdn/worldState.php"
DEFAULT_WORLDSTATE_BASE = "https://api.warframestat.us"
DEFAULT_WM_V2_BASE = "https://api.warframe.market/v2"
DEFAULT_WM_BASE = "https://api.warframe.market/v1"  # 仅拍卖（v2 未开放拍卖端点）
# browse.wf 的 oracle：赏金轮换 / 扎里曼派系 / 各派系本轮节点与挑战。
# 站点对无 UA 的请求直接 403，所以必须带一个像浏览器的 UA。
# 算法开源在 github.com/calamity-inc/wf.browse.oracle（.pluto 脚本可读）。
DEFAULT_ORACLE_BASE = "https://oracle.browse.wf"
_ORACLE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# 错别字容忍：中文名/英文名的模糊命中
# ---------------------------------------------------------------------------
def fuzzy_hits(query: str, candidates: Iterable[str], n: int = 3,
               cutoff: Optional[float] = None) -> list[str]:
    """在候选名里找与 query 形近的项（支持中文错别字，如 波斯顿→伯斯顿）。

    分档阈值：>=4 字 0.70 / 3 字 0.60 / 2 字 0.50（且要求至少一个字相同，
    否则「绝路」会把「绝望」也算进来）。
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    pool = [c for c in candidates if c and abs(len(c) - len(q)) <= 2]
    if not pool:
        return []
    if cutoff is None:
        cutoff = 0.70 if len(q) >= 4 else (0.60 if len(q) == 3 else 0.50)
    hits = difflib.get_close_matches(q, pool, n=max(n * 3, n), cutoff=cutoff)
    if len(q) == 2:
        hits = [h for h in hits if set(h) & set(q)]
    return hits[:n]

# 各端点 TTL（秒）：周期类短缓存，字典类长缓存
TTL_WORLDSTATE_FAST = 30      # 裂隙/警报/周期等
TTL_WORLDSTATE_MID = 120      # 突击/奸商/电波等
TTL_DE_RAW = 30               # DE 原始包（全端点共用）
TTL_WM_ITEMS = 24 * 3600      # WM 物品字典
TTL_WM_ORDERS = 90            # 订单
TTL_WM_AUCTIONS = 90          # 紫卡拍卖
TTL_WM_STATS = 1800           # v1 价格统计（48h/90d）
TTL_WM_DUCATS = 30 * 60       # v1 tools/ducats 榜单（整点更新一次）
TTL_WIKI = 6 * 3600

USER_AGENT = "warframe-sdjk/1.0 (AstrBot plugin)"

# 内部平台码 -> WM v2 Platform 头
_WM_PLATFORM = {"pc": "pc", "ps": "ps4", "ps4": "ps4", "xb": "xbox",
                "xbox": "xbox", "sw": "switch", "switch": "switch"}

# DE 源不包含、且外部独立源（10o.io）被网络环境阻断时给出降级提示。
# 文案要求：① 说明为什么没有 ② **给出可执行的替代指令** —— 只说「不可用」
# 等于把用户堵死，本项目把「静默/无出路」视为最高优先级缺陷。
# ★ 提示里提到的每个指令名都必须是 COMMAND_ALIASES 里真实存在的，
#   否则等于把用户引到第二条死路（曾误写「钢铁」「资源」两个不存在的指令）。
_EXTERNAL_ONLY = {
    "kuva":
        "赤毒虹吸数据源（10o.io）已停摆，DE 官方数据也不含此表，暂时查不到实时虹吸。\n"
        "可用的替代：\n"
        "· 赤毒武器 / 姐妹武器的融合数值 →「融合 电60 火58」或「玄骸」\n"
        "· 在售赤毒武器价格 →「wm 赤毒 布拉玛」\n"
        "· 每周轮换奖励 →「周报」查看各周期内容",
    "arbitration":
        "仲裁实时数据源（10o.io）已停摆。\n"
        "可用替代：\n"
        "· 「仲裁」查看当前与下一小时场次（已改用 arbi.wf.wiki 确定性排期）\n"
        "· 「仲裁表」查看整周排期\n"
        "· 「仲裁 生存」按任务类型筛选",
    "steelPath":
        "钢铁之路轮换需要外部数据源，DE 直连模式下拿不到。\n"
        "可用替代：\n"
        "· 「侵袭」查看当前钢铁之路侵袭任务\n"
        "· 「裂隙 钢铁」筛选钢铁之路模式的裂隙\n"
        "· 「仲裁」查看钢铁之路仲裁场次",
}


def norm_wm_name(s: str) -> str:
    """WM 物品名的匹配归一化：去空白、小写、抹掉**独立成词**的 Prime 字样。

    「Saryn Prime 蓝图」与用户输入「Saryn蓝图」归一化后同为「saryn蓝图」。

    ★ 2026-09-20 修：以前是 ``.replace("prime", "")`` 的**子串**替换，副作用有两个：
      · 英文「Primed Smite Grineer」被切成「dsmitegrineer」（Primed 里的 prime 被吃掉）；
      · 用户输入「毁灭Gprime」被切成「毁灭g」——**Prime 语义在归一化里凭空消失**，
        于是「毁灭Gprime」反而匹配到**非 Prime** 的 smite_grineer（价格/订单全不对）。
    现在只在 prime 后面**不紧跟 ASCII 字母**时才抹（词尾、后接中文都算独立出现），
    「primed」这类单词因此保持完整。
    """
    t = re.sub(r"\s+", "", (s or "").lower())
    return re.sub(r"prime(?![a-z])", "", t)


# 部件词 -> WM slug 英文词（长词在前，避免「神经光元」被「头部神经光元」截胡）
COMPONENT_WORDS: list[tuple[str, str]] = [
    ("头部神经光元", "neuroptics"), ("神经光元", "neuroptics"),
    ("蓝图", "blueprint"), ("机体", "chassis"), ("系统", "systems"),
    ("头盔", "helmet"), ("枪管", "barrel"), ("枪机", "receiver"),
    ("枪托", "stock"), ("枪膛", "barrel"), ("刀刃", "blade"),
    ("护手", "guard"), ("刃", "blade"), ("握柄", "hilt"), ("握把", "grip"),
    ("剑柄", "hilt"), ("弓弦", "string"), ("弓臂", "limb"),
    ("连结部", "link"), ("卷线器", "coil"), ("星体", "orbiter"),
]


def split_component_query(query: str) -> Optional[tuple[str, str, str]]:
    """把「席瓦蓝图」拆成 (基名「席瓦」, 部件词「蓝图」, 部件英文词 blueprint)。

    不是「基名+部件词」结构返回 None。
    """
    q = query.strip()
    for cn, en in COMPONENT_WORDS:
        if q.endswith(cn) and len(q) > len(cn):
            return q[:-len(cn)].strip(), cn, en
    return None


def match_wm_normalized(query: str, items: list[dict]) -> Optional[dict]:
    """按归一化名称匹配 WM 物品（精确 -> 包含，包含时整套优先）。

    Returns:
        命中的物品 dict；归一化后为空或无命中返回 None。
    """
    qn = norm_wm_name(query)
    if not qn:
        return None
    # 归一化会抹掉 Prime，于是「毁灭 Grineer」与「毁灭 Grineer Prime」会塌成同一个
    # 字符串 —— 精确分支和包含分支都可能同时命中两者。统一按**输入里有没有写 Prime**
    # 裁决：写了就选 Prime 版，没写就选普通版。
    want_prime = "prime" in re.sub(r"\s+", "", (query or "").lower())

    def _score(it: dict) -> int:
        tags = set(it.get("tags") or [])
        if "set" in tags:
            return 0
        if not ({"component", "blueprint"} & tags):
            return 1
        return 2

    def _prefer(cands: list[dict]) -> list[dict]:
        if len(cands) <= 1:
            return cands
        if want_prime:
            primed = [it for it in cands if "prime" in (it.get("url_name") or "")]
            if primed:
                return primed
        else:
            plain = [it for it in cands if "prime" not in (it.get("url_name") or "")]
            if plain:
                return plain
        return cands

    exact = [it for it in items if norm_wm_name(it.get("zh")) == qn]
    if exact:
        return min(_prefer(exact), key=_score)
    exact_en = [it for it in items
                if it.get("en") and norm_wm_name(it.get("en")) == qn]
    if exact_en:
        return min(_prefer(exact_en), key=_score)
    contains = [it for it in items
                if qn in norm_wm_name(it.get("zh"))
                or (it.get("en") and qn in norm_wm_name(it.get("en")))]
    if not contains:
        return None
    return min(_prefer(contains), key=_score)


def match_official_name(query: str, items: list[dict]) -> Optional[dict]:
    """按**官方名**精确匹配：官方简中 > 官方英文 > WM slug。

    ★ 2026-09-20 加。WM 物品表自带的官方简中（`i18n.zh-hans.name`）是唯一权威，
    必须排在别名词典（黑话）**之前**：词典里存在「官方名指向别的物品」的错误映射
    （如 `'压迫点' -> serration` —— 「压迫点」其实是 Pressure Point，
    而 serration 是「膛线」），以前词典优先，用户照官方名输入会拿到完全不相关的东西。

    用 casefold 比较，因此「压迫点 prime」也能精确命中官方名「压迫点 Prime」。
    """
    q = (query or "").strip()
    if not q:
        return None
    qf = q.casefold()
    for it in items:
        if (it.get("zh") or "").strip().casefold() == qf:
            return it
    low = q.lower()
    for it in items:
        if (it.get("en") or "").strip().lower() == low:
            return it
    slug = low.replace(" ", "_")
    for it in items:
        if (it.get("url_name") or "").lower() == slug:
            return it
    return None


class WarframeAPIError(Exception):
    """统一的接口错误（含降级提示语）。"""


def _rank_candidate(it: dict) -> bool:
    """价格榜爬取候选（决定抓取规模与榜单口径）。

    * 甲：**只有套装**（WM 已不再挂整件 Prime 甲，套装才是实际交易物）；
    * 武器：整件武器 + 套装（暮斩这类非 Prime 整件也在内），排除蓝图/部件；
    * 卡 / 赋能 / 部件 / 遗物全量。
    """
    tags = set(it.get("tags") or [])
    if "mod" in tags or "arcane_enhancement" in tags \
            or "relic" in tags or "component" in tags:
        return True
    if "warframe" in tags:
        return "set" in tags
    if "weapon" in tags:
        return "blueprint" not in tags
    return False


def parse_de_riven_weekly(text: str) -> list[dict]:
    """DE 官方紫卡周报是 JS 对象字面量（裸键 + 单引号字符串），转标准 JSON 解析。

    DE 不给键名加引号、字符串用单引号；本站数据值均为武器名/数字，
    不含撇号，因此两条正则即可安全转换。
    """
    fixed = re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', text)
    fixed = re.sub(r"'([^']*)'", r'"\1"', fixed)
    return json.loads(fixed)


def load_aliases() -> dict:
    path = DATA_DIR / "aliases.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


class WarframeClient:
    def __init__(
        self,
        *,
        kuvalog_url: str = "",
        worldsource: str = "de",
        worldstate_base: str = DEFAULT_WORLDSTATE_BASE,
        de_worldstate_url: str = DEFAULT_DE_WORLDSTATE,
        wm_v2_base: str = DEFAULT_WM_V2_BASE,
        wm_base: str = DEFAULT_WM_BASE,
        timeout: float = 15.0,
        proxy: Optional[str] = None,
        cache: Optional[TTLCache] = None,
        language: str = "zh-hans",
        flare_enabled: bool = True,
        flare_urls: Optional[list[str]] = None,
    ):
        if httpx is None:
            raise WarframeAPIError("缺少依赖 httpx：请在 AstrBot 插件管理中安装依赖，"
                                   "或手动执行 pip install httpx")
        self._kuvalog_url = kuvalog_url
        self.worldsource = worldsource if worldsource in ("de", "warframestat") else "de"
        self.worldstate_base = worldstate_base.rstrip("/")
        self.de_worldstate_url = de_worldstate_url.rstrip("/")
        self.wm_v2_base = wm_v2_base.rstrip("/")
        self.wm_base = wm_base.rstrip("/")
        self.language = language
        # FlareSolverr：开关 + 候选地址（面板可填；没填就用内置默认值）
        self._flare_enabled = bool(flare_enabled)
        self._flare_urls = list(flare_urls) if flare_urls else list(FLARESOLR_URLS)
        self.cache = cache or TTLCache(maxsize=1024)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            proxy=proxy or None,
        )
        self._flare_url = ""          # FlareSolverr 可用地址（首次连通后记忆）
        # 已确认「WM 无该玄骸武器挂单类目」的 slug（返回 400），避免重复请求
        self._lich_unsupported: set = set()
        self._aliases = load_aliases()
        self._wm_lock = asyncio.Lock()
        self._wm_last = 0.0
        self._de_parsed_cache: tuple[str, dict] = ("", {})

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def http(self):
        """共享的 httpx.AsyncClient（供其他需要长连接的子模块复用连接池）。"""
        return self._http

    # ------------------------------------------------------------------
    # 底层请求：带缓存、重试与指数退避
    # ------------------------------------------------------------------
    async def _fetch_json(
        self,
        url: str,
        *,
        ttl: float,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        retries: int = 2,
        cache_key: Optional[str] = None,
        wm_rate_limit: bool = False,
    ) -> Any:
        key = cache_key or (url + "|" + json.dumps(
            params or {}, sort_keys=True, ensure_ascii=False)
            + "|" + json.dumps(headers or {}, sort_keys=True))

        async def _do() -> Any:
            last_exc: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    if wm_rate_limit:  # WM 全局限速 3 req/s（v1/v2 共享）
                        async with self._wm_lock:
                            gap = 0.33 - (time.monotonic() - self._wm_last)
                            if gap > 0:
                                await asyncio.sleep(gap)
                            self._wm_last = time.monotonic()
                    resp = await self._http.get(url, params=params, headers=headers)
                    if resp.status_code == 404:
                        raise WarframeAPIError(f"接口不存在或暂未开放：{url}")
                    if resp.status_code == 429:  # 限速：退避后重试
                        last_exc = WarframeAPIError("warframe.market 限速（3 请求/秒）")
                        if attempt < retries:
                            await asyncio.sleep(1.2 * (attempt + 1))
                            continue
                        raise last_exc
                    resp.raise_for_status()
                    return resp.json()
                except WarframeAPIError:
                    raise
                except (httpx.HTTPError, json.JSONDecodeError) as exc:
                    last_exc = exc
                    if attempt < retries:
                        await asyncio.sleep(0.6 * (attempt + 1) + random.random() * 0.3)
            raise WarframeAPIError(f"请求失败（{url}）：{last_exc}")

        return await self.cache.get_or_fetch(key, ttl, _do)

    def _ws_base(self, platform: str) -> str:
        return self.worldstate_base

    # ------------------------------------------------------------------
    # 世界状态：DE 直连模式（默认）
    # ------------------------------------------------------------------
    async def _de_raw(self) -> dict:
        return await self._fetch_json(self.de_worldstate_url, ttl=TTL_DE_RAW)

    async def _de_bundle(self) -> dict:
        raw = await self._de_raw()
        if not isinstance(raw, dict) or not raw:
            raise WarframeAPIError("DE 世界状态返回异常，请稍后重试")
        raw_key = str(raw.get("Time", "")) + str(raw.get("BuildLabel", ""))
        if self._de_parsed_cache[0] != raw_key:
            # 解析必须走线程：内部要读多个 MB 级 DE 翻译表并建索引，
            # 同步跑会把 AstrBot 的单事件循环堵住（watchdog 实测 30s+）
            parsed = await asyncio.to_thread(de_worldstate.parse_worldstate, raw)
            self._de_parsed_cache = (raw_key, parsed)
        return self._de_parsed_cache[1]

    # ------------------------------------------------------------------
    # 世界状态统一入口
    # ------------------------------------------------------------------
    async def worldstate(self, platform: str, endpoint: str,
                         ttl: float = TTL_WORLDSTATE_FAST) -> Any:
        if self.worldsource == "de":
            bundle = await self._de_bundle()
            if endpoint in ("kuva", "arbitration", "steelPath") \
                    and not bundle.get(endpoint):
                raise WarframeAPIError(_EXTERNAL_ONLY[endpoint])
            return bundle.get(endpoint)
        url = f"{self._ws_base(platform)}/{platform}/{endpoint}"
        return await self._fetch_json(url, ttl=ttl,
                                      params={"language": self.language})

    async def cycle(self, platform: str, name: str) -> dict:
        """name: cetus / vallis / cambion / earth / duviri / zariman"""
        data = await self.worldstate(platform, f"{name}Cycle", ttl=TTL_WORLDSTATE_FAST)
        if isinstance(data, dict):
            data = dict(data)
            data["_name"] = name
        return data

    async def fissures(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "fissures", ttl=45) or []

    async def sortie(self, platform: str) -> dict:
        return await self.worldstate(platform, "sortie", ttl=TTL_WORLDSTATE_MID) or {}

    async def archon_hunt(self, platform: str) -> dict:
        return await self.worldstate(platform, "archonHunt", ttl=TTL_WORLDSTATE_MID) or {}

    async def void_trader(self, platform: str) -> dict:
        return await self.worldstate(platform, "voidTrader", ttl=TTL_WORLDSTATE_MID) or {}

    async def daily_deals(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "dailyDeals", ttl=TTL_WORLDSTATE_MID) or []

    async def nightwave(self, platform: str) -> dict:
        return await self.worldstate(platform, "nightwave", ttl=TTL_WORLDSTATE_MID) or {}

    async def arbitration(self, platform: str) -> dict:
        # 仲裁实时源（10o.io）已停摆；当前词条由 _h_arbitration 的锚点推算接管
        raise WarframeAPIError(_EXTERNAL_ONLY["arbitration"])

    async def alerts(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "alerts", ttl=60) or []

    async def invasions(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "invasions", ttl=120) or []

    async def news(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "news", ttl=300) or []

    async def goals(self, platform: str) -> list[dict]:
        """限时活动（含 GracePeriod 宽限期内已结束的条目）。"""
        return await self.worldstate(platform, "goals", ttl=300) or []

    async def fetch_arbseq(self) -> dict:
        """仲裁轮换序列（arbi.wf.wiki）。

        startTs/seq 是滚动窗口（站点按当前时间切片下发），
        不能长缓存，否则时间基准过期导致排期整体错位。
        """
        data = await self._fetch_json("https://arbi.wf.wiki/api/arbitration",
                                      ttl=120, retries=1)
        if not isinstance(data, dict) or not data.get("seq"):
            raise WarframeAPIError("仲裁序列源返回异常")
        return data

    async def kuva(self, platform: str) -> list[dict]:
        # 10o.io 源已停摆（域名失效）；赤毒指令返回降级提示
        raise WarframeAPIError(_EXTERNAL_ONLY["kuva"])

    async def steel_path(self, platform: str) -> dict:
        return await self.worldstate(platform, "steelPath", ttl=600) or {}

    async def descendia(self, platform: str) -> dict:
        """沉沦之地（炼狱塔）：DE ``Descents``，每周 21 层。"""
        return await self.worldstate(platform, "descendia", ttl=900) or {}

    async def acrithis_pool(self) -> dict:
        """言录使（Acrithis）商品池（静态，来自 ExportVendors）。"""
        return await asyncio.to_thread(self._load_acrithis_pool)

    def _load_acrithis_pool(self) -> dict:
        p = Path(__file__).resolve().parent / "data" / "de" / "acrichis_pool.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def next_weekly_reset(now=None, weekday: int = 0, hour_utc: int = 0) -> str:
        """下一个「周几 HH:00 UTC」的 UTC 时刻（ISO 串）。

        各系统重置点（以 wiki《Reset》总表 + 各页实时倒计时为准）：
          · **周常（Nightwave / Circuit / Netracells / Teshin / Yonta / Cavalero /
            Acrithis 言录使）= 周一 00:00 UTC** —— Update 32.3 起各商人周常也统一到周一
          · 信条（Ergo Glast）/ 终幕（Eleanor）= 每 4 天 00:00 UTC
          · Sortie = 每日 17:00 UTC（夏令 16:00）
          · Baro = 两周一次，周五 9:00 ET（另算）

        ⚠️ 别引用 DE《Update 33.0》那句「Acrithis rotating at Sundays at 00:00 UTC」——
        那是 2023 年初版，已被后续调整覆盖；wiki 现版是 **Monday**。
        一律以 **UTC 判定**，要展示给用户就换算成北京时间（``formatters._to_bj``）。
        """
        from datetime import datetime, timedelta, timezone
        now = now or datetime.now(timezone.utc)
        days = (int(weekday) - now.weekday()) % 7
        target = (now + timedelta(days=days)).replace(
            hour=int(hour_utc) % 24, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=7)
        return target.isoformat()

    def acrithis_next_reset(self) -> str:
        """言录使下次轮换时刻（UTC ISO）。

        周期在 ``rotations.json`` 的 ``acrichis.reset_weekday`` /
        ``acrichis.reset_hour_utc``（当前 = 周一 00:00 UTC，wiki 实时口径），改数据即可。
        """
        try:
            cfg = json.loads((Path(__file__).resolve().parent / "data"
                              / "rotations.json").read_text(encoding="utf-8"))
            sec = cfg.get("acrichis") or {}
        except Exception:  # noqa: BLE001
            sec = {}
        return self.next_weekly_reset(
            weekday=int(sec.get("reset_weekday", 0)),
            hour_utc=int(sec.get("reset_hour_utc", 0)))

    def acrithis_week_expired(self) -> bool:
        """言录使本周货单快照是否已过期（用于内置定时自检，过期只告警不抓取）。"""
        from datetime import datetime, timezone
        data = self._load_json_file(paths.read_path(ACRITHIS_WEEK_NAME)) or {}
        exp = data.get("expiry") or ""
        try:
            return datetime.fromisoformat(exp) <= datetime.now(timezone.utc)
        except ValueError:
            return True

    async def acrithis_week(self) -> dict:
        """言录使**本周货单**快照（运行期覆盖优先，回退包内种子）；过期返回空 dict。

        未过期时附带 ``next_reset``（按规则算出的下次轮换 UTC 时刻），
        卡面据此展示倒计时 + 北京时间，不依赖文件里写死的 expiry。
        """
        from datetime import datetime, timezone
        data = self._load_json_file(paths.read_path(ACRITHIS_WEEK_NAME)) or {}
        exp = data.get("expiry") or ""
        try:
            if datetime.fromisoformat(exp) <= datetime.now(timezone.utc):
                return {}
        except ValueError:
            return {}
        data["next_reset"] = self.acrithis_next_reset()
        return data

    @staticmethod
    def parse_acrichis_current(raw: str) -> dict:
        """解析 wiki《Acrithis/Current Offerings》子页 wikitext（社区当期 5 件上报）。

        子页是 vardefine 模板：``{{#vardefine:AcrithisObserved|September 21, 2026}}``
        + ``AcrithisItem1..5``；注释块里带 15 件合法名单（校验用）。经
        FlareSolverr 抓回时 ``<>`` 被转义进 ``<pre>``，先整体 unescape 再解析，
        两种形态（转义/纯 wikitext）都能吃。解析不出 5 件返回空 dict。
        """
        import html as _html
        text = _html.unescape(raw or "")
        m = re.search(r"\{\{#vardefine:AcrithisObserved\|([^}]*)\}\}", text)
        observed = m.group(1).strip() if m else ""
        items: list[str] = []
        for i in range(1, 6):
            m = re.search(r"\{\{#vardefine:AcrithisItem" + str(i) +
                          r"\|([^}]*)\}\}", text)
            if not m or not m.group(1).strip():
                return {}
            items.append(m.group(1).strip())
        valid: set[str] = set()
        for m in re.finditer(r"<!--\s*(.*?)\s*-->", text, flags=re.S):
            for line in m.group(1).splitlines():
                line = line.strip()
                if line and not line.lower().startswith("valid item names"):
                    valid.add(line)
        return {"observed": observed, "items": items, "valid": sorted(valid)}

    async def refresh_acrichis_week(self) -> str:
        """言录使本周货单：过期后自动抓 wiki 当期上报子页刷新（2026-09-21 起）。

        DE 不下发每周实际 5 件；wiki《Acrithis/Current Offerings》是社区人工
        维护的当期上报（observed 日期随更）。纪律与效价刷新一致：

        - 每件英文名必须命中快照 ``_en_catalog``（15 件全池对照）才落盘，
          不认识的名字**绝不写半成品**；
        - ``observed`` 早于本周周一 00:00 UTC 视为「社区还没跟上」，不落盘
          （防止拿上周货单当本周的）。

        返回 "fresh"（未过期）/ "refreshed" / "not-updated" / "failed"。
        """
        from datetime import datetime, timedelta, timezone
        if not self.acrithis_week_expired():
            return "fresh"
        snap = self._load_json_file(paths.read_path(ACRITHIS_WEEK_NAME)) or {}
        catalog = snap.get("_en_catalog") or {}
        if not catalog:
            logger.warning("[sdjk] 言录使快照缺 _en_catalog，无法自动刷新")
            return "failed"
        try:
            raw = await self.fetch_via_flaresolver(ACRITHIS_CURRENT_URL, ttl=0)
        except Exception as e:  # noqa: BLE001 - 抓取失败走告警，不阻断
            logger.warning("[sdjk] 言录使当期子页抓取失败：%s", e)
            return "failed"
        parsed = self.parse_acrichis_current(raw)
        if not parsed:
            logger.warning("[sdjk] 言录使当期子页解析失败（模板结构变了？）")
            return "failed"
        try:
            observed = datetime.strptime(
                parsed["observed"], "%B %d, %Y").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            logger.warning("[sdjk] 言录使当期上报日期无法解析：%r",
                           parsed.get("observed"))
            return "failed"
        now = datetime.now(timezone.utc)
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        if observed < monday:
            logger.info("[sdjk] 言录使当期上报尚未更新（observed %s < 本周一），"
                        "本轮不落盘", parsed["observed"])
            return "not-updated"
        items: list[dict] = []
        for en in parsed["items"]:
            ent = catalog.get(en)
            if ent is None:
                # 子页名单与本地目录出现分歧（wiki 改了池子）→ 拒绝写半成品
                logger.error(
                    "[sdjk] 言录使当期上报出现目录外物品 %r —— wiki 池子可能已"
                    "改动，需人工核对 _en_catalog 后重试", en)
                return "failed"
            it = {"name": ent["name"]}
            if ent.get("qty") is not None:
                it["qty"] = ent["qty"]
            it["price"] = ent["price"]
            items.append(it)
        out = {
            "expiry": self.acrithis_next_reset(),
            "observed": parsed["observed"],
            "source": (f"wiki《Acrithis/Current Offerings》当期上报（observed "
                       f"{parsed['observed']}）；插件自动抓取"),
            "items": items,
            "_en_catalog": catalog,
            "_catalog_price_source": snap.get("_catalog_price_source", ""),
            "_reset_rule": snap.get("_reset_rule", ""),
            "_过期口径": snap.get("_过期口径", ""),
        }
        paths.write_path(ACRITHIS_WEEK_NAME).write_text(
            json.dumps(out, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        logger.info("[sdjk] 言录使本周货单已自动刷新（observed %s，%d 件）",
                    parsed["observed"], len(items))
        return "refreshed"

    async def steel_path_incursions(self, platform: str) -> dict:
        """钢铁之路侵袭（每日 6 个钢路节点）。

        ⚠️ DE worldState **不下发**侵袭。社区（browse.wf，与仲裁排期同一作者）
        维护一张纯文本排期表 ``sp-incursions.txt``，每行
        ``<当日UTC零点的epoch秒>;<节点key,节点key,…>``，按今天的 UTC 零点取行即可，
        与 browse.wf/live 的「Steel Path Incursions」同源。取不到就返回空 dict。
        """
        url = "https://browse.wf/sp-incursions.txt"
        try:
            r = await self._http.get(url, timeout=15.0,
                                     headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            txt = r.text
        except Exception:  # noqa: BLE001 - 取不到就降级为空
            return {}
        day = int(time.time() // 86400 * 86400)
        nodes: list[str] = []
        for line in txt.strip().split("\n"):
            if ";" not in line:
                continue
            head, rest = line.split(";", 1)
            try:
                if int(head.strip()) != day:
                    continue
            except ValueError:
                continue
            nodes = [n.strip() for n in rest.split(",") if n.strip()]
            break
        if not nodes:
            return {}
        expiry = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(day + 86400))
        return {"expiry": expiry, "nodes": nodes}

    # ---- 以下 6 个取数，DE 源的 parse_worldstate 已直接产出对应键 ----
    async def void_storms(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "voidStorms", ttl=60) or []

    async def events(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "goals", ttl=TTL_WORLDSTATE_MID) or []

    async def conclave(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "conclaveChallenges",
                                     ttl=TTL_WORLDSTATE_MID) or []

    async def prime_vault(self, platform: str) -> dict:
        return await self.worldstate(platform, "primeVault", ttl=TTL_WORLDSTATE_MID) or {}

    async def clan_rewards(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "clanRewards", ttl=600) or []

    async def flash_sales(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "flashSales", ttl=TTL_WORLDSTATE_MID) or []

    async def construction(self, platform: str) -> dict:
        return await self.worldstate(platform, "constructionProgress", ttl=300) or {}

    async def synth_targets(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "synthTargets", ttl=600) or []

    async def syndicate_missions(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "syndicateMissions", ttl=300) or []

    async def bounty_cycle(self) -> dict:
        """browse.wf oracle 的赏金轮换表。

        返回 ``{"expiry", "rot", "vaultRot", "zarimanFaction", "bounties": {...}}``：
        其中 ``bounties`` 按 SyndicateMissions 的 Tag 给出本轮**节点（SolNode###）
        与挑战（/Lotus/Types/Challenges/…）**。

        为什么需要它：地球 / 金星 / 火卫二 的赏金 DE 会下发 Jobs，但
        **扎里曼（ZarimanSyndicate）/ 解剖圣所（EntratiLabSyndicate）/
        1999（HexSyndicate）的 Jobs 恒为空**，只有 oracle 才有「本轮是哪几个节点、
        每个节点的挑战是什么」，赏金卡才能补上任务名与任务目标。

        失败（网络/403/结构变化）时返回 ``{}``，调用方降级为只列等级。

        TTL 15 分钟：赏金轮换本身 2.5 小时才换一次，5 分钟缓存太短，
        会让「赏金」一览几乎每次都真去联网（实测 oracle 响应 1~2 秒，
        正是用户感知到的「慢」）。另加 3.5 秒硬超时，慢网络下快速降级。
        """
        try:
            data = await asyncio.wait_for(
                self._fetch_json(f"{DEFAULT_ORACLE_BASE}/bounty-cycle",
                                 ttl=900, headers=_ORACLE_HEADERS,
                                 cache_key="oracle:bounty-cycle"),
                timeout=3.5)
        except Exception:  # noqa: BLE001 —— 赏金的附加信息，拿不到不影响主流程
            return {}
        return data if isinstance(data, dict) else {}

    async def deep_archimedea(self, platform: str) -> Optional[dict]:
        try:
            return await self.worldstate(platform, "deepArchimedea", ttl=600)
        except WarframeAPIError:
            return None

    async def temporal_archimedea(self, platform: str) -> Optional[dict]:
        try:
            return await self.worldstate(platform, "temporalArchimedea", ttl=600)
        except WarframeAPIError:
            return None

    async def calendar(self, platform: str) -> Optional[dict]:
        try:
            data = await self.worldstate(platform, "calendar", ttl=600)
        except WarframeAPIError:
            return None
        if isinstance(data, dict):
            data = await self._resolve_calendar_rewards(data)
        return data

    async def _resolve_calendar_rewards(self, data: dict) -> dict:
        """用 WM v2 物品的 gameRef 索引把日历奖励路径解析成中文名（尽力而为）。"""
        try:
            items = await self.wm_items()
        except WarframeAPIError:
            return data
        by_ref: dict[str, str] = {}
        for it in items:
            ref = it.get("game_ref") or ""
            if ref and it.get("zh"):
                by_ref.setdefault(ref, it["zh"])
        for day in data.get("days") or []:
            for ev in day.get("events") or []:
                ref = ev.get("ref") or ""
                if ref and not ev.get("name"):
                    ev["name"] = by_ref.get(ref) or ev.get("name")
        return data

    async def search_items(self, query: str) -> list[dict]:
        """物品搜索：WM v2 字典本地匹配（zh/en 包含）。"""
        items = await self.wm_items()
        q = query.strip().lower()
        hits = []
        for it in items:
            hay = f"{it.get('zh', '')} {it.get('en', '')} {it.get('url_name', '')}".lower()
            if q in hay:
                hits.append({
                    "name": it.get("zh") or it.get("en") or it.get("url_name"),
                    "category": (it.get("tags") or ["item"])[0],
                    "description": (f"{it['ducats']} 杜卡德" if it.get("ducats")
                                    else ("可交易" if it.get("tradable") else "")),
                })
            if len(hits) >= 8:
                break
        return hits

    # ------------------------------------------------------------------
    # Warframe.Market v2：物品字典 / 订单
    # ------------------------------------------------------------------
    async def _wm_v2(self, path: str, *, ttl: float, headers: Optional[dict] = None,
                     platform: Optional[str] = None) -> Any:
        hdr = {"Language": self.language, "Crossplay": "true"}
        if platform:
            hdr["Platform"] = _WM_PLATFORM.get(platform, platform)
        hdr.update(headers or {})
        data = await self._fetch_json(
            f"{self.wm_v2_base}{path}", ttl=ttl, headers=hdr,
            cache_key=f"wmv2:{path}|{platform}|{self.language}",
            wm_rate_limit=True)
        if isinstance(data, dict):
            if data.get("error"):
                raise WarframeAPIError(f"WM v2 接口错误：{data['error']}")
            return data.get("data")
        return data

    async def wm_items(self) -> list[dict]:
        """全量物品（含 zh-hans 名称与杜卡德），归一化字段。"""
        raw = await self._wm_v2("/items", ttl=TTL_WM_ITEMS) or []
        out = []
        for it in raw:
            i18n = it.get("i18n") or {}
            zh = (i18n.get("zh-hans") or {}).get("name", "")
            en = (i18n.get("en") or {}).get("name", "")
            out.append({
                "id": it.get("id"), "url_name": it.get("slug", ""),
                "game_ref": it.get("gameRef", ""), "zh": zh, "en": en,
                "ducats": it.get("ducats"), "trading_tax": it.get("tradingTax"),
                "tradable": it.get("tradable"), "tags": it.get("tags") or [],
            })
        return out

    async def ducats_board(self) -> list[dict]:
        """WM 官方「杜卡德计算器」的同源榜单（网页 tools/ducats 用的就是它）。

        以前是逐项查订单再自己算「杜卡德/白金」，两个问题：
        一是只能抽样十几个 item，排序结果和官网对不上；二是价格口径
        （最低在售 vs 加权均价）不一致。这个接口直接给出权威指标：

        - ``ducats_per_platinum``：按最低价算的 杜/白（官网主排序依据）
        - ``ducats_per_platinum_wa``：按加权均价算的 杜/白（更抗刷单）
        - ``wa_price`` / ``median``：加权均价 / 中位数
        - ``volume``：成交量

        Returns:
            按 ``ducats_per_platinum`` 倒序的榜单；接口不可用时返回空列表。
        """
        try:
            raw = await self._fetch_json(
                f"{self.wm_base}/tools/ducats", ttl=TTL_WM_DUCATS,
                wm_rate_limit=True, cache_key="wmv1:tools/ducats")
        except Exception:  # noqa: BLE001 - 该接口是增强项，失败要能降级
            return []
        rows = ((raw or {}).get("payload") or {}).get("previous_hour") or []
        items = await self.wm_items()
        by_id = {it.get("id"): it for it in items}
        out = []
        for r in rows:
            it = by_id.get(r.get("item"))
            if not it:
                continue
            out.append({
                "name": it.get("zh") or it.get("en") or it.get("url_name", ""),
                "url_name": it.get("url_name", ""),
                "ducats": r.get("ducats") or it.get("ducats") or 0,
                "dpp": float(r.get("ducats_per_platinum") or 0),
                "dpp_wa": float(r.get("ducats_per_platinum_wa") or 0),
                "plat": float(r.get("wa_price") or 0),
                "median": float(r.get("median") or 0),
                "volume": int(r.get("volume") or 0),
                "tags": it.get("tags") or [],
            })
        out.sort(key=lambda r: -r["dpp"])
        return out

    async def ducats_price_map(self) -> dict:
        """``{去空格后的物品名: {"ducats","plat","dpp"}}``，用于给遗物奖励标注价格。

        名称去空格是为了对齐遗物索引里的写法（``席尔火枪Prime枪管``）
        与 WM 的写法（``席尔火枪 Prime 枪管``）。
        """

        async def _build() -> dict:
            rows = await self.ducats_board()
            out: dict = {}
            for r in rows:
                key = re.sub(r"\s+", "", r.get("name") or "")
                if not key:
                    continue
                out[key] = {"ducats": r.get("ducats", 0),
                            "plat": r.get("median") or r.get("plat") or 0,
                            "dpp": r.get("dpp", 0)}
            return out

        return await self.cache.get_or_fetch(
            "wmv1:ducats_price_map", TTL_WM_DUCATS, _build) or {}

    async def wm_orders(self, url_name: str, platform: str,
                        rank: Optional[int] = None) -> tuple[list[dict], dict]:
        """某物品近 48h 可见订单，字段向 v1 对齐（order_type/mod_rank/ingame_name）。"""
        path = f"/orders/item/{quote(url_name)}"
        params = {}
        if rank is not None:
            path += "/top"
            params["rank"] = rank
        raw = await self._wm_v2(path, ttl=TTL_WM_ORDERS, platform=platform,
                                headers=params or None)
        if isinstance(raw, dict):  # /top 返回 {sell: [...], buy: [...]}
            raw = (raw.get("sell") or []) + (raw.get("buy") or [])
        orders = []
        for o in raw or []:
            user = o.get("user") or {}
            orders.append({
                "id": o.get("id"),
                "order_type": o.get("type", "sell"),
                "platinum": o.get("platinum", 0),
                "quantity": o.get("quantity", 1),
                "mod_rank": o.get("rank"),
                "subtype": o.get("subtype"),
                "visible": o.get("visible", True),
                "platform": user.get("platform", platform),
                "last_update": o.get("updatedAt", ""),
                "user": {"ingame_name": user.get("ingameName", "?"),
                         "status": user.get("status", "offline"),
                         "reputation": user.get("reputation", 0)},
            })
        return orders, {}

    async def wm_item_detail(self, slug: str) -> dict:
        """WM v2 物品详情：set_root（是否套装根）/ set_parts（部件物品 ID 列表）。

        2026-09-14 用户要求：查套装默认仍输出套装单，但要把分件（蓝图/机体/
        枪管…）参考价一起给出 —— 部件在这里按 ID 给出，配合 ``wm_items()``
        的 id -> slug/名称 映射换算成部件物品。
        """
        d = await self._wm_v2(f"/items/{quote(slug)}", ttl=TTL_WM_ITEMS)
        if not isinstance(d, dict):
            return {}
        i18n = d.get("i18n") or {}
        return {
            "slug": d.get("slug", slug),
            "tags": d.get("tags") or [],
            "set_root": bool(d.get("setRoot")),
            "set_parts": list(d.get("setParts") or []),
            "zh": (i18n.get("zh-hans") or {}).get("name", ""),
            "en": (i18n.get("en") or {}).get("name", ""),
        }

    async def wm_set_parts(self, slug: str) -> list[dict]:
        """套装的部件物品列表（zh/en/url_name），保持 WM setParts 顺序。

        非套装（set_root=False 或无 setParts）返回空列表。
        """
        detail = await self.wm_item_detail(slug)
        part_ids = detail.get("set_parts") or []
        if not detail.get("set_root") or not part_ids:
            return []
        items = await self.wm_items()
        by_id = {it.get("id"): it for it in items}
        parts: list[dict] = []
        for pid in part_ids:
            it = by_id.get(pid)
            if it and it.get("url_name") and it["url_name"] != slug:
                parts.append(it)
        return parts

    async def wm_riven_weapons(self) -> list[dict]:
        """紫卡武器表（v2，含 zh-hans 名称与倾向指数）。

        ★ 2026-09-23 合并 core/data/dispositions_rivenmirror.json 静态补全：
        WM v2 该端点只有 418 条且几乎不含 Prime/新变体（rubico_prime、
        kuva_zarr、tenet_arca_plasmor 全缺），变体倾向会错给本体值。
        补全数据由 scripts/build_disposition.py 生成（riven-mirror 细粒度值
        + DE 官方 zh + 基类 riven_type 继承），按 url_name 去重后追加。
        """
        raw = await self._wm_v2("/riven/weapons", ttl=TTL_WM_ITEMS) or []
        out = []
        seen_urls = set()
        for w in raw:
            i18n = w.get("i18n") or {}
            url = w.get("slug", "")
            seen_urls.add(url)
            out.append({
                "url_name": url,
                "zh": (i18n.get("zh-hans") or {}).get("name", ""),
                "en": (i18n.get("en") or {}).get("name", ""),
                "game_ref": w.get("gameRef", ""),
                "disposition": w.get("disposition"),
                "riven_type": w.get("rivenType", ""),
                "group": w.get("group", ""),
            })
        try:
            extra = json.loads((DATA_DIR / "dispositions_rivenmirror.json")
                               .read_text(encoding="utf-8")).get("entries") or {}
        except Exception as exc:  # noqa: BLE001 - 数据缺失只降级不炸
            logger.warning("[sdjk] 倾向补全数据不可用：%s", exc)
            extra = {}
        by_url = {w["url_name"]: w for w in out}
        for en, e in extra.items():
            u = e.get("url_name") or ""
            hit = by_url.get(u)
            if hit is not None:
                # 覆盖：WM 手工维护值滞后于平衡补丁（Vectis Prime 0.9→1.0），
                # wiki 主源的值优先生效；zh 缺失时顺带补。
                hit["disposition"] = e.get("disposition")
                if not hit.get("zh"):
                    hit["zh"] = e.get("zh", "")
                continue
            by_url[u] = {"url_name": u, "zh": e.get("zh", ""), "en": en,
                         "game_ref": "",
                         "disposition": e.get("disposition"),
                         "riven_type": e.get("riven_type", ""),
                         "group": e.get("group", "")}
            out.append(by_url[u])
        return out

    # ------------------------------------------------------------------
    # Warframe.Market v1：价格统计（趋势 / 排行）
    # ------------------------------------------------------------------
    async def wm_statistics(self, slug: str, platform: str = "pc") -> dict:
        """v1 价格统计：近 48 小时逐小时 + 近 90 天逐日。

        返回 {"h48": [{t, volume, median, avg, min, max}], "d90": [...]}
        """
        hdr = {
            "Language": self.language,
            "Platform": _WM_PLATFORM.get(platform, platform),
            "Crossplay": "true",
        }
        data = await self._fetch_json(
            f"{self.wm_base}/items/{quote(slug)}/statistics",
            ttl=TTL_WM_STATS, headers=hdr,
            cache_key=f"wmstats:{slug}|{platform}", wm_rate_limit=True)
        payload = (data or {}).get("payload") or {}
        closed = payload.get("statistics_closed") or {}

        def norm(rows: list[dict]) -> list[dict]:
            out = []
            for r in rows or []:
                out.append({
                    "t": (r.get("datetime") or "")[:16].replace("T", " "),
                    "volume": r.get("volume") or 0,
                    "median": r.get("median") or 0,
                    "avg": r.get("avg_price") or 0,
                    "min": r.get("min_price") or 0,
                    "max": r.get("max_price") or 0,
                    "rank": r.get("mod_rank") or 0,
                })
            return out

        return {"h48": norm(closed.get("48hours")),
                "d90": norm(closed.get("90days"))}

    @staticmethod
    def summarize_stats(stats: dict) -> dict:
        """把统计原始行汇总成「热度 + 价格」指标。"""
        h48, d90 = stats.get("h48") or [], stats.get("d90") or []
        vol48 = sum(r["volume"] for r in h48)
        vol90 = sum(r["volume"] for r in d90)
        med_all = [r["median"] for r in d90 if r["median"]] or \
            [r["median"] for r in h48 if r["median"]]
        med48 = [r["median"] for r in h48 if r["median"]]
        avg90 = [r["avg"] for r in d90 if r["avg"]]
        recent = med48[-1] if med48 else (med_all[-1] if med_all else 0)
        early = med48[0] if med48 else recent
        prev = med_all[:max(1, len(med_all) // 3)]
        base = (sum(prev) / len(prev)) if prev else recent
        return {
            "vol48": vol48, "vol90": vol90,
            "median": round(sum(med_all) / len(med_all), 1) if med_all else 0,
            "median48": round(sum(med48) / len(med48), 1) if med48 else 0,
            "avg90": round(sum(avg90) / len(avg90), 1) if avg90 else 0,
            "last": recent, "first": early,
            "change": round((recent - base) / base * 100, 1) if base else 0.0,
        }

    # 排行分类 -> (必须包含的标签, 必须排除的标签)
    RANK_CATEGORIES: dict[str, tuple[set, set]] = {
        "甲": ({"warframe", "prime"}, {"mod"}),
        "卡": ({"mod"}, set()),
        "部件": ({"component"}, set()),
        "赋能": ({"arcane_enhancement"}, set()),
        "主武": ({"weapon", "primary"}, {"mod"}),
        "副武": ({"weapon", "secondary"}, {"mod"}),
        "近战": ({"weapon", "melee"}, {"mod"}),
        "遗物": ({"relic"}, set()),
    }

    # ------------------------------------------------------------------
    # 价格排行落盘（全量后台爬取）+ DE 官方紫卡周报
    # ------------------------------------------------------------------
    @staticmethod
    def _load_json_file(path) -> dict:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缺文件/坏文件都当空数据处理
            return {}

    @staticmethod
    def _save_json_file(path, payload) -> None:
        try:
            Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except Exception:  # noqa: BLE001 - 落盘失败不影响本次查询
            pass

    @staticmethod
    def _rank_record(it: dict, stats: dict) -> Optional[dict]:
        """单个物品的统计 → 排行行。

        * MOD 卡只取 0 级成交（用户要求「全部可交易的 0 级卡片」）；
        * **必须有 48h 成交**才入榜 —— 只有 90 天旧数据的物品没有
          「当前价」，排进榜里只会显示一排「—」（用户反馈）。
        """
        h48 = list(stats.get("h48") or [])
        d90 = list(stats.get("d90") or [])
        tags = it.get("tags") or []
        if "mod" in tags:
            h48 = [r for r in h48 if not r.get("rank")]
            d90 = [r for r in d90 if not r.get("rank")]
        med48 = [r["median"] for r in h48 if r.get("median")]
        if not med48:
            return None
        mins = [r["min"] for r in h48 if r.get("min")]
        maxs = [r["max"] for r in h48 if r.get("max")]
        # 「上期中位」= 当前 48h 窗口之前的两天（d90 日线按时间升序，
        # 去掉最近两天的点后取最后两个），给用户一个价格走势参照
        prev = [r for r in d90[:-2] if r.get("median")][-2:]
        prev_med = round(sum(r["median"] for r in prev) / len(prev), 1) if prev else 0
        return {
            "zh": it.get("zh") or "", "en": it.get("en") or "",
            "tags": tags,
            "median48": round(sum(med48) / len(med48), 1),
            "min48": min(mins) if mins else 0,
            "max48": max(maxs) if maxs else 0,
            "median_prev": prev_med,
            "vol48": sum(r.get("volume") or 0 for r in h48),
            "vol90": sum(r.get("volume") or 0 for r in d90),
        }

    async def crawl_wm_ranks(self, limit: Optional[int] = None) -> int:
        """抓取 WM 成交统计并落盘（约 3200 项，实测 1 项/4 秒）。

        2026-09-14 重做续跑逻辑：全量一轮要好几个小时，占用 WM 全局限速会
        拖慢用户的 wm/wr 查询，所以改成**游标续跑**——

        - 落盘记录 ``cursor``（下次从第几项继续）与 ``rows``（已抓结果）；
        - 每轮最多抓 ``limit`` 项，抓完就收工，下一轮（次日闲时）接着跑；
        - 跑满一轮（cursor 回到 0）才更新 ``ts``，即整体刷新周期仍是 48h；
        - 每 50 项落一次盘，中断不丢已抓部分，排行查询始终能显示已完成部分。

        Args:
            limit: 本轮最多抓取项数；None = 不限（一次跑完）。

        Returns:
            当前已入榜的物品数。
        """
        items = [it for it in await self.wm_items() if _rank_candidate(it)]
        total = len(items)
        old = self._load_json_file(paths.read_path(RANKS_NAME)) or {}
        rows: dict = old.get("rows") or {}
        cursor = int(old.get("cursor") or 0)
        if cursor >= total:        # 上一轮已跑满，从头开始新一轮
            cursor = 0
        done = 0
        idx = cursor
        while idx < total:
            if limit is not None and done >= limit:
                break
            it = items[idx]
            slug = it.get("url_name") or ""
            if slug:
                try:
                    rec = self._rank_record(it, await self.wm_statistics(slug, "pc"))
                    if rec:
                        rows[slug] = rec
                except Exception:  # noqa: BLE001 - 单项失败不阻断整轮
                    pass
            idx += 1
            done += 1
            if done % 50 == 0:
                self._save_json_file(paths.write_path(RANKS_NAME), {
                    "ts": old.get("ts") or "", "cursor": idx, "total": total,
                    "done": idx, "rows": rows})
        finished = idx >= total
        self._save_json_file(paths.write_path(RANKS_NAME), {
            # 跑满一轮才刷新 ts（整榜 48h 更新一次）；没跑满沿用旧 ts，
            # 下一轮仍判定为过期 -> 从 cursor 继续
            "ts": (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
                   if finished else (old.get("ts") or "")),
            "cursor": 0 if finished else idx,
            "total": total, "done": idx, "rows": rows})
        # api_client 没有模块级 logger，这里用标准库（保持与 main.py 前缀一致）
        logger.info(
            "[sdjk] 价格榜单抓取：本轮 %d 项，累计 %d/%d%s",
            done, idx, total, "（本轮跑满）" if finished else "")
        return len(rows)

    def start_rank_crawl(self) -> tuple[bool, int, int]:
        """启动后台全量抓取；返回 (是否新启动, 已完成项, 总项数)。"""
        data = self._load_json_file(paths.read_path(RANKS_NAME))
        task = getattr(self, "_rank_crawl_task", None)
        if task and not task.done():
            return False, int(data.get("done") or 0), int(data.get("total") or 0)
        self._rank_crawl_task = asyncio.get_running_loop().create_task(
            self.crawl_wm_ranks())
        return True, int(data.get("done") or 0), int(data.get("total") or 0)

    def rank_rows(self) -> list[dict]:
        """落盘的排行行（含 slug，供 formatter 按分类过滤）。"""
        data = self._load_json_file(paths.read_path(RANKS_NAME))
        return [{"slug": k, **v} for k, v in (data.get("rows") or {}).items()]

    async def de_weekly_rivens(self, platform: str = "pc",
                               force: bool = False) -> dict:
        """DE 官方每周紫卡交易数据（周销量/热度榜的权威来源）。

        官方文件为 JS 字面量，解析后落盘缓存 6 小时；``force=True``
        跳过缓存强制拉取（「紫卡排行 刷新」）。主机平台文件缺失回落 PC。
        """
        if not force:
            snap = self._load_json_file(paths.read_path(RIVEN_WEEKLY_NAME))
            if snap.get("entries") and \
                    time.time() - snap.get("fetched", 0) < TTL_RIVEN_WEEKLY:
                return snap
        plat = _DE_RIVEN_PLATFORM.get(platform, "PC")
        try:
            resp = await self._http.get(DE_RIVEN_WEEKLY.format(plat=plat))
            resp.raise_for_status()
            entries = parse_de_riven_weekly(resp.text)
        except Exception as exc:
            if platform != "pc":
                return await self.de_weekly_rivens("pc", force=force)
            raise WarframeAPIError(f"DE 紫卡周报拉取失败：{exc}") from exc
        snap = {"fetched": time.time(), "platform": plat, "entries": entries}
        self._save_json_file(paths.write_path(RIVEN_WEEKLY_NAME), snap)
        return snap


    # ------------------------------------------------------------------
    # Warframe.Market v1：拍卖（紫卡/玄骸，v2 尚未开放）
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_riven_stats(stats: Iterable[str], riven_type: str = "") -> list[str]:
        """把插件标准词条 id 转成 WM v1 拍卖数据的 url_name（含合并名）。"""
        from .parser import RIVEN_URL_COMPAT
        return list(dict.fromkeys(RIVEN_URL_COMPAT.get(s, s) for s in stats))

    async def wm_riven_auctions(
        self,
        weapon_url_name: str,
        platform: str = "pc",
        *,
        positives: Iterable[str] = (),
        negatives: Iterable[str] = (),
        polarity: Optional[str] = None,
        max_price: Optional[int] = None,
        min_price: Optional[int] = None,
        max_rerolls: Optional[int] = None,
        min_rerolls: Optional[int] = None,
        rank_range: Optional[tuple[int, int]] = None,
    ) -> list[dict]:
        """WM v1 紫卡拍卖搜索。

        ⚠ 参数名踩坑记录（2026-09-11 实测）：
          · 词条必须用 `positive_stats=a&positive_stats=b`（**不带方括号**）。
            写成 `positive_stats[]=...` 服务端会静默忽略，返回 500 条无关挂单，
            导致本地二次筛「一条都出不来」。
          · 洗数过滤是 `re_rolls_max` / `re_rolls_min`，不是 `rerolls_max`。
          · `negative_stats` 同样不带方括号，且确实生效（AND 语义）。
        """
        params: dict[str, Any] = {
            "type": "riven",
            "weapon_url_name": weapon_url_name,
            "platform": platform,
            "sort_by": "price_asc",
            "buyout_policy": "direct",
        }
        pos = list(dict.fromkeys(positives))
        neg = list(dict.fromkeys(negatives))
        if pos:
            # 服务端为 OR 语义（返回含任一命中词条的挂单），精确匹配仍需本地二次筛
            params["positive_stats"] = pos
        if neg:
            params["negative_stats"] = neg
        if polarity:
            params["polarity"] = polarity
        if max_price is not None:
            params["price_max"] = max_price
        if min_price is not None:
            params["price_min"] = min_price
        if max_rerolls is not None:
            params["re_rolls_max"] = max_rerolls
        if min_rerolls is not None:
            params["re_rolls_min"] = min_rerolls
        if rank_range:
            params["mastery_rank_min"], params["mastery_rank_max"] = rank_range
        try:
            data = await self._fetch_json(
                f"{self.wm_base}/auctions/search", ttl=TTL_WM_AUCTIONS, params=params,
                wm_rate_limit=True)
        except WarframeAPIError:
            if not (pos or neg):
                raise
            # 个别词条不被服务端接受时：去掉词条过滤重查，交给本地二次筛
            params.pop("positive_stats", None)
            params.pop("negative_stats", None)
            data = await self._fetch_json(
                f"{self.wm_base}/auctions/search", ttl=TTL_WM_AUCTIONS, params=params,
                wm_rate_limit=True)
        return (data or {}).get("payload", {}).get("auctions", [])

    async def wm_lich_auctions(self, weapon_url_name: str, platform: str = "pc",
                               *, lich_type: str = "lich",
                               element: Optional[str] = None,
                               damage_max: Optional[int] = None) -> list[dict]:
        """玄骸拍卖挂单。

        ★ 2026-09-18 容错（用户反馈「信条铁晶磁轨炮搜不出来」时暴露）：
        WM **不是每把玄骸武器都有拍卖类目** —— 实测部分近战 Tenet 武器与
        全部 Coda 武器都会返回 **400 Bad Request**（不是网络故障，是「没有
        这个类目」）。原来会把 400 当异常抛到上层变成「内部错误」，用户只看到
        「出错了」。现在：400 → 返回空列表并记入 ``_lich_unsupported``，
        由调用方给出「WM 暂无该武器挂单类目」的准确提示；429 单独提示限速。
        """
        params: dict[str, Any] = {"type": lich_type, "weapon_url_name": weapon_url_name,
                                  "platform": platform, "sort_by": "price_asc"}
        if element:
            params["element"] = element
        if damage_max:
            params["having_damage_max"] = damage_max
        try:
            data = await self._fetch_json(
                f"{self.wm_base}/auctions/search", ttl=TTL_WM_AUCTIONS,
                params=params, wm_rate_limit=True)
        except WarframeAPIError as exc:
            msg = str(exc)
            if "400" in msg:
                self._lich_unsupported.add(weapon_url_name)
                logger.info("[sdjk] WM 无该玄骸武器的挂单类目：%s（type=%s）",
                            weapon_url_name, lich_type)
                return []
            if "429" in msg:
                raise WarframeAPIError(
                    "warframe.market 限速了（3 请求/秒），请过几秒再试") from exc
            raise
        return (data or {}).get("payload", {}).get("auctions", [])

    def lich_unsupported(self, weapon_url_name: str) -> bool:
        """该武器是否已被确认「WM 没有挂单类目」（避免重复请求同一把）。"""
        return weapon_url_name in self._lich_unsupported

    # ------------------------------------------------------------------
    # 名称解析（CN 别名 -> WM url_name / wiki 页面）
    # ------------------------------------------------------------------
    def alias_lookup(self, query: str, table: str = "wm_items") -> Optional[str]:
        """本地别名词典精确/模糊匹配。返回 WM url_name 或 None。"""
        q = query.strip().lower()
        table = self._aliases.get(table, {})
        hit = table.get(q)
        if isinstance(hit, str) and hit:
            return hit
        # 大小写不敏感的包含匹配
        for k, v in table.items():
            if q and (k in q or q in k) and abs(len(k) - len(q)) <= 4:
                return v
        return None

    def _alias_fuzzy(self, query: str, table: str = "wm_items") -> Optional[str]:
        """别名词典的**模糊**匹配（双向包含 + 长度差 ≤4），不含精确键。

        ★ 2026-09-20 从 ``alias_lookup`` 拆出来：解析链里它必须**降级到末尾**。
        它是双向包含 —— 词典键可以是输入的子串（"压迫点" ⊂ "压迫点 p"），
        输入也可以是键的子串，长度差还放宽到 4。放在前面会把官方名一起捞走
        （实测「压迫点 p」经此规则配到 serration「膛线」）。
        """
        q = (query or "").strip().lower()
        # ★ 同 resolve_wm_item：测试用 __new__ 造实例时 _aliases 可能未初始化
        tbl = (getattr(self, "_aliases", None) or {}).get(table, {})
        for k, v in tbl.items():
            if q and (k in q or q in k) and abs(len(k) - len(q)) <= 4:
                return v
        return None

    _PRIME_SUFFIX = re.compile(r"^(.+?)(?:\s*prime|[pP])$")

    async def _resolve_prime_variant(self, base: str) -> Optional[dict]:
        """黑话+p：解析基础名并优先返回其 Prime 版本（WM 只交易 Prime 战甲）。"""
        item = await self.resolve_wm_item(base)
        if not item:
            return None
        url = item.get("url_name", "")
        if "prime" in url:
            return item
        cand = await self.resolve_wm_item(base + " prime")
        return cand or item

    async def resolve_wm_item(self, query: str) -> Optional[dict]:
        """把用户输入（中文名/黑话/英文名，可带 p/prime 后缀）解析成 WM 物品。

        ★ 优先级（2026-09-20 重排，修「压迫点 → 膛线」这类错误）：
          1. **官方名精确**（官方简中 / 官方英文 / WM slug）—— 官方名是唯一权威
          2. 别名词典**精确**键（黑话，如「咖喱」）
          3. 常规链路（包含 / 归一化 / 词典模糊 / 拼写模糊）
          4. 黑话 + p / + prime 回退（猴p -> 悟空 Prime 一套）

          以前别名词典（含**双向包含**的模糊匹配）排在最前，官方名会被词典里的
          错映射劫持；现在官方名永远赢，词典错映射不再影响「照官方名输入」的场景。
        """
        query = query.strip()
        if not query:
            return None
        # ★ 重入守卫：第 4 步的回退会递归调用本方法（base 与 base+" prime"），
        #   而 base+" prime" 自己又满足「带 prime 后缀」的条件 —— 于是
        #   「毁灭G prime」→「毁灭G」→「毁灭G prime」… 自激成 RecursionError
        #   （2026-09-20 容器内实测）。守卫让同一查询在调用栈里只解析一次。
        seen = getattr(self, "_prime_seen", None)
        if seen is None:
            seen = self._prime_seen = set()
        if query in seen:
            return None
        seen.add(query)
        try:
            return await self._resolve_wm_item_once(query)
        finally:
            seen.discard(query)

    async def _resolve_wm_item_once(self, query: str) -> Optional[dict]:
        """单次解析（不含重入守卫）；递归防护由 resolve_wm_item 负责。"""
        items = await self.wm_items()
        hit = match_official_name(query, items)
        if hit:
            return hit
        # 别名词典**精确**键（黑话）；模糊降级到后面的链路，避免劫持官方名
        # ★ getattr 兜底：部分测试用 __new__ 造实例，_aliases 可能还没初始化，
        #   这里不能因为「查不到词典键」就把整条解析链炸掉。
        url = ((getattr(self, "_aliases", None) or {}).get("wm_items") or {}).get(
            query.lower())
        if isinstance(url, str) and url:
            for it in items:
                if it.get("url_name") == url:
                    return it
        hit = await self._resolve_wm_item_inner(query)
        if hit:
            return hit
        # 常规链路没中：黑话+p / 黑话+prime（猴p -> 悟空 Prime 一套）
        m = self._PRIME_SUFFIX.match(query)
        if m and len(m.group(1).strip()) >= 1:
            return await self._resolve_prime_variant(m.group(1).strip())
        return None

    async def _resolve_wm_item_inner(self, query: str) -> Optional[dict]:
        """基础解析链（官方名精确 / 词典精确键 / 后缀回退已在 resolve_wm_item 试过）。"""
        if not query:
            return None
        items = await self.wm_items()
        low = query.lower()

        def _score(it: dict) -> int:
            tags = set(it.get("tags") or [])
            if "set" in tags:
                return 0
            if not ({"component", "blueprint"} & tags):
                return 1
            return 2

        # 中文名包含 -> slug 包含 -> 归一化
        zh_low = query.lower()
        zh_contains = [it for it in items
                       if it.get("zh") and zh_low in it["zh"].lower()]
        if zh_contains:
            # 包含命中多个时：整套 > 本体（非部件/蓝图）> 部件/蓝图
            return min(zh_contains, key=_score)
        url_contains = [it for it in items
                        if low.replace(" ", "_") in it.get("url_name", "")]
        if url_contains:
            return min(url_contains, key=_score)
        # 归一化匹配：用户输入「Saryn蓝图」这类省略 Prime/空格的部件名
        # （zh「Saryn Prime 蓝图」归一化后正好等于输入）—— 2026-09-14 加
        norm_hit = match_wm_normalized(query, items)
        if norm_hit:
            return norm_hit
        # ★ 别名词典模糊**降级到这儿**（2026-09-20）：这条是「双向包含 + 长度差≤4」，
        #   以前排在最前，会把「压迫点 p」里的「压迫点」捞出来配到 serration（膛线）。
        #   现在只有官方名/精确键/包含/归一化都没中时，才允许它兜底。
        alias = self._alias_fuzzy(query)
        if alias:
            # ★ 输入带 p / prime 后缀时，优先返回该物品的 Prime 变体 ——
            #   让「压迫点p」（连写）与「压迫点 p」（带空格）结果一致（2026-09-20）。
            #   找不到 Prime 变体就照常返回原物品（如 sawtooth clip 以 p 结尾也不受影响）。
            want_prime = bool(re.search(r"(?:^|[^a-z])p$|prime$", query.strip().lower()))
            # ★ primed 是**物品 dict**，比较时要取它的 url_name（不是拿 dict 去比）
            target = alias
            if want_prime:
                primed = next((x for x in items
                               if x.get("url_name") == "primed_" + alias), None)
                if primed:
                    target = primed.get("url_name")
            for it in items:
                if it.get("url_name") == target:
                    return it
        # 中文部件名：「席瓦蓝图」-> 基名「席瓦」先解析出 席瓦&神盾Prime 系，
        # 再按部件词的英文 slug 在同系物品里挑部件（WM 的 zh 名称混拉丁名，
        # 「席瓦蓝图」无法直接字符串命中「席瓦 & 神盾 Prime 蓝图」）
        comp = split_component_query(query)
        if comp:
            base_q, cn_word, en_word = comp
            base_item = await self._resolve_wm_item_inner(base_q)
            if base_item:
                prefix = re.sub(r"_set$", "", base_item.get("url_name", ""))
                cands = [it for it in items
                         if it.get("url_name", "").startswith(prefix)
                         and it.get("url_name") != base_item.get("url_name")
                         and (en_word in it.get("url_name", "")
                              or cn_word in norm_wm_name(it.get("zh")))]
                if cands:
                    return min(cands, key=_score)
        close = difflib.get_close_matches(
            low.replace(" ", "_"), [it.get("url_name", "") for it in items],
            n=1, cutoff=0.75)
        if close:
            return next(it for it in items if it.get("url_name") == close[0])
        close_en = difflib.get_close_matches(
            low, [(it.get("en") or "").lower() for it in items], n=1, cutoff=0.8)
        if close_en:
            return next(it for it in items
                        if (it.get("en") or "").lower() == close_en[0])
        # 中文名错别字容忍（波斯顿 -> 伯斯顿）
        zh_names = [(it.get("zh") or "") for it in items]
        zh_hits = fuzzy_hits(query, zh_names, n=1)
        if zh_hits:
            hit = next((it for it in items if it.get("zh") == zh_hits[0]), None)
            if hit:
                hit = dict(hit)
                hit["_fuzzy_from"] = query
                return hit
        return None

    def resolve_lich_weapon(self, query: str) -> Optional[str]:
        """玄骸武器：本地别名 -> slug。

        ★ 2026-09-18 增强：原来只做**精确**匹配（``dict.get(原样字符串)``），
        用户写「赤毒 海克」（中间带空格）或差一个字就查不到 —— 而玄骸武器的
        名字（赤毒·布拉玛 / 信条·弧电离子枪）各种写法满天飞。
        现在按 精确 → 去空格/大小写 → 模糊（形近字）三级兜。
        """
        table = self._aliases.get("lich_items", {})
        q = (query or "").strip()
        if not q:
            return None
        if q in table:
            return table[q]
        norm = re.sub(r"[\s·・]+", "", q).lower()
        # 变体等价形态并入（2026-09-23：沙皇赤毒 / kuva沙皇 这类写法）
        forms = {norm} | set(matching.expand_variants(q))
        for k, v in table.items():
            if re.sub(r"[\s·・]+", "", k).lower() in forms:
                return v
        # 形近字兜底（「赤毒弧电离子枪」→「赤毒·弧电离子枪」）
        near = fuzzy_hits(norm, [re.sub(r"[\s·・]+", "", k).lower()
                                 for k in table], n=1, cutoff=0.75)
        if near:
            for k, v in table.items():
                if re.sub(r"[\s·・]+", "", k).lower() == near[0]:
                    return v
        return None

    def lich_weapon_info(self, slug: str) -> dict:
        """玄骸武器的类型与官方译名（来自 core/data/lich_weapons.json）。

        返回 ``{"type": "lich"|"sister"|"coda", "en": …, "zh": …}``；
        查不到时按 slug 前缀兜底（kuva_→lich、tenet_→sister、coda_→coda）。
        """
        db = self._lich_db()
        hit = db.get(slug)
        if hit:
            return hit
        pre = (slug or "").split("_")[0]
        kind = {"kuva": "lich", "tenet": "sister", "coda": "coda"}.get(pre, "lich")
        return {"type": kind, "en": slug, "zh": slug}

    def _lich_db(self) -> dict:
        """玄骸武器表（延迟加载 + 缓存；缺文件返回空 dict，不炸）。"""
        cache = getattr(self, "_lich_cache", None)
        if cache is not None:
            return cache
        try:
            self._lich_cache = json.loads(
                (DATA_DIR / "lich_weapons.json").read_text(encoding="utf-8"))
        except Exception:                        # noqa: BLE001
            self._lich_cache = {}
        return self._lich_cache

    async def resolve_riven_weapon(self, query: str) -> Optional[dict]:
        """紫卡武器解析：本地别名 + WM v2 紫卡武器表（zh/en），支持 黑话+p。

        2026-09-23 变体解析并入（core/matching）：归一化完全与变体等价两层
        插在「精确」与「子串」之间（赤毒沙皇=赤毒 沙皇、kuva沙皇、沙皇赤毒）。
        ★ p/P 后缀现在**优先返回 Prime 版**——倾向/紫卡类型按变体分别计算，
        「绝路p」绝不能落回 base（旧实现正是回落 base，已修）。
        """
        query = query.strip()
        if not query:
            return None
        weapons = await self.wm_riven_weapons()
        m = self._PRIME_SUFFIX.match(query)
        if m and len(m.group(1).strip()) >= 1:
            base = await self.resolve_riven_weapon(m.group(1).strip())
            if base:
                # 2026-09-23 去掉「回落 base」：倾向/紫卡按变体分别计算，
                # 表中确无该变体条目时返回 None（未找到），绝不冒充本体值。
                return matching.prime_sibling(base, weapons)
        # 未命中则继续走常规链
        alias = self.alias_lookup(query.lower(), "riven_items")
        if alias:
            for w in weapons:
                if w.get("url_name") == alias:
                    return w
        low = query.lower().replace(" ", "_")
        for w in weapons:
            if w.get("zh") == query or (w.get("en") or "").lower() == low:
                return w
        # 归一化完全（去空格/分隔符/大小写；不抹 prime）
        nq = matching.normalize(query)
        if nq:
            for w in weapons:
                if nq in (matching.normalize(w.get("zh") or ""),
                          matching.normalize(w.get("en") or "")):
                    return w
            # 变体等价（p→prime / 语序互换 / zh↔en token）——只做等价变形，
            # 绝不剥 token 配 base（倾向按变体分算）
            forms = set(matching.expand_variants(query)) - {nq}
            if forms:
                for w in weapons:
                    if (matching.normalize(w.get("zh") or "") in forms
                            or matching.normalize(w.get("en") or "") in forms):
                        return w
        for w in weapons:
            if low in w.get("url_name", "") or query in (w.get("zh") or ""):
                return w
        close = difflib.get_close_matches(
            low, [w.get("url_name", "") for w in weapons], n=1, cutoff=0.7)
        if close:
            return next(w for w in weapons if w.get("url_name") == close[0])
        # 中文名错别字容忍
        zh_hits = fuzzy_hits(query, [(w.get("zh") or "") for w in weapons], n=1)
        if zh_hits:
            hit = next((w for w in weapons if w.get("zh") == zh_hits[0]), None)
            if hit:
                hit = dict(hit)
                hit["_fuzzy_from"] = query
                return hit
        return None

    async def suggest_wm_items(self, query: str, n: int = 3) -> list[str]:
        """给「未找到」的 wm 物品查询提供中文名候选。"""
        items = await self.wm_items()
        names = [(it.get("zh") or it.get("en") or it.get("url_name", "")) for it in items]
        return fuzzy_hits(query, names, n=n)

    _VEILED_SLUGS = {
        "Melee": "melee_riven_mod_(veiled)",
        "Rifle": "rifle_riven_mod_(veiled)",
        "Shotgun": "shotgun_riven_mod_(veiled)",
        "Pistol": "pistol_riven_mod_(veiled)",
        "Archgun": "archgun_riven_mod_(veiled)",
        "Kitgun": "kitgun_riven_mod_(veiled)",
        "Zaw": "zaw_riven_mod_(veiled)",
        "Robotic": "companion_weapon_riven_mod_(veiled)",
    }

    async def wm_veiled_stats(self, platform: str = "pc") -> dict:
        """未开紫卡的 WM 实时成交价（各武器类别一条）。

        DE 周报里的「未开」是周报口径（一周一条、明显滞后且偏高——
        组合枪/Zaw 报 590-600p，WM 实时只有个位数 P），所以未开视图
        直接用 WM 的 veiled 条目 48h 成交统计（统计走既有 TTL 缓存）。
        """
        out: dict[str, dict] = {}
        for tkey, slug in self._VEILED_SLUGS.items():
            try:
                st = await self.wm_statistics(slug, platform)
            except Exception:  # noqa: BLE001 - 单类失败不拖垮整张表
                continue
            h48 = list(st.get("h48") or [])
            med = [r["median"] for r in h48 if r.get("median")]
            if not med:
                continue
            mins = [r["min"] for r in h48 if r.get("min")]
            maxs = [r["max"] for r in h48 if r.get("max")]
            out[tkey] = {
                "median": round(sum(med) / len(med), 1),
                "min": min(mins) if mins else 0,
                "max": max(maxs) if maxs else 0,
                "vol": sum(r.get("volume") or 0 for r in h48),
            }
        return out

    _flare_cache: dict[str, tuple[float, str]] = {}

    async def fetch_via_flaresolver(self, url: str, ttl: int = TTL_FLARE) -> str:
        """经 FlareSolverr 抓取被 Cloudflare 拦的页面，返回渲染后的 HTML。

        未配置 FlareSolverr（连接失败）时抛 WarframeAPIError，由调用方降级。
        结果按 URL 做 TTL 内存缓存（FlareSolverr 一次求解要数秒，别反复打）。
        """
        import time as _t
        hit = self._flare_cache.get(url)
        if hit and _t.time() - hit[0] < ttl:
            return hit[1]

        async def _post(base: str, payload: dict) -> dict:
            r = await self._http.post(base, json=payload, timeout=120.0)
            r.raise_for_status()
            return r.json()

        async def _solve(base: str) -> dict:
            payload = {"cmd": "request.get", "url": url,
                       "session": "sdjk", "maxTimeout": 60000}
            data = await _post(base, payload)
            if data.get("status") == "ok":
                return data
            msg = str(data.get("message") or "")
            # 会话不存在 → 建会话重试；挑战求解失败 → 重建会话再试一次
            #（冷启动首次求解常超时，带 cookie 的第二次实测 22s 通过）
            if "session" in msg.lower() or "solving" in msg.lower():
                await _post(base, {"cmd": "sessions.create", "session": "sdjk"})
                data = await _post(base, payload)
            if data.get("status") != "ok":
                data = await _post(base, payload)
            return data

        last_err: Exception | None = None
        data: dict = {}
        if not self._flare_enabled:
            # 面板里关掉了：直接失败，让上层走它自己的降级提示（不静默空过）
            raise WarframeAPIError(
                "FlareSolverr 已在配置里关闭（「启用 CF 绕过代理」= 否）")
        # 记住上次可用的地址，优先复用；全挂才报错
        candidates = ([self._flare_url] if self._flare_url else []) + \
            [u for u in self._flare_urls if u != self._flare_url]
        for base in candidates:
            try:
                data = await _solve(base)
                self._flare_url = base
                break
            except Exception as e:  # noqa: BLE001 - 换下一个候选地址
                last_err = e
                data = {}
        if data.get("status") != "ok" \
                or not (data.get("solution") or {}).get("response"):
            raise WarframeAPIError(
                f"FlareSolverr 不可用/求解失败（{last_err or data.get('message')}）")
        html = data["solution"]["response"]
        self._flare_cache[url] = (_t.time(), html)
        return html

    @staticmethod
    def parse_wiki_valence(html: str) -> dict:
        """从 wiki「Reset」页 HTML 提取 Current Valence Bonuses。

        返回 {"tenet": {英文名: (元素, 百分比)}, "coda_batch": "A"/"B",
        "coda": {英文名: (元素, 百分比)}}。页面同时给出
        「Eleanor is selling Batch X weapons」的当前批标注。
        """
        txt = re.sub(r"<[^>]+>", "|", html)
        txt = txt.replace("&nbsp;", " ")
        txt = re.sub(r"\|+", "|", txt)
        # ★ 安全审查（2026-09-18）：原写法 `(?:\s*\|)+\s*` 是「重复里嵌重复」
        #   （\s* 在 + 里），属于灾难性回溯的典型形态 —— 输入是抓来的 wiki
        #   页面，虽然来源可信，但没必要留这种模式。[\s|]+ 与它语义等价。
        pat = re.compile(
            r"((?:Dual )?Coda \w+|Tenet \w+)[\s|]+"
            r"(Magnetic|Impact|Toxin|Cold|Heat|Electricity|Radiation)"
            r"[\s|]+([\d.]+)%")
        tenet: dict[str, tuple[str, float]] = {}
        coda: dict[str, tuple[str, float]] = {}
        for name, elem, pct in pat.findall(txt):
            val = (elem, float(pct))
            if name.startswith("Tenet"):
                tenet[name] = val
            else:
                coda[name] = val
        # 批次判定优先用 valence 表头「Weapon (Batch B)」——页面同时渲染
        # A/B 两个倒计时模板（"is selling Batch A … until Batch B" 两个都在
        # HTML 里，靠脚本二选一显示），取第一个匹配会判错批
        m = (re.search(r"Weapon\s*\(\s*Batch\s*(\w+)\s*\)", txt)
             or re.search(r"is selling[\s|]*Batch[\s|]*(\w+)", txt))
        return {"tenet": tenet, "coda_batch": m.group(1) if m else "",
                "coda": coda}

    @staticmethod
    def valence_is_stale(data: dict, now=None) -> bool:
        """该段的效价快照是否已过期（不在当前轮次窗口内）。

        窗口 = ``epoch + floor((now - epoch) / period) * period``；
        快照缺失或时间串不可解析都算过期（宁可重抓，也别拿坏值当新鲜）。
        """
        from datetime import datetime, timedelta, timezone
        now = now or datetime.now(timezone.utc)
        epoch = datetime.fromisoformat(data["epoch"])
        period = timedelta(hours=int(data.get("period_hours", 96)))
        win = epoch + (now - epoch) // period * period
        snap = data.get("valence_snapshot")
        if not snap:
            return True
        try:
            return datetime.fromisoformat(snap) < win
        except (TypeError, ValueError):
            return True

    @staticmethod
    def coda_anchor_for(observed_idx: int, epoch, period_hours: int,
                        now, n_batches: int) -> int:
        """由「wiki 上观测到的当前批」反解 ``anchor_idx``。

        ★ 卡面用的是 ``(anchor_idx + 换轮次数) % 批数``，所以 anchor_idx 是
        **相位基准**，不等于「当前批的下标」。旧代码直接存 ``anchor_idx = bi``，
        换轮次数一前进就整体错位一批（09-20 实测：B 批生效时会被算成 A 批）。
        """
        from datetime import timedelta
        passed = int((now - epoch) // timedelta(hours=int(period_hours)))
        return (observed_idx - passed) % n_batches

    async def refresh_valence(self) -> str:
        """检查并刷新信条/终幕元素加成快照（数据源 wiki Reset 页）。

        幂等：快照仍在当前 96h 轮次内则直接返回「fresh」不动文件。
        校验失败（解析残缺）抛异常，绝不写半成品。
        """
        from datetime import datetime, timedelta, timezone
        rot_path = paths.read_path("rotations.json")
        rot = json.loads(rot_path.read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc)
        # ★ 必须**两段都新鲜**才算 fresh，不能「遇到第一个新鲜的就 return」。
        #   信条与终幕的换轮锚点相差 24 小时（epoch 相差一天），所以任何时刻
        #   都必然有一段仍在本轮窗口内 —— 旧写法让自动刷新**永远空转**：
        #   服务器快照停在 2026-09-17，09-19 以来 52 条 sdjk 日志里一条刷新
        #   记录都没有，plugin_data 下也从没写出过 rotations.json。
        _sections = [rot.get(k) for k in ("tenet", "coda")]
        if not any(self.valence_is_stale(s, now) for s in _sections if s):
            return "fresh"
        html = await self.fetch_via_flaresolver(
            "https://wiki.warframe.com/w/Reset", ttl=600)
        parsed = self.parse_wiki_valence(html)
        tenet_data = rot.get("tenet") or {}
        coda_data = rot.get("coda") or {}
        want_t = {it.get("en") for it in tenet_data.get("items") or []}
        if not want_t or not want_t <= set(parsed["tenet"]):
            raise WarframeAPIError(
                f"valence 解析不全：tenet 缺 {want_t - set(parsed['tenet'])}")
        labels = coda_data.get("batch_label") or ["A", "B"]
        batches = coda_data.get("batches") or []
        batch = parsed["coda_batch"]
        if batch not in labels or len(batches) != len(labels):
            raise WarframeAPIError(f"valence 批次标注异常：{batch!r}")
        bi = labels.index(batch)
        coda_data["anchor_idx"] = self.coda_anchor_for(
            bi, datetime.fromisoformat(coda_data["epoch"]),
            int(coda_data.get("period_hours", 96)), now, len(batches))
        want_c = {it.get("en") for it in batches[bi]}
        if not want_c <= set(parsed["coda"]):
            raise WarframeAPIError(
                f"valence 解析不全：coda {batch} 批缺 {want_c - set(parsed['coda'])}")
        snap = now.replace(microsecond=0).isoformat()
        for it in tenet_data.get("items") or []:
            elem, pct = parsed["tenet"][it["en"]]
            it["element"], it["bonus"] = elem, pct
        coda_data["valence_snapshot"] = snap
        for idx, b_items in enumerate(batches):
            for it in b_items:
                if idx == bi:
                    elem, pct = parsed["coda"][it["en"]]
                    it["element"], it["bonus"] = elem, pct
                else:
                    it.pop("element", None)
                    it.pop("bonus", None)
        tenet_data["valence_snapshot"] = snap
        paths.write_path("rotations.json").write_text(json.dumps(rot, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        return f"refreshed {snap} batch={batch}"

    @staticmethod
    def parse_wiki_dispositions(html: str) -> dict:
        """从 wiki「Riven Mod」页 HTML 解析完整倾向表。

        行形态（管道化后）：``|Weapon Name| (0.95)|``。返回
        {英文小写名: 倾向值}，值域过滤 0.05-1.6、名称首字母须为字母。
        """
        txt = re.sub(r"<[^>]+>", "|", html)
        txt = re.sub(r"\|+", "|", txt)
        pat = re.compile(r"\|\s*([A-Za-z][^|()]{0,60}?)\s*\|\s*\((\d(?:\.\d+)?)\)")
        out: dict[str, float] = {}
        for name, val in pat.findall(txt):
            v = float(val)
            if not 0.05 <= v <= 1.6:
                continue
            name = name.strip()
            if 2 <= len(name) <= 60 and re.match(r"^[A-Za-z]", name):
                out[name.lower()] = v
        return out

    async def refresh_wiki_disp(self, max_age_hours: int = 24 * 7,
                                force: bool = False) -> str:
        """检查并刷新 wiki 变体倾向快照（默认 7 天过期）。

        幂等：快照未过期直接返回 "fresh"。解析条数 < 400 视为页面
        结构变化，抛异常不写文件。FlareSolverr 未部署时抛
        WarframeAPIError，由调用方降级（手输倾向仍可用）。
        """
        from datetime import datetime, timezone
        old = self._load_json_file(paths.read_path(WIKI_DISP_NAME)) or {}
        if not force:
            snap = old.get("snapshot")
            if snap:
                try:
                    age = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(snap)).total_seconds()
                    if age < max_age_hours * 3600:
                        return "fresh"
                except ValueError:
                    pass
        html = await self.fetch_via_flaresolver(
            "https://wiki.warframe.com/w/Riven_Mod", ttl=600)
        disp = self.parse_wiki_dispositions(html)
        if len(disp) < 400:
            raise WarframeAPIError(
                f"wiki 倾向表解析异常：仅 {len(disp)} 条（预期 ≥400）")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        meta = {
            "_note": ("武器倾向表（wiki Riven Mod 页 Riven Disposition，覆盖全部"
                      "变体：棱晶/Prime/亡魂/破坏者/赤毒/信条…）。来源 "
                      "wiki.warframe.com（CC BY-NC-SA）；DE 仅在大版本调整倾向，"
                      "插件内置定时每 7 天自动重抓（需 FlareSolverr）"),
            "snapshot": now,
            "disp": disp,
        }
        paths.write_path(WIKI_DISP_NAME).write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        return f"refreshed {len(disp)} 条 @ {now}"


    async def suggest_riven_weapons(self, query: str, n: int = 3) -> list[str]:
        """给「未找到」的紫卡武器查询提供候选。

        fuzzy（错别字容忍）未命中时再做**子串匹配**：像「棱晶欧玛」这种
        变体前缀写法，WM 紫卡表里只有母武器「欧玛」，fuzzy 距离不够但
        子串能命中。
        """
        weapons = await self.wm_riven_weapons()
        names = [(w.get("zh") or w.get("en") or w.get("url_name", ""))
                 for w in weapons]
        hits = fuzzy_hits(query, names, n=n)
        if not hits:
            hits = [nm for nm in names if nm and nm in query][:n]
        return hits

    @staticmethod
    def _family_match(base_zh: str, base_en: str, zh: str, en: str) -> bool:
        """判断 zh/en 是否属于「母武器」base 的同一家族（变体）。

        家族判定：中文名包含母名（棱晶·欧玛 含 欧玛）或英文名以
        空格+母名结尾（Prisma Ohma → Ohma）。纯函数，便于离线测试。
        """
        z = (zh or "").strip()
        e = (en or "").strip().lower()
        if not z and not e:
            return False
        if base_zh and z and base_zh in z:
            return True
        if base_en and e and e.endswith(" " + base_en.lower()):
            return True
        return False

    async def riven_family(self, weapon: dict) -> list:
        """同一武器家族的变体（棱晶/Prime/亡魂…）及其 wiki 倾向。

        游戏内紫卡卡面**只写母武器名**（紫卡对家族通用，可装在棱晶等
        变体上），变体信息不在截图里 —— 所以卡面读到母武器时列出家族
        变体倾向，用户对照即可知道该用哪个（或按需要带变体名重发）。
        返回 [(中文名, 倾向), ...]，按倾向升序。
        """
        base_zh = (weapon.get("zh") or "").strip()
        base_en = (weapon.get("en") or "").strip()
        table = (self._load_json_file(paths.read_path(WIKI_DISP_NAME)) or {}).get("disp") or {}
        if not table or not (base_zh or base_en):
            return []
        try:
            items = await self.wm_items()
        except Exception:  # noqa: BLE001 - WM 挂了就不提示家族
            return []
        tags = set(weapon.get("tags") or [])
        found: dict[str, float] = {}
        for it in items:
            zh = (it.get("zh") or "").strip()
            en = (it.get("en") or "").strip()
            if zh == base_zh or en.lower() == base_en.lower():
                continue
            if not self._family_match(base_zh, base_en, zh, en):
                continue
            if tags and not (tags & set(it.get("tags") or [])):
                continue
            v, _k = await self.resolve_variant_disp(zh)
            if v:
                found.setdefault(zh, v)
        return sorted(found.items(), key=lambda kv: kv[1])

    async def resolve_variant_disp(self, name: str) -> tuple:
        """变体武器倾向：中文名 → WM 物品表转英文 → wiki 倾向快照。

        棱晶/Prime/亡魂/破坏者/赤毒/信条 等变体的倾向在 WM 紫卡表里
        没有条目（紫卡表只挂母武器），wiki「Riven Mod」页的倾向表
        （core/data/de/wiki_disp.json，618 条静态快照）有完整数据。
        返回 (倾向值, 命中的表键)；查不到 (None, "")。
        """
        table = (self._load_json_file(paths.read_path(WIKI_DISP_NAME)) or {}).get("disp") or {}
        if not table or not name:
            return None, ""
        norm = re.sub(r"[\s·]+", "", (name or "").lower())
        if not norm:
            return None, ""
        en = norm
        try:
            items = await self.wm_items()
        except Exception:  # noqa: BLE001 - WM 挂了就走英文名直查
            items = []
        for it in items:
            zh = re.sub(r"[\s·]+", "", (it.get("zh") or "").lower())
            if zh == norm:
                en = (it.get("en") or "").lower()
                break
        key = re.sub(r"[\s\-]+", "", en)
        for k, v in table.items():
            if re.sub(r"[\s\-]+", "", k) == key:
                return v, k
        return None, ""

    # ------------------------------------------------------------------
    # Wiki（本地词库优先，搜不到给搜索链接）
    # ------------------------------------------------------------------
    def wiki_lookup(self, query: str) -> Optional[dict]:
        table = self._aliases.get("wiki", {})
        q = query.strip()
        if q in table:
            return {"title": table[q].get("title", q),
                    "url": table[q]["url"]} if isinstance(table[q], dict) else \
                   {"title": q, "url": table[q]}
        keys = list(table.keys())
        close = difflib.get_close_matches(q, keys, n=1, cutoff=0.6)
        if close and isinstance(table[close[0]], dict):
            return {"title": table[close[0]].get("title", close[0]),
                    "url": table[close[0]]["url"]}
        return None

    async def wiki_search_link(self, query: str) -> str:
        """本地词库未命中时的兜底：返回中文维基搜索链接。"""
        return "https://warframe.huijiwiki.com/w/index.php?search=" + quote(query)
