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
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import difflib
import hashlib
import json
import os
import re
import random
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

try:
    from . import matching  # core 包内正常导入
except ImportError:
    # ★ 只在**非包上下文**（离线脚本把 core/ 当顶层路径）才回退绝对导入；
    #   包内失败 = 真错误，原样抛出（2026-09-25 事故：兜底把真错掩盖成
    #   "No module named 'core'"，见 main.py 同处注释）。
    if __package__:
        raise
    import matching

try:  # 缺依赖时保持模块可导入（离线工具/测试），实例化时再给出明确提示
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from . import de_worldstate, i18n, paths
from . import __version__ as _CORE_VERSION  # UA 版本段随插件版本派生（单一来源）
from .cache import TTLCache
from .logging_compat import logger

DATA_DIR = Path(__file__).resolve().parent / "data"  # 包内静态数据（只读）
# 运行期写盘的文件名（排行落盘 / 快照 / 缓存）：实际路径经 core.paths 解析到
# data/plugin_data/<插件名>，绝不写插件包目录（AstrBot 插件规范要求）。
RANKS_NAME = "wm_ranks.json"  # 价格排行全量落盘（后台爬取）
RIVEN_RT_NAME = "riven_rt.json"  # 紫卡实时排行快照（WM 拍卖；2026-10-23 加）
RIVEN_RT_TOP_PRICES = 20  # 算「前 N 条中位价」的 N
RIVEN_RT_MANUAL_COOLDOWN = 600  # 手动「紫卡排行 刷新」两次启动的最短间隔（秒）
RANK_MIN_COVERAGE = 0.8  # 价格排行整轮成功率 ≥ 此值才刷新 ts（维护者 2026-10-17 定）
WIKI_DISP_NAME = "de/wiki_disp.json"  # wiki 变体倾向快照（2026-10 倾向取消后冻结，只作旧截图判据）
ROTATION_REVISIONS_NAME = "rotation_revisions.json"  # 仅运行目录：已处理的源页修订与业务指纹
ACRITHIS_WEEK_NAME = "de/acrichis_week.json"  # 言录使本周货单（运行期覆盖包内种子）
STOREITEMS_ZH_NAME = "de/storeitems_zh.json"  # StoreItems 路径 → 官方简中名（警报换名）
ACRITHIS_CURRENT_URL = (
    "https://wiki.warframe.com/w/Acrithis/Current_Offerings?action=raw"  # 社区当期 5 件上报页
)
# 该页最近修订时间（防陈旧闸门用；2026-10-05 立——wiki 把 observed 改成了魔法词，
# 恒等于「今天」，原「observed ≥ 本周一」判据失效，改用修订时间戳）
ACRITHIS_REVISION_API = (
    "https://wiki.warframe.com/api.php?action=query&prop=revisions"
    "&titles=Acrithis/Current_Offerings&rvprop=timestamp&rvlimit=1&format=json&maxlag=5"
)
# 信条/终幕源页的最近修订时间（2026-10-18 立）：wiki 是玩家人工上报，换轮后常拖几小时才有人改
# （10-02 拖 12h、10-03 终幕拖 18h、10-18 信条）；页面最后修订早于本轮起点 ⇒ 页面上还是上一轮
VALENCE_REVISION_API = (
    "https://wiki.warframe.com/api.php?action=query&prop=revisions"
    "&titles=Tenet_Weapons|Coda_Weapons&rvprop=timestamp&format=json&maxlag=5"
)
# 效价加成（信条/终幕）的**真源页**：Reset 页那两段是 {{#lst:}} 跨页嵌入、走 MediaWiki
# 解析缓存 —— 换批当天会吐**旧表**（2026-10-15 事故：00:05 对齐刷新抓到上一批，整轮数据
# 钉死）。源页 wikitext 无解析缓存，且 A/B 两批**同页全量**（一次抓取两批齐全）。
ROTATION_PAGES = {
    "tenet": "Tenet Weapons",
    "coda": "Coda Weapons",
    "acrithis": "Acrithis/Current Offerings",
}
ROTATION_REVISION_API = (
    "https://wiki.warframe.com/api.php?action=query&prop=revisions"
    "&titles=Tenet_Weapons|Coda_Weapons|Acrithis/Current_Offerings"
    "&rvprop=ids|timestamp&format=json&maxlag=5"
)
WIKI_RAW_CODA = "https://wiki.warframe.com/w/Coda_Weapons?action=raw"
WIKI_RAW_TENET = "https://wiki.warframe.com/w/Tenet_Weapons?action=raw"
# 社区快照（2026-09-25 方案④）：wiki 这两项数据都在 Cloudflare 盾后，没部署
# FlareSolverr 的用户只能吃随包种子。于是把**服务器上已经抓到的新鲜快照**发布到
# 公开仓的 bot-data 分支（automations/publish_community_snapshot.py），插件在
# 「本地快照过期 + FlareSolverr 不可用/关闭」时读它兜底：
#   · 只读、无凭据、无第三方 SDK，就是一次普通 GET
#   · 校验不过（缺武器/过期/比本地旧）一律**不采用**，仍退回包内种子
#   · 数据来源仍是 wiki（CC BY-NC-SA），文件里带 source/署名
# ★ 常规快照按序试：**raw 主 + jsDelivr 两个镜像** —— 实测（2026-09-26，两个 vantage）：
#   服务器（CN 云）只有 cdn.jsdelivr / gcore.jsdelivr 可达（raw / fastly / statically /
#   raw.githack 全部 12s 超时 HTTP 000）；住宅网络虽然六个都通，但按**最差 vantage**取交集，
#   只保留这两条镜像 + raw。原先的 statically 通道在服务器侧**根本不可用**（名义三通道、实际两通道），
#   已替换为 gcore；两条 jsDelivr 走同一网络但不同边缘，留作互为冗余（当天 cdn 曾整段 502）。
#   2026-10-17 复测：服务器上 raw 已变为**间歇可达**（时而 1.8s 200、时而 12s 超时），失败即冷却 30 分钟改走 jsDelivr。
#   连通性由服务器巡检每轮探针输出矩阵（`server_patrol.py`），不再"装了就算有"。
COMMUNITY_SNAPSHOT_BASE = "https://raw.githubusercontent.com/skyti1437/Warframe-SDJK-Bot/bot-data"
COMMUNITY_VALENCE_URL = f"{COMMUNITY_SNAPSHOT_BASE}/valence.json"  # 主通道
COMMUNITY_ACRITHIS_URL = f"{COMMUNITY_SNAPSHOT_BASE}/acrithis_week.json"
_REPO_SLUG = "skyti1437/Warframe-SDJK-Bot"
# Contents API 的 raw 媒体类型直接返回文件，不跳回 raw 域名；公开仓读取无需凭据。
# https://docs.github.com/en/rest/repos/contents#get-repository-content
COMMUNITY_VALENCE_API_URL = (
    f"https://api.github.com/repos/{_REPO_SLUG}/contents/valence.json?ref=bot-data"
)
COMMUNITY_VALENCE_URLS = (
    COMMUNITY_VALENCE_URL,
    COMMUNITY_VALENCE_API_URL,
    f"https://cdn.jsdelivr.net/gh/{_REPO_SLUG}@bot-data/valence.json",
    f"https://gcore.jsdelivr.net/gh/{_REPO_SLUG}@bot-data/valence.json",
)
COMMUNITY_ACRITHIS_URLS = (
    COMMUNITY_ACRITHIS_URL,
    f"https://cdn.jsdelivr.net/gh/{_REPO_SLUG}@bot-data/acrithis_week.json",
    f"https://gcore.jsdelivr.net/gh/{_REPO_SLUG}@bot-data/acrithis_week.json",
)
# ★ 2026-10-16 维护者定：原本只随版本更新的静态快照也进 bot-data，让用户能拉新的
#   （掉落表 + 遗物表这一组先不做）。文件内容与包内种子**同结构**，校验通过且比本地新才落盘。
#   键 = bot-data 上的文件名；local = 运行目录 / 包内种子的相对路径；stamp = 时间戳字段。
# ★ 2026-10-22（V06 后半）：静态快照的结构版本号（与发布端同值、同判据）。
COMMUNITY_STATIC_SCHEMA = 1

COMMUNITY_STATIC = {
    "arb_ratings.json": {"local": "arb_ratings.json", "stamp": "_updated", "label": "仲裁评级"},
    "baro_history.json": {
        "local": "baro_history.json",
        "stamp": "fetched",
        "label": "奸商到访历史",
    },
}


# 运维面板展示顺序 + 中文名；检查结论取值（面板按此上色）
COMMUNITY_FILES = (
    ("valence.json", "元素加成"),
    ("acrithis_week.json", "言录使货单"),
    ("arb_ratings.json", "仲裁评级"),
    ("baro_history.json", "奸商到访历史"),
)
COMMUNITY_RESULTS = {
    "adopted": "已采用新快照",
    "fresh": "远端不比本地新",
    "local_ok": "本地仍有效，本次未拉取",
    "stale": "远端是上一轮的旧快照",
    "invalid": "校验不过，未采用",
    "unavailable": "取不到",
    "unchecked": "本次启动后还没检查",
}

COMMUNITY_COOLDOWN = 1800  # 社区通道网络失败后冷却（秒）
COMMUNITY_BUDGET = 30.0  # 一次社区取数的总时间预算（秒）
# 单条通道的上限（秒）：ZCode 10-16 线上发现 raw 卡住时一个文件就吃满 30 s 总预算、镜像连试都没试
COMMUNITY_PER_CHANNEL = 10.0


def community_static_urls(name: str) -> tuple[str, ...]:
    return (
        f"{COMMUNITY_SNAPSHOT_BASE}/{name}",
        f"https://cdn.jsdelivr.net/gh/{_REPO_SLUG}@bot-data/{name}",
        f"https://gcore.jsdelivr.net/gh/{_REPO_SLUG}@bot-data/{name}",
    )


async def resolve_public_url(url: str) -> tuple[str, tuple[str, ...]]:
    """校验自填地址；返回拒绝原因及同一次解析得到的公网 IP 元组。

    只收 http(s)；拒绝带账号密码的地址；IP 直写或 DNS 解析结果落在私网 / 回环 / 链路本地 /
    保留 / 组播段的一律拒绝（任一解析结果命中即拒）。下载方必须连接返回的 IP，
    不得再次按域名连接。
    """
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    try:
        u = urlsplit(str(url or ""))
        port = u.port
    except ValueError:
        return "地址格式不对", ()
    if u.scheme not in ("http", "https") or not u.hostname:
        return "只支持 http(s) 地址", ()
    if u.username is not None or u.password is not None:
        return "地址里不能带账号密码", ()

    def _bad(ip: str) -> bool:
        try:
            if "%" in ip:
                return True
            a = ipaddress.ip_address(ip)
        except ValueError:
            return True
        # mapped IPv6 先按其 IPv4 地址分类，避免各 Python 版本对 CGNAT 的属性差异。
        a = getattr(a, "ipv4_mapped", None) or a
        return (
            not a.is_global
            or a.is_private
            or a.is_loopback
            or a.is_link_local
            or a.is_reserved
            or a.is_multicast
            or a.is_unspecified
        )

    host = u.hostname
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM
            )
        except (OSError, UnicodeError, ValueError):
            return "域名解析失败", ()
        ips = [i[4][0] for i in infos]
    if not ips or any(_bad(ip) for ip in ips):
        return "地址指向内网 / 本机（如确需使用内网镜像，在配置里开启 allow_private_urls）", ()
    return "", tuple(dict.fromkeys(ips))


async def private_url_reason(url: str) -> str:
    """旧入口仅作地址预检；字体下载通过 resolve_public_url 和 font_url_transport 固定目标。"""
    reason, _ = await resolve_public_url(url)
    return reason


def font_url_transport(proxy: str | None = None, *, network_backend=None, ssl_context=None):
    """字体直链专用传输：HTTPX 保留域名语义，HTTPcore 仅连接已校验 IP。

    只用公开 transport/pool/backend 接口；后端注入供离线协议测试，不参与面板配置。
    池按原域名隔离，代理的 CONNECT / SOCKS 目标也固定，TLS 始终校验原域名。
    """
    import httpcore
    from urllib.request import getproxies, proxy_bypass

    # 不能让 AsyncClient 的环境代理 mount 绕过此传输；在这里选择环境代理。
    environment = getproxies() if proxy is None else {}
    context = ssl_context or httpx.create_ssl_context()
    core_errors = (
        httpcore.TimeoutException,
        httpcore.NetworkError,
        httpcore.ProtocolError,
        httpcore.ProxyError,
        httpcore.UnsupportedProtocol,
    )

    def mapped(exc, request):
        kind = getattr(httpx, type(exc).__name__, httpx.TransportError)
        return kind(str(exc), request=request)

    class Stream(httpcore.AsyncNetworkStream):
        def __init__(self, stream, tls_names):
            self.stream, self.tls_names = stream, list(tls_names)

        async def read(self, max_bytes, timeout=None):
            return await self.stream.read(max_bytes, timeout)

        async def write(self, buffer, timeout=None):
            await self.stream.write(buffer, timeout)

        async def aclose(self):
            await self.stream.aclose()

        def get_extra_info(self, info):
            return self.stream.get_extra_info(info)

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            if not self.tls_names:
                raise httpcore.ConnectError("字体连接出现额外 TLS 层")
            hostname = self.tls_names.pop(0)
            stream = await self.stream.start_tls(ssl_context, hostname, timeout)
            return Stream(stream, self.tls_names)

    class Backend(httpcore.AsyncNetworkBackend):
        def __init__(self, tls_names):
            self.base = network_backend or httpcore.AnyIOBackend()
            self.tls_names = tls_names

        async def connect_tcp(
            self, host, port, timeout=None, local_address=None, socket_options=None
        ):
            stream = await self.base.connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
            return Stream(stream, self.tls_names)

        async def sleep(self, seconds):
            await self.base.sleep(seconds)

    class Body(httpx.AsyncByteStream):
        def __init__(self, stream, request):
            self.stream, self.request = stream, request

        async def __aiter__(self):
            try:
                async for chunk in self.stream:
                    yield chunk
            except core_errors as exc:
                raise mapped(exc, self.request) from exc

        async def aclose(self):
            await self.stream.aclose()

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.pools = {}

        async def handle_async_request(self, request):
            reason, ips = await resolve_public_url(str(request.url))
            if reason:
                raise httpx.RequestError(f"字体地址不可用：{reason}", request=request)
            ip = ips[0]  # 失败关闭；不回退域名重解析或未经校验的地址。
            selected = proxy
            if selected is None and not proxy_bypass(request.url.netloc.decode("ascii")):
                selected = environment.get(request.url.scheme) or environment.get("all")
            parsed_proxy = httpx.Proxy(selected) if selected else None
            key = (request.url.scheme, request.url.host, request.url.port, ip, selected)
            pool = self.pools.get(key)
            if pool is None:
                names = []
                if parsed_proxy and parsed_proxy.url.scheme == "https":
                    names.append(parsed_proxy.url.host)
                if request.url.scheme == "https":
                    names.append(request.url.host)
                opts = dict(
                    ssl_context=context,
                    network_backend=Backend(names),
                    http2=False,
                    max_keepalive_connections=0,
                )
                if parsed_proxy:
                    pu = parsed_proxy.url
                    opts.update(
                        proxy_url=httpcore.URL(
                            scheme=pu.raw_scheme, host=pu.raw_host, port=pu.port, target=pu.raw_path
                        ),
                        proxy_auth=parsed_proxy.raw_auth,
                    )
                    if pu.scheme in ("http", "https"):
                        pool = httpcore.AsyncHTTPProxy(
                            **opts,
                            proxy_headers=parsed_proxy.headers.raw,
                            proxy_ssl_context=parsed_proxy.ssl_context,
                        )
                    elif pu.scheme in ("socks5", "socks5h"):
                        pool = httpcore.AsyncSOCKSProxy(**opts)
                    else:
                        raise ValueError("不支持的字体下载代理类型")
                else:
                    pool = httpcore.AsyncConnectionPool(**opts)
                self.pools[key] = pool
            # HTTPcore 1.x 的代理 authority 不自动括 IPv6；仅代理的远端目标需括号。
            host = ip
            if parsed_proxy and parsed_proxy.url.scheme in ("http", "https") and ":" in ip:
                host = f"[{ip}]"
            headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"host"]
            headers.append((b"Host", request.headers["Host"].encode("ascii")))
            extensions = dict(request.extensions)
            extensions.pop("sni_hostname", None)  # 两层代理 TLS 由 Stream 分别处理。
            req = httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=host.encode("ascii"),
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=headers,
                content=request.stream,
                extensions=extensions,
            )
            try:
                response = await pool.handle_async_request(req)
            except core_errors as exc:
                raise mapped(exc, request) from exc
            return httpx.Response(
                response.status,
                headers=response.headers,
                stream=Body(response.stream, request),
                extensions=response.extensions,
            )

        async def aclose(self):
            for pool in self.pools.values():
                await pool.aclose()

    return Transport()


def normalize_community_base(raw: str) -> str:
    """用户自填的社区数据地址：只收 http(s)，去掉末尾斜杠；不合法返回空串（= 用默认 bot-data）。"""
    v = str(raw or "").strip().rstrip("/")
    return v if re.match(r"^https?://[^\s/]+(/\S*)?$", v) else ""


def _stamp_dt(v):
    """时间戳字段 → aware datetime（ISO 串或 YYYY-MM-DD；不认识返回 None）。"""
    from datetime import datetime, timezone

    try:
        d = datetime.fromisoformat(str(v).strip())
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def community_static_ok(name: str, d) -> tuple[bool, str]:
    """静态快照的结构校验（纯函数，离线可测）。不过就不采用。"""
    if not isinstance(d, dict):
        return False, "不是 JSON 对象"
    spec = COMMUNITY_STATIC.get(name)
    if spec is None:
        return False, "未知快照"
    # ★ 2026-10-22（GPT 评估 V06 后半）：结构版本号，与发布端
    #   `publish_community_snapshot.py::static_ok` **同口径** —— **缺省视为 1**
    #   （兼容旧快照），未知的**更高**版本拒收并给原因（免得新版结构被本端误读）。
    _ver = d.get("schema", COMMUNITY_STATIC_SCHEMA)
    if not isinstance(_ver, int) or isinstance(_ver, bool) or _ver < 1:
        return False, f"schema 不是正整数：{_ver!r}"
    if _ver > COMMUNITY_STATIC_SCHEMA:
        return False, f"schema {_ver} 高于本端支持的 {COMMUNITY_STATIC_SCHEMA}"
    from datetime import datetime, timedelta, timezone

    _st = _stamp_dt(d.get(spec["stamp"]))
    if _st is None:
        return False, f"缺时间戳 {spec['stamp']}"
    # GPT 评估 V06：未来时间的快照会永远「比本地新」、压住之后的正常快照 ⇒ 超过 1 天的未来时间拒收
    if _st > datetime.now(timezone.utc) + timedelta(days=1):
        return False, f"时间戳在未来：{d.get(spec['stamp'])}"
    if name == "arb_ratings.json":
        nodes = d.get("nodes")
        if not isinstance(nodes, dict) or len(nodes) < 50:
            return False, "节点太少"
        if not all(isinstance(v, dict) and isinstance(v.get("n"), int) for v in nodes.values()):
            return False, "节点缺样本数 n"
        tiers = {"S", "A+", "A", "A-", "F"}
        if any(v.get("tier") not in (None, *tiers) for v in nodes.values()):
            return False, "档位取值不认识"
    elif name == "baro_history.json":
        visits, items = d.get("visits"), d.get("items")
        if not isinstance(visits, list) or len(visits) < 10:
            return False, "到访记录太少"
        if not isinstance(items, dict) or len(items) < 50:
            return False, "物品太少"
    return True, ""


def _invalidate_static(name: str) -> None:
    """静态快照换新后清进程内缓存。"""
    try:
        if name == "arb_ratings.json":
            from . import arbi

            arbi.reset_measured()
        elif name == "baro_history.json":
            from . import baro

            baro.reset()
    except Exception:  # noqa: BLE001 - 清缓存失败最多是下次重启才生效
        pass


# Cloudflare 绕过代理（FlareSolverr）：wiki.warframe.com 等对非浏览器 403。
# 服务器上跑一个 flaresolverr 容器后自动启用；插件跑在 astrbot 容器里，
# 127.0.0.1 到不了宿主机端口，所以按候选顺序试（容器名 → docker0 网关 → 本机）。
# ★ 这些只是**默认值**：开源版用户可以在配置面板里自己填地址
#   （``flaresolverr_urls``，逗号/换行分隔），或直接关掉（``flaresolverr_enabled``）。
FLARESOLR_URLS = (
    [os.environ["WF_FLARESOLR"]]
    if os.environ.get("WF_FLARESOLR")
    else ["http://flaresolverr:8191/v1", "http://172.17.0.1:8191/v1", "http://127.0.0.1:8191/v1"]
)
TTL_FLARE = 3600

# ---- StoreItems 反查表（2026-10-04）：警报奖励换名的第一级 ----
# 构建期产物（scripts/build_storeitems_zh.py）；带 mtime 守卫，热重载后自动重读。
_STOREITEMS_ZH: Optional[dict] = None
_STOREITEMS_ZH_STAMP: Optional[float] = None


def store_items_path_variants(path: str) -> list[str]:
    """StoreItems 路径的等价形态（查表用，顺序：原样 → 去 StoreItems 段）。

    警报奖励路径多一段 ``/Lotus/StoreItems``，而 DE 导出键是 ``/Lotus/Types/...``
    （2026-10-04 实测：导出里 0 个键以 /Lotus/StoreItems 开头；
    另有 ``/Lotus/Types/StoreItems/...`` 变体）。
    """
    out = [path]
    if path.startswith("/Lotus/StoreItems/"):
        out.append("/Lotus/" + path[len("/Lotus/StoreItems/") :])
    if path.startswith("/Lotus/Types/StoreItems/"):
        out.append("/Lotus/Types/" + path[len("/Lotus/Types/StoreItems/") :])
    return out


def store_item_zh(path: str) -> str:
    """StoreItems 路径 → 官方简中名（查不到返回 ''，绝不造名）。"""
    global _STOREITEMS_ZH, _STOREITEMS_ZH_STAMP
    try:
        p = paths.read_path(STOREITEMS_ZH_NAME)
        stamp = p.stat().st_mtime
    except OSError:
        return ""
    if _STOREITEMS_ZH is None or _STOREITEMS_ZH_STAMP != stamp:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:  # noqa: BLE001
            logger.warning("[sdjk] storeitems_zh.json 读取失败：%s", e)
            return ""
        _STOREITEMS_ZH = data.get("items") or {}
        _STOREITEMS_ZH_STAMP = stamp
    for k in store_items_path_variants(path):
        v = _STOREITEMS_ZH.get(k)
        if v:
            return v
    return ""


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


# ---------------------------------------------------------------------------
# 社区快照（公开仓 bot-data 分支）的校验 —— 全是纯函数，离线可单测
# ---------------------------------------------------------------------------
# 纪律与两条刷新路径一致：**校验不过就绝不采用**（宁可退回随包种子，也不拿
# 残缺/过期/更旧的数据覆盖已有快照）。
def parse_community_valence(payload: dict) -> dict:
    """把发布的 ``valence.json`` 变成与 ``parse_wiki_valence`` 同形的解析结果。

    返回 ``{"tenet": {en: (elem, bonus)}, "coda": {...}, "coda_batch": "B",
    "snapshot": <ISO>}``；任何缺失/畸形抛 ``ValueError``。
    """
    if not isinstance(payload, dict):
        raise ValueError("社区快照不是对象")
    out: dict[str, dict] = {}
    for tag in ("tenet", "coda"):
        sect = payload.get(tag)
        if not isinstance(sect, dict):
            raise ValueError(f"社区快照缺 {tag}")
        items = sect.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError(f"社区快照 {tag} 无物品")
        m: dict[str, tuple[str, float]] = {}
        for it in items:
            if not isinstance(it, dict):
                raise ValueError(f"{tag} 条目畸形")
            en, elem, bonus = it.get("en"), it.get("element"), it.get("bonus")
            if not en or not elem or not isinstance(bonus, (int, float)):
                raise ValueError(f"{tag} 条目缺字段：{en!r}")
            m[str(en)] = (str(elem), float(bonus))
        out[tag] = m
    batch = payload.get("coda", {}).get("batch")
    if not isinstance(batch, str) or not batch:
        raise ValueError("社区快照缺 coda.batch")
    out["coda_batch"] = batch
    out["snapshot"] = str(payload.get("published_at") or "")
    # ★ 2026-10-19 分段时间戳：信条与终幕各自换轮（epoch 差一天），一个 published_at 表达不了两段。
    #   新发布器给每段写 `<段>.snapshot`（= 发布端运行期该段的 valence_snapshot）；旧快照没有 ⇒
    #   回退 published_at（兼容旧发布器）。published_at 本身保留（= 两段较晚者，供旧版插件用）。
    out["snapshots"] = {
        tag: str((payload.get(tag) or {}).get("snapshot") or out["snapshot"])
        for tag in ("tenet", "coda")
    }
    return out


# WFHelper 公开接口（2026-10-18 ZCode 调研；后台可选，默认关）：免登录、无 Cloudflare，直接给信条 5 件与
# 终幕 A/B 两批的元素 / 加成。它自己注明数据同样来自 wiki 玩家上报 ⇒ **不比 wiki 快**，用处是给没装
# FlareSolverr、又不想只靠本项目社区快照的使用者多一条通道（第三方站点，稳定性由对方负责）。
WFHELPER_VALENCE_URL = "https://api.wfhelper.com/v1/adversary-vendors"


def parse_wfhelper_valence(payload: dict) -> dict:
    """WFHelper ``adversary-vendors`` → ``{"tenet": {en: (elem, bonus)}, "coda": {批次标签: {...}}}``。

    ``coda`` / ``codaNext`` 两段各带 ``batch``（A/B），两批都收；任何缺失/畸形抛 ``ValueError``。
    """
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError("WFHelper 返回不是 ok 对象")

    def _items(sect, tag):
        items = (sect or {}).get("items") if isinstance(sect, dict) else None
        if not isinstance(items, list) or not items:
            raise ValueError(f"WFHelper {tag} 无物品")
        m: dict[str, tuple[str, float]] = {}
        for it in items:
            if not isinstance(it, dict):
                raise ValueError(f"WFHelper {tag} 条目畸形")
            en, elem, bonus = it.get("name"), it.get("element"), it.get("bonus")
            if not en or not elem or isinstance(bonus, bool) or not isinstance(bonus, (int, float)):
                raise ValueError(f"WFHelper {tag} 条目缺字段：{en!r}")
            m[str(en)] = (str(elem), float(bonus))
        return m

    out = {"tenet": _items(payload.get("tenet"), "tenet"), "coda": {}}
    for key in ("coda", "codaNext"):
        sect = payload.get(key)
        batch = sect.get("batch") if isinstance(sect, dict) else None
        if not isinstance(batch, str) or not batch:
            raise ValueError(f"WFHelper {key} 缺 batch")
        out["coda"][batch] = _items(sect, key)
    return out


def community_valence_newer(snapshot: str, *locals_: str) -> bool:
    """发布的快照时间是否**严格新于**本地各段 —— 防「拿旧的覆盖新的」。

    任何一个时间戳解析不了都返回 False（保守：不用它）。
    """
    from datetime import datetime

    try:
        pub = datetime.fromisoformat(snapshot)
    except (TypeError, ValueError):
        return False
    for raw in locals_:
        try:
            loc = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            continue  # 本地没有时间戳 → 不构成拒绝理由
        if pub <= loc:
            return False
    return True


_ENGLISH_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _parse_acrithis_observed(value: str):
    """社区货单日期固定用英文月份或 ISO 日期，不受宿主 LC_TIME 影响。"""
    from datetime import datetime, timezone

    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", value.strip())
    try:
        if match:
            month = tuple(name.lower() for name in _ENGLISH_MONTHS).index(match[1].lower()) + 1
            return datetime(int(match[3]), month, int(match[2]), tzinfo=timezone.utc)
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def community_acrithis_ok(payload: dict, now=None) -> tuple[bool, str]:
    """发布的言录使货单是否可用：未过期 + 有物品 + observed 不早于本周一。"""
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict):
        return False, "不是对象"
    try:
        exp = datetime.fromisoformat(str(payload.get("expiry") or ""))
    except ValueError:
        return False, "expiry 缺失/畸形"
    if exp.tzinfo is None:
        return False, "expiry 缺时区"
    if exp <= now:
        return False, f"已过期（{payload.get('expiry')}）"
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return False, "无物品"
    if any(
        not isinstance(it, dict) or not isinstance(it.get("name"), str) or not it["name"].strip()
        for it in items
    ):
        return False, "有条目缺 name"
    for it in items:
        for field in ("price", "qty"):
            value = it.get(field)
            if field == "qty" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                return False, f"{field} 非法"
    obs_raw = str(payload.get("observed") or "")
    if obs_raw:
        obs = _parse_acrithis_observed(obs_raw)
        # 解析不了的 observed 不算问题（expiry 才是硬判据）
        if obs is not None:
            monday = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if obs < monday:
                return False, f"observed 早于本周一（{obs_raw}）"
    return True, "ok"


_DE_RIVEN_PLATFORM = {
    "pc": "PC",
    "ps": "PS4",
    "ps4": "PS4",
    "xb": "XB1",
    "xb1": "XB1",
    "sw": "SWITCH",
    "switch": "SWITCH",
}
TTL_RIVEN_WEEKLY = 6 * 3600


def _de_riven_platform(platform: str) -> str:
    """统一解析器平台码、旧接口别名和官方文件平台名。"""
    return _DE_RIVEN_PLATFORM.get(str(platform or "pc").strip().lower(), "PC")


DEFAULT_DE_WORLDSTATE = "https://api.warframe.com/cdn/worldState.php"
DEFAULT_WORLDSTATE_BASE = "https://api.warframestat.us"
DEFAULT_WM_V2_BASE = "https://api.warframe.market/v2"
DEFAULT_WM_BASE = "https://api.warframe.market/v1"  # 仅拍卖（v2 未开放拍卖端点）
# browse.wf 的 oracle：赏金轮换 / 扎里曼派系 / 各派系本轮节点与挑战。
# 站点对无 UA 的请求直接 403，所以必须带一个像浏览器的 UA。
# 算法开源在 github.com/calamity-inc/wf.browse.oracle（.pluto 脚本可读）。
DEFAULT_ORACLE_BASE = "https://oracle.browse.wf"
_ORACLE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# 错别字容忍：中文名/英文名的模糊命中
# ---------------------------------------------------------------------------
def fuzzy_hits(
    query: str, candidates: Iterable[str], n: int = 3, cutoff: Optional[float] = None
) -> list[str]:
    """在候选名里找与 query 形近的项（支持中文错别字，如 波斯顿→伯斯顿）。

    分档阈值：>=4 字 0.70 / 3 字 0.60 / **2 字 0.75**（且要求至少一个字相同，
    否则「绝路」会把「绝望」也算进来）。
    ★ 2026-10-05：2 字档由 0.50 提到 0.75 —— 两个字里只共享一个字的 difflib
      ratio **恰好 = 0.5**，0.50 等于「共用一个字即命中」（`wm 圣剑` 被硬配到
      「剑风」reach、`wm 呼风` 配到「旋风」）。0.75 起：2 字对 2 字需全同（1.0）、
      2 字对 3 字需共享两字（0.8）；表里真实存在的 2 字名走 zh_contains 精确命中，
      不依赖本档（实测 125/125 不受影响）。
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    pool = [c for c in candidates if c and abs(len(c) - len(q)) <= 2]
    if not pool:
        return []
    if cutoff is None:
        cutoff = 0.70 if len(q) >= 4 else (0.60 if len(q) == 3 else 0.75)
    hits = difflib.get_close_matches(q, pool, n=max(n * 3, n), cutoff=cutoff)
    if len(q) == 2:
        hits = [h for h in hits if set(h) & set(q)]
    return hits[:n]


# 各端点 TTL（秒）：周期类短缓存，字典类长缓存
TTL_WORLDSTATE_FAST = 30  # 裂隙/警报/周期等
TTL_WORLDSTATE_MID = 120  # 突击/奸商/电波等
TTL_DE_RAW = 30  # DE 原始包（全端点共用）
TTL_WM_ITEMS = 24 * 3600  # WM 物品字典
TTL_WM_ORDERS = 90  # 订单
TTL_WM_AUCTIONS = 90  # 紫卡拍卖
TTL_WM_STATS = 1800  # v1 价格统计（48h/90d）
TTL_WM_DUCATS = 30 * 60  # v1 tools/ducats 榜单（整点更新一次）
TTL_WIKI = 6 * 3600

USER_AGENT = (
    f"warframe-sdjk/{_CORE_VERSION} "
    "(AstrBot plugin; +https://github.com/skyti1437/Warframe-SDJK-Bot)"
)

# 内部平台码 -> WM v2 Platform 头
_WM_PLATFORM = {
    "pc": "pc",
    "ps": "ps4",
    "ps4": "ps4",
    "xb": "xbox",
    "xbox": "xbox",
    "sw": "switch",
    "switch": "switch",
}

# DE 源不包含、且外部独立源（10o.io）被网络环境阻断时给出降级提示。
# 文案要求：① 说明为什么没有 ② **给出可执行的替代指令** —— 只说「不可用」
# 等于把用户堵死，本项目把「静默/无出路」视为最高优先级缺陷。
# ★ 提示里提到的每个指令名都必须是 COMMAND_ALIASES 里真实存在的，
#   否则等于把用户引到第二条死路（曾误写「钢铁」「资源」两个不存在的指令）。
_EXTERNAL_ONLY = {
    "kuva": "赤毒虹吸数据源（10o.io）已停摆，DE 官方数据也不含此表，暂时查不到实时虹吸。\n"
    "可用的替代：\n"
    "· 赤毒武器 / 姐妹武器的融合数值 →「融合 电60 火58」或「玄骸」\n"
    "· 在售赤毒武器价格 →「wm 赤毒 布拉玛」\n"
    "· 每周轮换奖励 →「周报」查看各周期内容",
    "arbitration": "仲裁实时数据源（10o.io）已停摆。\n"
    "可用替代：\n"
    "· 「仲裁」查看当前与下一小时场次（已改用 arbi.wf.wiki 确定性排期）\n"
    "· 「仲裁表」查看整周排期\n"
    "· 「仲裁 生存」按任务类型筛选",
    "steelPath": "钢铁之路轮换需要外部数据源，DE 直连模式下拿不到。\n"
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
    ("头部神经光元", "neuroptics"),
    ("神经光元", "neuroptics"),
    ("蓝图", "blueprint"),
    ("机体", "chassis"),
    ("系统", "systems"),
    ("头盔", "helmet"),
    ("枪管", "barrel"),
    ("枪机", "receiver"),
    ("枪托", "stock"),
    ("枪膛", "barrel"),
    ("刀刃", "blade"),
    ("护手", "guard"),
    ("刃", "blade"),
    ("握柄", "hilt"),
    ("握把", "grip"),
    ("剑柄", "hilt"),
    ("弓弦", "string"),
    ("弓臂", "limb"),
    ("连结部", "link"),
    ("卷线器", "coil"),
    ("星体", "orbiter"),
]


def split_component_query(query: str) -> Optional[tuple[str, str, str]]:
    """把「席瓦蓝图」拆成 (基名「席瓦」, 部件词「蓝图」, 部件英文词 blueprint)。

    不是「基名+部件词」结构返回 None。
    """
    q = query.strip()
    for cn, en in COMPONENT_WORDS:
        if q.endswith(cn) and len(q) > len(cn):
            return q[: -len(cn)].strip(), cn, en
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
    exact_en = [it for it in items if it.get("en") and norm_wm_name(it.get("en")) == qn]
    if exact_en:
        return min(_prefer(exact_en), key=_score)

    def _contains_ok(name: str) -> bool:
        """包含匹配的护栏（2026-10-05）：只认「**前缀**」或「近似整名」。

        ★ 中段子串会静默劫持：实测 `wm Garap` 的 `garap` 是
          `akjagaraprimeset`（Akjagara Prime Set）的中段子串 ⇒ 被配到
          akjagara_prime_set；真值是 gara_prime_set（`garap` 是它的前缀）。
        「近似整名」保留「只写名字后半段」的查询（如 `wm 膛室 → 分裂膛室`）。
        """
        n = norm_wm_name(name)
        if not n or qn not in n:
            return False
        return n.startswith(qn) or len(qn) >= len(n) - 4

    contains = [
        it
        for it in items
        if _contains_ok(it.get("zh")) or (it.get("en") and _contains_ok(it.get("en")))
    ]
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


# ---------------------------------------------------------------------------
# 紫卡武器变体名的中文补全（2026-09-24）
#
# dispositions_rivenmirror.json 的补全条目里有 48 条没有中文名（Prime 系新
# 武器 / MK1 系 / 组合枪 (Primary)(Secondary) / 守护武器 / Kuva 系…），
# 倾向卡会直接漏出英文（用户实测：翠雀家族里「Larkspur Prime 倾向 0.80 步枪」）。
# 补全来源逐级降级，**补不到就保持英文，不猜**。
# ---------------------------------------------------------------------------

# 组合枪腔体名（KB 组合枪部件表口径：08 其他与机制术语）。
# ⚠ Gaze 在 KB 另有一处「凝视」，组合枪部件节用的是「凝目」，按部件节取。
_KITGUN_CHAMBER_ZH = {
    "catchmoon": "捕月",
    "gaze": "凝目",
    "rattleguts": "响胆",
    "sporelacer": "孢射",
    "tombfinger": "墓指",
    "vermisplicer": "虫置",
}

# 派系/品质前缀（WM/DE 官方写法：棱晶·什长双枪 / 赤毒·低音爆囊 / 终幕·血肢）
_VARIANT_PREFIX_ZH = (
    ("prisma", "棱晶·"),
    ("kuva", "赤毒·"),
    ("tenet", "信条·"),
    ("coda", "终幕·"),
    ("sancti", "圣洁·"),
    ("rakta", "绯红·"),
    ("telos", "终极·"),
    ("synoid", "共生·"),
    ("vaykor", "惩击·"),
    ("secura", "保障·"),
    ("vandal", None),
)
# 品质后缀（官方写法：马谢特砍刀·亡魂 / 凯旋将军·破坏者）
_VARIANT_SUFFIX_ZH = (("prime", " Prime"), ("vandal", "·破坏者"), ("wraith", "·亡魂"))


def _norm_name_key(s: str) -> str:
    """变体名查表用归一化：小写 + 把 -/_/空白统一成单空格。"""
    return re.sub(r"[\s\-_]+", " ", (s or "").strip().lower())


def localize_variant_en(en: str, base_zh: dict) -> str:
    """英文变体名 → 中文写法（母武器官方简中 + 变体词拼装）。

    Args:
        en: 英文名，如 ``Larkspur Prime`` / ``MK1-Braton`` /
            ``Tombfinger (Secondary)`` / ``Kuva Grattler``。
        base_zh: ``{归一化英文基名: 官方简中}``（母武器译名查询表）。

    Returns:
        中文写法；规则不认识或基名查不到时返回空串（调用方保持英文）。
    """
    name = (en or "").strip()
    if not name:
        return ""

    def _base(key: str) -> str:
        return base_zh.get(_norm_name_key(key), "")

    # 组合枪腔体：Tombfinger (Secondary) → 墓指（次要）
    m = re.match(r"^(.+?)\s*\((Primary|Secondary)\)$", name, re.I)
    if m:
        b = _KITGUN_CHAMBER_ZH.get(_norm_name_key(m.group(1))) or _base(m.group(1))
        if b:
            return b + ("（主要）" if m.group(2).lower() == "primary" else "（次要）")
        return ""
    # MK1- 系：MK1-Braton → MK1-布莱顿
    m = re.match(r"^MK1-(.+)$", name, re.I)
    if m:
        b = _base(m.group(1))
        return f"MK1-{b}" if b else ""
    low = name.lower()
    # 前缀系：Prisma / Kuva / Tenet / Coda / 派系武器
    for pre, zhprefix in _VARIANT_PREFIX_ZH:
        if zhprefix and low.startswith(pre + " "):
            b = _base(name[len(pre) + 1 :])
            return f"{zhprefix}{b}" if b else ""
    # 后缀系：Prime / Vandal / Wraith
    for suf, zhsuffix in _VARIANT_SUFFIX_ZH:
        if low.endswith(" " + suf):
            b = _base(name[: -(len(suf) + 1)])
            return f"{b}{zhsuffix}" if b else ""
    return ""


_name_en_zh_cache: "dict | None" = None


def _name_en_zh() -> dict:
    """本地 DE 官方简中表（``core/data/de/name_en_zh.json``，缓存）。"""
    global _name_en_zh_cache
    if _name_en_zh_cache is None:
        try:
            raw = json.loads((DATA_DIR / "de" / "name_en_zh.json").read_text(encoding="utf-8"))
            _name_en_zh_cache = raw.get("names") or {}
        except Exception:  # noqa: BLE001 - 缺文件只降级
            _name_en_zh_cache = {}
    return _name_en_zh_cache


# ---------------------------------------------------------------------------
# ★ 2026-10-03（紫卡变体倾向走本地表）：中英名映射**不再经 WM 物品表**。
#   玄骸武器（赤毒/信条/终幕）本体不可交易 —— WM 全量 3892 条里 **0 条武器**
#   ⇒ 绕 WM 的映射必然拿不到它们的英文名/中文名，家族候选与手输变体名双路全挂
#   （「鳄神」卡反推 0.8 = 赤毒·鳄神 无匹配；手输「赤毒·鳄神」取不到 0.8）。
#   改用本地三表**级联**（任一命中即用）：
#     · core/data/weapons_stats.json   {uniqueName: {name(en), zh}}
#     · core/data/de/name_en_zh.json   键为小写英文名
#     · core/data/de/de_items_zh.json
#   ★ 三表全无的 232 件里有倾向的仅 34 件（Kuva Ghoulsaw / Coda Bubonico /
#     Afentis Prime / Zaw 部件 …）⇒ 退回英文名显示，**绝不丢候选**（用户红线）。
# ---------------------------------------------------------------------------
_LOCAL_NAME_INDEX: "tuple | None" = None

# ★ 2026-10-03：原先这里有一张手工补录表（_LOCAL_NAME_PATCH: Coda Bubonico）。
#   构建脚本补收 DE 官方家族表（riven_families.json 819 件）进 name_space 后，
#   `终幕·横痃重炮` 已由 ② de/name_en_zh.json 桥接直接命中 ⇒ 补丁删除，
#   中英对照保持**单一真源**（构建期生成，运行期只读表）。


def _norm_name(s: str) -> str:
    """名字归一（去空白/中点/连字符 + 小写）——三表与查询侧同口径。"""
    return re.sub(r"[\s·\-_]+", "", (s or "").lower())


def _local_name_index() -> tuple:
    """本地三表的名字索引 (en_norm → zh, zh_norm → en)，惰性构建一次。"""
    global _LOCAL_NAME_INDEX
    if _LOCAL_NAME_INDEX is None:
        en_zh: dict = {}
        zh_en: dict = {}

        def _put(en, zh):
            e, z = _norm_name(en), _norm_name(zh)
            if e and z and e != z:
                en_zh.setdefault(e, zh)
                zh_en.setdefault(z, en)

        try:  # ① weapons_stats.json（en→zh 覆盖最广）
            raw = json.loads((DATA_DIR / "weapons_stats.json").read_text(encoding="utf-8"))
            for v in raw.values():
                if isinstance(v, dict):
                    _put(v.get("name"), v.get("zh"))
        except Exception:  # noqa: BLE001 - 缺文件只降级
            pass
        try:  # ② de/name_en_zh.json（键为小写英文名）
            for en, zh in _name_en_zh().items():
                _put(en, zh)
        except Exception:  # noqa: BLE001
            pass
        try:  # ③ de/de_items_zh.json
            raw = json.loads((DATA_DIR / "de" / "de_items_zh.json").read_text(encoding="utf-8"))
            for it in raw.get("items") or []:
                _put(it.get("en"), it.get("zh"))
        except Exception:  # noqa: BLE001
            pass
        _LOCAL_NAME_INDEX = (en_zh, zh_en)
    return _LOCAL_NAME_INDEX


# ★ 2026-10-03 双模式条目（组合枪 Primary/Secondary、空枪 Atmosphere、
#   Vinquibus (Melee)、Dark Split-Sword 双形态）的中文模式后缀 —— 与
#   WM 拆分行的官方写法一致（捕月（主要））。
_MODE_ZH = {
    "primary": "（主要）",
    "secondary": "（次要）",
    "atmosphere": "（大气）",
    "melee": "（近战）",
    "dual swords": "（双剑）",
    "heavy blade": "（重刃）",
}


def _zh_name_of_en(en: str) -> str:
    """英文名 → 中文名（本地三表级联；查不到返回 ""）。

    ★ 2026-10-03：带模式后缀的名字（如 Catchmoon (Primary)）三表没有
    整名时，按「基名中文 + 模式后缀」拼装（家族候选显示用）。
    """
    en_zh, _ = _local_name_index()
    hit = en_zh.get(_norm_name(en))
    if hit:
        return hit
    m = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", (en or "").strip())
    if m:
        mode = _MODE_ZH.get(m.group(2).strip().lower())
        base = en_zh.get(_norm_name(m.group(1)))
        if mode and base:
            return base + mode
    return ""


def _en_name_of_zh(zh: str) -> str:
    """中文名 → 英文名（本地三表级联；「一套/组合包/蓝图」尾缀先剥）。

    查不到返回 "" —— 调用方按「原串可能是英文名」继续试，不要失败退出。
    """
    _, zh_en = _local_name_index()
    z = _norm_name(zh)
    for suf in ("一套", "组合包", "蓝图", "set", "blueprint", "blueprints"):
        if z.endswith(suf) and len(z) > len(suf):
            z = z[: -len(suf)]
            break
    return zh_en.get(z, "")


class WarframeAPIError(Exception):
    """统一的接口错误（含降级提示语）。"""


def wm_retry_after_seconds(value, *, now=None, fallback=90.0) -> float:
    """解析 WM 的 Retry-After；长等待不截短，超出浮点范围按无限等待处理。"""
    text = str("" if value is None else value).strip()
    if re.fullmatch(r"[0-9]+", text):
        digits = text.lstrip("0") or "0"
        return float(digits) if len(digits) < 309 else math.inf
    try:
        stamp = parsedate_to_datetime(text)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, stamp.timestamp() - (time.time() if now is None else now))
    except (ValueError, TypeError, OverflowError):
        return float(fallback)


class _WikiRetryLater(WarframeAPIError):
    """上游要求退避；本轮不能换传输再次请求同一 wiki。"""


class _WikiRequestRejected(WarframeAPIError):
    """源站明确拒绝或返回重定向；本轮不再换传输请求。"""


class _CommunitySnapshotRejected(WarframeAPIError):
    """结构合法但当前不可采用；逐源继续，不当作网络故障。"""

    def __init__(self, result: str, reason: str, stamp: str):
        super().__init__(reason)
        self.result = result
        self.stamp = stamp


class _CommunityContentError(WarframeAPIError):
    """社区通道可达但快照结构不可用；不作为整台主机的网络故障冷却。"""


class _WMV1ContentError(WarframeAPIError):
    """WM v1 信封不符合消费契约；不能当空结果，也不靠去筛选条件重试。"""


# ---------------------------------------------------------------------------
# 报错脱敏：异常原文（上游 URL / httpx 异常 / FlareSolverr 内网地址）只进日志，
# 发到群里的只保留「哪个数据源 + 怎么办」。调用方：main.py 指令总入口、各 commands。
# ---------------------------------------------------------------------------
_ERR_URL_RE = re.compile(r"https?://[^\s'\"<>()（）\[\]，,]+")
# IPv4（可带端口）以及 httpx/OSError 的元组写法 ('172.17.0.1', 8191)
_ERR_IP_RE = re.compile(
    r"\(\s*'\d{1,3}(?:\.\d{1,3}){3}'\s*,\s*\d{1,5}\s*\)|\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b"
)
# (关键词, 对外展示的数据源名)；按顺序取第一个命中
_ERR_SOURCES: tuple[tuple[str, str], ...] = (
    ("flaresolverr", "FlareSolverr（CF 绕过代理）"),
    ("warframe.market", "warframe.market"),
    ("wm v2", "warframe.market"),
    ("紫卡周报", "DE 官方紫卡周报"),
    ("warframestat", "warframestat.us"),
    ("huijiwiki", "灰机 wiki"),
    ("wf.wiki", "arbi.wf.wiki"),
    ("wiki.warframe", "Warframe wiki"),
    ("valence", "Warframe wiki（言录使数据）"),
    ("倾向表", "Warframe wiki（紫卡倾向表）"),
    ("warframe.com", "DE 官方接口"),
    ("世界状态", "DE 官方接口"),
)
# 这些前缀的报错文本里拼了上游原文（URL / 异常串 / 内网地址）⇒ 整句换成通用提示
_ERR_RAW_PREFIXES = (
    "请求失败（",
    "接口不存在或暂未开放：",
    "FlareSolverr 不可用",
    "DE 紫卡周报拉取失败：",
    "WM v2 接口错误：",
    "valence ",
    "wiki 倾向表解析异常",
)


def error_source(text: str) -> str:
    """从报错原文里认出数据源名（不认识返回空串）。"""
    low = str(text).lower()
    for key, label in _ERR_SOURCES:
        if key in low:
            return label
    return ""


def redact_error_text(text: str) -> str:
    """去掉文本里的 URL（换成数据源名）与 IP/端口（换成「内部地址」）。"""

    def _url(m: re.Match) -> str:
        return error_source(m.group(0)) or "上游接口"

    out = _ERR_URL_RE.sub(_url, str(text))
    return _ERR_IP_RE.sub("内部地址", out)


# 英文模式：数据源展示名
_ERR_SOURCES_EN = {
    "FlareSolverr（CF 绕过代理）": "FlareSolverr (CF bypass proxy)",
    "DE 官方接口": "The DE official API",
    "DE 官方紫卡周报": "The DE official Riven weekly report",
    "灰机 wiki": "Huiji wiki",
    "Warframe wiki（言录使数据）": "Warframe wiki (Acrithis data)",
    "Warframe wiki（紫卡倾向表）": "Warframe wiki (Riven disposition table)",
    "上游接口": "The upstream API",
}

# 英文模式：无替代指令的外部源停摆提示（与 _EXTERNAL_ONLY 的中文原文一一对应）
_EXTERNAL_ONLY_EN = {
    "kuva": "The Kuva siphon data source (10o.io) is down and the DE official data does not "
    "include it, so live siphons are unavailable.\n"
    "Alternatives:\n"
    "- Kuva / Sister weapon prices: wm kuva bramma\n"
    "- Kuva / Tenet weapon auctions: xh\n"
    "- Weekly rotations: timers",
    "arbitration": "The live Arbitration data source (10o.io) is down.\n"
    "Alternatives:\n"
    "- arbitration: current and next-hour missions (now from the arbi.wf.wiki fixed schedule)\n"
    "- arbtable: the full weekly schedule\n"
    "- arbitration survival: filter by mission type",
    "steelPath": "Steel Path rotation needs an external data source, which is not available in "
    "direct DE mode.\n"
    "Alternatives:\n"
    "- incursions: current Steel Path Incursions\n"
    "- fissures steelpath: Steel Path fissures\n"
    "- arbitration: Steel Path Arbitration missions",
}

# 英文模式：本插件自己写的中文降级提示 -> 英文（按顺序取第一个命中；其余含中文的原文一律不透传）
_ERR_PLUGIN_EN: tuple[tuple[str, str], ...] = (
    ("请求额度繁忙", "warframe.market request quota is busy, please try again later"),
    ("限速", "warframe.market is rate-limiting requests, please try again later"),
    ("WM 请求被拒绝", "warframe.market rejected the request, please contact an admin"),
    ("WM 重定向已拒绝", "warframe.market redirected the request, please contact an admin"),
    ("客户端已关闭", "The plugin is reloading or shutting down, please try again shortly"),
    (
        "自填地址指向受限网络",
        "The configured address points to a restricted network, request not sent",
    ),
    ("返回结构异常", "The data source returned an unexpected response, please try again later"),
    ("世界状态访问被拒绝", "DE world state access was denied (HTTP 403), please contact an admin"),
    ("世界状态返回异常", "DE world state returned an unexpected response, please try again later"),
    (
        "这一段解析失败",
        "That part of the world state failed to parse, other features are unaffected, please try again later",
    ),
    ("世界状态", "World state data is temporarily unavailable, please try again later"),
    (
        "价格排行刷新保护中",
        "Price ranking refresh is protected, please try again 30 minutes after the current round ends",
    ),
    ("词条无法完整转换", "Some stats cannot be fully converted into a market query"),
    ("FlareSolverr", "The CF bypass proxy is unavailable, please try again later"),
    ("wiki", "The wiki data source is temporarily unavailable, please try again later"),
)
_CJK_RE = re.compile(r"[\u3400-\u9fff\uff00-\uffef\u3000-\u303f]")


def _public_error_text_en(msg: str) -> str:
    """``public_error_text`` 的英文出口：只改返回值，判断仍按中文原文前缀。"""
    if msg.startswith(_ERR_RAW_PREFIXES):
        zh_src = error_source(msg) or "上游接口"
        src = _ERR_SOURCES_EN.get(zh_src, zh_src)
        if msg.startswith("接口不存在或暂未开放"):
            return f"{src} endpoint does not exist or is not open yet, please try again later"
        status_match = re.search(r"(?:\bHTTP\s+|\berror\s+['\"])([45]\d{2})\b", msg, re.I)
        if status_match:
            status = int(status_match[1])
            if status == 403:
                return f"{src} denied access (HTTP 403), please ask an admin to check the data source connection"
            if status == 429:
                return f"{src} is rate-limiting requests (HTTP 429), please try again later"
            if status >= 500:
                return f"{src} service error (HTTP {status}), please resend in a moment"
            return f"{src} request failed (HTTP {status}), please ask an admin to check the data source config"
        if re.search(r"timeout|timed out|超时", msg, re.I):
            return f"{src} request timed out, please resend in a moment"
        return f"{src} is temporarily unavailable (connection or response error), please resend in a moment"
    for key, text in _EXTERNAL_ONLY.items():
        if msg == text.strip():
            return _EXTERNAL_ONLY_EN[key]
    for key, text in _ERR_PLUGIN_EN:
        if key in msg:
            return text
    cleaned = redact_error_text(msg)
    if not cleaned or _CJK_RE.search(cleaned):
        return "The data source is temporarily unavailable, please try again later"
    return cleaned


def public_error_text(exc: BaseException | str) -> str:
    """把接口异常转成可以发到群里的提示（详情由调用方写日志）。

    * 本插件自己写的降级提示（限速 / 数据源停摆 / 开关关闭等）原样保留，仅兜底去 URL/IP；
    * 拼了上游原文的（见 ``_ERR_RAW_PREFIXES``）整句换成「<数据源> 暂时不可用，请稍后再试」。
    英文模式只翻译返回值（判断依据仍是中文原文），无法归类的中文原文不透传。
    """
    msg = str(exc).strip()
    if i18n.is_en():
        return _public_error_text_en(msg)
    if msg.startswith(_ERR_RAW_PREFIXES):
        src = error_source(msg) or "上游接口"
        if msg.startswith("接口不存在或暂未开放"):
            return f"{src} 接口不存在或暂未开放，请稍后再试"
        # HTTP 拒绝、服务错误和超时是不同故障；只展示状态码，保留 URL/凭据脱敏。
        status_match = re.search(r"(?:\bHTTP\s+|\berror\s+['\"])([45]\d{2})\b", msg, re.I)
        if status_match:
            status = int(status_match[1])
            if status == 403:
                return f"{src} 拒绝访问（HTTP 403），请联系管理员检查数据源连接"
            if status == 429:
                return f"{src} 请求受限（HTTP 429），请稍后再试"
            if status >= 500:
                return f"{src} 服务异常（HTTP {status}），请稍后重发一次"
            return f"{src} 请求失败（HTTP {status}），请联系管理员检查数据源配置"
        if re.search(r"timeout|timed out|超时", msg, re.I):
            return f"{src} 请求超时，请稍后重发一次"
        return f"{src} 暂时不可用（连接或响应异常），请稍后重发一次"
    return redact_error_text(msg) or "上游接口暂时不可用，请稍后再试"


def _rank_candidate(it: dict) -> bool:
    """价格榜爬取候选（决定抓取规模与榜单口径）。

    * 甲：**只有套装**（WM 已不再挂整件 Prime 甲，套装才是实际交易物）；
    * 武器：整件武器 + 套装（暮斩这类非 Prime 整件也在内），排除蓝图/部件；
    * 卡 / 赋能 / 部件 / 遗物全量。
    """
    tags = set(it.get("tags") or [])
    if "mod" in tags or "arcane_enhancement" in tags or "relic" in tags or "component" in tags:
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


# ★ 2026-10-23：紫卡实时排行（WM 拍卖）的目标武器集参数。
RIVEN_RT_TOP_N = 60  # 取 DE 周报热度前 N 把
RIVEN_RT_MAX_DISP = 0.7  # 原倾向（取消前旧值）≤ 此值：数值 ×1.7~2.4，1.2 热修后涨幅最大


def riven_realtime_targets(
    weekly_entries: list,
    wm_weapons: list,
    dispositions: dict,
    families: dict | None = None,
    *,
    top_n: int = RIVEN_RT_TOP_N,
    max_disp: float = RIVEN_RT_MAX_DISP,
) -> list[str]:
    """紫卡实时排行的**目标武器集**（WM ``url_name`` 列表，已去重、家族只留主武器）。

    ★ 关键：DE 周报的 ``compatibility`` 是**内部名**（`1999InfShotgunWeapon` / `CrpBriefcaseLauncher`
    这类），**不是 WM 的 url_name** —— 必须经 ``wm_weapons``（WM 紫卡武器表，含 ``en``/``zh``/``url_name``）
    按英文名（小写）或中文名解析；解析不到的（内部/废弃条目）直接跳过。

    两路合并（2026-10-23 指令）：
      ① DE 周报**热度前 N** —— 按解析出的 ``url_name`` 聚合，热度 = 0 洗 + 已洗的 ``pop`` 之和；
      ② **原倾向 ≤ ``max_disp``**（取消前旧值，来自 ``dispositions_rivenmirror.json``）——
         这批紫卡数值 ×1.7~2.4，1.2 热修后涨幅最大，周报里反而可能排不上前 N。

    家族处理与 ``wr`` 同规则：变体折算到**母武器** ``url_name``（WM 紫卡拍卖按母武器挂）。
    **不抓全部 670 把**，只抓这批目标。
    """
    fam_groups = (families or {}).get("families") or {}
    # ★ 2026-10-25：家族表按名字查**不分大小写**（家族表写「MK1-Paris」、倾向表写「Mk1-Paris」，
    #   原来查不到 ⇒ mk1-paris 没折回 paris，WM 400 item_not_exist）
    fam_by_name = {str(k).lower(): v for k, v in ((families or {}).get("by_name") or {}).items()}

    by_name: dict[str, str] = {}
    name_of_url: dict[str, str] = {}
    # WM 原始紫卡表里**真实存在**的 url（合并表里带 game_ref 的行；倾向表静态补全的变体行
    # game_ref 为空）—— WM 拍卖只认这些，别的一律 400 item_not_exist（ZCode 10-25 实测）
    wm_real: set[str] = set()
    for w in wm_weapons or []:
        url = str(w.get("url_name") or "")
        if not url:
            continue
        if w.get("game_ref"):
            wm_real.add(url)
        for key in (w.get("en"), w.get("zh")):
            k = str(key or "").strip()
            if k:
                by_name.setdefault(k.lower(), url)
        nm = str(w.get("en") or w.get("zh") or "").strip()
        if nm:
            name_of_url.setdefault(url, nm)

    # ★ 变体（Prime/赤毒/…）在**原始** WM `/riven/weapons` 里常常缺席，是插件从
    #   `dispositions_rivenmirror.json` 静态补全进合并表的（该表**以英文名为键**，
    #   行内另有 `zh`/`url_name`）⇒ 名字来源**同时**取两边，家族折叠才不会因传了哪张表而失效。
    for en_name, row in (dispositions or {}).items():
        if not isinstance(row, dict):
            continue
        url = str(row.get("url_name") or "")
        if not url:
            continue
        for nm in (str(en_name or "").strip(), str(row.get("zh") or "").strip()):
            if nm:
                by_name.setdefault(nm.lower(), url)
        nm = str(en_name or row.get("zh") or "").strip()
        if nm:
            name_of_url.setdefault(url, nm)

    url_of_name = {v: k for k, v in name_of_url.items()}

    def main_of(url: str) -> str:
        """变体 → 母武器 url_name（家族表按**显示名**索引：url→名→家族→母武器）。

        ★ 2026-10-25（ZCode 线上实测 7 个 400：kuva_sobek / kuva_zarr / mk1-paris …）：原写法取
        家族**首成员**，而成员按字母排序 ⇒「Kuva Sobek」排在「Sobek」前被当成母武器。现在
        取成员里 **WM 原始表真实存在**的那个（多个时取英文名最短的，即无变体前缀的本体）；
        拿不到 WM 原始表信息时退回旧规则。
        """
        name = name_of_url.get(url)
        key = fam_by_name.get(name.lower()) if name else None
        members = fam_groups.get(key) if key else None
        if not members:
            return url
        urls = [by_name.get(str(m).lower()) for m in members]
        real = [u for u in urls if u and u in wm_real]
        if real:
            return min(real, key=lambda u: (len(name_of_url.get(u, u)), u))
        main_name = str(members[0])
        return by_name.get(main_name.lower(), url_of_name.get(main_name, url))

    picked: dict[str, int] = {}

    hot: dict[str, int] = {}
    for e in weekly_entries or []:
        c = e.get("compatibility")
        if not c:
            continue
        url = by_name.get(str(c).strip().lower())
        if not url:
            continue
        hot[url] = hot.get(url, 0) + int(e.get("pop") or 0)
    for url, pop in sorted(hot.items(), key=lambda kv: -kv[1])[: max(0, top_n)]:
        picked[main_of(url)] = max(picked.get(main_of(url), 0), pop)

    for row in (dispositions or {}).values():
        if not isinstance(row, dict):
            continue
        url = str(row.get("url_name") or "")
        disp = row.get("disposition")
        if not url or not isinstance(disp, (int, float)):
            continue
        if float(disp) <= max_disp:
            picked.setdefault(main_of(url), 0)

    if wm_real:  # 兜底：WM 原始表里没有的 url 一律不抓（折不回母武器的孤儿变体）
        return sorted(u for u in picked if u in wm_real)
    return sorted(picked)


def load_aliases() -> dict:
    path = DATA_DIR / "aliases.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


class WorldstateRows(list):
    """保留整包来源身份的列表，不影响既有 formatter 的列表协议。"""

    source = ""


class WarframeClient:
    # 紫卡实时排行爬取的节奏。ZCode 2026-10-25 容器内实测 WM `/auctions/search`：**不是 3 请求/秒**，
    # 而是「约 90 s 窗口内约 8 次」—— 2 s 间隔下第 9 次起持续 429，+0/+30/+60 s 仍 429、+90 s 恢复。
    # ⇒ 每项间隔 15 s（每窗口约 6 次，给群里 wr 查询留余量）、429 后退避 100 s（长于恢复窗口）、
    #   连续 4 次失败才收工。一轮 ~101 把约 30 分钟，凌晨闲时窗口内跑得完。
    RIVEN_RT_GAP_S = 15.0
    RIVEN_RT_FAIL_BACKOFF_S = 100.0
    RIVEN_RT_MAX_FAIL_STREAK = 4

    # WM 上游抖动时的重试节奏（2026-10-26 立，源于线上日志实测）：
    #   ① **429 不做重试** —— WM 的限速是「约 90 s 窗口内约 8 次」的**整窗**封禁
    #      （2026-10-25 实测：+0/+30/+60 s 仍 429、+90 s 才恢复）。原先 1.2 s / 2.4 s
    #      的退避重试必然再吃一次 429 —— 既救不回这次请求，又把封禁窗口拖得更长、
    #      给已经吃紧的上游再加压。⇒ 直接把可执行的提示抛给用户。
    #   ② 超时 / 5xx 的退避**逐次拉长**（5 s、10 s，带抖动）—— 上游偶发整段故障实测
    #      持续 45–120 s（期间 ReadTimeout 与 504 交替），0.6 s 级退避会让三次尝试
    #      全落在**同一个**坏窗口里，等于没重试。
    WM_RETRY_BACKOFF_S = 5.0
    # 提示不写具体次数：「90 s 约 8 次」只在 /auctions/search 上实测过，其它 WM 接口标称约 3 次/秒
    WM_RATE_LIMIT_HINT = "warframe.market 限速，请稍后再试"
    WM_AUCTION_COOLDOWN_S = 90
    # 所有前后台WM请求共享：普通0.35秒/次；拍卖最多短时2次，持续15秒补1次。
    # 拍卖等待不占HTTP槽，最多3个在途；有效Retry-After绝不截短。
    WM_MIN_INTERVAL_S = 0.35
    WM_AUCTION_CAPACITY = 2.0
    WM_AUCTION_REFILL_S = 15.0
    # 本插件自行保守设置，并非 Wiki 官方公布的配额；HTTP 与 FS 求解共用。
    WIKI_MIN_INTERVAL_S = 1.0

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
        community_base: str = "",
        allow_private_urls: bool = False,
        wfhelper_valence: bool = False,
    ):
        if httpx is None:
            raise WarframeAPIError(
                "缺少依赖 httpx：请在 AstrBot 插件管理中安装依赖，或手动执行 pip install httpx"
            )
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
        # 社区快照地址：留空 = 本项目 bot-data（raw + 两个 jsDelivr 镜像）；填了就只用它
        #   （用户不想用我们的数据时自建，目录下放同名文件：valence.json / arb_ratings.json …）
        self.community_base = normalize_community_base(community_base)
        self.allow_private_urls = bool(allow_private_urls)
        # 元素加成可选第三方通道 WFHelper（后台开关，默认关）：开了就排在社区快照前面
        self.wfhelper_valence = bool(wfhelper_valence)
        # 社区通道记忆（2026-10-16 GPT 评估 P04）：国内服务器到 raw 必超时，每次都等它超时再换镜像
        #   ⇒ 按**主机**记上次成功的通道、失败的主机冷却一段时间（raw 挂了是五个文件一起挂）
        self._comm_good_host: Optional[str] = None
        self._comm_cool: dict[str, float] = {}
        # 社区快照采用记录（2026-10-17 批次 B「全链路健康记录」插件侧）：文件名 → 最近一次检查的结论、
        #   走的通道、数据自身时间、原因。运维面板「数据」页展示；只在内存里，重启清空（这是「本进程看到了什么」）。
        self.community_log: dict[str, dict] = {}
        self.cache = cache or TTLCache(maxsize=1024)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            proxy=proxy or None,
        )
        self._flare_url = ""  # FlareSolverr 可用地址（首次连通后记忆）
        self._flare_cache: dict[str, tuple[float, str]] = {}
        # 已确认「WM 无该玄骸武器挂单类目」的 slug（返回 400），避免重复请求
        self._lich_unsupported: set = set()
        self._aliases = load_aliases()
        self._wm_lock = asyncio.Lock()
        self._wm_last = -math.inf
        self._wm_slots = asyncio.Semaphore(3)
        self._wm_gate_timeout = min(15.0, max(0.0, timeout))
        self._wm_auction_tokens = self.WM_AUCTION_CAPACITY
        self._wm_auction_updated = time.monotonic()
        self._wm_auction_cool_until = 0.0
        self._valence_refresh_lock = asyncio.Lock()
        # (语言, Time+BuildLabel) -> 解析结果；解析期已按语言本地化，两种语言各存一份。
        self._de_parsed_cache: dict[tuple[str, str], dict] = {}
        self._ws_lock = asyncio.Lock()
        self._de_retry_at = 0.0
        self._ws_failure_until: dict[str, float] = {}
        self._ws_failure: dict[str, Exception] = {}
        self._ws_last_good: dict[str, dict] = {}

    def _lang_code(self) -> str:
        """上游请求用的语言码：英文模式发 en，否则沿用构造时的语言（默认 zh-hans）。"""
        return "en" if i18n.is_en() else self.language

    def begin_close(self) -> None:
        """同步封住新请求和新抓取；宿主在扫描旧任务之前调用。"""
        self._closing = True

    def _ensure_open(self) -> None:
        if getattr(self, "_closing", False) or getattr(self, "_closed", False):
            raise WarframeAPIError("客户端已关闭或正在关闭（插件已卸载或正在重载），本次请求未发出")

    async def _wm_acquire(self, auction: bool, timeout: Optional[float] = None) -> None:
        """在出口同时预留全局间隔、拍卖额度和在途槽；等待时不占锁或槽。"""
        budget = (
            self._wm_gate_timeout
            if timeout is None
            else min(self._wm_gate_timeout, max(0.0, timeout))
        )
        deadline = time.monotonic() + budget
        busy = "warframe.market 请求额度繁忙，请稍后再试"
        while True:
            self._ensure_open()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WarframeAPIError(busy)
            async with self._wm_lock:
                now = time.monotonic()
                if now < self._wm_auction_cool_until:
                    raise WarframeAPIError(self.WM_RATE_LIMIT_HINT)
                self._wm_auction_tokens = min(
                    self.WM_AUCTION_CAPACITY,
                    self._wm_auction_tokens
                    + max(0.0, now - self._wm_auction_updated) / self.WM_AUCTION_REFILL_S,
                )
                self._wm_auction_updated = now
                # 单调时钟浮点舍入可能留下不足一个时钟精度的尾差，避免零进度忙转。
                if math.isclose(self._wm_auction_tokens, 1.0, rel_tol=0.0, abs_tol=1e-10):
                    self._wm_auction_tokens = 1.0
                delay = max(0.0, self._wm_last + self.WM_MIN_INTERVAL_S - now)
                if auction:
                    delay = max(delay, (1.0 - self._wm_auction_tokens) * self.WM_AUCTION_REFILL_S)
            if delay > 0:
                await asyncio.sleep(min(delay, remaining))
                continue
            # 仅等待Semaphore本身，不把已经预留额度/槽的整段逻辑放进子任务。
            # asyncio.Semaphore取消获取会归还已唤醒但尚未认领的槽。
            try:
                await asyncio.wait_for(self._wm_slots.acquire(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                self._ensure_open()
                raise WarframeAPIError(busy) from exc
            reserved = False
            try:
                self._ensure_open()
                async with self._wm_lock:
                    now = time.monotonic()
                    if now >= deadline:
                        raise WarframeAPIError(busy)
                    if now < self._wm_auction_cool_until:
                        raise WarframeAPIError(self.WM_RATE_LIMIT_HINT)
                    if now < self._wm_last + self.WM_MIN_INTERVAL_S:
                        continue
                    if auction and self._wm_auction_tokens < 1.0:
                        continue
                    self._wm_last = now
                    if auction:
                        self._wm_auction_tokens -= 1.0
                    reserved = True
                    return
            finally:
                if not reserved:
                    self._wm_slots.release()

    async def _wm_try_acquire(self) -> str:
        """探测专用：在同一锁下立即预留普通 WM 请求，绝不排队。"""
        self._ensure_open()
        if self._wm_lock.locked():
            return "wm_quota_busy"
        async with self._wm_lock:
            now = time.monotonic()
            if now < self._wm_auction_cool_until:
                return "cooldown"
            if now < self._wm_last + self.WM_MIN_INTERVAL_S or self._wm_slots.locked():
                return "wm_quota_busy"
            # 锁定期间、已确认槽可用；Semaphore.acquire 的立即路径不让出执行权。
            await self._wm_slots.acquire()
            self._wm_last = now
            return ""

    async def probe_data_source(
        self, target: str, platform: str = "pc", worldsource: Optional[str] = None
    ) -> dict:
        """只探测传输，不采用、解析或缓存业务数据；WM 探测计入普通查询额度。

        worldsource：面板按设置里保存的世界状态来源测（该项改了要重载才生效，
        运行中的客户端可能还是旧值）；不传就按运行中的来源。
        """
        self._ensure_open()
        if target not in {"worldstate", "community", "wiki", "market"}:
            raise ValueError("未知数据源测试项")
        if target == "worldstate":
            source = worldsource if worldsource in ("de", "warframestat") else self.worldsource
            url = (
                self.de_worldstate_url
                if source == "de"
                else f"{self._ws_base(platform)}/{platform}/events"
            )
            urls = (("世界状态", url),)
        elif target == "community":
            urls = (
                (("自填地址", f"{self.community_base}/valence.json"),)
                if self.community_base
                else tuple((urlsplit(u).hostname or "社区通道", u) for u in COMMUNITY_VALENCE_URLS)
            )
        elif target == "wiki":
            if not self.flare_enabled:
                return {
                    "ok": False,
                    "detail": "FlareSolverr 已关闭，未测试源页重抓",
                    "attempts": [],
                    "skipped_reason": "disabled",
                }
            urls = (("wiki 源页", self._wiki_request_url(WIKI_RAW_CODA)),)
        else:
            reason = await self._wm_try_acquire()
            if reason:
                return {
                    "ok": False,
                    "detail": "WM 冷却中，未测试"
                    if reason == "cooldown"
                    else "WM 额度繁忙，未测试",
                    "attempts": [],
                    "skipped_reason": "wm_quota_busy",
                }
            urls = (("warframe.market", f"{self.wm_v2_base}/items"),)
        attempts = []
        deadline = time.monotonic() + COMMUNITY_BUDGET if target == "community" else None
        try:
            for index, (label, url) in enumerate(urls):
                self._ensure_open()
                started = time.perf_counter()
                row = {"label": label, "ok": False, "status": None, "error": ""}
                # 不暴露账号、查询参数、片段或自填路径（路径也可能携带令牌）。
                parts = urlsplit(url)
                row["url"] = f"{parts.scheme}://{parts.hostname or ''}" + (
                    f":{parts.port}" if parts.port else ""
                )
                if target == "community" and self.community_base and self.allow_private_urls:
                    row["url"] = ""  # 显式允许内网时也不把自填内部地址回传页面。
                try:
                    if (
                        target == "community"
                        and self.community_base
                        and not self.allow_private_urls
                    ):
                        if await private_url_reason(url):
                            row.update(
                                url="",
                                error="自填地址指向受限网络，未发送请求",
                                skipped_reason="private_url",
                                ms=0,
                            )
                            attempts.append(row)
                            continue
                    timeout = (
                        8.0
                        if target == "worldstate" and url == DEFAULT_DE_WORLDSTATE
                        else self._http.timeout
                    )
                    headers = _ORACLE_HEADERS if target == "community" else None
                    if target == "community":
                        left = deadline - time.monotonic()
                        if left <= 1.0:
                            row.update(
                                error="本次社区探测预算已耗尽，未测试",
                                skipped_reason="budget_exhausted",
                                ms=0,
                            )
                            attempts.append(row)
                            continue
                        timeout = min((left - 0.5) / (len(urls) - index), COMMUNITY_PER_CHANNEL)
                        if url == COMMUNITY_VALENCE_API_URL and not self.community_base:
                            headers = {
                                **_ORACLE_HEADERS,
                                "Accept": "application/vnd.github.raw+json",
                                "X-GitHub-Api-Version": "2026-03-10",
                            }
                            url += f"&cb={int(time.time() // 60)}"
                    if target == "wiki":
                        await self._wiki_wait(url)
                        timeout = 10.0
                        headers = {"Accept": "application/json, text/plain;q=0.9"}
                    request_options = {}
                    if target in {"wiki", "market"} or (
                        target == "community"
                        and (self.community_base or "api.github.com" == urlsplit(url).hostname)
                    ):
                        request_options["follow_redirects"] = False
                    try:
                        self._ensure_open()
                        async with self._http.stream(
                            "GET", url, headers=headers, timeout=timeout, **request_options
                        ) as response:
                            self._ensure_open()
                            row["status"] = response.status_code
                            if target == "market" and (
                                response.status_code == 429
                                or (
                                    response.status_code >= 500
                                    and response.headers.get("Retry-After")
                                )
                            ):
                                seconds = wm_retry_after_seconds(
                                    response.headers.get("Retry-After", "")
                                )
                                async with self._wm_lock:
                                    self._wm_auction_cool_until = max(
                                        self._wm_auction_cool_until, time.monotonic() + seconds
                                    )
                            if target == "wiki":
                                self._wiki_check_response(
                                    url,
                                    response.status_code,
                                    response.headers,
                                    allow_challenge=True,
                                )
                            response.raise_for_status()
                            row["ok"] = 200 <= response.status_code < 300
                    except (_WikiRetryLater, _WikiRequestRejected):
                        raise
                    except Exception:
                        self._ensure_open()
                        if target != "wiki" or not self.flare_enabled:
                            raise
                        await self.fetch_via_flaresolver(
                            url, ttl=0, cache_result=False, probe_only=True
                        )
                        row["ok"] = True
                        row["status"] = None
                        row["via"] = "flaresolverr"
                        row["label"] = "wiki 源页（FlareSolverr）"
                except Exception as exc:  # noqa: BLE001 - 对外只给脱敏摘要
                    self._ensure_open()
                    if isinstance(exc, httpx.TimeoutException):
                        row.update(error_code="timeout", error="请求超时")
                    elif row["status"]:
                        row["error"] = f"HTTP {row['status']}"
                    else:
                        row.update(error_code="connection_or_response", error="连接或响应异常")
                row["ms"] = int((time.perf_counter() - started) * 1000)
                attempts.append(row)
        finally:
            if target == "market":
                self._wm_slots.release()
        ok = any(row["ok"] for row in attempts)
        return {
            "ok": ok,
            "detail": (
                "经 FlareSolverr 连通（求解器内部可能产生多条请求；不验证数据有效性或新鲜度）"
                if ok and any(row.get("via") == "flaresolverr" for row in attempts)
                else "HTTP 连通（不验证数据有效性或新鲜度）"
                if ok
                else "所测通道未连通"
            ),
            "attempts": attempts,
            "skipped_reason": "",
        }

    async def close(self) -> None:
        self.begin_close()
        self._closed = True
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
        timeout: Optional[float] = None,
        follow_redirects: Optional[bool] = None,
        validator: Optional[Callable[[Any], None]] = None,
    ) -> Any:
        key = cache_key or (
            url
            + "|"
            + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
            + "|"
            + json.dumps(headers or {}, sort_keys=True)
        )

        ensure_open = self._ensure_open
        ensure_open()

        # 按URL归属兜底，避免新增入口遗漏标志；比较origin及路径边界，拒绝伪前缀。
        def normalized_origin(parts):
            scheme = parts.scheme.lower()
            port = parts.port or {"https": 443, "http": 80}.get(scheme)
            return scheme, (parts.hostname or "").lower(), port

        target = urlsplit(url)
        for base in (self.wm_base, self.wm_v2_base):
            origin = urlsplit(base)
            prefix = origin.path.rstrip("/")
            if normalized_origin(target) == normalized_origin(origin) and (
                target.path == prefix or target.path.startswith(prefix + "/")
            ):
                wm_rate_limit = True
                break
        auction_search = (
            wm_rate_limit and target.path == urlsplit(f"{self.wm_base}/auctions/search").path
        )

        async def _do() -> Any:
            last_exc: Exception | None = None
            for attempt in range(retries + 1):
                # 退避或 WM 限速等待期间可能发生卸载，不能再使用已关闭的连接池。
                ensure_open()
                try:
                    # timeout：单次请求自己的上限（社区通道用）。只靠外层 wait_for 取消不够 ——
                    #   httpx 被取消时连接清理是受保护的，可能拖到客户端默认超时才返回（ZCode 10-17：
                    #   raw 卡住吃满 30 s 预算，镜像没轮到）。
                    kw = {"timeout": timeout} if timeout is not None else {}
                    if wm_rate_limit:
                        kw["follow_redirects"] = False
                    elif follow_redirects is not None:
                        kw["follow_redirects"] = follow_redirects
                    ensure_open()
                    if wm_rate_limit:
                        await self._wm_acquire(auction_search, timeout)
                    try:
                        ensure_open()
                        resp = await self._http.get(url, params=params, headers=headers, **kw)
                    finally:
                        if wm_rate_limit:
                            self._wm_slots.release()
                    if wm_rate_limit and 300 <= resp.status_code < 400:
                        raise WarframeAPIError("WM 重定向已拒绝，请检查接口地址")
                    retry_after = getattr(resp, "headers", {}).get("Retry-After", "")
                    if wm_rate_limit and (
                        resp.status_code == 429 or (resp.status_code >= 500 and retry_after)
                    ):
                        seconds = wm_retry_after_seconds(retry_after)
                        self._wm_auction_cool_until = max(
                            self._wm_auction_cool_until, time.monotonic() + seconds
                        )
                        try:
                            resp.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            raise WarframeAPIError(self.WM_RATE_LIMIT_HINT) from exc
                    if wm_rate_limit and 400 <= resp.status_code < 500:
                        try:
                            resp.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            raise WarframeAPIError(
                                f"WM 请求被拒绝（HTTP {resp.status_code}）"
                            ) from exc
                    if resp.status_code == 404:
                        try:
                            resp.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            raise WarframeAPIError(f"接口不存在或暂未开放：{url}") from exc
                    # 限速：WM 是整窗封禁，短退避重试无益（见 WM_RETRY_BACKOFF_S 注释）；
                    # 非 WM 源（社区镜像等）恢复快，保留原有重试。
                    if resp.status_code == 429:
                        if wm_rate_limit or auction_search:
                            try:
                                resp.raise_for_status()
                            except httpx.HTTPStatusError as exc:
                                raise WarframeAPIError(self.WM_RATE_LIMIT_HINT) from exc
                        last_exc = WarframeAPIError("warframe.market 限速（HTTP 429）")
                        if attempt < retries:
                            await asyncio.sleep(1.2 * (attempt + 1))
                            continue
                        raise last_exc
                    if resp.status_code == 403 and url == DEFAULT_DE_WORLDSTATE:
                        try:
                            resp.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            raise WarframeAPIError("DE 世界状态访问被拒绝（HTTP 403）") from exc
                    resp.raise_for_status()
                    data = resp.json()
                    if validator is not None:
                        validator(data)
                    return data
                except WarframeAPIError:
                    raise
                except (httpx.HTTPError, json.JSONDecodeError) as exc:
                    last_exc = exc
                    if attempt < retries:
                        if wm_rate_limit:
                            # WM：退避逐次拉长（5 s、10 s…），让后续尝试有机会落到坏窗口之后
                            await asyncio.sleep(
                                self.WM_RETRY_BACKOFF_S * (attempt + 1) + random.random() * 2.0
                            )
                        else:
                            await asyncio.sleep(0.6 * (attempt + 1) + random.random() * 0.3)
            # 超时类异常 str() 为空（ZCode 10-17：日志 / 面板「请求失败（…）：」冒号后啥都没有）⇒ 退回类型名
            why = (str(last_exc) or type(last_exc).__name__) if last_exc else "未知错误"
            raise WarframeAPIError(f"请求失败（{url}）：{why}") from last_exc

        data = await self.cache.get_or_fetch(key, ttl, _do)
        if validator is not None:
            try:
                validator(data)
            except WarframeAPIError:
                # 兼容升级前留下的坏响应：只淘汰本次完整缓存身份，再正常单飞回源。
                await self.cache.set(key, None, ttl=0)
                return await self.cache.get_or_fetch(key, ttl, _do)
        return data

    def _ws_base(self, platform: str) -> str:
        return self.worldstate_base

    # ------------------------------------------------------------------
    # 世界状态：DE 直连模式（默认）
    # ------------------------------------------------------------------
    async def _de_raw(self) -> dict:
        if self.de_worldstate_url == DEFAULT_DE_WORLDSTATE:
            # 默认源故障交给有界后备；单次最多 8 秒，不在按钮请求内反复碰壁。
            return await self._fetch_json(
                self.de_worldstate_url, ttl=TTL_DE_RAW, retries=0, timeout=8
            )
        return await self._fetch_json(self.de_worldstate_url, ttl=TTL_DE_RAW)

    @staticmethod
    def _temporary_de_error(exc: Exception) -> bool:
        cause = exc.__cause__
        if isinstance(cause, httpx.HTTPStatusError):
            response = cause.response
            return response is not None and (
                response.status_code in (403, 429) or response.status_code >= 500
            )
        return isinstance(cause, (httpx.TimeoutException, httpx.NetworkError))

    @staticmethod
    def _validate_community_ws(data: Any) -> None:
        try:
            if not isinstance(data, dict):
                raise ValueError("整包不是对象")
            stamp = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or not -120 <= time.time() - stamp.timestamp() <= 600:
                raise ValueError("时间戳过期或超前")
            for key, typ in (
                ("fissures", list),
                ("sortie", dict),
                ("nightwave", dict),
                ("voidTrader", dict),
                ("constructionProgress", dict),
            ):
                if not isinstance(data.get(key), typ):
                    raise ValueError(f"{key} 缺失或结构错误")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise WarframeAPIError(f"社区世界状态不可用：{exc}") from exc

    @staticmethod
    def _community_node_name(value: str) -> str:
        """社区显示名精确反查本地节点；未知节点保留原文。"""
        if not value:
            return value
        if value in de_worldstate.nodes_zh():
            return de_worldstate._node_name(value)
        for key, info in (de_worldstate._load("solNodes.json") or {}).items():
            if isinstance(info, dict) and info.get("value") == value:
                return de_worldstate._node_name(key)
        return value

    async def _community_ws_bundle(self, platform: str, *, fallback: bool = True) -> dict:
        # 整包缓存身份同时包含源地址、平台、语言；校验失败不进入缓存。
        code = self._lang_code()
        language = "zh" if code in ("zh-hans", "zh-CN") else code
        url = f"{self.worldstate_base}/{platform}"
        key = f"ws-fallback|{url}|{language}"
        data = None
        if time.monotonic() < self._ws_failure_until.get(key, 0):
            cached = self._ws_last_good.get(key)
            if cached is not None:
                try:
                    self._validate_community_ws(cached)
                    data = deepcopy(cached)
                except WarframeAPIError:
                    pass
            if data is None:
                raise WarframeAPIError("社区世界状态暂不可用，请稍后重试") from self._ws_failure[
                    key
                ]
        try:
            if data is None:
                try:
                    data = await self._fetch_json(
                        url,
                        ttl=30,
                        params={"language": language},
                        retries=1,
                        timeout=10,
                        cache_key=key,
                        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                        validator=self._validate_community_ws,
                    )
                except WarframeAPIError as exc:
                    # 网络抖动时沿用仍通过原始时间戳校验的最近成功包；不延长其新鲜期。
                    cached = self._ws_last_good.get(key)
                    if cached is None:
                        raise
                    self._validate_community_ws(cached)
                    self._ws_failure_until[key] = time.monotonic() + 30
                    self._ws_failure[key] = exc
                    data = deepcopy(cached)
            bundle = dict(data)
            bundle["fissures"] = [
                dict(row, node=self._community_node_name(row.get("node", "")))
                for row in data["fissures"]
            ]
            storms = []
            storms_failed = False
            now = time.time()
            for row in bundle["fissures"]:
                if row.get("isStorm") is not True:
                    continue
                try:
                    expiry = datetime.fromisoformat(row["expiry"].replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        raise ValueError("虚空风暴到期时间缺少时区")
                    if expiry.timestamp() > now:
                        storms.append(dict(row, nodeCn=row.get("node", "")))
                except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                    storms_failed = True
            bundle["voidStorms"] = storms
            if storms_failed:
                bundle["_failed"] = list(bundle.get("_failed") or []) + ["voidStorms"]
            sortie = dict(data["sortie"])
            sortie["variants"] = [
                dict(row, node=self._community_node_name(row.get("node", "")))
                for row in sortie.get("variants") or []
            ]
            bundle["sortie"] = sortie
            # 日历：warframestat 的事件类型、名字字段与 DE 解析结果不同，转成同一内部结构，
            # 否则日历卡认不出事件，名字全显示成「?」。转换失败只标记这一段，不拖垮整包。
            if isinstance(data.get("calendar"), dict):
                try:
                    bundle["calendar"] = de_worldstate.community_calendar(data["calendar"])
                except (KeyError, TypeError, ValueError, AttributeError):
                    bundle["_failed"] = list(bundle.get("_failed") or []) + ["calendar"]
            construction = data["constructionProgress"]
            projects = []
            for field, name, en, faction, event in (
                ("fomorianProgress", "巴罗尔巨人战舰", "Fomorian", "Grineer", "巨人战舰破坏"),
                ("razorbackProgress", "利刃豺狼舰队", "Razorback", "Corpus", "利刃豺狼舰队"),
            ):
                pct = float(str(construction[field]).rstrip("%"))
                if not math.isfinite(pct) or not 0 <= pct <= 100:
                    raise ValueError("舰队进度越界")
                projects.append(dict(name=name, en=en, faction=faction, event=event, pct=pct))
            bundle["constructionProgress"] = {
                "projects": projects,
                "assaults": [],
                "assaults_unknown": True,
                "_source": "社区 warframestat（DE 后备）",
            }
            trader = dict(data["voidTrader"])
            activation = datetime.fromisoformat(trader["activation"].replace("Z", "+00:00"))
            expiry = datetime.fromisoformat(trader["expiry"].replace("Z", "+00:00"))
            if activation.tzinfo is None or expiry.tzinfo is None or activation >= expiry:
                raise ValueError("奸商时间窗口错误")
            trader["active"] = activation.timestamp() <= time.time() < expiry.timestamp()
            character = trader.get("character") or ""
            if "Baro" in character or "Teel" in character:
                trader["character"] = "Baro Ki'Teer"
            trader["location"] = self._community_node_name(trader.get("location", ""))
            inventory = []
            for row in trader.get("inventory") or []:
                item = dict(row)
                store = item.get("uniqueName") or item.get("store") or ""
                item["store"] = store
                if store:
                    item["item"] = (
                        ("" if i18n.is_en() else store_item_zh(store))
                        or de_worldstate.item_name_opt(store)
                        or item.get("item", "")
                    )
                inventory.append(item)
            trader["inventory"] = inventory
            bundle["voidTrader"] = trader
            nightwave = data.get("nightwave")
            if isinstance(nightwave, dict):
                nightwave = dict(nightwave)
                challenge_meta = {}
                for path, meta in (
                    (de_worldstate._load("nightwave_zh.json") or {}).get("challenges") or {}
                ).items():
                    tail = path.rsplit("/", 1)[-1].lower()
                    challenge_meta.setdefault(tail, []).append(meta)
                challenges = []
                for original in nightwave.get("activeChallenges") or []:
                    challenge = dict(original)
                    challenge["standing"] = challenge.get(
                        "standing", challenge.get("reputation", 0)
                    )
                    tail = re.sub(r"^\d+", "", str(challenge.get("id") or "")).lower()
                    matches = challenge_meta.get(tail) or []
                    if len(matches) == 1:
                        meta = matches[0]
                        challenge["title"] = meta.get("name") or challenge.get("title", "")
                        challenge["desc"] = meta.get("desc") or challenge.get("desc", "")
                        if meta.get("required") is not None:
                            challenge["required"] = meta["required"]
                    challenges.append(challenge)
                nightwave["activeChallenges"] = challenges
                bundle["nightwave"] = nightwave
            hunt = data.get("archonHunt")
            if isinstance(hunt, dict):
                hunt = dict(hunt)
                variants = hunt.get("variants") or hunt.get("missions") or []
                hunt["variants"] = [
                    dict(
                        mission,
                        node=self._community_node_name(mission.get("node", "")),
                        missionType=mission.get("missionType") or mission.get("type", ""),
                        level=de_worldstate.archon_stage_level(index),
                    )
                    for index, mission in enumerate(variants)
                ]
                boss = hunt.get("boss") or ""
                hunt["bossCode"] = de_worldstate.archon_code(boss)
                hunt["boss"] = de_worldstate.archon_name(boss)
                bundle["archonHunt"] = hunt
            if isinstance(data.get("syndicateMissions"), list):
                bundle["syndicateMissions"] = de_worldstate.adapt_community_syndicate_missions(
                    data["syndicateMissions"]
                )
            # 警报 / 入侵 / 每日特惠 / 周期：社区字段与 DE 解析结构不同，逐项转换；
            # 只标记失败的那一段，不拖垮整包。
            for field, convert in (
                ("alerts", lambda v: de_worldstate.community_alerts(v, self._community_node_name)),
                (
                    "invasions",
                    lambda v: de_worldstate.community_invasions(v, self._community_node_name),
                ),
                ("dailyDeals", de_worldstate.community_daily_deals),
                ("conclaveChallenges", de_worldstate.community_conclave),
                ("flashSales", de_worldstate.community_flash_sales),
                ("descendia", de_worldstate.community_descendia),
                ("duviriCycle", lambda v: de_worldstate.community_cycle("duviri", v)),
                ("zarimanCycle", lambda v: de_worldstate.community_cycle("zariman", v)),
            ):
                if not isinstance(data.get(field), (list, dict)):
                    continue
                try:
                    bundle[field] = convert(data[field])
                except (KeyError, TypeError, ValueError, AttributeError):
                    bundle["_failed"] = list(bundle.get("_failed") or []) + [field]
            # 科研：社区把深层 / 时光合在 archimedeas 一个列表里，按类型拆成两项
            if isinstance(data.get("archimedeas"), list):
                try:
                    bundle.update(de_worldstate.community_archimedeas(data["archimedeas"]))
                except (KeyError, TypeError, ValueError, AttributeError):
                    bundle["_failed"] = list(bundle.get("_failed") or []) + ["archimedeas"]
            bundle.pop("goals", None)
            for field, target, convert in (
                (
                    "events",
                    "goals",
                    lambda v: de_worldstate.community_goals(v, self._community_node_name),
                ),
                ("vaultTrader", "primeVault", de_worldstate.community_prime_vault),
                ("clanWeeklyInitiative", "clanRewards", de_worldstate.community_clan_rewards),
            ):
                if not isinstance(data.get(field), (list, dict)):
                    continue
                try:
                    bundle[target] = convert(data[field])
                except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                    bundle[target] = None
                    bundle["_failed"] = list(bundle.get("_failed") or []) + [target]
            # 结合目标在 DE 模式也走同一份 WFCD 随包静态表。
            bundle["synthTargets"] = de_worldstate._parse_synth({})
            if isinstance(data.get("news"), list):
                bundle["news"] = de_worldstate.community_news(data["news"])
            label = (
                "社区 warframestat"
                if self.worldstate_base == DEFAULT_WORLDSTATE_BASE
                else "自定义社区世界状态"
            ) + ("（DE 后备）" if fallback else "")
            bundle["_source"] = f"数据源：{label}｜快照 {data['timestamp']}"
            self._ws_last_good[key] = deepcopy(data)
            return bundle
        except (WarframeAPIError, KeyError, ValueError, TypeError, AttributeError) as exc:
            self._ws_failure_until[key] = time.monotonic() + 30
            self._ws_failure[key] = exc
            raise WarframeAPIError(f"社区世界状态暂不可用：{exc}") from exc

    async def _resilient_de_bundle(self, platform: str) -> dict:
        if self.de_worldstate_url != DEFAULT_DE_WORLDSTATE.rstrip("/"):
            return await self._de_bundle()
        async with self._ws_lock:
            if time.monotonic() >= self._de_retry_at:
                try:
                    bundle = await self._de_bundle()
                    return bundle
                except WarframeAPIError as exc:
                    if not self._temporary_de_error(exc):
                        raise
                    self._de_retry_at = time.monotonic() + 120
            return await self._community_ws_bundle(platform)

    async def _de_bundle(self) -> dict:
        raw = await self._de_raw()
        if not isinstance(raw, dict) or not raw:
            raise WarframeAPIError("DE 世界状态返回异常，请稍后重试")
        raw_key = str(raw.get("Time", "")) + str(raw.get("BuildLabel", ""))
        key = (i18n.current_lang(), raw_key)
        parsed = self._de_parsed_cache.get(key)
        if parsed is None:
            # 解析必须走线程：内部要读多个 MB 级 DE 翻译表并建索引，
            # 同步跑会把 AstrBot 的单事件循环堵住（watchdog 实测 30s+）
            parsed = await asyncio.to_thread(de_worldstate.parse_worldstate, raw)
            # 只留当前这份原始数据的各语言结果，旧快照随新数据一起淘汰。
            self._de_parsed_cache = {
                k: v for k, v in self._de_parsed_cache.items() if k[1] == raw_key
            }
            self._de_parsed_cache[key] = parsed
        return parsed

    # ------------------------------------------------------------------
    # 世界状态统一入口
    # ------------------------------------------------------------------
    async def worldstate(
        self, platform: str, endpoint: str, ttl: float = TTL_WORLDSTATE_FAST
    ) -> Any:
        if self.worldsource == "de":
            bundle = await self._resilient_de_bundle(platform)
            if endpoint in ("kuva", "arbitration", "steelPath") and not bundle.get(endpoint):
                raise WarframeAPIError(_EXTERNAL_ONLY[endpoint])
            if endpoint in (bundle.get("_failed") or ()):
                # P09：这段解析失败 ≠ 当前没有事件 —— 明说，别回空值让卡片显示「暂无」
                source_name = "社区世界状态" if "_source" in bundle else "DE 世界状态"
                raise WarframeAPIError(
                    f"{source_name}的「{endpoint}」这一段解析失败（数据格式可能有变），"
                    "其余功能不受影响，请稍后重试"
                )
            if endpoint not in bundle:
                raise WarframeAPIError(f"当前世界状态源未提供「{endpoint}」，暂不可用")
            value = bundle.get(endpoint)
            if "_source" not in bundle:
                return value
            source = bundle["_source"]
            if isinstance(value, dict):
                value = dict(value, _source=source)
            elif isinstance(value, list):
                value = WorldstateRows(value)
                value.source = source
            return value
        # 直连社区源：与 DE 后备共用同一个整包和同一套格式转换（各栏目字段名、节点中文名等），
        # 整包里没有的栏目才单独请求原地址。
        try:
            bundle = await self._community_ws_bundle(platform, fallback=False)
        except WarframeAPIError:
            bundle = None
        if bundle is not None and endpoint in bundle:
            if endpoint in (bundle.get("_failed") or ()):
                raise WarframeAPIError(
                    f"社区世界状态的「{endpoint}」这一段解析失败（数据格式可能有变），"
                    "其余功能不受影响，请稍后重试"
                )
            value = bundle.get(endpoint)
            source = bundle.get("_source")
            if source and isinstance(value, dict):
                value = dict(value, _source=source)
            elif source and isinstance(value, list):
                value = WorldstateRows(value)
                value.source = source
            return value
        url = f"{self._ws_base(platform)}/{platform}/{endpoint}"
        return await self._fetch_json(url, ttl=ttl, params={"language": self._lang_code()})

    async def cycle(self, platform: str, name: str) -> dict:
        """name: cetus / vallis / cambion / earth / duviri / zariman"""
        data = await self.worldstate(platform, f"{name}Cycle", ttl=TTL_WORLDSTATE_FAST)
        if isinstance(data, dict):
            data = dict(data)
            data["_name"] = name
        return data

    async def fissures(self, platform: str) -> list[dict]:
        data = await self.worldstate(platform, "fissures", ttl=45)
        return data if data is not None else []

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
        """警报：解析后把奖励物品**路径**经 WM gameRef 索引换成中文名（C3）。

        模式与 `_resolve_calendar_rewards` 同源（同用 WM v2 物品的 game_ref）。
        """
        data = await self.worldstate(platform, "alerts", ttl=60) or []
        return await self._resolve_alert_items(data)

    async def _resolve_alert_items(self, data: list) -> list:
        """警报奖励路径 → 中文名（三级回落：官方导出反查表 → WM gameRef → 未收录）。

        2026-10-04 新增第一级 `de/storeitems_zh.json`：WM 物品表是**可交易品**，
        装饰品（ShipDecos）不在其中，此前只能回落 `_prettify` 出 "Plushy2021 QTCC"
        这类怪名（用户实测报的「警报奖励没翻译」）。三级都未命中时**不造名字**：
        显示「路径末段（未收录）」。
        """
        by_ref: dict[str, str] = {}
        try:
            items = await self.wm_items()
        except WarframeAPIError:
            items = []
        locale = i18n.current_lang()
        for it in items:
            ref = it.get("game_ref") or ""
            name = it.get(locale)
            if ref and name:
                by_ref.setdefault(ref, name)

        for a in data or []:
            rw = (a.get("mission") or {}).get("reward") or {}
            names, unknown = [], []
            paths = rw.get("items") or []
            counts = list(rw.get("counts") or []) + [1] * len(paths)
            for p, cnt in zip(paths, counts):
                # 四级：装饰品反查表 → 官方词表（name_zh + WFCD i18n 补充，2026-10-16 加）
                #   → WM gameRef → 未收录（不造名字）。数量 >1 时追加「 ×N」（同入侵段写法）。
                nm = (
                    (store_item_zh(p) if locale == "zh" else "")
                    or de_worldstate.official_item_zh(p)
                    or ""
                )
                if not nm and p.lower() in (
                    "/lotus/types/gameplay/events/roadtotauevent/roadtotaueventresource",
                    "/lotus/storeitems/types/gameplay/events/roadtotauevent/roadtotaueventresource",
                ):
                    nm = de_worldstate.language_text_zh(
                        "/Lotus/Language/TauPrologue/RoadToTauEventResourceName"
                    )
                if not nm:
                    nm = next(
                        (by_ref[k] for k in store_items_path_variants(p) if by_ref.get(k)), ""
                    )
                suffix = f" ×{cnt}" if isinstance(cnt, int) and cnt > 1 else ""
                if nm:
                    names.append(nm + suffix)
                else:
                    unknown.append((p.rsplit("/", 1)[-1], suffix))
            if names:
                rw["item_names"] = names
                rw["item"] = names[0]  # 中文名优先于 prettify 兜底
                if unknown:
                    rw["item_unknown"] = [t for t, _ in unknown]
            elif unknown:
                rw["item_names"] = []
                rw["item"] = i18n.L("、", ", ").join(
                    t + i18n.L("（未收录）", " (unlisted)") + sfx for t, sfx in unknown
                )
        return data

    async def invasions(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "invasions", ttl=120) or []

    async def news(self, platform: str) -> list[dict]:
        return await self.worldstate(platform, "news", ttl=300) or []

    async def goals(self, platform: str) -> list[dict]:
        """限时活动（含 GracePeriod 宽限期内已结束的条目）。"""
        return await self.worldstate(platform, "goals", ttl=300) or []

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
        一律以 UTC 判定，展示时按用户所选的显示时区换算。
        """
        from datetime import datetime, timedelta, timezone

        now = now or datetime.now(timezone.utc)
        days = (int(weekday) - now.weekday()) % 7
        target = (now + timedelta(days=days)).replace(
            hour=int(hour_utc) % 24, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=7)
        return target.isoformat()

    def acrithis_next_reset(self) -> str:
        """言录使下次轮换时刻（UTC ISO）。

        周期在 ``rotations.json`` 的 ``acrichis.reset_weekday`` /
        ``acrichis.reset_hour_utc``（当前 = 周一 00:00 UTC，wiki 实时口径），改数据即可。
        """
        try:
            cfg = json.loads(
                (Path(__file__).resolve().parent / "data" / "rotations.json").read_text(
                    encoding="utf-8"
                )
            )
            sec = cfg.get("acrichis") or {}
        except Exception:  # noqa: BLE001
            sec = {}
        return self.next_weekly_reset(
            weekday=int(sec.get("reset_weekday", 0)), hour_utc=int(sec.get("reset_hour_utc", 0))
        )

    @staticmethod
    def _acrichis_runtime_ready() -> bool:
        """同效价刷新：离线未注入运行目录时禁止把动态数据写回种子。"""
        if paths.run_dir().resolve() == paths.PKG_DATA.resolve():
            logger.warning("[sdjk] 言录使刷新未注入运行目录，已跳过以保护包内静态数据")
            return False
        return True

    def _acrichis_snapshot(self) -> dict:
        """仅运行副本提供动态货单；缺少的静态字段独立回退包内目录。"""
        seed = self._load_json_file(paths.PKG_DATA / ACRITHIS_WEEK_NAME) or {}
        cached = self._load_json_file(paths.run_dir() / ACRITHIS_WEEK_NAME) or {}
        if not isinstance(cached, dict):
            cached = {}
        data = dict(cached) if paths.run_dir() != paths.PKG_DATA else {}
        for key, value in seed.items():
            if key.startswith("_") and not data.get(key):
                data[key] = value
        return data

    def acrithis_week_expired(self) -> bool:
        """缺失、非法或过期的运行货单都需要自动获取。"""
        return not community_acrithis_ok(self._acrichis_snapshot())[0]

    async def acrithis_week(self) -> dict:
        """仅返回合法且未过期的运行货单；种子只提供静态目录。"""
        data = self._acrichis_snapshot()
        if not community_acrithis_ok(data)[0]:
            return {}
        data["next_reset"] = self.acrithis_next_reset()
        return data

    @staticmethod
    @staticmethod
    def _expand_mw_current(value: str) -> str:
        """展开 MediaWiki 魔法词 ``{{CURRENTMONTHNAME}} {{CURRENTDAY}}, {{CURRENTYEAR}}``。

        ★ 2026-10-05：wiki 编辑把 ``AcrithisObserved`` 从字面日期改成了这串魔法词
        （页面自注「Current UTC date (copy this into AcrithisObserved)」）——
        旧解析按 ``[^}]*`` 只截到 ``{{CURRENTMONTHNAME``，日期解析必失败 ⇒
        刷新恒 ``failed``、新货单永远落不了盘。按 MediaWiki 语义展开为
        **当前 UTC 日期**；不含魔法词的原样返回。
        """
        if "{{CURRENT" not in value:
            return value
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        out = re.sub(r"\{\{\s*CURRENTMONTHNAME\s*\}\}", _ENGLISH_MONTHS[now.month - 1], value)
        out = re.sub(r"\{\{\s*CURRENTDAY2?\s*\}\}", str(now.day), out)
        out = re.sub(r"\{\{\s*CURRENTYEAR\s*\}\}", str(now.year), out)
        return out.strip()

    @staticmethod
    def parse_acrichis_current(raw: str) -> dict:
        """解析 wiki《Acrithis/Current Offerings》子页 wikitext（社区当期 5 件上报）。

        子页是 vardefine 模板：``{{#vardefine:AcrithisObserved|September 21, 2026}}``
        + ``AcrithisItem1..5``；注释块里带 15 件合法名单（校验用）。经
        FlareSolverr 抓回时 ``<>`` 被转义进 ``<pre>``，先整体 unescape 再解析，
        两种形态（转义/纯 wikitext）都能吃。解析不出 5 件返回空 dict。

        ★ 2026-10-05：``AcrithisObserved`` 的取值可能是魔法词模板
        （``{{CURRENTMONTHNAME}} {{CURRENTDAY}}, {{CURRENTYEAR}}``，见
        `_expand_mw_current`）——取值按「| 之后到模板收尾 ``}}``」整段截取，
        不能再按 ``[^}]*`` 截（会切在魔法词的第一个 ``}`` 上）。
        """
        import html as _html

        text = _html.unescape(raw or "")
        m = re.search(r"\{\{#vardefine:AcrithisObserved\|([^\n]*)", text)
        value = re.sub(r"<!--.*$", "", m.group(1)) if m else ""
        cut = value.rfind("}}")  # 模板自身收尾
        if cut >= 0:
            value = value[:cut]
        observed = WarframeClient._expand_mw_current(value.strip())
        items: list[str] = []
        for i in range(1, 6):
            m = re.search(r"\{\{#vardefine:AcrithisItem" + str(i) + r"\|([^}]*)\}\}", text)
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

    async def _wiki_revisions(self, api_url: str, what: str) -> dict:
        """wiki 页面最近修订时间 ``{页面标题: UTC datetime}``；查询/解析失败返回 {}（调用方降级）。"""
        from datetime import datetime

        try:
            raw = await self.fetch_wiki_source(api_url, ttl=0)
        except Exception as exc:  # noqa: BLE001 - 查询失败降级，不阻断刷新
            logger.warning("[sdjk] %s页面修订查询失败（降级不按修订时间判定）：%s", what, exc)
            return {}
        text = raw if isinstance(raw, str) else str(raw)
        m = re.search(r"<pre>(.*?)</pre>", text, flags=re.S)
        if m:
            text = m.group(1)
        try:
            pages = (json.loads(text).get("query") or {}).get("pages") or {}
            out = {}
            for page in pages.values():
                revs = (page or {}).get("revisions") or []
                if revs:
                    ts = str(revs[0]["timestamp"]).replace("Z", "+00:00")
                    out[str(page.get("title"))] = datetime.fromisoformat(ts)
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sdjk] %s页面修订解析失败（降级不按修订时间判定）：%s", what, exc)
            return {}

    async def _acrichis_page_revision(self):
        """言录使当期子页最近修订时间（UTC datetime）；失败返回 None（调用方降级按 observed 判定）。"""
        revs = await self._wiki_revisions(ACRITHIS_REVISION_API, "言录使")
        return next(iter(revs.values()), None)

    def _rotation_lock(self):
        lock = getattr(self, "_valence_refresh_lock", None)
        if lock is None:
            lock = self._valence_refresh_lock = asyncio.Lock()
        return lock

    async def refresh_acrichis_week(self, community_only: bool = False, force: bool = False) -> str:
        self._ensure_open()
        async with self._rotation_lock():
            self._ensure_open()
            return await self._refresh_acrichis_week_locked(community_only, force)

    async def _refresh_acrichis_week_locked(
        self, community_only: bool = False, force: bool = False
    ) -> str:
        """言录使本周货单：过期后自动抓 wiki 当期上报子页刷新（2026-09-21 起）。

        DE 不下发每周实际 5 件；wiki《Acrithis/Current Offerings》是社区人工
        维护的当期上报（observed 日期随更）。纪律与效价刷新一致：

        - 每件英文名必须命中快照 ``_en_catalog``（15 件全池对照）才落盘，
          不认识的名字**绝不写半成品**；
        - ``observed`` 早于本周周一 00:00 UTC 视为「社区还没跟上」，不落盘
          （防止拿上周货单当本周的）。

        返回 "fresh"（未过期）/ "refreshed" / "not-updated" / "failed"。
        """
        if not self._acrichis_runtime_ready():
            return "failed"
        expired = self.acrithis_week_expired()
        if not expired and not force:  # force：面板「从源头重抓」跳过这道门，其余闸门照旧
            self._note_comm("acrithis_week.json", "local_ok", "本周货单未到期")
            return "fresh"
        snap = self._acrichis_snapshot()
        catalog = snap.get("_en_catalog") or {}
        if not catalog:
            logger.warning("[sdjk] 言录使快照缺 _en_catalog，无法自动刷新")
            return "failed"
        if community_only:
            # 显式社区模式：不请求 wiki HTTP 或 FlareSolverr
            return await self._community_acrithis_week(snap, expired)
        if self._rotation_observations().get("pages"):
            result = await self._check_rotation_revisions_locked()
            status = result["acrithis"]
            if status in {"fresh", "unchanged", "refreshed"}:
                return "fresh" if status == "unchanged" else status
            snap = self._acrichis_snapshot()
            fallback = await self._community_acrithis_week(snap, self.acrithis_week_expired())
            return "not-updated" if fallback == "failed" else fallback
        try:
            raw = await self.fetch_wiki_source(
                ACRITHIS_CURRENT_URL,
                ttl=0,
                validator=lambda source: self._validate_rotation_source("acrithis", source),
            )
        except Exception as e:  # noqa: BLE001 - 抓取失败走告警，不阻断
            logger.warning("[sdjk] 言录使当期子页抓取失败：%s，尝试社区快照", e)
            return await self._community_acrithis_week(snap, expired)
        rev = await self._acrichis_page_revision()
        self._ensure_open()
        return self._adopt_acrichis_source(snap, raw, rev, expired)

    @staticmethod
    def _offering_values(items) -> list:
        """货单业务指纹：顺序、说明与动态 observed 日期不参与比较。"""
        return sorted(
            (str(it.get("name")), str(it.get("qty")), str(it.get("price")))
            for it in (items if isinstance(items, list) else [])
            if isinstance(it, dict) and isinstance(it.get("name"), str)
        )

    def _adopt_acrichis_source(self, snap, raw, rev, expired) -> str:
        from datetime import datetime, timedelta, timezone

        self._ensure_open()
        if not self._acrichis_runtime_ready():
            return "failed"
        catalog = snap.get("_en_catalog") or {}
        parsed = self.parse_acrichis_current(raw)
        if not parsed:
            logger.warning("[sdjk] 言录使当期子页解析失败（模板结构变了？）")
            return "failed"
        observed = _parse_acrithis_observed(parsed.get("observed", ""))
        if observed is None:
            logger.warning("[sdjk] 言录使当期上报日期无法解析：%r", parsed.get("observed"))
            return "failed"
        now = datetime.now(timezone.utc)
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if observed < monday:
            logger.info(
                "[sdjk] 言录使当期上报尚未更新（observed %s < 本周一），本轮不落盘",
                parsed["observed"],
            )
            return "not-updated"
        # ★ 2026-10-05：wiki 把 observed 改成魔法词后它恒等于「今天」⇒ 上面的
        #   observed 闸门对新格式失效。补**页面修订时间戳**闸门（社区每周一编辑
        #   该页，实测 09-21 / 09-28 / 10-05 各有 "rotation updated"）；
        #   查询失败时降级为只按 observed 判定（记 WARN，不阻断刷新）。
        if not self._rotation_source_not_older("acrithis", rev):
            return "not-updated"
        if rev and (rev < monday or rev > now):
            logger.info(
                "[sdjk] 言录使页面本周尚未修订（last rev %s < 本周一），本轮不落盘",
                rev.isoformat(),
            )
            return "not-updated"
        if len(set(parsed["items"])) != 5:
            return "failed"
        items: list[dict] = []
        for en in parsed["items"]:
            ent = catalog.get(en)
            if ent is None:
                # 子页名单与本地目录出现分歧（wiki 改了池子）→ 拒绝写半成品
                logger.error(
                    "[sdjk] 言录使当期上报出现目录外物品 %r —— wiki 池子可能已"
                    "改动，需人工核对 _en_catalog 后重试",
                    en,
                )
                return "failed"
            it = {"name": ent["name"]}
            if ent.get("qty") is not None:
                it["qty"] = ent["qty"]
            it["price"] = ent["price"]
            items.append(it)
        # ★ 2026-10-18：observed / 页面修订两道闸门都可能被「本周有人编辑过页面、
        #   但货单还没改」骗过；每周 5 件不会与上周完全相同 ⇒ 与上周一致就当未更新。
        prev = self._offering_values(snap.get("items"))
        fingerprint = json.dumps(
            self._offering_values(items), ensure_ascii=False, separators=(",", ":")
        )
        if (
            self._processed_source_unchanged("acrithis", fingerprint)
            and self._offering_values(items) != prev
        ):
            return "not-updated" if expired else "unchanged"
        if prev and self._offering_values(items) == prev:
            if not expired:  # 强制重抓、本地货单还没到期：一样就是本地已是最新
                return "fresh"
            logger.info("[sdjk] 言录使当期上报与上周货单完全相同（wiki 尚未更新），本轮不落盘")
            return "not-updated"
        out = {
            "expiry": self.acrithis_next_reset(),
            "observed": parsed["observed"],
            "source": (
                f"wiki《Acrithis/Current Offerings》当期上报（observed "
                f"{parsed['observed']}）；插件自动抓取"
            ),
            "items": items,
            "_price_note": "价格来自静态候选目录，非本周实时核验；以游戏内为准",
            "_en_catalog": catalog,
            "_catalog_price_source": snap.get("_catalog_price_source", ""),
            "_reset_rule": snap.get("_reset_rule", ""),
            "_过期口径": snap.get("_过期口径", ""),
        }
        self._ensure_open()
        paths.atomic_write_text(
            paths.write_path(ACRITHIS_WEEK_NAME),
            json.dumps(out, ensure_ascii=False, indent=1) + "\n",
        )
        logger.info(
            "[sdjk] 言录使本周货单已自动刷新（observed %s，%d 件）", parsed["observed"], len(items)
        )
        return "refreshed"

    async def _community_acrithis_week(self, snap: dict, expired: bool = True) -> str:
        """言录使兜底：读公开仓社区快照并落盘；校验不过返回 ``"failed"``。

        只替换动态字段（expiry/observed/items），本地 ``_en_catalog`` 等归档信息保留。
        """
        if not self._acrichis_runtime_ready():
            return "failed"
        payload = await self._community_json(COMMUNITY_ACRITHIS_URLS)
        if payload is None:
            return "failed"
        ok, why = community_acrithis_ok(payload)
        if not ok:
            logger.warning("[sdjk] 社区言录使快照校验不过：%s", why)
            self._note_comm("acrithis_week.json", "invalid", why, payload.get("expiry"))
            return "failed"
        items: list[dict] = []
        for it in payload["items"]:
            ent = {"name": str(it["name"])}
            if it.get("qty") is not None:
                ent["qty"] = it["qty"]
            if it.get("price") is not None:
                ent["price"] = it["price"]
            items.append(ent)
        prev = self._offering_values(snap.get("items"))
        if prev and self._offering_values(items) == prev:
            if not expired:  # 强制重抓、本地货单还没到期：一样就是本地已是最新
                return "fresh"
            # 发布端也可能抓到「还没改的上周货单」⇒ 与上周一致不采用（同 refresh_acrichis_week）
            logger.info("[sdjk] 社区言录使快照与上周货单完全相同（wiki 尚未更新），不采用")
            return "not-updated"
        out = {
            **snap,  # 保留 _en_catalog / _reset_rule 等
            "expiry": payload["expiry"],
            "observed": payload.get("observed") or snap.get("observed", ""),
            "items": items,
            "source": (
                payload.get("source")
                or "社区快照（wiki《Acrithis/Current Offerings》经有 FS 的机器发布）"
            ),
        }
        self._ensure_open()
        paths.atomic_write_text(
            paths.write_path(ACRITHIS_WEEK_NAME),
            json.dumps(out, ensure_ascii=False, indent=1) + "\n",
        )
        logger.info(
            "[sdjk] 言录使本周货单已从社区快照刷新（observed %s，%d 件）",
            out["observed"],
            len(items),
        )
        self._note_comm("acrithis_week.json", "adopted", "", payload.get("expiry"))
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
            r = await self._http.get(url, timeout=15.0, headers={"User-Agent": USER_AGENT})
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
        return await self.worldstate(platform, "conclaveChallenges", ttl=TTL_WORLDSTATE_MID) or []

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
                self._fetch_json(
                    f"{DEFAULT_ORACLE_BASE}/bounty-cycle",
                    ttl=900,
                    headers=_ORACLE_HEADERS,
                    cache_key="oracle:bounty-cycle",
                ),
                timeout=3.5,
            )
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
        if isinstance(data, dict) and self._is_community_calendar(data):
            # 直连社区源（worldsource=warframestat）时拿到的是 warframestat 原始格式，
            # 转成与 DE 解析相同的内部结构；DE 后备整包里已经转过的不会再命中
            try:
                data = de_worldstate.community_calendar(data)
            except (KeyError, TypeError, ValueError, AttributeError):
                return None
        if isinstance(data, dict):
            data = await self._resolve_calendar_rewards(data)
        return data

    @staticmethod
    def _is_community_calendar(data: dict) -> bool:
        """warframestat 日历：事件类型是 To Do / Big Prize! / Override（DE 解析结果是 CHALLENGE 等）。"""
        for day in data.get("days") or []:
            for ev in (day or {}).get("events") or []:
                if isinstance(ev, dict) and ev.get("type") in ("To Do", "Big Prize!", "Override"):
                    return True
        return False

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

    # ------------------------------------------------------------------
    # Warframe.Market v2：物品字典 / 订单
    # ------------------------------------------------------------------
    async def _wm_v2(
        self,
        path: str,
        *,
        ttl: float,
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
        platform: Optional[str] = None,
    ) -> Any:
        hdr = {"language": self._lang_code(), "crossplay": "true"}
        if platform:
            hdr["platform"] = _WM_PLATFORM.get(platform, platform)
        # HTTP 字段名不区分大小写；按实际发送值建立缓存身份，避免不同等级串单。
        hdr.update({k.lower(): str(v) for k, v in (headers or {}).items()})
        query = {k: str(v) for k, v in (params or {}).items()}
        url = f"{self.wm_v2_base}{path}"
        identity = json.dumps(
            {"url": url, "params": query, "headers": hdr}, sort_keys=True
        ).encode()
        # 自定义请求头可能含凭据，只保存摘要，不把原值写进缓存键或日志。
        digest = hashlib.sha256(identity).hexdigest()
        data = await self._fetch_json(
            url,
            ttl=ttl,
            headers=hdr,
            params=query or None,
            cache_key=f"wmv2:{path}|{digest}",
            wm_rate_limit=True,
            validator=self._validate_wm_v2,
        )
        # 保留调用边界校验，兼容替换 _fetch_json 的离线适配器。
        self._validate_wm_v2(data)
        return data.get("data") if isinstance(data, dict) else data

    @staticmethod
    def _validate_wm_v2(data: Any) -> None:
        """WM 错误信封必须在写入共享请求缓存之前拒绝。"""
        if isinstance(data, dict):
            if data.get("error"):
                raise WarframeAPIError(f"WM v2 接口错误：{data['error']}")
            # ★ 2026-10-25（Astra 故障注入）：结构缺字段时原样返回 None ⇒ 上层当成「零条挂单」，
            #   卡面写「没有符合条件的在售单」把接口异常说成真实零结果。缺 data 一律按结构异常报。
            if "data" not in data:
                raise WarframeAPIError("warframe.market 返回结构异常（缺 data 字段），请稍后再试")

    async def wm_items(self) -> list[dict]:
        """全量物品（含 zh-hans 名称、杜卡德与**满级**），归一化字段。"""
        raw = await self._wm_v2("/items", ttl=TTL_WM_ITEMS) or []
        out = []
        for it in raw:
            i18n = it.get("i18n") or {}
            zh = (i18n.get("zh-hans") or {}).get("name", "")
            en = (i18n.get("en") or {}).get("name", "")
            out.append(
                {
                    "id": it.get("id"),
                    "url_name": it.get("slug", ""),
                    "game_ref": it.get("gameRef", ""),
                    "zh": zh,
                    "en": en,
                    "ducats": it.get("ducats"),
                    "trading_tax": it.get("tradingTax"),
                    "tradable": it.get("tradable"),
                    "tags": it.get("tags") or [],
                    # ★ 2026-09-25：WM 自带 maxRank（赋能 5 / 川流不息 5 / 生命力 10）——
                    #   「满级」筛选过去写死 10，赋能与一批 5 级 MOD 必然查空。
                    "max_rank": it.get("maxRank"),
                }
            )
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
                f"{self.wm_base}/tools/ducats",
                ttl=TTL_WM_DUCATS,
                wm_rate_limit=True,
                cache_key="wmv1:tools/ducats",
            )
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
            out.append(
                {
                    "name": (
                        (it.get("en") or it.get("zh"))
                        if i18n.is_en()
                        else (it.get("zh") or it.get("en"))
                    )
                    or it.get("url_name", ""),
                    "url_name": it.get("url_name", ""),
                    "ducats": r.get("ducats") or it.get("ducats") or 0,
                    "dpp": float(r.get("ducats_per_platinum") or 0),
                    "dpp_wa": float(r.get("ducats_per_platinum_wa") or 0),
                    "plat": float(r.get("wa_price") or 0),
                    "median": float(r.get("median") or 0),
                    "volume": int(r.get("volume") or 0),
                    "tags": it.get("tags") or [],
                }
            )
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
                out[key] = {
                    "ducats": r.get("ducats", 0),
                    "plat": r.get("median") or r.get("plat") or 0,
                    "dpp": r.get("dpp", 0),
                }
            return out

        # 键里的物品名随显示语言变化，两种语言各存一份，避免英文会话读到中文键（反之亦然）。
        key = f"wmv1:ducats_price_map|{i18n.current_lang()}"
        return await self.cache.get_or_fetch(key, TTL_WM_DUCATS, _build) or {}

    async def wm_orders(
        self, url_name: str, platform: str, rank: Optional[int] = None
    ) -> tuple[list[dict], dict]:
        """某物品近 48h 可见订单，字段向 v1 对齐（order_type/mod_rank/ingame_name）。"""
        path = f"/orders/item/{quote(url_name)}"
        params = {}
        if rank is not None:
            path += "/top"
            params["rank"] = str(rank)
        # WM v2 官方 Orders 文档：rank 是 /top 的 query 参数，不是请求头。
        raw = await self._wm_v2(path, ttl=TTL_WM_ORDERS, platform=platform, params=params or None)
        if isinstance(raw, dict):  # /top 返回 {sell: [...], buy: [...]}
            raw = (raw.get("sell") or []) + (raw.get("buy") or [])
        if raw is not None and not isinstance(raw, list):
            raise WarframeAPIError("warframe.market 返回结构异常（订单不是列表），请稍后再试")
        # 有订单、却**全都**缺价格 / 类型 ⇒ 结构变了，不能当「没有挂单」（2026-10-25，同上）
        if raw and not any(isinstance(o, dict) and "platinum" in o and "type" in o for o in raw):
            raise WarframeAPIError("warframe.market 返回结构异常（订单缺价格字段），请稍后再试")
        orders = []
        for o in raw or []:
            if not isinstance(o, dict) or "platinum" not in o:
                continue
            user = o.get("user") or {}
            orders.append(
                {
                    "id": o.get("id"),
                    "order_type": o.get("type", "sell"),
                    "platinum": o.get("platinum", 0),
                    "quantity": o.get("quantity", 1),
                    "mod_rank": o.get("rank"),
                    "subtype": o.get("subtype"),
                    "visible": o.get("visible", True),
                    "platform": user.get("platform", platform),
                    "last_update": o.get("updatedAt", ""),
                    "user": {
                        "ingame_name": user.get("ingameName", "?"),
                        "status": user.get("status", "offline"),
                        "reputation": user.get("reputation", 0),
                    },
                }
            )
        return orders, {}

    async def wm_item_detail(self, slug: str) -> dict:
        """WM v2 物品详情：set_root（是否套装根）/ set_parts（部件物品 ID 列表）。

        2026-09-14 维护者要求：查套装默认仍输出套装单，但要把分件（蓝图/机体/
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
        WM v2 该端点（2026-09-23 实测 418 条；**2026-10-05 实测 420 条**）几乎
        不含 Prime/新变体（rubico_prime、kuva_zarr、tenet_arca_plasmor 全缺），
        变体倾向会错给本体值。补全数据由 scripts/build_disposition.py 生成
        （riven-mirror 细粒度值 + DE 官方 zh + 基类 riven_type 继承），按
        url_name 去重后追加 ⇒ **合并表 670 条**（420 + 去重并入 250；
        容器内 `wm_riven_weapons()` 实测，2026-10-05）。
        """
        raw = await self._wm_v2("/riven/weapons", ttl=TTL_WM_ITEMS) or []
        out = []
        seen_urls = set()
        for w in raw:
            i18n = w.get("i18n") or {}
            url = w.get("slug", "")
            seen_urls.add(url)
            out.append(
                {
                    "url_name": url,
                    "zh": (i18n.get("zh-hans") or {}).get("name", ""),
                    "en": (i18n.get("en") or {}).get("name", ""),
                    "game_ref": w.get("gameRef", ""),
                    "disposition": w.get("disposition"),
                    "riven_type": w.get("rivenType", ""),
                    "group": w.get("group", ""),
                }
            )
        try:
            extra = (
                json.loads(
                    (DATA_DIR / "dispositions_rivenmirror.json").read_text(encoding="utf-8")
                ).get("entries")
                or {}
            )
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
                # ★ 2026-10-19：`riven_type`/`group` 也**本地非空则优先**。
                #   本地表这两列绝大多数继承自 WM（同值 ⇒ 无影响），但这样能带上少量
                #   **源数据修正**：WM 把 Bubonico/Coda Bubonico 标成 `rifle`，实为霰弹枪
                #   （实卡 + wfsim `mod_pools: [primary, shotgun]` 双证），按步枪列算会把
                #   暴伤基值 120 当成 90 用。**空值不覆盖** —— 免得把 WM 有值的条目标空
                #   （本地表里仍有 21 条 group/riven_type 为空，多为基类不在表里的形态行）。
                if e.get("riven_type"):
                    hit["riven_type"] = e["riven_type"]
                if e.get("group"):
                    hit["group"] = e["group"]
                continue
            by_url[u] = {
                "url_name": u,
                "zh": e.get("zh", ""),
                "en": en,
                "game_ref": "",
                "disposition": e.get("disposition"),
                "riven_type": e.get("riven_type", ""),
                "group": e.get("group", ""),
            }
            out.append(by_url[u])
        # ★ 2026-09-24：补全条目里有一批没有中文名（实测 48 条），不补会
        #   在倾向卡里漏出英文（玩家报障「Larkspur Prime 倾向 0.80」）。
        await self._fill_variant_zh(out)
        return out

    async def _fill_variant_zh(self, entries: list[dict]) -> None:
        """给缺中文名的紫卡武器条目补中文写法（就地写回；补不到保持英文）。

        逐级来源：WM 主物品表（slug / slug+_set / 英文名）→ 译名候选表
        （表内母武器 + DE 官方简中表 de/name_en_zh.json）→ 玄骸武器表 →
        变体规则拼装（``localize_variant_en``）。
        """
        miss = [e for e in entries if not (e.get("zh") or "").strip()]
        if not miss:
            return
        # 译名候选表：表内已带中文的条目（母武器）+ DE 官方简中表
        base_zh: dict[str, str] = {}
        for e in entries:
            zh = (e.get("zh") or "").strip()
            if zh:
                for key in (e.get("en"), e.get("url_name")):
                    k = _norm_name_key(key or "")
                    if k:
                        base_zh.setdefault(k, zh)
        for k, v in _name_en_zh().items():
            base_zh.setdefault(_norm_name_key(k), v)
        # WM 主物品表（套装名去掉「 一套」后缀）
        try:
            items = await self.wm_items()
        except Exception:  # noqa: BLE001 - WM 挂了只用本地表
            items = []
        by_slug: dict[str, str] = {}
        for it in items:
            zh = (it.get("zh") or "").strip()
            if not zh:
                continue
            for suf in (" 一套", " Set"):
                if zh.endswith(suf):
                    zh = zh[: -len(suf)]
                    break
            slug = it.get("url_name") or ""
            if slug:
                by_slug[slug] = zh
            for key in (slug, it.get("en") or ""):
                k = _norm_name_key(key)
                if k:
                    base_zh.setdefault(k, zh)
        filled = 0
        for e in miss:
            url = e.get("url_name") or ""
            en = e.get("en") or ""
            zh = ""
            for cand in (url, url + "_set"):
                zh = by_slug.get(cand, "")
                if zh:
                    break
            if not zh:
                zh = base_zh.get(_norm_name_key(en), "")
            if not zh and url:
                info = self.lich_weapon_info(url)
                # 玄骸表查不到时按前缀回落成 slug（"kuva_x"），不算译名
                if info.get("zh") and info.get("zh") != url:
                    zh = info["zh"]
            if not zh:
                zh = localize_variant_en(en, base_zh)
            if zh:
                e["zh"] = zh
                filled += 1
        logger.info("[sdjk] 紫卡武器中文名补全 %d/%d 条（余下保持英文）", filled, len(miss))

    # ------------------------------------------------------------------
    # Warframe.Market v1：价格统计（趋势 / 排行）
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_wm_statistics(data: Any) -> None:
        """拒绝错误/缺字段信封，保留合法的空统计与可缺省的周期。"""
        payload = data.get("payload") if isinstance(data, dict) and not data.get("error") else None
        closed = payload.get("statistics_closed") if isinstance(payload, dict) else None
        if not isinstance(closed, dict):
            raise _WMV1ContentError("warframe.market 返回结构异常（成交统计信封），请稍后再试")
        for period in ("48hours", "90days"):
            rows = closed.get(period)
            if rows is not None and (
                not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            ):
                raise _WMV1ContentError("warframe.market 返回结构异常（成交统计列表），请稍后再试")

    @staticmethod
    def _validate_wm_auctions(data: Any) -> None:
        """两类拍卖共用信封；空数组代表真实零挂单，缺字段代表接口异常。"""
        payload = data.get("payload") if isinstance(data, dict) and not data.get("error") else None
        rows = payload.get("auctions") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise _WMV1ContentError("warframe.market 返回结构异常（拍卖挂单列表），请稍后再试")

    async def wm_statistics(self, slug: str, platform: str = "pc") -> dict:
        """v1 价格统计：近 48 小时逐小时 + 近 90 天逐日。

        返回 {"h48": [{t, volume, median, avg, min, max}], "d90": [...]}
        """
        hdr = {
            "Language": self._lang_code(),
            "Platform": _WM_PLATFORM.get(platform, platform),
            "Crossplay": "true",
        }
        data = await self._fetch_json(
            f"{self.wm_base}/items/{quote(slug)}/statistics",
            ttl=TTL_WM_STATS,
            headers=hdr,
            cache_key=f"wmstats:{slug}|{platform}",
            wm_rate_limit=True,
            validator=self._validate_wm_statistics,
        )
        self._validate_wm_statistics(data)
        payload = (data or {}).get("payload") or {}
        closed = payload.get("statistics_closed") or {}

        def norm(rows: list[dict]) -> list[dict]:
            out = []
            for r in rows or []:
                out.append(
                    {
                        "t": (r.get("datetime") or "")[:16].replace("T", " "),
                        "volume": r.get("volume") or 0,
                        "median": r.get("median") or 0,
                        "avg": r.get("avg_price") or 0,
                        "min": r.get("min_price") or 0,
                        "max": r.get("max_price") or 0,
                        "rank": r.get("mod_rank") or 0,
                    }
                )
            return out

        return {"h48": norm(closed.get("48hours")), "d90": norm(closed.get("90days"))}

    @staticmethod
    def summarize_stats(stats: dict) -> dict:
        """把统计原始行汇总成「热度 + 价格」指标。"""
        h48, d90 = stats.get("h48") or [], stats.get("d90") or []
        vol48 = sum(r["volume"] for r in h48)
        vol90 = sum(r["volume"] for r in d90)
        med_all = [r["median"] for r in d90 if r["median"]] or [
            r["median"] for r in h48 if r["median"]
        ]
        med48 = [r["median"] for r in h48 if r["median"]]
        avg90 = [r["avg"] for r in d90 if r["avg"]]
        recent = med48[-1] if med48 else (med_all[-1] if med_all else 0)
        early = med48[0] if med48 else recent
        prev = med_all[: max(1, len(med_all) // 3)]
        base = (sum(prev) / len(prev)) if prev else recent
        return {
            "vol48": vol48,
            "vol90": vol90,
            "median": round(sum(med_all) / len(med_all), 1) if med_all else 0,
            "median48": round(sum(med48) / len(med48), 1) if med48 else 0,
            "avg90": round(sum(avg90) / len(avg90), 1) if avg90 else 0,
            "last": recent,
            "first": early,
            # ★ 2026-10-25（Astra 真实宿主补测）：卡面原写「vs 区间初 {first}」，而 change 的基线是
            #   这段的「前三分之一均价」⇒「▲27.3%（最新 30p vs 区间初 40p）」自相矛盾。把真实基线带出去。
            "base": round(base, 1) if base else 0,
            "base_span": "90 天" if d90 and any(r["median"] for r in d90) else "48 小时",
            # 百分比按**显示出来的**基线算（ZCode 10-25：未舍入基线算出 +55.3%，括号 35.0 vs 22.5
            # 复算是 +55.6%，差 0.3pp）⇒ 拿卡面两个数就能复算出同一个百分比
            "change": round((recent - round(base, 1)) / round(base, 1) * 100, 1)
            if base and round(base, 1)
            else 0.0,
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
    def _save_json_file(path, payload) -> bool:
        """落盘；失败不抛（不影响本次查询），返回 False 让关心的调用方（如价格榜单）记状态。"""
        try:
            paths.atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=1))
            return True
        except Exception:  # noqa: BLE001 - 落盘失败不影响本次查询
            return False

    @staticmethod
    def _rank_record(it: dict, stats: dict) -> Optional[dict]:
        """单个物品的统计 → 排行行。

        * MOD 卡只取 0 级成交（维护者要求「全部可交易的 0 级卡片」）；
        * **必须有 48h 成交**才入榜 —— 只有 90 天旧数据的物品没有
          「当前价」，排进榜里只会显示一排「—」（玩家反馈）。
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
            "zh": it.get("zh") or "",
            "en": it.get("en") or "",
            "tags": tags,
            "median48": round(sum(med48) / len(med48), 1),
            "min48": min(mins) if mins else 0,
            "max48": max(maxs) if maxs else 0,
            "median_prev": prev_med,
            "vol48": sum(r.get("volume") or 0 for r in h48),
            "vol90": sum(r.get("volume") or 0 for r in d90),
        }

    async def crawl_wm_ranks(self, limit: Optional[int] = None) -> int:
        """单飞包装：自动循环与手动「排行 刷新」/ 面板按钮同时触发时只跑一份（2026-10-24）。

        两路并发会各自读旧游标、各自落盘互相覆盖，还会双倍占用 WM 限速；后到的直接返回当前已入榜数。
        """
        self._ensure_open()
        lock = self._crawl_lock("_rank_crawl_lock")
        if lock.locked():
            self._note_manual_crawl("degraded", "已有价格榜抓取在进行，本轮未重复执行")
            return len((self._load_json_file(paths.read_path(RANKS_NAME)) or {}).get("rows") or {})
        async with lock:
            try:
                return await self._crawl_wm_ranks(limit)
            finally:
                self._rank_crawl_finished_at = time.time()

    def _crawl_lock(self, name: str) -> asyncio.Lock:
        lock = getattr(self, name, None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(self, name, lock)
        return lock

    async def _crawl_wm_ranks(self, limit: Optional[int] = None) -> int:
        """抓取 WM 成交统计并落盘（约 3200 项，实测 1 项/4 秒）。

        2026-09-14 重做续跑逻辑：全量一轮要好几个小时，占用 WM 全局限速会
        拖慢用户的 wm/wr 查询，所以改成**游标续跑**——

        - 落盘记录 ``cursor``（下次从第几项继续）与 ``rows``（已抓结果）；
        - 每轮最多抓 ``limit`` 项，抓完就收工，下一轮（次日闲时）接着跑；
        - 跑满一轮（cursor 回到 0）**且成功率 ≥ RANK_MIN_COVERAGE** 才更新 ``ts``，整体刷新周期仍是 48h；
          失败项进 ``retry``，下次先补（2026-10-17）；不足 80% 记 ``low_coverage``、``ts`` 不动；
        - 每 50 项落一次盘，中断不丢已抓部分，排行查询始终能显示已完成部分。

        Args:
            limit: 本轮最多抓取项数；None = 不限（一次跑完）。

        Returns:
            当前已入榜的物品数。
        """
        self._ensure_open()
        self._rank_save_failed = False
        self._rank_source_empty = False
        items = [it for it in await self.wm_items() if _rank_candidate(it)]
        total = len(items)
        self._rank_source_empty = total == 0
        old = self._load_json_file(paths.read_path(RANKS_NAME)) or {}
        if total == 0:
            # 物品表取不到时 total=0，旧实现会「瞬间跑满一轮」并刷新 ts（一项没抓却标成新的）
            logger.warning("[sdjk] 价格榜单抓取：WM 物品表为空（可能取不到），本轮跳过、不刷新时间")
            self._note_manual_crawl("degraded", "物品表为空，本轮未刷新榜单")
            return len(old.get("rows") or {})
        targets = [it.get("url_name") or "" for it in items]
        target_set = set(targets)
        rows: dict = {k: v for k, v in (old.get("rows") or {}).items() if k in target_set}
        cursor = int(old.get("cursor") or 0)
        if cursor >= total:  # 上一轮已跑满，从头开始新一轮
            cursor = 0
        # ★ 2026-10-17 覆盖率闸门（维护者定 80%，GPT 评估 P14）：游标跑完 ≠ 成功跑完。
        #   failed = 本轮（进行中）请求失败的物品；retry = 上一轮失败、本次先补的物品。
        #   整轮成功率 ≥ RANK_MIN_COVERAGE 才刷新 ts；不足则 ts 不动（48h 闸门仍判过期 ⇒
        #   下个闲时继续先补失败项），low_coverage 供面板提示，旧行照常展示。
        failed: set = set(old.get("round_failed") or []) & target_set
        retry: list = [s for s in dict.fromkeys(old.get("retry") or []) if s in target_set]
        low = bool(old.get("low_coverage"))
        ts = old.get("ts") or ""
        coverage = old.get("coverage")
        if old.get("cursor_targets") != targets:
            # 旧断点或目标序列变化：旧下标不能套在新列表上，完整重扫后才认证覆盖率。
            cursor, low, coverage = 0, False, None
        by_slug = {it.get("url_name"): it for it in items if it.get("url_name")}
        save_failed = False
        attempted: dict[str, bool] = {}

        def _now_iso() -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

        async def _fetch(it: dict) -> bool:
            """抓一项；返回请求是否成功（成功但无 48h 成交 = 成功 + 出榜）。"""
            self._ensure_open()
            slug = it.get("url_name") or ""
            if slug in attempted:
                return attempted[slug]  # 补抓与主游标重叠时复用本次结果，不重复请求或重判成功。
            # ★ 2026-10-03 僵尸行修复（价格排行交办）：`rec is None` 必须与「请求失败」严格区分 ——
            #   请求失败（网络抖动）⇒ 保留旧行；_rank_record 返回 None = 确实无 48h 成交 ⇒ 出榜。
            try:
                stats = await self.wm_statistics(slug, "pc")
            except Exception:  # noqa: BLE001 - 单项请求失败不阻断整轮
                stats = None
            self._ensure_open()
            if stats is None:
                attempted[slug] = False
                return False
            rec = self._rank_record(it, stats)
            if rec:
                rec["at"] = _now_iso()  # 每行记自己的成功时间（整榜 ts 不代表每行都新）
                rows[slug] = rec
            else:
                rows.pop(slug, None)
            attempted[slug] = True
            return True

        def _save(cur: int) -> None:
            nonlocal save_failed
            ok = self._save_json_file(
                paths.write_path(RANKS_NAME),
                {
                    "ts": ts,
                    "cursor": cur,
                    "cursor_targets": targets,
                    "total": total,
                    "done": cur,
                    "rows": rows,
                    "round_failed": sorted(failed),
                    "retry": retry,
                    "coverage": coverage,
                    "low_coverage": low,
                },
            )
            if ok is False:
                save_failed = self._rank_save_failed = True

        done = 0
        # ① 先补上一轮失败的物品
        still: list = []
        for slug in retry:
            it = by_slug.get(slug)
            if it is None:  # 已不在候选里（下架 / 不再可交易）
                continue
            if limit is not None and done >= limit:
                still.append(slug)
                continue
            done += 1
            if not await _fetch(it):
                still.append(slug)
                failed.add(slug)
            else:
                failed.discard(slug)
        retry = still
        if low and (total - len(retry)) / total >= RANK_MIN_COVERAGE:
            ts, low = _now_iso(), False  # 上一轮靠补抓达标
            coverage = round((total - len(retry)) / total, 3)
        # ② 主游标续跑
        idx = cursor
        while idx < total:
            it = items[idx]
            slug = it.get("url_name") or ""
            already = slug in attempted
            if not already and limit is not None and done >= limit:
                break
            if slug:
                if await _fetch(it):
                    failed.discard(slug)
                    if slug in retry:
                        retry.remove(slug)
                else:
                    failed.add(slug)
            idx += 1
            if not already:
                done += 1
            if not already and done % 50 == 0:
                _save(idx)
        finished = idx >= total
        if finished:
            cov = (total - len(failed)) / total
            coverage = round(cov, 3)
            if cov >= RANK_MIN_COVERAGE:
                ts, low = _now_iso(), False
            else:
                low = True  # ts 不动：下个闲时继续，先补失败项
            retry = sorted(set(retry) | failed)
            failed = set()
        _save(0 if finished else idx)
        if save_failed:
            logger.warning("[sdjk] 价格榜单落盘失败（磁盘满 / 无权限？），本轮结果可能未保存")
        logger.info(
            "[sdjk] 价格榜单抓取：本轮 %d 项，累计 %d/%d%s；待补 %d 项",
            done,
            idx,
            total,
            (f"（本轮跑满，覆盖率 {coverage:.0%}" + ("，不足 80%，时间不刷新）" if low else "）"))
            if finished
            else "",
            len(retry),
        )
        if save_failed:
            self._note_manual_crawl("error", "本轮榜单保存失败")
        elif not finished or low or failed or retry:
            self._note_manual_crawl("degraded", "本轮存在未完成或失败项目，榜单尚待补抓")
        else:
            self._note_manual_crawl("ok", "本轮抓取完成")
        return len(rows)

    # ------------------------------------------------------------------
    # 紫卡实时排行（WM 拍卖快照）
    # ------------------------------------------------------------------
    async def crawl_riven_realtime(self, limit: Optional[int] = None) -> int:
        """单飞包装：凌晨自动重建与手动「紫卡排行 刷新」/ 面板按钮只跑一份（2026-10-24），理由同 `crawl_wm_ranks`。"""
        self._ensure_open()
        lock = self._crawl_lock("_riven_rt_lock")
        if lock.locked():
            self._note_manual_crawl("degraded", "已有紫卡现价抓取在进行，本轮未重复执行")
            return len(self.riven_realtime_rows().get("rows") or {})
        async with lock:
            try:
                return await self._crawl_riven_realtime(limit)
            finally:
                self._riven_rt_finished_at = time.time()

    async def _crawl_riven_realtime(self, limit: Optional[int] = None) -> int:
        """抓紫卡实时排行快照（WM 拍卖）并落盘；返回已入表武器数。

        2026-10-23（玩家报「紫卡排行用的 DE 周报时效差，1.2 热修后暴涨看不出」）：
        周报**每周一 00:00 UTC 才更新一次**，热修后那几周全是旧数；这里改用
        **WM 拍卖的实时挂单**做默认视图（周报保留作 `紫卡排行 周报` 子视图）。

        续跑逻辑与 `crawl_wm_ranks` 同套（用户口径一致）：
        - 目标集 = `riven_realtime_targets()`（周报热度前 N + 原倾向 ≤0.7，去重、折母武器）；
        - 落盘记 ``cursor`` / ``rows``，每轮最多 ``limit`` 项，**每 20 项落一次盘**，中断可续；
        - 跑满一轮**且成功率 ≥ RANK_MIN_COVERAGE** 才更新 ``ts``；失败项进 ``retry`` 下次先补；
        - 每把只查价格升序一次；节奏见 `RIVEN_RT_GAP_S` 等类常量（拍卖搜索限得严，2026-10-24）。
        """
        self._ensure_open()
        weekly = await self.de_weekly_rivens()
        weapons = await self.wm_riven_weapons()
        try:
            disp = (
                json.loads(
                    (DATA_DIR / "dispositions_rivenmirror.json").read_text(encoding="utf-8")
                ).get("entries")
                or {}
            )
        except Exception:  # noqa: BLE001 - 缺表就只用周报那一路
            disp = {}
        try:
            fam = json.loads(paths.read_path("de/riven_families.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            fam = {}
        targets = riven_realtime_targets(weekly.get("entries") or [], weapons, disp, fam)

        old = self._load_json_file(paths.read_path(RIVEN_RT_NAME)) or {}
        rows: dict = old.get("rows") or {}
        cursor = int(old.get("cursor") or 0)
        total = len(targets)
        if total == 0:
            logger.warning("[sdjk] 紫卡 WM 现价：目标集为空（周报/武器表取不到？），本轮跳过")
            self._note_manual_crawl("degraded", "目标集为空，本轮未刷新现价")
            return len(rows)
        if cursor >= total:
            cursor = 0
        # ★ 2026-10-25（ZCode A）：旧轮失败项只保留仍在目标集里的 —— 目标集变过（折母武器修正）后，
        #   残留的旧 slug 还计入覆盖率分母，133/133 全成功也只算出 0.887
        failed: set = set(old.get("round_failed") or []) & set(targets)
        retry: list = list(dict.fromkeys(old.get("retry") or []))
        ts = old.get("ts") or ""
        coverage = old.get("coverage")
        save_failed = False

        def _now_iso() -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

        last_err = ""
        # WM 回 400（item_not_exist）的 url：**永久无效**，移出目标集、不计连续失败、不进覆盖率分母
        #   （2026-10-25 ZCode 实测：目标集混进 7 个 WM 没有的变体 slug，连续 400 触发提前收工）
        invalid: set = set(old.get("invalid") or []) & set(targets)
        targets = [t for t in targets if t not in invalid]
        total = len(targets)
        if total == 0:
            logger.warning("[sdjk] 紫卡 WM 现价：目标集全被标为无效，本轮跳过")
            self._note_manual_crawl("degraded", "目标集均无效，本轮未刷新现价")
            return len(rows)
        if cursor >= total:
            cursor = 0
        # 游标只对保存时的有效目标序列成立。旧快照没有序列，或目标顺序/成员变化，
        # 保守从头重抓，不能把旧下标套在压缩后的列表上而漏掉尚未请求的武器。
        if old.get("cursor_targets") != targets:
            cursor = 0
            retry = []  # 主游标将覆盖整个有效序列，不再先补同一批条目。
        # ★ 2026-10-25（ZCode：卡面「已收录 98/97」）：目标集每周随周报热度变，退出目标集的旧行
        #   （如 dex_nikana）原先一直留在 rows 里 ⇒ 分子大于分母，旧价也会混进周报卡。按目标集裁掉。
        stale = [k for k in rows if k not in set(targets)]
        for k in stale:
            rows.pop(k, None)

        async def _one(slug: str):
            """取一把武器的挂单并算指标；返回 True（成功）/ False（失败，待补）/ None（WM 无此条目）。"""
            nonlocal last_err
            try:
                auctions = await self.wm_riven_auctions(slug, "pc", sorts=("price_asc",), retries=0)
            except Exception as exc:  # noqa: BLE001 - 单项失败不阻断整轮
                msg = str(exc) or type(exc).__name__
                if "400 Bad Request" in msg or "item_not_exist" in msg:
                    return None
                last_err = f"{slug}: {msg}"[:160]
                return False
            prices: list[int] = []
            online_low: Optional[int] = None
            for a in auctions or []:
                if a.get("closed") or a.get("visible") is False:
                    continue
                b = a.get("buyout_price")
                if b is None:
                    b = a.get("starting_price")
                if not b:
                    continue
                prices.append(int(b))
                owner = (a.get("owner") or {}).get("status")
                if owner in ("ingame", "online"):
                    online_low = int(b) if online_low is None else min(online_low, int(b))
            if not prices:
                rows.pop(slug, None)
                return True
            top = sorted(prices)[:RIVEN_RT_TOP_PRICES]
            mid = (
                top[len(top) // 2]
                if len(top) % 2
                else round((top[len(top) // 2 - 1] + top[len(top) // 2]) / 2, 1)
            )
            rows[slug] = {
                "online_low": online_low,
                "low": min(prices),
                "median": mid,
                "listings": len(prices),
                "at": _now_iso(),
            }
            return True

        def _save(cur: int) -> bool:
            active = [t for t in targets if t not in invalid]
            # 本轮才发现的 400 也会在下次被过滤，保存时同步压缩已扫描前缀的游标。
            resume = sum(t not in invalid for t in targets[:cur])
            return (
                self._save_json_file(
                    paths.read_path(RIVEN_RT_NAME),
                    {
                        "ts": ts,
                        "cursor": resume,
                        "cursor_targets": active,
                        "rows": rows,
                        "round_failed": sorted(failed),
                        "retry": sorted(failed),
                        "invalid": sorted(invalid),
                        "coverage": coverage,
                        "total": len(active),
                    },
                )
                is not False
            )

        # ★ 2026-10-24 两处修正（ZCode 线上实测：7 分钟只收 8 把、72 项失败、ts 一直空）：
        #   ① **补抓与游标分开**（同 `crawl_wm_ranks`）：原写法把 retry 混进待抓表，又按「已抓条数」
        #      推进游标 ⇒ 补抓的条目也算进进度，游标被推到错位、整轮永远跑不满；
        #   ② **爬取层限速**：每项间隔 RIVEN_RT_GAP_S、失败后退避 RIVEN_RT_FAIL_BACKOFF_S，
        #      连续失败 RIVEN_RT_MAX_FAIL_STREAK 次就收工（落盘、下次续跑），别顶着 429 硬打。
        n_done = 0
        streak = 0
        aborted = False

        async def _step(slug: str) -> None:
            nonlocal n_done, streak, aborted
            self._ensure_open()
            ok = await _one(slug)
            self._ensure_open()
            n_done += 1
            if ok is None:
                invalid.add(slug)
                failed.discard(slug)
                rows.pop(slug, None)
                logger.info("[sdjk] 紫卡 WM 现价：%s 在 WM 不存在（400），移出目标集", slug)
                await asyncio.sleep(self.RIVEN_RT_GAP_S)
                return
            if ok:
                failed.discard(slug)
                streak = 0
                await asyncio.sleep(self.RIVEN_RT_GAP_S)
            else:
                failed.add(slug)
                streak += 1
                if streak >= self.RIVEN_RT_MAX_FAIL_STREAK:
                    aborted = True
                else:
                    await asyncio.sleep(self.RIVEN_RT_FAIL_BACKOFF_S)

        if stale:  # 裁剪结果立刻落盘，别让「已收录」在下一次存盘前继续溢出
            save_failed = (not _save(cursor)) or save_failed
        target_set = set(targets)
        remaining = set(targets[cursor:])
        retry_todo = [s for s in retry if s in target_set and s not in remaining]
        # 两个阶段各打一条进度（ZCode 10-25：补抓阶段 18 分钟零日志，被误判成没跑起来）
        logger.info(
            "[sdjk] 紫卡 WM 现价：先补 %d 项，再从第 %d/%d 项续跑（每项约 %.0f 秒）",
            len(retry_todo),
            cursor,
            total,
            self.RIVEN_RT_GAP_S,
        )
        for slug in retry_todo:
            if aborted or (limit is not None and n_done >= limit):
                break
            await _step(slug)
            if n_done % 20 == 0:
                save_failed = (not _save(cursor)) or save_failed
        idx = cursor
        while not aborted and idx < total and (limit is None or n_done < limit):
            await _step(targets[idx])
            idx += 1
            if n_done % 20 == 0:
                save_failed = (not _save(idx)) or save_failed
                logger.info("[sdjk] 紫卡 WM 现价：进度 %d/%d，已收录 %d 把", idx, total, len(rows))
        round_done = idx >= total
        cursor = 0 if round_done else idx
        if round_done:  # 跑满一轮（本次新发现的无效项不进覆盖率分母）
            valid_n = total - len(invalid & set(targets))
            cov = (valid_n - len(failed)) / valid_n if valid_n else 0.0
            coverage = round(cov, 3)
            if cov >= RANK_MIN_COVERAGE:
                ts = _now_iso()
            else:
                logger.warning(
                    "[sdjk] 紫卡 WM 现价本轮成功率 %.0f%% < %.0f%%，不刷新时间戳",
                    cov * 100,
                    RANK_MIN_COVERAGE * 100,
                )
        if failed:
            logger.warning(
                "[sdjk] 紫卡 WM 现价：本次抓 %d 项，%d 项待补%s（最近一次失败：%s）",
                n_done,
                len(failed),
                "，连续失败已提前收工" if aborted else "",
                last_err or "未知",
            )
        if not save_failed:
            save_failed = not _save(cursor)
        if save_failed:
            self._note_manual_crawl("error", "本轮紫卡现价保存失败")
        elif (
            aborted or failed or not round_done or coverage is None or coverage < RANK_MIN_COVERAGE
        ):
            self._note_manual_crawl("degraded", "本轮存在未完成或失败项目，现价尚待补抓")
        else:
            self._note_manual_crawl("ok", "本轮抓取完成")
        return len(rows)

    def riven_realtime_rows(self) -> dict:
        """落盘的紫卡实时排行快照（``{"ts":…, "rows":{slug: {…}}}``）。"""
        return self._load_json_file(paths.read_path(RIVEN_RT_NAME)) or {}

    @staticmethod
    def _note_manual_crawl(result: str, detail: str) -> None:
        """只更新正在执行的手动Task；自动循环不共享这一轮结果。"""
        task = asyncio.current_task()
        meta = getattr(task, "_wf_crawl_status", None)
        if isinstance(meta, dict):
            meta.update(result=result, detail=detail)

    def _start_manual_crawl(self, kind: str) -> asyncio.Task:
        meta = {"state": "running", "result": None, "last_attempt": time.time()}

        async def run():
            try:
                # 不改变公共crawl签名，兼容适配器与无参数测试替身。
                count = await (
                    self.crawl_wm_ranks() if kind == "rank" else self.crawl_riven_realtime()
                )
                if meta.get("result") is None:
                    meta.update(result="ok", detail="本轮抓取完成")
                meta["state"] = "failed" if meta["result"] == "error" else "completed"
                if meta["result"] == "ok":
                    meta["last_success"] = time.time()
                return count
            except asyncio.CancelledError:
                meta.update(state="cancelled", result=None, detail="已取消")
                raise
            except Exception as exc:
                meta.update(
                    state="failed",
                    result="error",
                    last_error=type(exc).__name__,
                    last_error_at=time.time(),
                    detail="本轮抓取异常退出",
                )
                raise
            finally:
                meta["last_finished"] = time.time()

        loop = asyncio.get_running_loop()
        coro = run()
        try:
            task = loop.create_task(coro)
        except BaseException:
            coro.close()
            raise
        # create_task尚未让出当前事件循环，协程开工前就绑定本轮结果。
        task._wf_crawl_status = meta

        def finished(done):
            if done.cancelled():
                # 尚未调度就取消时run的finally不会执行；仍补齐本任务结束状态。
                meta.update(state="cancelled", result=None, detail="已取消")
                meta.setdefault("last_finished", time.time())
                return
            exc = done.exception()
            if exc is not None:
                logger.warning("[sdjk] 手动%s抓取异常退出：%s", kind, type(exc).__name__)

        task.add_done_callback(finished)
        return task

    def start_riven_rt_crawl(self, *, force: bool = False) -> tuple[str, int, int]:
        """手动重抓紫卡实时排行（不受凌晨闲时闸门；2026-10-24 维护者定：和价格排行一样能手动拉）。

        返回 ``(状态, 已收录把数, 目标总数)``，状态 = ``started`` / ``running`` / ``cooldown``。
        群里人人能发，所以距上次手动启动不足 ``RIVEN_RT_MANUAL_COOLDOWN`` 秒时不再开抓
        （从启动和结束分别计算）；``force`` 不跳过安全冷却。
        """
        self._ensure_open()
        data = self.riven_realtime_rows()
        n, total = len(data.get("rows") or {}), int(data.get("total") or 0)
        if total:  # 旧快照可能还留着退出目标集的行（抓取开始时才裁），展示别溢出
            n = min(n, total)
        task = getattr(self, "_riven_rt_crawl_task", None)
        if (task and not task.done()) or self._crawl_lock("_riven_rt_lock").locked():
            return "running", n, total
        now = time.time()
        meta = getattr(task, "_wf_crawl_status", {})
        finished = meta.get("last_finished", now if task and task.cancelled() else 0.0)
        anchor = max(
            getattr(self, "_riven_rt_manual_at", 0.0),
            finished,
            getattr(self, "_riven_rt_finished_at", 0.0),
        )
        if anchor and now - anchor < RIVEN_RT_MANUAL_COOLDOWN:
            return "cooldown", n, total
        self._riven_rt_crawl_task = self._start_manual_crawl("riven")
        self._riven_rt_manual_at = now
        return "started", n, total

    def start_rank_crawl(self) -> tuple[bool, int, int]:
        """启动后台全量抓取；返回 (是否新启动, 已完成项, 总项数)。"""
        self._ensure_open()
        data = self._load_json_file(paths.read_path(RANKS_NAME))
        task = getattr(self, "_rank_crawl_task", None)
        if (task and not task.done()) or self._crawl_lock("_rank_crawl_lock").locked():
            return False, int(data.get("done") or 0), int(data.get("total") or 0)
        meta = getattr(task, "_wf_crawl_status", {})
        anchor = max(
            meta.get("last_attempt", 0.0),
            meta.get("last_finished", 0.0),
            getattr(self, "_rank_crawl_finished_at", 0.0),
        )
        if task and task.cancelled() and not meta.get("last_finished"):
            anchor = time.time()
        if anchor and time.time() - anchor < TTL_WM_STATS:
            raise WarframeAPIError("价格排行刷新保护中，请在本轮结束 30 分钟后再试")
        self._rank_crawl_task = self._start_manual_crawl("rank")
        return True, int(data.get("done") or 0), int(data.get("total") or 0)

    def rank_rows(self) -> list[dict]:
        """落盘的排行行（含 slug，供 formatter 按分类过滤）。"""
        data = self._load_json_file(paths.read_path(RANKS_NAME))
        return [{"slug": k, **v} for k, v in (data.get("rows") or {}).items()]

    async def de_weekly_rivens(self, platform: str = "pc", force: bool = False) -> dict:
        """DE 官方每周紫卡交易数据（周销量/热度榜的权威来源）。

        官方文件为 JS 字面量，解析后落盘缓存 6 小时；``force=True``
        跳过缓存强制拉取（「紫卡排行 刷新」）。主机平台文件缺失回落 PC，
        返回副本带 fallback_from；不会把兜底结果记成主机平台缓存。
        """
        plat = _de_riven_platform(platform)
        name = RIVEN_WEEKLY_NAME if plat == "PC" else f"_cache/riven_weekly_{plat.lower()}.json"

        def cache_ok(snap):
            if not isinstance(snap, dict) or snap.get("platform") != plat:
                return False
            fetched, entries = snap.get("fetched"), snap.get("entries")
            return (
                isinstance(fetched, (int, float))
                and not isinstance(fetched, bool)
                and 0 <= time.time() - fetched < TTL_RIVEN_WEEKLY
                and isinstance(entries, list)
                and bool(entries)
            )

        if not force:
            snap = self._load_json_file(paths.read_path(name))
            if cache_ok(snap):
                return snap
            if plat != "PC":
                # 兼容旧版本写在共用文件中的主机快照；只复制匹配的平台，不动PC文件。
                legacy = self._load_json_file(paths.read_path(RIVEN_WEEKLY_NAME))
                if cache_ok(legacy):
                    self._ensure_open()
                    self._save_json_file(paths.write_path(name), legacy)
                    return legacy
        try:
            resp = await self._http.get(DE_RIVEN_WEEKLY.format(plat=plat))
            resp.raise_for_status()
            entries = parse_de_riven_weekly(resp.text)
        except Exception as exc:
            if plat != "PC":
                fallback = await self.de_weekly_rivens("pc", force=force)
                return {**fallback, "fallback_from": plat}
            raise WarframeAPIError(f"DE 紫卡周报拉取失败：{exc}") from exc
        snap = {"fetched": time.time(), "platform": plat, "entries": entries}
        self._ensure_open()
        self._save_json_file(paths.write_path(name), snap)
        return snap

    # ------------------------------------------------------------------
    # Warframe.Market v1：拍卖（紫卡/玄骸，v2 尚未开放）
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_riven_stats(stats: Iterable[str], riven_type: str = "") -> list[str]:
        """把插件标准词条 id 转成 WM v1 拍卖数据的 url_name（含合并名）。"""
        from .parser import RIVEN_URL_COMPAT

        return list(dict.fromkeys(RIVEN_URL_COMPAT.get(s, s) for s in stats))

    @staticmethod
    def riven_buyout_summary(auctions: list[dict]) -> dict:
        """仅统计已返回挂单，不读历史榜；未知可见/关闭状态和无有效买断的条目不计。"""
        import math

        prices, online = [], []
        for auction in auctions:
            if (
                not isinstance(auction, dict)
                or auction.get("visible") is not True
                or auction.get("closed") is not False
            ):
                continue
            value = auction.get("buyout_price")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                continue
            try:
                if not math.isfinite(value):
                    continue
            except OverflowError:
                continue
            prices.append(value)
            owner = auction.get("owner")
            if isinstance(owner, dict) and owner.get("status") in ("online", "ingame"):
                online.append(value)
        prices.sort()
        sample = prices[:RIVEN_RT_TOP_PRICES]
        count = len(sample)
        median = None
        if count:
            median = sample[count // 2]
            if count % 2 == 0:
                lower, upper = sample[count // 2 - 1], sample[count // 2]
                median = lower + (upper - lower) / 2
        return {
            "low": prices[0] if prices else None,
            "online_low": min(online) if online else None,
            "median": median,
            "sample_count": count,
            "sample_limit": RIVEN_RT_TOP_PRICES,
            "returned_count": len(auctions),
            "valid_count": len(prices),
        }

    async def riven_price_reference(self, weapon_url_name: str, platform: str = "pc") -> dict:
        """单武器低价参考：一次 price_asc 查询，不触发历史榜或后台全量爬取。

        slug/platform 沿用已规范化的 WM 拍卖入参。low/online_low 取本次返回的
        有效一口价；median 只取最低20条。returned_count 是拍卖入口按id去重后的
        返回条数，均不是全市场数量。无有效样本时价格为None；请求错误原样上抛。
        fetched 是本次查询完成的Unix秒，可命中现有90秒缓存，不能作为原始挂单
        更新时间；前端宜标「查询于」，并保留一口价样本非成交价的说明。
        """
        auctions = await self.wm_riven_auctions(
            weapon_url_name, platform, sorts=("price_asc",), retries=0
        )
        return {
            **self.riven_buyout_summary(auctions),
            "weapon_url_name": weapon_url_name,
            "platform": platform,
            "fetched": time.time(),
            "cache_ttl_seconds": TTL_WM_AUCTIONS,
            "source": "warframe.market v1 拍卖查询（price_asc）",
            "scope": (
                "查询返回样本中的可见、未关闭一口价；非成交价，不代表全市场；"
                f"中位数取最低{RIVEN_RT_TOP_PRICES}条有效样本。"
            ),
        }

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
        require_negative: bool = False,
        sorts: tuple = ("price_asc", "price_desc"),
        retries: int = 2,
        allow_attribute_fallback: bool = True,
        timeout: Optional[float] = None,
    ) -> list[dict]:
        """WM v1 紫卡拍卖搜索（2026-10-01 双向排序合并 + 逗号 AND + require_negative）。

        ``sorts`` / ``retries``（2026-10-24）：紫卡实时排行爬取只要最低价一侧（``("price_asc",)``）、
        且自己在爬取层退避（``retries=0``）—— 拍卖搜索比别的 WM 接口限得严（实测约 90 s 窗口 8 次，
        见 `RIVEN_RT_GAP_S` 注释），双向 + 1.2 s 快速重试只会更快撞 429。

        去词条兼容回退仅适用于首方向带词条过滤时的明确 HTTP 400；调用方可设
        ``allow_attribute_fallback=False`` 禁用。原请求与回退均沿用 ``retries`` / ``timeout``。

        ⚠ 服务端行为**三坑**（2026-10-01 实测，weapon_url_name=ocucor/burston）：
          1. **词条参数必须拼逗号**：`positive_stats=a,b,c` = **AND**（服务端就筛好）；
             httpx 把 list 序列化成**重复参数** `a&b&c` = **OR**（含任一命中即返回）
             → 500 条截断 → 在线好单被挤掉。`negative_stats` 同理。
             （2026-09-11 注释「positive_stats=a&positive_stats=b」已过时——那是 OR。）
          2. 该端点固定最多返回 **500 条**，且 `page` / `price_min` / `buyout_price_*` /
             `status` 全被忽略 ⇒ `price_max`/`price_min` 传了也白传，
             真正过滤靠 `_auction_match` 本地判断。
          3. **唯一有效的是 `sort_by` 的方向**（穷举：`time_asc`/`time_desc`/`re_rolls_asc`
             均 400 Bad Request）。
          ⇒ `price_asc` + `price_desc` 各查一次 → 按挂单 id 合并去重
          （双查合并保留为「结果集 > 500」时的兜底；逗号 AND 后结果集通常很小）。
          `_fetch_json` 的 TTL 缓存以 params 为 key，两方向互不覆盖（TTL 90s）。
          限速 3 req/s（`wm_rate_limit=True` 自己排队）。
          `price_desc` 失败**降级为单方向结果**（不抛异常打挂指令）。
          `require_negative=True` → 拼 `negative_stats=has`（服务端筛出「带负词条」的挂单）。
        """
        base_params: dict[str, Any] = {
            "type": "riven",
            "weapon_url_name": weapon_url_name,
            "platform": platform,
            # ★ 2026-10-08（用户口径）：「也打开」——不再只收一口价直购，
            #   竞价型（只有起拍价、无 buyout）挂单也列出来；卡片上这类价格前标「起」
            #   以示区别（见 fmt_wr_auctions）。
        }
        pos = list(dict.fromkeys(positives))
        neg = list(dict.fromkeys(negatives))
        if pos:
            # ★ 逗号 = 服务端 AND（实测 46 条精确 / 重复参数 = OR 触顶 500）；
            #   本地 pos_set ⊆ urls 二次筛保留（防御 + 兼容服务端行为变化）
            base_params["positive_stats"] = ",".join(pos)
        if neg:
            base_params["negative_stats"] = ",".join(neg)
        if require_negative:
            # 「任意负」：不指定具体词条，只要求带 ≥1 条负面
            # （与 negative_stats=具体词条互斥——服务端 only accepts one form）
            if "negative_stats" not in base_params:
                base_params["negative_stats"] = "has"
        if polarity:
            base_params["polarity"] = polarity
        if max_price is not None:
            base_params["price_max"] = max_price
        if min_price is not None:
            base_params["price_min"] = min_price
        if max_rerolls is not None:
            base_params["re_rolls_max"] = max_rerolls
        if min_rerolls is not None:
            base_params["re_rolls_min"] = min_rerolls
        if rank_range:
            base_params["mastery_rank_min"], base_params["mastery_rank_max"] = rank_range

        merged: dict[str, dict] = {}
        for sort in sorts:
            params = dict(base_params)
            params["sort_by"] = sort
            data = None
            try:
                data = await self._fetch_json(
                    f"{self.wm_base}/auctions/search",
                    ttl=TTL_WM_AUCTIONS,
                    params=params,
                    wm_rate_limit=True,
                    retries=retries,
                    timeout=timeout,
                    validator=self._validate_wm_auctions,
                )
                self._validate_wm_auctions(data)
            except _WMV1ContentError:
                if sort == "price_asc":
                    raise
                # 第二方向结构异常仍可使用第一方向；去词条不能修复坏信封。
                continue
            except WarframeAPIError as exc:
                if sort != "price_asc":
                    # 次方向失败保留升序结果，不额外发起去词条查询。
                    continue
                cause = exc.__cause__
                if not (
                    allow_attribute_fallback
                    and ("positive_stats" in params or "negative_stats" in params)
                    and isinstance(cause, httpx.HTTPStatusError)
                    and cause.response is not None
                    and cause.response.status_code == 400
                ):
                    raise
                # 明确400且有词条过滤：兼容服务端词条拒绝，交给本地二次筛。
                params.pop("positive_stats", None)
                params.pop("negative_stats", None)
                try:
                    data = await self._fetch_json(
                        f"{self.wm_base}/auctions/search",
                        ttl=TTL_WM_AUCTIONS,
                        params=params,
                        wm_rate_limit=True,
                        retries=retries,
                        timeout=timeout,
                        validator=self._validate_wm_auctions,
                    )
                    self._validate_wm_auctions(data)
                except WarframeAPIError:
                    if sort == "price_asc":
                        raise
                    continue
            for a in (data or {}).get("payload", {}).get("auctions", []) or []:
                aid = a.get("id")
                if aid and aid not in merged:
                    merged[aid] = a
        # 合并后顺序无所谓：渲染层 fmt_wr_auctions 按「在线优先 → 价格升序」重排
        return list(merged.values())

    async def wm_lich_auctions(
        self,
        weapon_url_name: str,
        platform: str = "pc",
        *,
        lich_type: str = "lich",
        element: Optional[str] = None,
        damage_max: Optional[int] = None,
    ) -> list[dict]:
        """玄骸拍卖挂单。

        ★ 2026-09-18 容错（玩家反馈「信条铁晶磁轨炮搜不出来」时暴露）：
        WM **不是每把玄骸武器都有拍卖类目** —— 实测部分近战 Tenet 武器与
        全部 Coda 武器都会返回 **400 Bad Request**（不是网络故障，是「没有
        这个类目」）。原来会把 400 当异常抛到上层变成「内部错误」，用户只看到
        「出错了」。现在：400 → 返回空列表并记入 ``_lich_unsupported``，
        由调用方给出「WM 暂无该武器挂单类目」的准确提示；429 单独提示限速。
        """
        params: dict[str, Any] = {
            "type": lich_type,
            "weapon_url_name": weapon_url_name,
            "platform": platform,
            "sort_by": "price_asc",
        }
        if element:
            params["element"] = element
        if damage_max:
            params["having_damage_max"] = damage_max
        try:
            data = await self._fetch_json(
                f"{self.wm_base}/auctions/search",
                ttl=TTL_WM_AUCTIONS,
                params=params,
                wm_rate_limit=True,
                validator=self._validate_wm_auctions,
            )
            self._validate_wm_auctions(data)
        except WarframeAPIError as exc:
            msg = str(exc)
            if "400" in msg:
                self._lich_unsupported.add(weapon_url_name)
                logger.info(
                    "[sdjk] WM 无该玄骸武器的挂单类目：%s（type=%s）", weapon_url_name, lich_type
                )
                return []
            if "429" in msg or "限速" in msg:
                # WM 侧 `_fetch_json` 已直接抛可执行提示（见 WM_RATE_LIMIT_HINT）；这里统一口径，
                # 免得玄骸这条路再吐「HTTP 429」这种用户看不懂的措辞。
                raise WarframeAPIError(self.WM_RATE_LIMIT_HINT) from exc
            raise
        return (data or {}).get("payload", {}).get("auctions", [])

    def lich_unsupported(self, weapon_url_name: str) -> bool:
        """该武器是否已被确认「WM 没有挂单类目」（避免重复请求同一把）。"""
        return weapon_url_name in self._lich_unsupported

    # ------------------------------------------------------------------
    # 名称解析（CN 别名 -> WM url_name / wiki 页面）
    # ------------------------------------------------------------------
    def alias_lookup(
        self, query: str, table: str = "wm_items", *, exact: bool = False
    ) -> Optional[str]:
        """本地别名词典精确/模糊匹配。返回 WM url_name 或 None。

        ``exact=True`` 时**只认精确键**、不做双向包含 —— 「官方名优先」的解析链
        必须用它：2026-10-01 玩家报障，riven_items 的包含匹配（长度差 ≤4）让
        官方名「盗贼」(Furis 1.35) 命中别名键「盗贼双枪」(Afuris 1.45)，卡面
        明明写着盗贼、倾向却按 1.45 算。同源纪律见 ``_alias_fuzzy`` 的降级说明。
        """
        q = query.strip().lower()
        table = self._aliases.get(table, {})
        hit = table.get(q)
        if isinstance(hit, str) and hit:
            return hit
        if exact:
            return None
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
            if not q or not (k in q or q in k) or abs(len(k) - len(q)) > 4:
                continue
            # ★ 2026-09-24：**单字键**（电/猫/沙/鸟 等战甲单字黑话）只认
            #   「本字 / 本字+p / 本字+prime」三种形态 —— 双向包含会让
            #   「电击伤害」「冰霜」这类正常查询被劫持到 Volt/Frost 上。
            if len(k) == 1 and q not in (k, k + "p", k + "prime", k + " p", k + " prime"):
                continue
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

    async def _prime_upgrade(self, base: str) -> Optional[dict]:
        """「基名 + Prime 后缀」→ 该基名的 Prime 条目；找不到返回 **None**。

        三级后备（2026-10-05 立，修「`wm 手枪精通p` 落普通版」——Prime 意图前置用）：
          ① 基名解析结果**已是 Prime** → 直接返回（武器/战甲多数如此：普通版不可交易、
             表里根本没有普通条目，`格拉姆 → gram_prime_set`）
          ② `primed_ + <基名条目 url_name>`（MOD 的固定形态：
             `pistol_gambit → primed_pistol_gambit`）
          ③ `matching.prime_sibling()`（普通版可交易时，Prime 是 `_prime_set` 兄弟）
          ④ 兜底 `resolve(base + " prime")`（官方名精确形态）

        ★ 与 `_resolve_prime_variant` 的区别：**不回落非 Prime 本体** —— 四级全不中
          必须返回 None，让调用方继续原链（保护「表里没有 Prime 版的基名」，
          如 `塞多p`：普通版可交易且无 Prime ⇒ 继续原链落 cedo_set）。
        """
        items = await self.wm_items()
        item = await self.resolve_wm_item(base)
        if item:
            url = item.get("url_name") or ""
            if "prime" in url:
                return item
            primed = next((x for x in items if x.get("url_name") == "primed_" + url), None)
            if primed:
                return primed
            sib = matching.prime_sibling(item, items)
            if sib:
                return sib
        # ★ 兜底只认**精确**（`resolve_wm_exact`）——用完整模糊链会漏配
        #   （实测「圣剑p」：`resolve("圣剑 prime")` 经 fuzzy_hits 命中「剑风 Prime」
        #   ⇒ primed_reach；而「圣剑」在 WM 里根本不存在，应落未找到）。
        return await self.resolve_wm_exact(base + " prime")

    async def resolve_wm_exact(self, query: str) -> Optional[dict]:
        """**仅精确**解析：官方名精确 / 别名词典精确键，绝不做模糊。

        供「头」部件黑话消歧用（2026-10-02）：模糊链路会把「白霜弹»沾到
        白霜（Frost）Prime 上，导致剥头误判 —— 剥头与否的判据必须只认精确。
        """
        query = (query or "").strip()
        if not query:
            return None
        items = await self.wm_items()
        hit = match_official_name(query, items)
        if hit:
            return hit
        url = ((getattr(self, "_aliases", None) or {}).get("wm_items") or {}).get(query.lower())
        if isinstance(url, str) and url:
            for it in items:
                if it.get("url_name") == url:
                    return it
        return None

    def _wm_families(self, items: list[dict]) -> frozenset:
        """WM 物品官方简中名里的族名集合（按物品表对象缓存）。"""
        cache = getattr(self, "_wm_family_cache", None)
        if cache and cache[0] is items:
            return cache[1]
        fams = matching.family_prefixes(it.get("zh") or "" for it in items)
        self._wm_family_cache = (items, fams)
        return fams

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
        # 每个 asyncio 任务有独立调用栈；并发请求（含子任务）不能互相占位。
        key = (asyncio.current_task(), query)
        if key in seen:
            return None
        seen.add(key)
        try:
            return await self._resolve_wm_item_once(query)
        finally:
            seen.discard(key)

    async def _resolve_wm_item_once(self, query: str) -> Optional[dict]:
        """单次解析（不含重入守卫）；递归防护由 resolve_wm_item 负责。"""
        items = await self.wm_items()
        hit = match_official_name(query, items)
        if hit:
            return hit
        # 族名后置写法（「收割赋能」→ 官方「赋能·收割」；中文 wiki 常这样写，2026-11-10）：只认精确命中
        for cand in matching.family_suffix_candidates(query, self._wm_families(items)):
            hit = match_official_name(cand, items)
            if hit:
                return hit
        # 别名词典**精确**键（黑话）；模糊降级到后面的链路，避免劫持官方名
        # ★ getattr 兜底：部分测试用 __new__ 造实例，_aliases 可能还没初始化，
        #   这里不能因为「查不到词典键」就把整条解析链炸掉。
        url = ((getattr(self, "_aliases", None) or {}).get("wm_items") or {}).get(query.lower())
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
        zh_contains = [it for it in items if it.get("zh") and zh_low in it["zh"].lower()]
        if zh_contains:
            # 包含命中多个时：整套 > 本体（非部件/蓝图）> 部件/蓝图
            return min(zh_contains, key=_score)
        url_contains = [it for it in items if low.replace(" ", "_") in it.get("url_name", "")]
        if url_contains:
            return min(url_contains, key=_score)
        # 归一化匹配：用户输入「Saryn蓝图」这类省略 Prime/空格的部件名
        # （zh「Saryn Prime 蓝图」归一化后正好等于输入）—— 2026-09-14 加
        norm_hit = match_wm_normalized(query, items)
        if norm_hit:
            return norm_hit
        # ★ 2026-10-05：Prime 意图前置（修「手枪精通p → 普通版」玩家报障）——
        #   必须插在 alias 模糊 / difflib / fuzzy_hits **之前**：
        #   ① fuzzy_hits 的中文错别字兜底会把「手枪精通p」当「手枪精通」的错别字
        #      提前 return（池化 |len差|≤2 通过、ratio 8/9=0.889 ≥ 阈值 0.70）；
        #   ② alias 模糊在**带 p 的整串**上会误配（实测「分裂斩斧p」→ split_chamber、
        #      「野马双枪p」→ bronco_prime_set，真值是 scindo/akbronco_prime_set）——
        #      而「基名 + Prime 意图」能确定性给出正确答案。
        #   命中即返回；**全不中必须继续原链**（保护 rifle amp / cold snap
        #   这类「以 p 结尾但非 Prime」的英文名，以及表里没有 Prime 版的基名）。
        _pb = matching.prime_base(query)
        if _pb:
            _up = await self._prime_upgrade(_pb)
            if _up:
                return _up
        # ★ 别名词典模糊**降级到这儿**（2026-09-20）：这条是「双向包含 + 长度差≤4」，
        #   以前排在最前，会把「压迫点 p」里的「压迫点」捞出来配到 serration（膛线）。
        #   现在只有官方名/精确键/包含/归一化都没中时，才允许它兜底。
        alias = self._alias_fuzzy(query)
        if alias:
            # ★ 输入带 p / prime 后缀时，优先返回该物品的 Prime 变体 ——
            #   让「压迫点p」（连写）与「压迫点 p」（带空格）结果一致（2026-09-20）。
            #   找不到 Prime 变体就照常返回原物品（如 sawtooth clip 以 p 结尾也不受影响）。
            # ★ 2026-10-05：口径并入 `matching.prime_base`（含 p版/P版 与混排
            #   「驱逐 Grineerp」）—— 旧正则不认 p版，别名分支会提前返回普通版
            #   （实测「压迫点p版 → pressure_point」）。
            want_prime = (
                bool(re.search(r"(?:^|[^a-z])p(?:版)?$|prime$", query.strip().lower()))
                or matching.prime_base(query) is not None
            )
            # ★ primed 是**物品 dict**，比较时要取它的 url_name（不是拿 dict 去比）
            target = alias
            # ★ 2026-10-05：别名命中的目标**已经是 Prime** 时直接用 —— 旧逻辑还要去
            #   找 `primed_` 前缀 / Prime 兄弟，会把**已正确的答案「升级」成错误答案**
            #   （实测：`wm Garap` → 别名键 `gara` → `gara_prime_set`，被 prime_sibling
            #   带偏成 `akjagara_prime_set`）。这是 2026-09-24「鹦鹉螺p」修复的同族遗漏。
            if want_prime and "prime" not in alias:
                primed = next((x for x in items if x.get("url_name") == "primed_" + alias), None)
                if primed:
                    target = primed.get("url_name")
                else:
                    # ★ 2026-09-24：`primed_` 是 MOD 的形态；普通版也能交易时
                    #   （哨兵/武器成套上架），Prime 版是 `_prime_set` 这类**兄弟
                    #   条目** —— 旧实现只找 primed_，于是「wm 鹦鹉螺p」落回基础版
                    #   nautilus_set（玩家报障）。改为按名称找 Prime 兄弟。
                    base_it = next((x for x in items if x.get("url_name") == alias), None)
                    sib = matching.prime_sibling(base_it, items) if base_it else None
                    if sib:
                        target = sib.get("url_name")
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
                cands = [
                    it
                    for it in items
                    if it.get("url_name", "").startswith(prefix)
                    and it.get("url_name") != base_item.get("url_name")
                    and (en_word in it.get("url_name", "") or cn_word in norm_wm_name(it.get("zh")))
                ]
                if cands:
                    return min(cands, key=_score)
        close = difflib.get_close_matches(
            low.replace(" ", "_"), [it.get("url_name", "") for it in items], n=1, cutoff=0.75
        )
        if close:
            return next(it for it in items if it.get("url_name") == close[0])
        close_en = difflib.get_close_matches(
            low, [(it.get("en") or "").lower() for it in items], n=1, cutoff=0.8
        )
        if close_en:
            return next(it for it in items if (it.get("en") or "").lower() == close_en[0])
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

    def resolve_lich_weapon(self, query: str, *, fuzzy: bool = True) -> Optional[str]:
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
        if not fuzzy:
            return None
        # 形近字兜底（「赤毒弧电离子枪」→「赤毒·弧电离子枪」）
        near = fuzzy_hits(
            norm, [re.sub(r"[\s·・]+", "", k).lower() for k in table], n=1, cutoff=0.75
        )
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
                (DATA_DIR / "lich_weapons.json").read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            self._lich_cache = {}
        return self._lich_cache

    async def wm_riven_weapon_slugs(self) -> set[str]:
        """WM **自家**紫卡武器 slug 集（原始端点，即拍卖端点接受的取值）。

        ★ 与 `wm_riven_weapons()` 的区别：后者合并了本地
        `dispositions_rivenmirror.json` 的 670 条变体补全（按 url_name 去重后
        实际并入 250 条，合并表 670 条；rubico_prime 等在其中），
        那些 slug **拍卖端点不认**（2026-10-05 实测：`weapon_url_name=rubico_prime`
        → **HTTP 400**，`rubico` → 200）。市场路径（`wr`）必须先过这一关。
        """
        raw = await self._wm_v2("/riven/weapons", ttl=TTL_WM_ITEMS) or []
        return {w.get("slug", "") for w in raw if w.get("slug")}

    async def resolve_riven_weapon(self, query: str) -> Optional[dict]:
        """紫卡武器解析：本地别名 + WM v2 紫卡武器表（zh/en），支持 黑话+p。

        2026-09-23 变体解析并入（core/matching）：归一化完全与变体等价两层
        插在「精确」与「子串」之间（赤毒沙皇=赤毒 沙皇、kuva沙皇、沙皇赤毒）。
        ★ p/P 后缀**优先返回变体条目**（紫卡分析按变体算倾向；表中确无该变体
        条目时返回 None，绝不冒充本体值）。
        ★ 2026-10-05 实测口径（勿再踩）：**运行期表 = WM v2 原始端点（420 条）
        + 本地 `dispositions_rivenmirror.json` 670 条补全按 url_name 去重后并入
        （实际新增 250 条）⇒ 合并表 **670 条**（容器内 `wm_riven_weapons()` 实测）**——
        所以 `rubico_prime` **在表里**（拿原始端点验会得出「只挂母武器」的假象）。
        市场路径（`wr`）另有「剥到母武器」口径，见 `market.py::_h_wr`。
        """
        query = query.strip()
        if not query:
            return None
        weapons = await self.wm_riven_weapons()
        # 官方全名精确命中先于「p = Prime」简写：英文名本身以 p 结尾的武器
        # （如 Arbucep）此前会被拆成「Arbuce + p」再找 Prime 变体 ⇒ 查不到
        _low0 = query.lower().replace(" ", "_")
        for w in weapons:
            if w.get("zh") == query or (w.get("en") or "").lower().replace(" ", "_") == _low0:
                return w
        m = self._PRIME_SUFFIX.match(query)
        if m and len(m.group(1).strip()) >= 1:
            base = await self.resolve_riven_weapon(m.group(1).strip())
            if base:
                # 2026-09-23 去掉「回落 base」：倾向/紫卡按变体分别计算，
                # 表中确无该变体条目时返回 None（未找到），绝不冒充本体值。
                # ★ 市场路径（wr）按用户口径另行「剥到母武器」，见 market.py::_h_wr。
                # 母名本身已解析到 Prime 条目（只有 Prime 版的武器，如「悦音」→ 悦音 Prime）
                # ⇒ 「悦音p」就是它，不再去找「Prime 的 Prime」
                if matching.normalize(base.get("en") or "").endswith("prime"):
                    return base
                return matching.prime_sibling(base, weapons)
        # ★ 2026-10-01：**官方名精确匹配先于别名词典**。别名表的「双向包含」
        #   会让官方名被别名键捞走：实测「盗贼」(Furis, 1.35) 命中别名键
        #   「盗贼双枪」(Afuris, 1.45)，卡面写着盗贼、倾向却按 1.45 算，
        #   于是四条数值全落 0% 并误报「倾向调整前洗出的老卡」（玩家报障）。
        #   别名的**模糊**匹配统一降级到链尾（与 _alias_fuzzy 同一条纪律）。
        low = query.lower().replace(" ", "_")
        for w in weapons:
            if w.get("zh") == query or (w.get("en") or "").lower() == low:
                return w
        # 别名**精确键**（黑话/无中点写法，如「棱晶空刃」→ prisma_skana）
        alias = self.alias_lookup(query.lower(), "riven_items", exact=True)
        if alias:
            for w in weapons:
                if w.get("url_name") == alias:
                    return w
        # 归一化完全（去空格/分隔符/大小写；不抹 prime）
        nq = matching.normalize(query)
        if nq:
            for w in weapons:
                if nq in (
                    matching.normalize(w.get("zh") or ""),
                    matching.normalize(w.get("en") or ""),
                ):
                    return w
            # 变体等价（p→prime / 语序互换 / zh↔en token）——只做等价变形，
            # 绝不剥 token 配 base（倾向按变体分算）
            forms = set(matching.expand_variants(query)) - {nq}
            if forms:
                for w in weapons:
                    if (
                        matching.normalize(w.get("zh") or "") in forms
                        or matching.normalize(w.get("en") or "") in forms
                    ):
                        return w
        for w in weapons:
            if low in w.get("url_name", "") or query in (w.get("zh") or ""):
                return w
        # 归一化子串（**唯一命中才采纳**）：玩家常省略官方名里的间隔号 / 后缀，
        #   如「赤毒努寇」「信条弧电」——上一步按原文比，「赤毒努寇」不是
        #   「赤毒·努寇微波枪」的子串（中间隔着「·」）。多个命中时不猜，交给后面的模糊兜底。
        if nq and len(nq) >= 2:
            sub = [
                w
                for w in weapons
                if nq in matching.normalize(w.get("zh") or "")
                or nq in matching.normalize(w.get("en") or "")
            ]
            if len({w.get("url_name") for w in sub}) == 1:
                return sub[0]
        close = difflib.get_close_matches(
            low, [w.get("url_name", "") for w in weapons], n=1, cutoff=0.7
        )
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
        # ★ 2026-09-24 兜底：卡面识别会把「武器名 + 紫卡自命名」连写或音译
        #   进武器名（「翁 Acri-paracron」→「翁洛奇亚鲁姆」、「初始 翁」），
        #   精确/归一化/形近都捞不回来。最后一跳：在**归一化输入**里找最长的
        #   已知武器名（中/英都试），命中即用并标 _fuzzy_from（卡面注明来源）。
        nq_in = matching.normalize(query)
        if nq_in:
            best: tuple[int, dict] | None = None
            for w in weapons:
                for cand in (w.get("zh") or "", w.get("en") or ""):
                    nc = matching.normalize(cand)
                    if nc and nc in nq_in and (best is None or len(nc) > best[0]):
                        best = (len(nc), w)
            if best:
                hit = dict(best[1])
                hit["_fuzzy_from"] = query
                return hit
        # 链尾最后一跳：别名词典的**模糊**匹配（双向包含 + 长度差 ≤4）——
        # 只做兜底，绝不抢在官方名前面（2026-10-01 盗贼/盗贼双枪事故）。
        falias = self._alias_fuzzy(query, "riven_items")
        if falias:
            for w in weapons:
                if w.get("url_name") == falias:
                    return w
        return None

    # ★ 2026-10-23：WM 现行 slug 无括号（`pistol_riven_mod_veiled`）；旧写法 `…_(veiled)` 在 v1 返回 301，
    #   只靠 httpx 跟随跳转才能用，跳转一撤「紫卡排行 未开」就整张表空掉。8 个已逐个实测 200。
    _VEILED_SLUGS = {
        "Melee": "melee_riven_mod_veiled",
        "Rifle": "rifle_riven_mod_veiled",
        "Shotgun": "shotgun_riven_mod_veiled",
        "Pistol": "pistol_riven_mod_veiled",
        "Archgun": "archgun_riven_mod_veiled",
        "Kitgun": "kitgun_riven_mod_veiled",
        "Zaw": "zaw_riven_mod_veiled",
        "Robotic": "companion_weapon_riven_mod_veiled",
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

    # ★ FlareSolverr 调参（2026-09-25 事故：60s 偏紧 + 坏会话不自愈）
    _FLARE_SESSION = "sdjk"
    _FLARE_MAX_TIMEOUT = 120000  # CF 解页实测 ~61s，60000 会整轮卡在边缘
    # 仅会话/浏览器故障才恢复；CF 求解超时本身不是坏会话，不靠重建反复撞源。
    _FLARE_REBUILD_HINTS = ("session", "tab crashed", "chrome")

    @staticmethod
    def _wiki_source_text(raw: str) -> str:
        import html

        if not isinstance(raw, str) or not raw.strip() or len(raw.encode("utf-8")) > 2_000_000:
            raise WarframeAPIError("wiki 源正文为空或超过大小上限")
        # JSON/普通 wikitext 中也可能有 <pre>；只解包浏览器返回的外层 HTML。
        wrapper = re.match(r"\s*(?:<!doctype\s+html|<html(?:\s|>)|<pre(?:\s|>))", raw, re.I)
        match = re.search(r"<pre[^>]*>(.*?)</pre>", raw, flags=re.S | re.I) if wrapper else None
        text = html.unescape(match.group(1)) if match else raw
        if not text.strip() or re.match(r"\s*(?:<!doctype\s+html|<html(?:\s|>))", text, re.I):
            raise WarframeAPIError("wiki 返回 HTML/挑战页，未取得源正文")
        return text

    @staticmethod
    def _wiki_request_url(url: str) -> str:
        """MediaWiki API 主动声明 maxlag；raw 页面不附加无关参数。"""
        target = urlsplit(url)
        if not target.path.endswith("/api.php"):
            return url
        params = parse_qsl(target.query, keep_blank_values=True)
        maxlag = [(key, value) for key, value in params if key == "maxlag"]
        if maxlag == [("maxlag", "5")]:
            return url
        params = [(key, value) for key, value in params if key != "maxlag"]
        # 原 URL 没有 maxlag 时只追加，保留上游已有参数的编码和次序。
        query = (
            target.query + ("&" if target.query else "") + "maxlag=5"
            if not maxlag
            else urlencode(params + [("maxlag", "5")])
        )
        return urlunsplit(target._replace(query=query))

    @staticmethod
    def _wiki_host(url: str) -> str:
        return (urlsplit(url).hostname or "").lower().rstrip(".")

    def _wiki_defer(self, url: str, retry_after="") -> None:
        """共享单调时钟退避；短响应不能覆盖更长的等待。"""
        delay = wm_retry_after_seconds(retry_after, fallback=60.0)
        cooldown = self.__dict__.setdefault("_wiki_retry_until", {})
        host = self._wiki_host(url)
        cooldown[host] = max(cooldown.get(host, 0.0), time.monotonic() + max(1.0, delay))

    async def _wiki_wait(self, url: str) -> None:
        """按主机间隔启动 HTTP/FS 请求；缓存、会话管理不消耗源站名额。"""
        host = self._wiki_host(url)
        locks = self.__dict__.setdefault("_wiki_host_locks", {})
        lock = locks.setdefault(host, asyncio.Lock())
        last = self.__dict__.setdefault("_wiki_last_request", {})
        cooldown = self.__dict__.setdefault("_wiki_retry_until", {})
        async with lock:
            while True:
                self._ensure_open()
                now = time.monotonic()
                if cooldown.get(host, 0.0) > now:
                    raise _WikiRetryLater("wiki 上游处于 Retry-After 退避窗口")
                wait = last.get(host, -math.inf) + self.WIKI_MIN_INTERVAL_S - now
                if wait <= 0:
                    last[host] = now
                    return
                await asyncio.sleep(wait)

    def _wiki_check_response(
        self, url: str, status, headers=None, body: str = "", *, allow_challenge: bool = False
    ) -> None:
        """直连及 FS 源站响应使用相同的 HTTP/API 退避规则。"""
        try:
            code = int(status)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _WikiRequestRejected("wiki 未提供有效的源站 HTTP 状态") from exc
        if isinstance(status, bool) or not 100 <= code <= 599:
            raise _WikiRequestRejected("wiki 未提供有效的源站 HTTP 状态")
        try:
            headers = httpx.Headers(headers or {})
        except (TypeError, ValueError, AttributeError) as exc:
            raise _WikiRequestRejected("wiki 源站响应头格式无效") from exc
        retry_after = headers.get("Retry-After", "")
        if code == 429 or code >= 500 or (code >= 400 and retry_after):
            self._wiki_defer(url, retry_after)
            raise _WikiRetryLater(f"wiki HTTP {code}：本轮停止获取，稍后重试")
        if not 200 <= code < 300 and not (allow_challenge and code == 403):
            raise _WikiRequestRejected(f"wiki HTTP {code}：本轮停止获取")
        if headers.get("cf-mitigated", "").lower() == "challenge":
            if allow_challenge:
                raise WarframeAPIError("wiki HTTP 返回 Cloudflare challenge")
            raise _WikiRequestRejected("wiki FS 返回 Cloudflare challenge，未取得源正文")
        if urlsplit(url).path.endswith("/api.php") and body:
            try:
                payload = json.loads(self._wiki_source_text(body))
            except (ValueError, WarframeAPIError):
                return  # 正文/业务校验由调用方执行，保留原接力行为。
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict) and error.get("code") in {"maxlag", "ratelimited"}:
                self._wiki_defer(url, retry_after)
                raise _WikiRetryLater(f"wiki API {error['code']}：上游要求稍后重试")

    async def fetch_wiki_source(
        self, url: str, ttl: int = TTL_FLARE, *, validator: Optional[Callable[[str], None]] = None
    ) -> str:
        """普通 HTTP 优先，失败且开关允许时才用 FS；两路都通过正文校验才缓存。"""
        self._ensure_open()
        url = self._wiki_request_url(url)
        cache = self.__dict__.setdefault("_wiki_source_cache", {})

        def validate(raw):
            text = self._wiki_source_text(raw)
            if "/api.php?" in url:
                payload = self._wiki_json(text)
                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, dict) and error.get("code") in {"maxlag", "ratelimited"}:
                    raise _WikiRetryLater(f"wiki API {error['code']}：上游要求稍后重试")
                self._wiki_revision_pages(text)
            if validator:
                validator(text)
            return text

        hit = cache.get(url)
        if hit and time.time() - hit[0] < ttl:
            try:
                return validate(hit[1])
            except Exception:  # noqa: BLE001 - 本地缓存也必须服从当前业务校验
                cache.pop(url, None)
        await self._wiki_wait(url)
        retry_after = ""
        try:
            response = await self._http.get(
                url,
                timeout=10.0,
                headers={"Accept": "application/json, text/plain;q=0.9"},
                follow_redirects=False,
            )
            self._ensure_open()
            retry_after = response.headers.get("Retry-After", "")
            self._wiki_check_response(
                url, response.status_code, response.headers, response.text, allow_challenge=True
            )
            response.raise_for_status()
            text = validate(response.text)
        except _WikiRetryLater:
            self._wiki_defer(url, retry_after)
            raise
        except _WikiRequestRejected:
            raise
        except Exception as exc:  # noqa: BLE001 - 连接/正文失败才允许 FS 接力
            self._ensure_open()
            if not self.flare_enabled:
                raise WarframeAPIError(f"wiki HTTP 不可用且 FlareSolverr 已关闭：{exc}") from exc
            logger.debug("[sdjk] wiki HTTP 未取得有效源正文，尝试 FlareSolverr：%s", exc)
            try:
                text = validate(await self.fetch_via_flaresolver(url, ttl=0))
            except _WikiRetryLater:
                self._flare_cache.pop(url, None)
                self._wiki_defer(url)
                raise
            except Exception:
                # FS 自身有历史缓存；非法正文不能让后续请求反复命中。
                self._flare_cache.pop(url, None)
                raise
        self._ensure_open()
        cache[url] = (time.time(), text)
        return text

    def _validate_rotation_source(self, key: str, source: str) -> None:
        """传输层接力前先拒绝残缺正文；时效/数值变化仍由采用层判断。"""
        if key == "acrithis":
            parsed = self.parse_acrichis_current(source)
            snap = self._acrichis_snapshot()
            catalog = snap.get("_en_catalog") or {}
            items = parsed.get("items", [])
            if (
                len(set(items)) != 5
                or not set(items) <= set(catalog)
                or _parse_acrithis_observed(parsed.get("observed", "")) is None
            ):
                raise WarframeAPIError("言录使源正文货单/日期不完整或物品未知")
            return
        # 源正文名单以静态骨架为准，损坏/缺行的运行缓存不能缩小校验集合。
        rot = paths.load_json(paths.PKG_DATA / "rotations.json")
        data = rot.get(key) or {}
        groups = [data.get("items", [])] if key == "tenet" else data.get("batches", [])
        if not groups or any(not items for items in groups):
            raise WarframeAPIError("缺少本地效价武器名单，不能校验源页")
        parsed = self.parse_wikitext(source)
        for index, items in enumerate(groups):
            rows = parsed["tenet"] if key == "tenet" else parsed["coda"].get(index + 1, [])
            self._valence_table(rows, {item["en"] for item in items})

    async def fetch_via_flaresolver(
        self, url: str, ttl: int = TTL_FLARE, *, cache_result: bool = True, probe_only: bool = False
    ) -> str:
        """经 FlareSolverr 抓取被 Cloudflare 拦的页面，返回渲染后的 HTML。

        未配置 FlareSolverr（连接失败）时抛 WarframeAPIError，由调用方降级。
        结果按 URL 做 TTL 内存缓存（FlareSolverr 一次求解要数秒，别反复打）。
        """
        import time as _t

        url = self._wiki_request_url(url)
        hit = self._flare_cache.get(url)
        if cache_result and hit and _t.time() - hit[0] < ttl:
            return hit[1]

        async def _post(base: str, payload: dict) -> dict:
            if probe_only:
                self._ensure_open()
            r = await self._http.post(base, json=payload, timeout=120.0, follow_redirects=False)
            if payload.get("cmd") == "request.get":
                self._wiki_check_response(url, r.status_code, getattr(r, "headers", {}))
            r.raise_for_status()
            return r.json()

        async def _recreate_session(base: str) -> None:
            # ★ 会话重建必须 destroy→create：对已存在的同名会话，
            # sessions.create 不会换掉崩掉的 Chromium（2026-09-25 事故：
            # tab crashed 后插件每小时拿坏会话重试、连败一整天）。
            # destroy 对不存在的会话会报错，属正常，忽略。
            for cmd in ("sessions.destroy", "sessions.create"):
                try:
                    await _post(base, {"cmd": cmd, "session": self._FLARE_SESSION})
                except Exception:  # noqa: BLE001 - 单步失败不阻断重建
                    continue

        async def _solve(base: str) -> dict:
            payload = {
                "cmd": "request.get",
                "url": url,
                "session": self._FLARE_SESSION,
                "maxTimeout": self._FLARE_MAX_TIMEOUT,
            }
            for attempt in range(1 if probe_only else 2):
                await self._wiki_wait(url)
                try:
                    data = await _post(base, payload)
                except (_WikiRetryLater, _WikiRequestRejected):
                    raise
                except Exception:  # noqa: BLE001 - 连接/非 JSON 失败至多恢复一次
                    if probe_only or attempt:
                        raise
                    await _recreate_session(base)
                    continue
                # 顶层失败也可能携带源站响应，先服从限流，不能先重建再撞源站。
                if not isinstance(data, dict):
                    raise _WikiRequestRejected("FlareSolverr 响应结构无效")
                solution = data.get("solution")
                if solution is not None and not isinstance(solution, dict):
                    raise _WikiRequestRejected("FlareSolverr 源站响应结构无效")
                if solution is not None:
                    self._wiki_check_response(
                        url,
                        solution.get("status"),
                        solution.get("headers"),
                        solution.get("response") or "",
                    )
                    if (
                        not isinstance(solution.get("response"), str)
                        or not solution["response"].strip()
                    ):
                        raise _WikiRequestRejected("FlareSolverr 源正文为空或格式无效")
                if data.get("status") == "ok":
                    if solution is None:
                        raise _WikiRequestRejected("FlareSolverr 缺少源站响应")
                    return data
                msg = str(data.get("message") or "")
                if (
                    probe_only
                    or attempt
                    or not any(h in msg.lower() for h in self._FLARE_REBUILD_HINTS)
                ):
                    break
                await _recreate_session(base)
            return data

        last_err: Exception | None = None
        err_msg = ""
        data: dict = {}
        if not self._flare_enabled:
            # 面板里关掉了：直接失败，让上层走它自己的降级提示（不静默空过）
            raise WarframeAPIError("FlareSolverr 已在配置里关闭（「启用 CF 绕过代理」= 否）")
        # 记住上次可用的地址，优先复用；全挂才报错
        candidates = ([self._flare_url] if self._flare_url else []) + [
            u for u in self._flare_urls if u != self._flare_url
        ]
        if probe_only:
            candidates = candidates[:1]
        for base in candidates:
            try:
                data = await _solve(base)
            except (_WikiRetryLater, _WikiRequestRejected):
                raise
            except Exception as e:  # noqa: BLE001 - 换下一个候选地址
                last_err = e
                data = {}
                continue
            self._flare_url = base
            if data.get("status") == "ok":
                break
            # 拿到了响应（哪怕 status=error）：连接是好的，它才是可信的失败原因——
            # 别让更早候选的 ConnectError 给它顶包（2026-09-25 事故：真因是 FS 的
            # tab crashed，日志却报「All connection attempts failed」）。
            err_msg = str(data.get("message") or "")
            last_err = None
            break
        if data.get("status") != "ok" or not (data.get("solution") or {}).get("response"):
            raise WarframeAPIError(
                f"FlareSolverr 不可用/求解失败（{err_msg or last_err or data.get('message')}）"
            )
        html = data["solution"]["response"]
        if cache_result:
            self._flare_cache[url] = (_t.time(), html)
        return html

    @property
    def flare_enabled(self) -> bool:
        """面板开关状态（仅控制 HTTP 失败后的 FlareSolverr 接力）。"""
        return self._flare_enabled

    async def flare_reachable(self, timeout: float = 6.0) -> bool:
        """轻量探测：任一候选地址能应答 ``sessions.list`` 即视为「已部署可连」。

        与 :meth:`fetch_via_flaresolver` 的区别：**不做真实求解**（秒级返回），
        只用于**告警分级**——「没部署 FS」与「部署了但求解失败」是两类问题，
        前者不该让用户每小时收 WARN（issue #1 反馈，2026-09-25）。
        """
        if not self._flare_enabled:
            return False
        candidates = ([self._flare_url] if self._flare_url else []) + [
            u for u in self._flare_urls if u != self._flare_url
        ]
        for base in candidates:
            try:
                r = await self._http.post(base, json={"cmd": "sessions.list"}, timeout=timeout)
                if r.status_code < 500:
                    self._flare_url = base
                    return True
            except Exception:  # noqa: BLE001 - 换下一个候选
                continue
        return False

    async def recycle_flare_session(self) -> bool:
        self._ensure_open()
        async with self._rotation_lock():
            self._ensure_open()
            return await self._recycle_flare_session_locked()

    async def _recycle_flare_session_locked(self) -> bool:
        """主动回收持久会话（destroy→create），防 Chromium 标签页长跑崩。

        2026-09-25 事故：02:18 那轮刷新用的标签页在 08:18 崩掉，插件随后每小时
        拿坏会话重试、连败。调用方（``_valence_autoloop``）改在**每轮快照刷新前**
        回收一次 —— 让每轮都用一个全新标签页；代价是该轮首解走冷启动
        （失败自有 destroy→create 重试兜底）。

        未启用 / 全部候选不可达时返回 False（不抛，best-effort）。
        """
        if not self._flare_enabled:
            return False
        candidates = ([self._flare_url] if self._flare_url else []) + [
            u for u in self._flare_urls if u != self._flare_url
        ]
        for base in candidates:
            try:
                r = await self._http.post(
                    base,
                    json={"cmd": "sessions.destroy", "session": self._FLARE_SESSION},
                    timeout=60.0,
                )
                r.raise_for_status()
                r2 = await self._http.post(
                    base,
                    json={"cmd": "sessions.create", "session": self._FLARE_SESSION},
                    timeout=60.0,
                )
                r2.raise_for_status()
                self._flare_url = base
                return True
            except Exception:  # noqa: BLE001 - 换下一个候选
                continue
        return False

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
            r"[\s|]+([\d.]+)%"
        )
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
        m = re.search(r"Weapon\s*\(\s*Batch\s*(\w+)\s*\)", txt) or re.search(
            r"is selling[\s|]*Batch[\s|]*(\w+)", txt
        )
        return {"tenet": tenet, "coda_batch": m.group(1) if m else "", "coda": coda}

    # ★ 2026-10-15：**源页 wikitext** 解析（Coda_Weapons / Tenet_Weapons ``?action=raw``）。
    #   与上面的 ``parse_wiki_valence``（Reset **渲染页**）相对：渲染页那两张表是
    #   ``{{#lst:}}`` 跨页嵌入、走 MediaWiki **解析缓存**，换批当天会吐**旧表**
    #   （2026-10-15 事故：对齐到 00:05 的刷新抓到上一批，anchor 反解成 1、整轮
    #   数据钉死）；源页是真源、**无解析缓存**，且 A/B 两批**同页全量**。
    @staticmethod
    def _wt_plain(cell: str) -> str:
        """wikitext 单元格 → 纯文本（模板壳由 ``_weapon_row_wt`` 再剥）。"""
        import html as _h

        txt = re.sub(r"<[^>]+>", " ", cell)
        return re.sub(r"\s+", " ", _h.unescape(txt)).strip()

    @classmethod
    def _weapon_row_wt(cls, row: str) -> dict:
        """源页数据行 → ``{en, element, bonus}``。

        行形如 ``<tr><td>Coda Hema</td><td>{{D|Magnetic}}</td>
        <td>{{ValenceBonusPercentageColor|34.2}}</td></tr>``；Tenet 页名字带
        ``{{Weapon|Tenet Ferrox}}`` 壳。
        """
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        out = {"en": "", "element": "", "bonus": None}
        if cells:
            m = re.search(r"\{\{\s*Weapon\s*\|\s*([^}|]+?)\s*\}\}", cells[0])
            out["en"] = m.group(1) if m else cls._wt_plain(cells[0])
        if len(cells) > 1:
            m = re.search(r"\{\{\s*D\s*\|\s*([^}|]+?)\s*\}\}", cells[1])
            out["element"] = m.group(1) if m else cls._wt_plain(cells[1])
        if len(cells) > 2:
            m = re.search(r"ValenceBonusPercentageColor\s*\|([^}|]*)", cells[2])
            number = m.group(1).strip() if m else cls._wt_plain(cells[2]).removesuffix("%").strip()
            out["bonus"] = float(number) if re.fullmatch(r"\d+(?:\.\d+)?", number) else None
        return out

    @classmethod
    def parse_wikitext(cls, src: str) -> dict:
        """解析 Coda Weapons / Tenet Weapons 源页的 wikitext（真源，无解析缓存）。

        ``?action=raw`` 的响应是 ``<pre>`` 包裹、实体转义过的 wikitext ⇒ 先
        ``html.unescape``。返回 ``{"coda": {1: [{en,element,bonus}…], 2: […]},
        "tenet": [ … ]}`` —— coda 的键是**批次号**（1=A、2=B），两批同页全量。
        """
        import html as _h

        src = _h.unescape(src)
        res: dict = {"coda": {}, "tenet": []}
        for tm in re.finditer(r"<table[^>]*>", src):
            start = tm.end()
            end = src.find("</table>", start)
            if end < 0:
                continue
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", src[start:end], re.S)
            heads = [r for r in rows if "<th" in r]  # 表头行（<th>）
            data = [r for r in rows if len(re.findall(r"<td", r)) >= 3]
            if not heads or not data:
                continue
            head = re.sub(r"<[^>]+>", " ", heads[0])
            head = re.sub(r"\s+", " ", _h.unescape(head)).strip()
            mb = re.match(r"Weapon\s*\(Batch\s*([AB])\)", head, re.I)
            if mb:
                res["coda"][{"A": 1, "B": 2}[mb.group(1).upper()]] = [
                    cls._weapon_row_wt(r) for r in data
                ]
                continue
            if head.lower().startswith("weapon"):  # 无批次后缀 = Tenet（Ergo Glast）
                items = [cls._weapon_row_wt(r) for r in data]
                items = [it for it in items if (it["en"] or "").lower().startswith("tenet")]
                if items:
                    res["tenet"] = items
        return res

    @staticmethod
    def _valence_since(
        old_vals, items, recorded, legacy, revision, fallback, data, now, half, page
    ):
        """共用数值采用口径；无关修订保留最早时刻，跨轮百分比半更不续鲜。"""
        from datetime import datetime, timedelta

        values = WarframeClient.valence_values(items)
        same = values == old_vals
        epoch = datetime.fromisoformat(data["epoch"])
        period = timedelta(hours=int(data.get("period_hours", 96)))
        window = epoch + (now - epoch) // period * period
        previous = _stamp_dt(recorded or legacy)
        if (
            not same
            and old_vals
            and len(values) == len(old_vals)
            and previous is not None
            and previous < window
            and all(e not in ("None", "") for _, e, _ in old_vals)
            and [v[:2] for v in values] == [v[:2] for v in old_vals]
        ):
            same = True
            half.append(page)
        if same and recorded:
            return recorded
        stamp = revision.replace(microsecond=0).isoformat() if revision else None
        if not same:
            return stamp or fallback
        candidates = [t for t in (stamp, legacy) if _stamp_dt(t) is not None]
        return min(candidates, key=_stamp_dt) if candidates else fallback

    @staticmethod
    def _wiki_json(raw):
        import html

        text = str(raw)
        if text.lstrip().startswith(("{", "[")):
            return json.loads(text)
        match = re.search(r"<pre[^>]*>(.*?)</pre>", text, flags=re.S | re.I)
        return json.loads(html.unescape(match.group(1)) if match else text)

    @classmethod
    def _wiki_revision_pages(cls, raw) -> dict:
        """区分 MediaWiki API 错误与正常页面，防 HTTP 200 错误对象静默变成空表。"""
        payload = cls._wiki_json(raw)
        if not isinstance(payload, dict):
            raise WarframeAPIError("wiki API invalid-response：响应不是对象")
        if "error" in payload:
            error = payload["error"]
            code = (
                str(error.get("code", "unknown-error"))
                if isinstance(error, dict)
                else "unknown-error"
            )
            info = str(error.get("info", ""))[:160] if isinstance(error, dict) else str(error)[:160]
            raise WarframeAPIError(f"wiki API {code}：{info}")
        query = payload.get("query")
        pages = query.get("pages") if isinstance(query, dict) else None
        if (
            not isinstance(pages, dict)
            or not pages
            or any(not isinstance(page, dict) for page in pages.values())
        ):
            raise WarframeAPIError("wiki API invalid-response：缺少有效 query.pages 对象")
        return pages

    def _rotation_observations(self) -> dict:
        # 不走种子回落；此文件只能存在于 plugin_data，开发树离线调用不创建包文件。
        target = paths.run_dir() / ROTATION_REVISIONS_NAME
        value = self._load_json_file(target)
        if not isinstance(value, dict) or not isinstance(value.get("pages", {}), dict):
            return {}
        valid = {}
        for key, record in value.get("pages", {}).items():
            if (
                key in ROTATION_PAGES
                and isinstance(record, dict)
                and type(record.get("revid")) is int
                and record["revid"] > 0
                and _stamp_dt(record.get("timestamp")) is not None
            ):
                valid[key] = record
        return {"pages": valid}

    def _rotation_source_not_older(self, key, revision, fingerprint=None) -> bool:
        record = (self._rotation_observations().get("pages") or {}).get(key) or {}
        previous = _stamp_dt(record.get("timestamp"))
        if previous is None:
            return True
        if revision is None or revision < previous:
            return False
        if revision == previous and fingerprint is not None:
            return fingerprint == record.get("source_fingerprint", record.get("fingerprint"))
        return True

    def _source_fingerprint(self, key, source):
        if key == "acrithis":
            parsed = self.parse_acrichis_current(source)
            snap = self._acrichis_snapshot()
            catalog = snap.get("_en_catalog") or {}
            values = self._offering_values([catalog[name] for name in parsed.get("items", [])])
        else:
            parsed = self.parse_wikitext(source)
            values = (
                self.valence_values(parsed["tenet"])
                if key == "tenet"
                else [self.valence_values(rows) for _, rows in sorted(parsed["coda"].items())]
            )
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))

    def _processed_source_unchanged(self, key, fingerprint) -> bool:
        record = (self._rotation_observations().get("pages") or {}).get(key) or {}
        return fingerprint == record.get("source_fingerprint", record.get("fingerprint"))

    @staticmethod
    def _valence_table(rows, expected):
        """固定修订正文必须给出恰好一组合法数值，禁止重复/残缺/非有限加成。"""
        import math

        names = [row.get("en") for row in rows]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise WarframeAPIError("修订正文武器名单不完整或重复")
        for row in rows:
            value = row.get("bonus")
            if (
                row.get("element")
                not in {"Heat", "Cold", "Electricity", "Toxin", "Impact", "Magnetic", "Radiation"}
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 25 <= value <= 60
            ):
                raise WarframeAPIError("修订正文元素或加成无效")
        return {row["en"]: (row["element"], row["bonus"]) for row in rows}

    def _adopt_valence_revision(self, key, source, revision) -> str:
        from datetime import datetime, timezone

        rot = self._restore_valence_skeleton(
            json.loads(paths.read_path("rotations.json").read_text(encoding="utf-8"))
        )
        data = rot[key]
        parsed = self.parse_wikitext(source)
        now = datetime.now(timezone.utc)
        stamp = revision.isoformat()
        if self.valence_is_stale({**data, "valence_snapshot": stamp}, now):
            return "not-updated"
        groups = [data["items"]] if key == "tenet" else data["batches"]
        tables = []
        for idx, items in enumerate(groups):
            rows = parsed["tenet"] if key == "tenet" else parsed["coda"].get(idx + 1, [])
            tables.append(self._valence_table(rows, [it["en"] for it in items]))
        if not self._rotation_source_not_older(key, revision):
            return "not-updated"
        fingerprint = self._source_fingerprint(key, source)
        if self._processed_source_unchanged(
            key, fingerprint
        ) and fingerprint != self._rotation_fingerprint(key, data):
            return "unchanged" if self._rotation_fresh(key) else "not-updated"
        old_snapshot = data.get("valence_snapshot")
        sinces = (
            [data.get("valence_values_since")]
            if key == "tenet"
            else list(data.get("batch_values_since") or [])
        )
        sinces += [None] * (len(groups) - len(sinces))
        record = (self._rotation_observations().get("pages") or {}).get(key) or {}
        try:
            previous_source = json.loads(
                record.get("source_fingerprint", record.get("fingerprint", "null"))
            )
        except (TypeError, ValueError):
            previous_source = None
        previous_groups = previous_source if key == "coda" else [previous_source]
        half = []
        changed = False
        pending = False
        for idx, items in enumerate(groups):
            candidate_values = self.valence_values(
                [
                    {"en": name, "element": values[0], "bonus": values[1]}
                    for name, values in tables[idx].items()
                ]
            )
            if (
                isinstance(previous_groups, list)
                and idx < len(previous_groups)
                and json.loads(json.dumps(candidate_values)) == previous_groups[idx]
            ):
                # 只另一批被编辑时，本批正文仍是已处理旧值，不能覆回社区/管理员的更正。
                continue
            old = self.valence_values(items)
            if not self._rotation_source_not_older(key, revision) or (
                _stamp_dt(sinces[idx]) and revision < _stamp_dt(sinces[idx])
            ):
                # 此组已由更晚来源更正；保留它，不能算整页已处理。
                pending = pending or self.valence_values(items) != self.valence_values(
                    [
                        {"en": name, "element": values[0], "bonus": values[1]}
                        for name, values in tables[idx].items()
                    ]
                )
                continue
            replacements = [
                {**it, "element": tables[idx][it["en"]][0], "bonus": tables[idx][it["en"]][1]}
                for it in items
            ]
            if self.valence_values(replacements) == old:
                # 页面其它字段或另一批改变不能给本组更新时间；首次同值也不写业务文件。
                continue
            since = self._valence_since(
                old,
                replacements,
                sinces[idx],
                data.get("valence_snapshot"),
                revision,
                stamp,
                data,
                now,
                half,
                ROTATION_PAGES[key],
            )
            if half:
                # 跨轮只改百分比：标记本页未完整更新，本轮暂不采用。
                half.clear()
                pending = True
                continue
            items[:] = replacements
            sinces[idx] = since
            changed = True
        if not changed:
            return (
                "not-updated"
                if (
                    pending
                    or self.valence_is_stale(data, now)
                    or self.valence_values_stale(data, now)
                )
                else "fresh"
            )
        if key == "tenet":
            data["valence_values_since"] = sinces[0]
        else:
            data["batch_values_since"] = sinces
        previous_snapshot = _stamp_dt(old_snapshot)
        data["valence_snapshot"] = (
            old_snapshot
            if previous_snapshot is not None and previous_snapshot > revision
            else stamp
        )
        data["valence_source"] = "wiki 固定修订源页 wikitext"
        # 未变批 since 保留；若升级前缺 since，以旧快照填充，不能借另一批变化续鲜。
        if key == "coda":
            data["batch_values_since"] = [value or old_snapshot for value in sinces]
        if pending or self.valence_is_stale(data, now) or self.valence_values_stale(data, now):
            # 不写未完整采用的部分页：否则无processed游标时旧兜底可能覆回这部分值。
            return "not-updated"
        self._ensure_open()
        paths.atomic_write_text(
            paths.write_path("rotations.json"), json.dumps(rot, ensure_ascii=False, indent=1)
        )
        return "refreshed"

    def _rotation_fingerprint(self, key, data=None) -> str:
        if data is None:
            name = ACRITHIS_WEEK_NAME if key == "acrithis" else "rotations.json"
            data = self._load_json_file(paths.read_path(name)) or {}
            if key != "acrithis":
                data = data.get(key) or {}
        if key == "acrithis":
            value = self._offering_values(data.get("items"))
        elif key == "tenet":
            value = self.valence_values(data.get("items"))
        else:
            value = [self.valence_values(items) for items in data.get("batches") or []]
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _rotation_fresh(self, key) -> bool:
        if key == "acrithis":
            return not self.acrithis_week_expired()
        rot = self._load_json_file(paths.read_path("rotations.json")) or {}
        data = rot.get(key) or {}
        return (
            bool(data.get("epoch"))
            and self.valence_data_complete(data)
            and not (self.valence_is_stale(data) or self.valence_values_stale(data))
        )

    async def check_rotation_revisions(self) -> dict[str, str]:
        """批量观察三页修订；候选只取固定 revid 正文，失败/未更新不推进处理游标。

        fresh=正文业务值相同且本地有效；unchanged=已处理同版且本地仍有效；
        refreshed=采用有效更新；not-updated=旧版本/旧业务值/未完整换轮；failed=本页失败。
        """
        self._ensure_open()
        async with self._rotation_lock():
            return await self._check_rotation_revisions_locked()

    async def _check_rotation_revisions_locked(self) -> dict[str, str]:
        from datetime import datetime, timezone

        self._ensure_open()
        results = dict.fromkeys(ROTATION_PAGES, "failed")
        if paths.run_dir() == paths.PKG_DATA:
            return results
        try:
            raw = await self.fetch_wiki_source(ROTATION_REVISION_API, ttl=0)
            self._ensure_open()
            pages = self._wiki_revision_pages(raw)
            metadata = {}
            for page in pages.values():
                title = page.get("title", "").replace("_", " ")
                if title not in ROTATION_PAGES.values() or page.get("missing") is not None:
                    continue
                revisions = page.get("revisions") or []
                if len(revisions) != 1:
                    continue
                rev = revisions[0]
                number, stamp = rev.get("revid"), _stamp_dt(rev.get("timestamp"))
                if (
                    isinstance(number, bool)
                    or not isinstance(number, int)
                    or number <= 0
                    or stamp is None
                    or stamp > datetime.now(timezone.utc)
                    or not str(rev.get("timestamp", "")).endswith(("Z", "+00:00"))
                ):
                    continue
                metadata[title] = (number, stamp)
            missing = sorted(set(ROTATION_PAGES.values()) - set(metadata))
            if missing:
                logger.warning("[sdjk] 轮换修订元数据缺少有效页面：%s", "、".join(missing))
        except Exception as exc:  # noqa: BLE001 - 每轮元数据失败留给定时兜底
            self._ensure_open()
            logger.warning("[sdjk] 轮换源页修订查询失败：%s", exc)
            return results
        state = self._rotation_observations()
        records = state.setdefault("pages", {})
        for key, title in ROTATION_PAGES.items():
            if title not in metadata:
                continue
            number, stamp = metadata[title]
            previous = records.get(key) or {}
            if number < previous.get("revid", 0):
                results[key] = "not-updated"
                continue
            if number == previous.get("revid") and _stamp_dt(previous.get("timestamp")) != stamp:
                continue
            if number == previous.get("revid"):
                # 已成功处理的固定正文不会变化；社区/管理员后续更正不能被旧版本回滚。
                results[key] = "unchanged" if self._rotation_fresh(key) else "not-updated"
                continue
            try:
                # API 按 revid 精确取正文，随后核对回包 id/时间/标题，拒绝缓存串版。
                url = (
                    "https://wiki.warframe.com/api.php?action=query&prop=revisions"
                    f"&revids={number}&rvprop=ids|timestamp|content&rvslots=main&format=json"
                )

                def fixed_source(response):
                    body_pages = self._wiki_revision_pages(response)
                    if len(body_pages) != 1:
                        raise WarframeAPIError("固定修订正文缺页")
                    page = next(iter(body_pages.values()))
                    rev = (page.get("revisions") or [{}])[0]
                    if (
                        page.get("title", "").replace("_", " ") != title
                        or rev.get("revid") != number
                        or _stamp_dt(rev.get("timestamp")) != stamp
                    ):
                        raise WarframeAPIError("固定修订正文与元数据不一致")
                    source = (rev.get("slots") or {}).get("main", {}).get("*")
                    if not isinstance(source, str) or not source:
                        raise WarframeAPIError("固定修订正文为空")
                    self._validate_rotation_source(key, source)
                    return source

                raw = await self.fetch_wiki_source(url, ttl=0, validator=fixed_source)
                self._ensure_open()
                source = fixed_source(raw)
                if key == "acrithis":
                    snap = self._acrichis_snapshot()
                    status = self._adopt_acrichis_source(
                        snap, source, stamp, self.acrithis_week_expired()
                    )
                else:
                    status = self._adopt_valence_revision(key, source, stamp)
                results[key] = status
                if status in ("fresh", "refreshed", "unchanged"):
                    self._ensure_open()
                    if paths.run_dir() == paths.PKG_DATA:
                        raise WarframeAPIError("未设置 plugin_data，不能写修订观测件")
                    record = {
                        "revid": number,
                        "timestamp": stamp.isoformat(),
                        "fingerprint": self._rotation_fingerprint(key),
                        "source_fingerprint": self._source_fingerprint(key, source),
                    }
                    updated = {**records, key: record}
                    paths.atomic_write_text(
                        paths.write_path(ROTATION_REVISIONS_NAME),
                        json.dumps({"pages": updated}, ensure_ascii=False, indent=1),
                    )
                    records[key] = record
            except Exception as exc:  # noqa: BLE001 - 单页失败不挡其它页；取消传播
                self._ensure_open()
                logger.warning("[sdjk] %s修订 %s 未采用：%s", title, number, exc)
                results[key] = "failed"
        return results

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
            dt = datetime.fromisoformat(snap)
        except (TypeError, ValueError):
            return True
        # GPT 评估 V01：未来时间（> now + 1 天）的快照会永远「在窗口内」⇒ 一样算过期
        return dt < win or dt > now + timedelta(days=1)

    @classmethod
    def valence_data_complete(cls, data: dict, now=None) -> bool:
        """静态商品骨架完整，且当前在售组具有合法值；非当前终幕批允许无值。"""
        import math
        from datetime import datetime, timedelta, timezone

        try:
            if not isinstance(data, dict):
                return False
            period_hours = data.get("period_hours", 96)
            if (
                isinstance(period_hours, bool)
                or not isinstance(period_hours, (int, float))
                or not math.isfinite(period_hours)
                or period_hours <= 0
            ):
                return False
            batches = data.get("batches")
            if "batches" in data:
                if not isinstance(batches, list) or len(batches) != 2:
                    return False
                groups, size = batches, 7
            else:
                groups, size = [data.get("items")], 5
            names = []
            for group in groups:
                if not isinstance(group, list) or len(group) != size:
                    return False
                for row in group:
                    if (
                        not isinstance(row, dict)
                        or not isinstance(row.get("en"), str)
                        or not row["en"].strip()
                    ):
                        return False
                    names.append(row["en"])
            if len(names) != len(set(names)):
                return False
            seed = paths.load_json(paths.PKG_DATA / "rotations.json")
            expected_groups = seed["coda"]["batches"] if batches else [seed["tenet"]["items"]]
            if any(
                {row["en"] for row in group} != {row["en"] for row in expected}
                for group, expected in zip(groups, expected_groups)
            ):
                return False
            if batches:
                now = now or datetime.now(timezone.utc)
                epoch = datetime.fromisoformat(data["epoch"])
                period = timedelta(hours=period_hours)
                passed = int((now - epoch) // period)
                rows = batches[(int(data.get("anchor_idx", 0)) + passed) % len(batches)]
            else:
                rows = groups[0]
            cls._valence_table(rows, [row["en"] for row in rows])
            return True
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
            ZeroDivisionError,
            WarframeAPIError,
        ):
            return False

    @staticmethod
    def _restore_valence_skeleton(rot: dict) -> dict:
        """缺行缓存按包内静态商品表补回；保留同名已有值，采用源验证成功后才落盘。"""
        import copy

        seed = paths.load_json(paths.PKG_DATA / "rotations.json")
        for key in ("tenet", "coda"):
            data = rot.get(key)
            if not isinstance(data, dict):
                data = rot[key] = copy.deepcopy(seed[key])
            field = "items" if key == "tenet" else "batches"
            old_groups = [data.get(field)] if key == "tenet" else data.get(field)
            old_groups = old_groups if isinstance(old_groups, list) else []
            known = {
                row["en"]: row
                for group in old_groups
                if isinstance(group, list)
                for row in group
                if isinstance(row, dict) and isinstance(row.get("en"), str)
            }
            source_groups = [seed[key][field]] if key == "tenet" else seed[key][field]
            groups = copy.deepcopy(source_groups)
            for group in groups:
                for row in group:
                    cached = known.get(row["en"], {})
                    try:
                        WarframeClient._valence_table([cached], [row["en"]])
                    except WarframeAPIError:
                        continue
                    row["element"], row["bonus"] = cached["element"], cached["bonus"]
            data[field] = groups[0] if key == "tenet" else groups
        return rot

    @staticmethod
    def valence_values(items) -> list:
        """一组武器的数值指纹（武器名 + 元素 + 加成），用来判断「换轮后数值到底变没变」。"""
        return sorted(
            (
                str(it.get("en")),
                str(it.get("element")),
                format(it["bonus"], ".12g")
                if isinstance(it.get("bonus"), (int, float))
                and not isinstance(it.get("bonus"), bool)
                else str(it.get("bonus")),
            )
            for it in items or []
        )

    @staticmethod
    def valence_values_stale(data: dict, now=None) -> bool:
        """快照时刻在本轮，但**当前在售那组数值从上一轮起就没变过** ⇒ wiki 还没人更新。

        ★ 2026-10-18 实测：信条 00:00Z 换轮，00:17 的刷新从 wiki 抓到的仍是上一轮
        的 5 个值（wiki 是玩家人工上报，换轮后要等人改），却因快照时刻落在本轮窗口
        而被标成「本轮快照」，并且整轮不再重抓（10-02 换轮也出过同样的事）。
        信条与终幕的元素 / 加成每轮都会重新生成，所以「当前这组数值最早出现的时刻」
        早于本轮窗口起点 ⇒ 数值已存在超过一个周期 ⇒ 判为未更新。该时刻优先取 wiki
        源页的最近修订时间（数值真正发布的时间），查不到才退回快照时刻。

        - 信条：``valence_values_since``；
        - 终幕：``batch_values_since[当前批]``（每批各记各的，当前批按 epoch+anchor 计算）。
        没有记录时不判（旧数据无从比较）。
        """
        from datetime import datetime, timedelta, timezone

        now = now or datetime.now(timezone.utc)
        epoch = datetime.fromisoformat(data["epoch"])
        period = timedelta(hours=int(data.get("period_hours", 96)))
        passed = int((now - epoch) // period)
        win = epoch + period * passed
        batches = data.get("batches")
        if batches:
            sinces = data.get("batch_values_since") or []
            bi = (int(data.get("anchor_idx", 0)) + passed) % len(batches)
            since = sinces[bi] if bi < len(sinces) else None
        else:
            since = data.get("valence_values_since")
        if not since:
            return False
        try:
            return datetime.fromisoformat(since) < win
        except (TypeError, ValueError):
            return False

    @staticmethod
    def coda_anchor_for(observed_idx: int, epoch, period_hours: int, now, n_batches: int) -> int:
        """由「wiki 上观测到的当前批」反解 ``anchor_idx``。

        ★ 卡面用的是 ``(anchor_idx + 换轮次数) % 批数``，所以 anchor_idx 是
        **相位基准**，不等于「当前批的下标」。旧代码直接存 ``anchor_idx = bi``，
        换轮次数一前进就整体错位一批（09-20 实测：B 批生效时会被算成 A 批）。
        """
        from datetime import timedelta

        passed = int((now - epoch) // timedelta(hours=int(period_hours)))
        return (observed_idx - passed) % n_batches

    async def refresh_community_static(self, names: Optional[tuple[str, ...]] = None) -> dict:
        """拉 bot-data 的静态快照（仲裁评级 / 奸商历史 / 紫卡倾向），校验通过且**比本地新**才落盘。

        返回 {文件名: 状态}：refreshed <时间戳> / fresh（本地不旧）/ invalid: 原因 / unavailable。
        落盘后清掉对应模块缓存（arbi / baro 在进程内缓存了整表）。
        """
        out: dict[str, str] = {}
        for name in names or tuple(COMMUNITY_STATIC):
            spec = COMMUNITY_STATIC[name]
            payload = await self._community_json(community_static_urls(name))
            if payload is None:
                out[name] = "unavailable"
                continue
            ok, why = community_static_ok(name, payload)
            if not ok:
                out[name] = f"invalid: {why}"
                self._note_comm(name, "invalid", why, payload.get(spec["stamp"]))
                continue
            local = self._load_json_file(paths.read_path(spec["local"])) or {}
            new_t, old_t = (
                _stamp_dt(payload.get(spec["stamp"])),
                _stamp_dt(local.get(spec["stamp"])),
            )
            if old_t is not None and new_t <= old_t:
                out[name] = "fresh"
                self._note_comm(
                    name, "fresh", f"本地 {local.get(spec['stamp'])}", payload.get(spec["stamp"])
                )
                continue
            paths.atomic_write_text(
                paths.write_path(spec["local"]), json.dumps(payload, ensure_ascii=False, indent=1)
            )
            _invalidate_static(name)
            out[name] = f"refreshed {payload.get(spec['stamp'])}"
            self._note_comm(name, "adopted", "", payload.get(spec["stamp"]))
        return out

    def _note_comm(
        self,
        name: str,
        result: str,
        reason: str = "",
        stamp: str = "",
        *,
        failures: Optional[list[dict]] = None,
    ) -> None:
        """记一笔社区快照检查结论（result 取值见 COMMUNITY_RESULTS）。通道由 _community_json 记。"""
        ent = self.community_log.setdefault(name, {})
        ent.update(
            checked_at=time.time(),
            result=result,
            reason=str(reason)[:200],
            stamp=str(stamp or ""),
            failures=failures or [],
        )

    def community_report(self) -> list[dict]:
        """五份社区快照的最近检查记录（按固定顺序；没检查过的给 result=unchecked）。"""
        out = []
        for name, label in COMMUNITY_FILES:
            ent = dict(self.community_log.get(name) or {})
            ent.setdefault("result", "unchecked")
            out.append({"name": name, "label": label, **ent})
        return out

    async def _community_json(
        self, urls: tuple[str, ...], *, validator: Optional[Callable[[dict], None]] = None
    ) -> Optional[dict]:
        """读公开仓 bot-data 的社区快照（普通 GET、免盾）。取不到返回 None。

        按候选顺序试；效价另有 Contents API 独立回退。结构和调用方的时效校验
        都通过才缓存并返回，旧镜像继续下一源；全部不可采用才返回 None。
        ★ 2026-10-16（GPT 评估 P04）：
          · 上次成功的主机排最前；网络失败的主机冷却 COMMUNITY_COOLDOWN 秒（404 只说明该镜像没这个文件，不冷却）；
          · 全部在冷却时仍试排第一的那个（低频重探，网络恢复能自己回来）；
          · 每条通道只试一次（多通道本身就是重试），整次取数受 COMMUNITY_BUDGET 秒总预算约束。
        """
        name = urls[0].rsplit("/", 1)[-1]

        def validate(payload) -> None:
            # 验证放在真实HTTP缓存之前，也会检查升级前遗留的同key坏缓存。
            # 此处只判断文件是否可解析；当前轮次/expiry/本地更新关系仍由消费入口决定。
            try:
                if not isinstance(payload, dict):
                    raise ValueError("不是 JSON 对象")
                if name in COMMUNITY_STATIC:
                    ok, why = community_static_ok(name, payload)
                    if not ok:
                        raise ValueError(why)
                elif name == "valence.json":
                    parsed = parse_community_valence(payload)
                    if any(_stamp_dt(stamp) is None for stamp in parsed["snapshots"].values()):
                        raise ValueError("缺少有效的分段快照时间戳")
                elif name == "acrithis_week.json":
                    # 不在这里判断是否过期，避免把正常旧货单误判为网络故障。
                    from datetime import datetime

                    expiry = datetime.fromisoformat(str(payload.get("expiry") or ""))
                    if expiry.tzinfo is None:
                        raise ValueError("expiry 缺少时区")
                    items = payload.get("items")
                    if (
                        not isinstance(items, list)
                        or not items
                        or any(not isinstance(item, dict) or not item.get("name") for item in items)
                    ):
                        raise ValueError("物品列表缺失或条目缺 name")
            except (ValueError, TypeError, OverflowError) as exc:
                raise _CommunityContentError(f"社区快照结构不合法（{name}）：{exc}") from exc
            if validator is not None:
                validator(payload)

        custom = bool(self.community_base)
        if custom:  # 用户自填地址：只走它，不再回落到本项目 bot-data
            urls = (f"{self.community_base}/{name}",)
            # ★ 2026-10-25（Astra 评估 S4）：自填地址不许指向内网（可显式放行），且不跟随重定向
            if not getattr(self, "allow_private_urls", False):
                why = await private_url_reason(urls[0])
                if why:
                    logger.warning("[sdjk] 社区数据地址被拒绝（%s）：%s", name, why)
                    return None
        now = time.monotonic()

        def host(u: str) -> str:
            return u.split("/")[2]

        order = sorted(urls, key=lambda u: host(u) != self._comm_good_host)
        live = [u for u in order if self._comm_cool.get(host(u), 0.0) <= now] or order[:1]
        deadline = now + COMMUNITY_BUDGET
        errors: list[str] = []
        failures: list[dict] = []

        def report_failure(code, channel="", *, message="", detail="", url="", limit=160):
            if custom:
                channel = ""
            errors.append((f"{channel}：{message}" if channel else message)[:limit])
            failures.append(
                {"code": code, "channel": channel, "detail": str(detail)[:limit], "url": url}
            )

        content_failed = False
        rejected: list[_CommunitySnapshotRejected] = []
        for i, url in enumerate(live):
            left = deadline - time.monotonic()
            if left <= 1.0:
                logger.info("[sdjk] 社区快照（%s）超出本次时间预算，余下通道下次再试", name)
                report_failure("budget_exhausted", message="超出本次时间预算")
                break
            # 为尚未尝试的通道留出预算，新增候选不能让末尾镜像永远轮不到。
            cap = min((left - 0.5) / (len(live) - i), COMMUNITY_PER_CHANNEL)
            github_api = not custom and url == COMMUNITY_VALENCE_API_URL
            request_url = url + f"&cb={int(time.time() // 60)}" if github_api else url
            headers = (
                {
                    **_ORACLE_HEADERS,
                    "Accept": "application/vnd.github.raw+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                }
                if github_api
                else _ORACLE_HEADERS
            )
            try:
                data = await asyncio.wait_for(
                    self._fetch_json(
                        request_url,
                        ttl=600,
                        headers=headers,
                        cache_key=f"community:{name}",
                        retries=0,
                        timeout=cap,  # httpx 自己按时中止（主保险）
                        # 自填地址不跟随重定向（重定向可绕过上面的内网校验）
                        follow_redirects=False if custom or github_api else None,
                        validator=validate,
                    ),
                    timeout=cap + 2.0,  # 外层取消只作兜底
                )
            except _CommunitySnapshotRejected as exc:
                # 旧快照不是网络故障；不冷却整台主机，也不把它记成成功通道。
                rejected.append(exc)
                report_failure("not_adopted", host(url), message=str(exc), detail=str(exc))
                logger.info("[sdjk] 社区快照通道 %s 暂不可采用（%s）：%s", host(url), name, exc)
                continue
            except _CommunityContentError as exc:
                content_failed = True
                # 坏内容是该文件的问题，不让同主机的其它快照一起进入网络冷却。
                logger.info("[sdjk] 社区快照通道 %s 内容不可用（%s）：%s", host(url), name, exc)
                report_failure("invalid_content", host(url), message="社区快照结构不合法")
                continue
            except Exception as exc:  # noqa: BLE001
                missing = isinstance(exc, WarframeAPIError) and str(exc).startswith("接口不存在")
                if not missing:
                    self._comm_cool[host(url)] = time.monotonic() + COMMUNITY_COOLDOWN
                logger.info(
                    "[sdjk] 社区快照通道 %s 不可用（%s）：%s%s",
                    host(url),
                    name,
                    # 超时类异常 str() 为空（ZCode 10-16：日志冒号后啥都没有）⇒ 退回异常类型名
                    str(exc) or type(exc).__name__,
                    "" if missing else f"；冷却 {COMMUNITY_COOLDOWN // 60} 分钟",
                )
                if missing:
                    report_failure(
                        "missing_file", host(url), message="该镜像没有这个文件", limit=80
                    )
                else:
                    # 代理、解析器和 HTTP 客户端的异常可能包含内网地址、响应原文或凭据。
                    # 面板只接收稳定分类、状态码及插件内置的公开源地址。
                    cause = exc.__cause__ or exc
                    timed_out = isinstance(cause, (TimeoutError, httpx.TimeoutException))
                    status = (
                        cause.response.status_code
                        if isinstance(cause, httpx.HTTPStatusError)
                        else None
                    )
                    detail = f"HTTP {status}" if status is not None else ""
                    message = "请求超时" if timed_out else "请求失败"
                    report_failure(
                        "request_timeout" if timed_out else "request_failed",
                        host(url),
                        message=f"{message}（{detail}）" if detail else message,
                        detail=detail,
                        url=url if not custom else "",
                        limit=80,
                    )
                continue
            if isinstance(data, dict):
                self._comm_cool.pop(host(url), None)
                if host(url) != self._comm_good_host:
                    if self._comm_good_host is not None or i:
                        logger.info("[sdjk] 社区快照改走通道 %s", host(url))
                    self._comm_good_host = host(url)
                self.community_log.setdefault(name, {})["channel"] = host(url)
                return data
        self.community_log.setdefault(name, {})["channel"] = ""
        if rejected:
            # 至少一份可解析快照可解释本轮未采用的原因，不误报成断网。
            outcome = next((r for r in rejected if r.result == "fresh"), rejected[-1])
            self._note_comm(
                name, outcome.result, "；".join(errors), outcome.stamp, failures=failures
            )
        else:
            self._note_comm(
                name,
                "invalid" if content_failed else "unavailable",
                "；".join(errors) or ("所有通道都在冷却中" if not live else "取不到"),
                failures=failures,
            )
        return None

    def _validate_community_valence(self, payload: dict, rot: dict) -> None:
        """逐通道采用闸门；拒绝旧段、禁止任一段回退，并保留源时间戳。"""
        from datetime import datetime, timezone

        parsed = parse_community_valence(payload)
        snaps = parsed["snapshots"]
        now = datetime.now(timezone.utc)
        for sect in ("tenet", "coda"):
            data = rot.get(sect) or {}
            if data.get("epoch") and self.valence_is_stale(
                {**data, "valence_snapshot": snaps[sect]}, now
            ):
                raise _CommunitySnapshotRejected("stale", f"{sect} 段属于上一轮", snaps[sect])
        older = [
            sect
            for sect in ("tenet", "coda")
            if not community_valence_newer(
                snaps[sect], (rot.get(sect) or {}).get("valence_snapshot", "")
            )
            and snaps[sect] != str((rot.get(sect) or {}).get("valence_snapshot", ""))
        ]
        newer = [
            sect
            for sect in ("tenet", "coda")
            if community_valence_newer(
                snaps[sect], (rot.get(sect) or {}).get("valence_snapshot", "")
            )
        ]
        if older or not newer:
            reason = "远端分段快照不比本地新" + ("：" + "、".join(older) if older else "")
            raise _CommunitySnapshotRejected("fresh", reason, parsed["snapshot"])

    async def _community_valence_parsed(self, rot: dict) -> Optional[dict]:
        """读取首个可采用的社区快照；旧镜像或旧缓存不能挡住后续新源。"""
        payload = await self._community_json(
            COMMUNITY_VALENCE_URLS,
            validator=lambda data: self._validate_community_valence(data, rot),
        )
        if payload is None:
            return None
        try:
            # 保留入口末端复验，防止获取过程中跨轮次或旧调用方绕过逐源校验。
            self._validate_community_valence(payload, rot)
            return parse_community_valence(payload)
        except _CommunitySnapshotRejected as exc:
            self._note_comm("valence.json", exc.result, str(exc), exc.stamp)
        except ValueError as exc:
            logger.warning("[sdjk] 社区元素加成快照校验不过：%s", exc)
            self._note_comm("valence.json", "invalid", exc, payload.get("published_at"))
        return None

    async def _wfhelper_valence_parsed(self) -> Optional[dict]:
        """WFHelper 元素加成（开关打开时才调用）；取不到 / 校验不过返回 None。"""
        try:
            r = await self._http.get(
                WFHELPER_VALENCE_URL, timeout=15.0, headers={"User-Agent": USER_AGENT}
            )
            r.raise_for_status()
            return parse_wfhelper_valence(r.json())
        except Exception as exc:  # noqa: BLE001 - 第三方通道失败只降级，不阻断
            logger.info("[sdjk] WFHelper 元素加成不可用（%s），改用社区快照", exc)
            return None

    async def _valence_fallback(self, rot: dict) -> tuple[str, Optional[dict]]:
        """源页抓不到时的兜底：WFHelper（开关打开时）→ 社区快照。返回 ``(通道, 结果)``。"""
        if self.wfhelper_valence:
            w = await self._wfhelper_valence_parsed()
            if w is not None:
                return "wfhelper", w
        return "community", await self._community_valence_parsed(rot)

    async def refresh_valence(self, community_only: bool = False, force: bool = False) -> str:
        """检查并刷新信条/终幕元素加成快照。

        ★ 2026-10-15 数据源改为**源页 wikitext**（``Coda_Weapons`` /
        ``Tenet_Weapons`` 的 ``?action=raw``）。原先走 Reset **渲染页**，而那段是
        ``{{#lst:}}`` 跨页嵌入、受 MediaWiki 解析缓存影响 —— 换批当天会吐**旧表**
        （2026-10-15 事故：对齐到 00:05 的刷新抓到上一批，anchor 反解成 1、整轮
        数据钉死）。源页是真源、无解析缓存，且 **A/B 两批同页全量** ⇒ 两批都能
        填值（旧实现只填当前批、把另一批的 element/bonus 清空）。
        当前批**由 epoch+anchor 计算**（``(anchor_idx + 换轮次数) % 批数``），不再
        从页面反解 —— 页面那份不可靠；anchor_idx 是**相位基准**，只在首次标定时改。

        ``community_only=True``（后台的独立社区轮询模式）：**不碰
        FlareSolverr**，直接用公开仓社区快照；取不到就返回 "no-community" 静默跳过。

        幂等：快照仍在当前 96h 轮次内则直接返回「fresh」不动文件；``force=True``（面板「从源头
        重抓」）跳过这道门，其余校验（解析完整、数值未更新判定）照旧。
        校验失败（解析残缺）抛异常，绝不写半成品。
        """
        # 后台社区刷新与面板源页重抓共用同一把资源锁，避免先读的旧快照后写覆盖新值。
        # async with 同时保证异常或取消后释放；其它资源的刷新不占此锁。
        self._ensure_open()
        async with self._rotation_lock():
            self._ensure_open()
            return await self._refresh_valence_locked(community_only=community_only, force=force)

    async def _refresh_valence_locked(
        self, community_only: bool = False, force: bool = False
    ) -> str:
        from datetime import datetime, timedelta, timezone

        # 效价刷新只能写插件运行目录；离线客户端未注入目录时不得改包内种子。
        if paths.run_dir().resolve() == paths.PKG_DATA.resolve():
            logger.warning("[sdjk] 效价刷新未注入运行目录，已跳过以保护包内静态数据")
            return "failed"
        rot_path = paths.read_path("rotations.json")
        rot = json.loads(rot_path.read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc)
        # ★ 必须**两段都新鲜**才算 fresh，不能「遇到第一个新鲜的就 return」。
        #   信条与终幕的换轮锚点相差 24 小时（epoch 相差一天），所以任何时刻
        #   都必然有一段仍在本轮窗口内 —— 旧写法让自动刷新**永远空转**：
        #   服务器快照停在 2026-09-17，09-19 以来 52 条 sdjk 日志里一条刷新
        #   记录都没有，plugin_data 下也从没写出过 rotations.json。
        _sections = [rot.get(k) for k in ("tenet", "coda")]
        # 升级前的数据还没有「数值最早出现时刻」记录 ⇒ 先刷一次补上（否则本轮未更新也认不出来）
        _unrecorded = not (rot.get("tenet") or {}).get("valence_values_since") or not (
            rot.get("coda") or {}
        ).get("batch_values_since")
        if (
            not force
            and not _unrecorded
            and not any(
                not self.valence_data_complete(s, now)
                or self.valence_is_stale(s, now)
                or self.valence_values_stale(s, now)
                for s in _sections
                if s
            )
        ):
            self._note_comm("valence.json", "local_ok", "本地快照在本轮窗口内")
            return "fresh"

        rot = self._restore_valence_skeleton(rot)
        pinned_fallback = False
        if not community_only and self._rotation_observations().get("pages"):
            result = await self._check_rotation_revisions_locked()
            states = [result[key] for key in ("tenet", "coda")]
            if all(state in {"fresh", "unchanged", "refreshed"} for state in states):
                return "refreshed (revision)" if "refreshed" in states else "fresh"
            # 某页已成功落盘，兜底必须重新读最新副本，不能用本轮最早读取的旧rot覆盖。
            rot = self._restore_valence_skeleton(
                json.loads(paths.read_path("rotations.json").read_text(encoding="utf-8"))
            )
            pinned_fallback = True
        tenet_data = rot.get("tenet") or {}
        coda_data = rot.get("coda") or {}
        labels = coda_data.get("batch_label") or ["A", "B"]
        batches = coda_data.get("batches") or []
        if len(batches) != len(labels):
            raise WarframeAPIError("valence 批次表与标签数不一致")

        used_community = used_wfhelper = False
        revs: dict = {}  # 源页最近修订时间（只在直抓源页时查；兜底通道没有）
        # 归一形：tenet {en: (elem, pct)}、coda {批次号(1-based): {en: (elem, pct)}}
        if community_only or pinned_fallback:
            if pinned_fallback:
                kind, raw = "community", await self._community_valence_parsed(rot)
            else:
                kind, raw = await self._valence_fallback(rot)
            if raw is None:
                if pinned_fallback:
                    return "not-updated unverified-source"
                return "no-community"
            used_community, used_wfhelper = kind == "community", kind == "wfhelper"
        else:
            try:
                ttl = 0 if force else 600
                p_coda_source = await self.fetch_wiki_source(
                    WIKI_RAW_CODA,
                    ttl=ttl,
                    validator=lambda source: self._validate_rotation_source("coda", source),
                )
                p_tenet_source = await self.fetch_wiki_source(
                    WIKI_RAW_TENET,
                    ttl=ttl,
                    validator=lambda source: self._validate_rotation_source("tenet", source),
                )
                p_coda = self.parse_wikitext(p_coda_source)
                p_tenet = self.parse_wikitext(p_tenet_source)
                raw = None
                revs = await self._wiki_revisions(VALENCE_REVISION_API, "元素加成")
            except Exception as exc:  # noqa: BLE001
                # ★ 没部署 / 连不上 FlareSolverr 时的兜底：读公开仓 bot-data 的
                #   社区快照（同一份 wiki 数据，由有 FS 的那台机器发布）。校验不过
                #   就照旧抛错（调用方降级），绝不采用残缺或更旧的数据。
                logger.info("[sdjk] 元素加成源页不可用（%s），尝试兜底通道", exc)
                kind, raw = await self._valence_fallback(rot)
                if raw is None:
                    raise
                used_community, used_wfhelper = kind == "community", kind == "wfhelper"

        if used_wfhelper:
            coda = {labels.index(b) + 1: m for b, m in raw["coda"].items() if b in labels}
            if not coda:
                raise WarframeAPIError(f"WFHelper 批次标注异常：{sorted(raw['coda'])!r}")
            parsed = {"tenet": raw["tenet"], "coda": coda}
        elif used_community:
            cb = raw.get("coda_batch") or ""
            if cb not in labels:
                raise WarframeAPIError(f"valence 批次标注异常：{cb!r}")
            parsed = {"tenet": raw["tenet"], "coda": {labels.index(cb) + 1: raw["coda"]}}
        else:
            parsed = {
                "tenet": {it["en"]: (it["element"], it["bonus"]) for it in p_tenet["tenet"]},
                "coda": {
                    idx: {it["en"]: (it["element"], it["bonus"]) for it in items}
                    for idx, items in p_coda["coda"].items()
                },
            }
            if not parsed["tenet"]:  # 兜底：源文件顺序与预期相反也能解析
                parsed["tenet"] = {it["en"]: (it["element"], it["bonus"]) for it in p_coda["tenet"]}
            if not parsed["coda"]:
                parsed["coda"] = {
                    idx: {it["en"]: (it["element"], it["bonus"]) for it in items}
                    for idx, items in p_tenet["coda"].items()
                }

        want_t = {it.get("en") for it in tenet_data.get("items") or []}
        if not want_t or not want_t <= set(parsed["tenet"]):
            raise WarframeAPIError(f"valence 解析不全：tenet 缺 {want_t - set(parsed['tenet'])}")

        # 当前批 = **计算**（anchor_idx 是相位基准；不读页面 —— 页面那份受缓存影响不可靠）
        epoch = datetime.fromisoformat(coda_data["epoch"])
        period = timedelta(hours=int(coda_data.get("period_hours", 96)))
        passed = int((now - epoch) // period)
        bi = (int(coda_data.get("anchor_idx", 0)) + passed) % len(batches)
        want_c = {it.get("en") for it in batches[bi]}
        if not want_c <= set(parsed["coda"].get(bi + 1, {})):
            raise WarframeAPIError(
                f"valence 解析不全：coda {labels[bi]} 批缺 "
                f"{want_c - set(parsed['coda'].get(bi + 1, {}))}"
            )

        snap = now.replace(microsecond=0).isoformat()
        if used_community and raw.get("snapshot"):
            # ★ 社区快照保留**数据源的时刻**（GPT 评估 V01）：写 now 会让旧数据看起来是刚抓的
            snap = str(raw["snapshot"])

        half: list = []  # 判为「只改了百分比」的源页（日志用）

        # 社区快照各段带自己的快照时刻（2026-10-19）；其它通道两段同一时刻
        sect_snap = {
            k: (str(raw["snapshots"][k]) if used_community and raw.get("snapshots") else snap)
            for k in ("tenet", "coda")
        }

        def _since(old_vals, new_items, recorded, legacy, page, fb=None):
            return self._valence_since(
                old_vals,
                new_items,
                recorded,
                legacy,
                revs.get(page),
                fb or snap,
                tenet_data if page == "Tenet Weapons" else coda_data,
                now,
                half,
                page,
            )

        old_t = self.valence_values(tenet_data.get("items"))
        old_t_since = tenet_data.get("valence_values_since")
        old_t_snap = tenet_data.get("valence_snapshot")
        for it in tenet_data.get("items") or []:
            elem, pct = parsed["tenet"][it["en"]]
            it["element"], it["bonus"] = elem, pct
        tenet_data["valence_values_since"] = _since(
            old_t,
            tenet_data.get("items"),
            old_t_since,
            old_t_snap,
            "Tenet Weapons",
            sect_snap["tenet"],
        )
        # ★ A/B 两批都给值（源页两批同页全量）；缺的那批保留原值，不清空
        old_c_since = list(coda_data.get("batch_values_since") or [])
        old_c_since += [None] * (len(batches) - len(old_c_since))
        old_c_snap = coda_data.get("valence_snapshot")
        new_c_since = []
        for idx, b_items in enumerate(batches):
            old_c = self.valence_values(b_items)
            table = parsed["coda"].get(idx + 1) or {}
            for it in b_items:
                if it.get("en") in table:
                    elem, pct = table[it["en"]]
                    it["element"], it["bonus"] = elem, pct
            new_c_since.append(
                _since(
                    old_c, b_items, old_c_since[idx], old_c_snap, "Coda Weapons", sect_snap["coda"]
                )
            )
        coda_data["batch_values_since"] = new_c_since
        tenet_data["valence_snapshot"] = sect_snap["tenet"]
        coda_data["valence_snapshot"] = sect_snap["coda"]
        via = "(wfhelper)" if used_wfhelper else "(community)" if used_community else ""
        if used_wfhelper:
            src = "WFHelper 公开接口（数据同样来自 wiki 玩家上报；后台可选通道）"
        elif used_community:
            src = "community-snapshot（wiki《Reset》数据，由有 FS 的机器发布）"
        else:
            src = "wiki 源页 wikitext（Coda_Weapons / Tenet_Weapons）"
        tenet_data["valence_source"] = src
        coda_data["valence_source"] = src
        self._ensure_open()
        paths.atomic_write_text(
            paths.write_path("rotations.json"), json.dumps(rot, ensure_ascii=False, indent=1)
        )
        if used_community:
            self._note_comm("valence.json", "adopted", f"批次 {labels[bi]}", snap)
        same = [
            n
            for n, d in (("信条", tenet_data), ("终幕", coda_data))
            if self.valence_values_stale(d, now)
        ]
        if same:
            logger.warning(
                "[sdjk] %s元素加成已换轮，但抓到的数值疑似仍是上一轮（wiki 尚未完整更新%s），"
                "卡面标注疑似上一轮；后台每轮继续重抓",
                "、".join(same),
                "：元素列与上一轮完全相同、只改了百分比" if half else "",
            )
            return f"not-updated {snap} batch={labels[bi]}{via}"
        return f"refreshed {snap} batch={labels[bi]}{via}"

    async def nearmiss_riven_weapons(self, query: str, n: int = 4) -> list[dict]:
        """与 ``query`` **等长且恰有 1 个字不同** 的紫卡武器（近形字候选）。

        ★ 2026-10-07 为何需要它：`fuzzy_hits` 的 2 字档阈值 0.75，而两个字
        「只差一个」的 difflib ratio 恰好 **0.5** ⇒ 永远命中不了。线上报障
        （2026-10-07 三次：10:59 / 17:11 / 17:13）卡面「冰凇」被 OCR 读成
        「冰松」⇒ 纯图路径整卡失败（该卡无手输名可兜底）。

        这里只做**候选生成**（字面近似），是否采纳交给调用方用**卡面数值可行性**
        （`riven_analysis.disp_feasible`）当闸门 —— 保证不是纯字面替换：表里
        真实存在的近形对（如 绝路/绝望）也会被列出来，可行性能挡住错配。
        """
        q = (query or "").strip()
        if len(q) < 2:
            return []
        out: list[dict] = []
        for w in await self.wm_riven_weapons():
            zh = (w.get("zh") or "").strip()
            if not zh or len(zh) != len(q):
                continue
            if sum(1 for a, b in zip(zh, q) if a != b) == 1:
                out.append(w)
                if len(out) >= n:
                    break
        return out

    async def suggest_riven_weapons(self, query: str, n: int = 3) -> list[str]:
        """给「未找到」的紫卡武器查询提供候选。

        fuzzy（错别字容忍）未命中时再做**子串匹配**：像「棱晶欧玛」这种
        变体前缀写法，WM 紫卡表里只有母武器「欧玛」，fuzzy 距离不够但
        子串能命中。
        """
        weapons = await self.wm_riven_weapons()
        names = [(w.get("zh") or w.get("en") or w.get("url_name", "")) for w in weapons]
        hits = fuzzy_hits(query, names, n=n)
        if not hits:
            hits = [nm for nm in names if nm and nm in query][:n]
        return hits

    @staticmethod
    def _family_match(base_zh: str, base_en: str, zh: str, en: str) -> bool:
        """判断 zh/en 是否属于「母武器」base 的同一家族（变体）。

        ★ 2026-10-02：判据换成 **DE 官方 `parentName` 谱系**（唯一权威 =
        `core/data/de/riven_families.json`，统一经 `matching.family_key` 读取）。
        中文只用于显示、**永不参与判定**（`base_zh` / `zh` 仅为形参兼容）；
        英文名不在表里先剥变体词再查，仍不在 ⇒ 退回英文主干；英文为空 ⇒
        不入家族（宁可少列，不可错列）。
        旧版「中文主干相等 **或** 英文主干相等」双分支会让 `Dex 盗贼双枪`
        （官方 parentName 指向 Afuris）**同时**并进 Furis 族 —— 玩家报障。
        """
        kb = matching.family_key(base_en)
        k = matching.family_key(en)
        return bool(kb and k and kb == k)

    async def riven_family(self, weapon: dict) -> list:
        """同一武器家族的变体（棱晶/Prime/亡魂/赤毒…）及其**取消前的旧**倾向。

        ★ 2026-10 DE 取消倾向后只剩一个用途：卡面数值按 1.2 对不上时，看它是否吻合
        家族某成员的旧倾向 ⇒ 判「倾向取消前的旧截图」。wiki 表已冻结（不再重抓）。

        ★ 2026-10-03 重写（用户口径：变体倾向只准取自本地 wiki 表）：
          · 家族关系 = DE 官方 parentName 表（core/data/de/riven_families.json）；
          · 倾向值   = 本地 wiki 快照（core/data/de/wiki_disp.json）；
          · 展示名   = 本地三表级联（拿不到就退回英文名，**绝不丢候选**）。
        旧实现遍历 WM 物品表找变体名 —— 玄骸武器本体不可交易（WM 全量 3892 条
        里 0 条武器）⇒ 赤毒/信条/终幕变体永远列不出候选（「鳄神」卡反推 0.8
        = 赤毒·鳄神 无匹配 ⇒ 被误判成「倾向调整前洗出的老卡」）。不再依赖 WM，
        也省掉一次 2.5MB 物品表拉取。
        返回 [(展示名, 倾向), ...]，按倾向升序。
        """
        base_en = (weapon.get("en") or "").strip()
        table = (self._load_json_file(paths.read_path(WIKI_DISP_NAME)) or {}).get("disp") or {}
        if not table or not base_en:
            return []
        by_name = matching._load_riven_families()  # {英文名: 官方家族根}
        root = matching.family_key(base_en)
        found: dict[str, float] = {}
        for en, r in by_name.items():
            if r != root or en.lower() == base_en.lower():
                continue  # 非本族成员 / 自身
            v = table.get(en.strip().lower())  # ★ 本地 wiki 表（键=小写英文）
            if v is None:
                continue
            found.setdefault(_zh_name_of_en(en) or en, v)  # 没中文名退回英文名
        return sorted(found.items(), key=lambda kv: kv[1])

    # ------------------------------------------------------------------
    # Wiki（本地词库优先，搜不到给搜索链接）
    # ------------------------------------------------------------------
    def wiki_lookup(self, query: str) -> Optional[dict]:
        table = self._aliases.get("wiki", {})
        q = query.strip()
        # ★ C1（2026-10-03）：先做**归一化精确**（大小写/空格/中点/全半角无关，
        #   matching.normalize_name）——「花p/花P」类混用与「阿索代prime」类
        #   写法一并覆盖；归一化未中再走原精确/模糊链（零回归）。
        _nq = matching.normalize_name(q)
        if _nq:
            for k, v in table.items():
                if matching.normalize_name(k) == _nq:
                    return (
                        {"title": v.get("title", k), "url": v["url"], "en": v.get("en", "")}
                        if isinstance(v, dict)
                        else {"title": k, "url": v}
                    )
        # 「花p」这类小写 p 后缀 ≡ 「花P」（社区写法混用，2026-09-25）
        cands = [q]
        if len(q) > 1 and q.endswith(("p", "P")) and q[:-1] + "P" != q:
            cands.append(q[:-1] + "P")
        for c in cands:
            e = table.get(c)
            if e is None:
                continue
            return (
                {"title": e.get("title", c), "url": e["url"], "en": e.get("en", "")}
                if isinstance(e, dict)
                else {"title": c, "url": e}
            )
        close = difflib.get_close_matches(q, list(table), n=1, cutoff=0.8)
        if close and isinstance(table[close[0]], dict):
            return {
                "title": table[close[0]].get("title", close[0]),
                "url": table[close[0]]["url"],
                "en": table[close[0]].get("en", ""),
            }
        return None

    def wiki_clarify(self, query: str) -> str:
        """易混淆澄清（``aliases.json::clarify``）：命中返回一句话，否则空串。

        由来：战甲黑话清单里几组「问 A 答 B」高发区（花甲=Wisp 不是 Garuda、
        血妈=Garuda 不叫鸟妹、猫=Khora 而 Valkyr 是老猫甲、奶妈/奶爸、电=Volt
        与 高=Gauss 的「跑男」两代、明神=Limbo 的双关）——wiki 卡片与检索提示
        把这句话带上（2026-09-25 用户口径）。
        """
        table = self._aliases.get("clarify") or {}
        q = (query or "").strip()
        if q in table:
            return str(table[q])
        if len(q) > 1 and q.endswith(("p", "P")) and (q[:-1] + "P") in table:
            return str(table[q[:-1] + "P"])
        return ""

    async def wiki_search_link(self, query: str) -> str:
        """本地词库未命中时的兜底：返回中文维基搜索链接。"""
        return "https://warframe.huijiwiki.com/w/index.php?search=" + quote(query)
