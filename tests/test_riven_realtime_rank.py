# -*- coding: utf-8 -*-
"""紫卡实时排行（WM 拍卖）回归：目标武器集 + 实时卡渲染。

背景（维护者 2026-10-23）：「紫卡排行」用的 DE 官方周报时效差 —— 该文件**每周一 00:00 UTC
才更新一次**，1.2 热修后不少武器紫卡暴涨却看不出。故新增 WM 拍卖实时快照，
`紫卡排行` 默认改看实时、周报作 `紫卡排行 周报` 子视图。

钉住三件事：
① 目标集 = 周报热度前 N + 原倾向 ≤0.7，去重、**家族折到母武器**；
   DE 周报的 `compatibility` 是**内部名**（不是 WM url_name），必须经 WM 表解析、解析不到就跳过；
② 实时卡按**挂单数**降序、含「更新于」时间；
③ 快照缺失时给出明确文案（不假装有数据）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import formatters as F  # noqa: E402
from core.api_client import riven_realtime_targets  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        ("[PASS] " if cond else "[FAIL] ")
        + name
        + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


# 构造：WM 表（含 en/zh/url_name）+ 倾向表（**以英文名为键**）+ 家族表
WM = [
    {"url_name": "acceltra", "en": "Acceltra", "zh": "迅发电浆炮"},
    {"url_name": "acceltra_prime", "en": "Acceltra Prime", "zh": "迅发电浆炮 Prime"},
    {"url_name": "torid", "en": "Torid", "zh": "托里德"},
    {"url_name": "rubico", "en": "Rubico", "zh": "绝路"},
]
DISP = {
    "Acceltra": {"url_name": "acceltra", "zh": "迅发电浆炮", "disposition": 0.65},
    "Acceltra Prime": {"url_name": "acceltra_prime", "zh": "迅发电浆炮 Prime", "disposition": 0.55},
    "Torid": {"url_name": "torid", "zh": "托里德", "disposition": 1.3},
    "Rubico": {"url_name": "rubico", "zh": "绝路", "disposition": 0.95},
}
FAM = {
    "by_name": {"Acceltra": "K", "Acceltra Prime": "K"},
    "families": {"K": ["Acceltra", "Acceltra Prime"]},
}

# ① 周报路：compatibility 用**英文名**（真实表里是内部名，解析不到就跳过 —— 见下一条）
WK = [
    {"compatibility": "Torid", "pop": 90},
    {"compatibility": "Rubico", "pop": 80},
    {"compatibility": "1999InfShotgunWeapon", "pop": 999},  # 内部名 ⇒ 必须跳过
]
t = riven_realtime_targets(WK, WM, DISP, FAM)
check("目标集：周报热度前 N 取到", "torid" in t and "rubico" in t, str(t))
check("目标集：内部名（1999InfShotgunWeapon）被跳过", "1999InfShotgunWeapon" not in t, str(t))
check("目标集：原倾向 ≤0.7 的进来", "acceltra" in t, str(t))
check("目标集：家族折到母武器（不留 acceltra_prime）", "acceltra_prime" not in t, str(t))
check("目标集：倾向 1.3 的托里德只因周报入榜", t.count("torid") == 1, str(t))

t2 = riven_realtime_targets([], WM, DISP, FAM)
check("目标集：只有倾向路 ⇒ 仅 ≤0.7 的 acceltra 入榜", t2 == ["acceltra"], str(t2))

t3 = riven_realtime_targets(WK, WM, DISP, FAM, top_n=1)
check("目标集：top_n=1 只留最热（torid）", t3 == ["acceltra", "torid"], str(t3))

# ② 实时卡渲染：按挂单数降序 + 含更新时间
# ②③④ 2026-10-25 改口径：不再有独立的「实时排行」卡 —— 周报卡在管理员拉过 WM 现价后追加两列
_SNAP = {
    "fetched": 0,
    "entries": [
        {
            "itemType": "Rifle Riven Mod",
            "compatibility": "Glaxion",
            "rerolled": False,
            "median": 60,
            "pop": 9,
        },
        {
            "itemType": "Rifle Riven Mod",
            "compatibility": "Glaxion",
            "rerolled": True,
            "median": 80,
            "pop": 5,
        },
        {
            "itemType": "Rifle Riven Mod",
            "compatibility": "Rubico",
            "rerolled": False,
            "median": 100,
            "pop": 8,
        },
    ],
}
_ZH = {"glaxion": "冷冻光束步枪", "rubico": "绝路"}
_t0, _l0 = F.fmt_riven_weekly(_SNAP, _ZH)
check(
    "没拉过现价：表头没有现价两列、注脚提示管理员可拉",
    not any("WM挂单" in x for x in _l0) and any("紫卡排行 刷新" in x for x in _l0),
    str(_l0),
)
_now = {"glaxion": {"median": 140}, "rubico": {"median": 105}}
_t1, _l1 = F.fmt_riven_weekly(_SNAP, _ZH, now=_now, now_ts="2026-10-25T06:00:00+00:00")
_row_g = next(x for x in _l1 if "冷冻光束步枪" in x)
_row_r = next(x for x in _l1 if "绝路" in x)
check(
    "★ 拉过现价：表头加「WM挂单　变化」；冷冻光束 60→140 记 ▲133%；绝路 100→105 记 ≈",
    any(x.endswith("　WM挂单　变化") for x in _l1)
    and _row_g.endswith("140p　▲133%")
    and _row_r.endswith("105p　≈"),
    f"{_row_g} / {_row_r}",
)
check(
    "拉过现价：注脚写明拉取时间与「挂单价≠成交价」",
    any("2026-10-25 06:00" in x and "挂单价≠成交价" in x for x in _l1),
    str(_l1[-3:]),
)
check(
    "变化列：缺数写「—」、跌幅写 ▼",
    F._riven_delta(None, 5) == "—" and F._riven_delta(100, 50) == "▼50%",
)
_t2, _l2 = F.fmt_riven_type("步枪", _SNAP, _ZH, now=_now, now_ts="2026-10-25T06:00:00+00:00")
check(
    "分类完整榜同样带现价两列",
    any(x.endswith("　WM挂单　变化") for x in _l2) and any("▲133%" in x for x in _l2),
    str(_l2),
)

# ⑤ 手动「紫卡排行 刷新」/ 面板按钮（2026-10-24）：启动 → 在抓不重复 → 启动与结束后均10分钟冷却、面板force不绕过；
#    自动循环与手动同时触发只跑一份（单飞锁）
import asyncio  # noqa: E402

from core.api_client import WarframeClient  # noqa: E402


class _RtClient(WarframeClient):
    def __init__(self):
        self.runs = 0
        self.store = {"rows": {"a": {}}, "total": 3}

    def riven_realtime_rows(self):
        return self.store

    async def _crawl_riven_realtime(self, limit=None):
        self.runs += 1
        await asyncio.sleep(0.05)
        return 1


async def _manual():
    c = _RtClient()
    s1 = c.start_riven_rt_crawl()
    s2 = c.start_riven_rt_crawl()
    await c._riven_rt_crawl_task
    s3 = c.start_riven_rt_crawl()
    s4 = c.start_riven_rt_crawl(force=True)
    await c._riven_rt_crawl_task
    c2 = _RtClient()
    await asyncio.gather(c2.crawl_riven_realtime(), c2.crawl_riven_realtime())
    return c, (s1, s2, s3, s4), c2.runs


c, states, dual_runs = asyncio.run(_manual())
check("手动刷新：首次启动并带回已收录/总数", states[0] == ("started", 1, 3), str(states))
check("手动刷新：在抓时不重复启动", states[1][0] == "running", str(states))
check("手动刷新：群指令 10 分钟内冷却", states[2][0] == "cooldown", str(states))
check("手动刷新：面板 force 仍受安全冷却", states[3][0] == "cooldown" and c.runs == 1, str(states))
check("★ 自动与手动同时触发只抓一份", dual_runs == 1, str(dual_runs))


# ⑥ 爬取续跑与节奏（2026-10-24，ZCode 线上实测：retry 混进游标 ⇒ 整轮跑不满；双向 + 快速重试 ⇒ 429）
# ⑥½ 节奏常量按 10-25 实测（拍卖搜索约 90 s 窗口 8 次）：间隔 ≥ 窗口/8、退避 ≥ 恢复窗口
from core import api_client as _AC  # noqa: E402


class _CrawlClient(WarframeClient):
    RIVEN_RT_GAP_S = 0.0
    RIVEN_RT_FAIL_BACKOFF_S = 0.0

    def __init__(self, store, bad=()):
        self.store, self.bad, self.calls = store, set(bad), []

    async def de_weekly_rivens(self, *a, **k):
        return {}

    async def wm_riven_weapons(self):
        return []

    async def wm_riven_auctions(self, slug, platform="pc", **kw):
        self.calls.append((slug, kw.get("sorts"), kw.get("retries")))
        if slug in self.bad:
            raise RuntimeError("warframe.market 限速")
        return [{"buyout_price": 100, "owner": {"status": "ingame"}}]

    def _load_json_file(self, path):
        return dict(self.store)

    def _save_json_file(self, path, data):
        self.store.clear()
        self.store.update(data)


check(
    "★ 节奏常量：每项间隔 ≥ 90/8 s、429 退避 ≥ 90 s（实测恢复窗口）",
    WarframeClient.RIVEN_RT_GAP_S >= 90 / 8 and WarframeClient.RIVEN_RT_FAIL_BACKOFF_S >= 90,
)
_T = [f"w{i}" for i in range(101)]
_orig_targets = _AC.riven_realtime_targets
_AC.riven_realtime_targets = lambda *a, **k: list(_T)
try:
    _cc = _CrawlClient(
        {"cursor": 99, "cursor_targets": _T, "retry": _T[:72], "round_failed": _T[:72], "ts": ""}
    )
    asyncio.run(_cc.crawl_riven_realtime())
    check(
        "★ 续跑 ZCode 留下的状态（游标 99、待补 72）：先补 72 再跑完 2 项即满一轮、写 ts",
        len(_cc.calls) == 74
        and _cc.store["cursor"] == 0
        and _cc.store["ts"]
        and _cc.store["retry"] == [],
        str({k: _cc.store.get(k) for k in ("cursor", "ts", "coverage")}),
    )
    check(
        "★ 每把只查价格升序一次、爬取层不让底层快速重试",
        all(c[1] == ("price_asc",) and c[2] == 0 for c in _cc.calls),
        str(_cc.calls[:2]),
    )
    _cd = _CrawlClient({}, bad=_T[5:9])
    asyncio.run(_cd.crawl_riven_realtime())
    check(
        "★ 连续失败 4 次提前收工：只抓到第 9 项、游标停在 9、ts 不写、失败进待补",
        len(_cd.calls) == 9
        and _cd.store["cursor"] == 9
        and not _cd.store["ts"]
        and _cd.store["retry"] == _T[5:9],
        str({k: _cd.store.get(k) for k in ("cursor", "ts", "retry")}),
    )
    asyncio.run(_CrawlClient.crawl_riven_realtime(_cd))  # 下次续跑（仍失败的那 3 项先补）
    _cd.bad = set()
    asyncio.run(_cd.crawl_riven_realtime())
    check(
        "续跑：补抓成功后从游标继续、最终满一轮写 ts",
        _cd.store["cursor"] == 0 and _cd.store["ts"] and _cd.store["retry"] == [],
        str({k: _cd.store.get(k) for k in ("cursor", "ts", "retry")}),
    )

    # ⑦ WM 回 400（item_not_exist）⇒ 永久无效：移出目标集、不计连续失败、不挡满一轮（10-25 实测 7 个连着 400）
    class _Bad400(_CrawlClient):
        async def wm_riven_auctions(self, slug, platform="pc", **kw):
            self.calls.append((slug, kw.get("sorts"), kw.get("retries")))
            if slug in self.bad:
                raise RuntimeError("请求失败（x）：Client error '400 Bad Request' for url y")
            return [{"buyout_price": 100, "owner": {"status": "ingame"}}]

    _ce = _Bad400({}, bad=_T[10:17])
    asyncio.run(_ce.crawl_riven_realtime())
    check(
        "★ 连续 7 个 400 不触发提前收工：整轮跑满、ts 写上、7 项进 invalid、不进待补",
        _ce.store["cursor"] == 0
        and _ce.store["ts"]
        and _ce.store["invalid"] == sorted(_T[10:17])
        and _ce.store["retry"] == []
        and _ce.store["coverage"] == 1.0,
        str({k: _ce.store.get(k) for k in ("cursor", "ts", "coverage", "retry")}),
    )
    _ce.calls.clear()
    asyncio.run(_ce.crawl_riven_realtime())
    check(
        "下一轮不再请求 invalid 里的条目",
        len(_ce.calls) == 101 - 7 and not any(c[0] in _T[10:17] for c in _ce.calls),
        str(len(_ce.calls)),
    )

    # 400 会压缩下一轮目标列表；断点必须跟有效序列同步，旧数据/序列变化保守重跑。
    class _ResumeClient(_CrawlClient):
        def __init__(self, store, bad=(), invalid=()):
            super().__init__(store, bad)
            self.invalid_slugs = set(invalid)

        async def wm_riven_auctions(self, slug, platform="pc", **kw):
            self.calls.append((slug, kw.get("sorts"), kw.get("retries")))
            if slug in self.invalid_slugs:
                raise RuntimeError("item_not_exist")
            if slug in self.bad:
                raise RuntimeError("temporary upstream error")
            return [{"buyout_price": 100, "owner": {"status": "ingame"}}]

    _small = list("abcdefg")
    _AC.riven_realtime_targets = lambda *a, **k: list(_small)
    try:
        _limited = _ResumeClient({}, invalid="a")
        asyncio.run(_limited.crawl_riven_realtime(limit=2))
        check(
            "400+限量断点：有效游标、目标序列及总数同口径",
            _limited.store["cursor"] == 1
            and _limited.store["cursor_targets"] == list("bcdefg")
            and _limited.store["total"] == 6
            and not _limited.store["ts"],
            str(_limited.store),
        )
        _limited.calls.clear()
        asyncio.run(_limited.crawl_riven_realtime())
        check(
            "400+限量恢复：从c续跑，不漏有效项也不重抓a/b",
            [c[0] for c in _limited.calls] == list("cdefg")
            and set(_limited.store["rows"]) == set("bcdefg")
            and _limited.store["coverage"] == 1.0,
            str(_limited.calls),
        )
        _interrupted = _ResumeClient({}, bad="bcde", invalid="a")
        asyncio.run(_interrupted.crawl_riven_realtime())
        check(
            "400+连续错误断点：提前收工不标新鲜，保存压缩后游标4",
            _interrupted.store["cursor"] == 4
            and _interrupted.store["retry"] == list("bcde")
            and not _interrupted.store["ts"],
            str(_interrupted.store),
        )
        _interrupted.bad.clear()
        _interrupted.calls.clear()
        asyncio.run(_interrupted.crawl_riven_realtime())
        check(
            "400+连续错误恢复：先补b-e再抓f/g，不漏项才标100%",
            [c[0] for c in _interrupted.calls] == list("bcdefg")
            and set(_interrupted.store["rows"]) == set("bcdefg")
            and _interrupted.store["coverage"] == 1.0
            and _interrupted.store["ts"],
            str(_interrupted.calls),
        )
        _legacy = _ResumeClient(
            {"cursor": 5, "invalid": ["a"], "retry": list("bcde"), "round_failed": list("bcde")},
            invalid="a",
        )
        asyncio.run(_legacy.crawl_riven_realtime(limit=2))
        check(
            "旧checkpoint首次迁移：保守从b重跑，不先重复补抓",
            [c[0] for c in _legacy.calls] == list("bc")
            and _legacy.store["cursor"] == 2
            and _legacy.store["cursor_targets"] == list("bcdefg"),
            str(_legacy.calls),
        )
        _legacy.calls.clear()
        asyncio.run(_legacy.crawl_riven_realtime())
        check(
            "旧checkpoint迁移后按新断点续跑，不再次回0",
            [c[0] for c in _legacy.calls] == list("defg")
            and set(_legacy.store["rows"]) == set("bcdefg"),
            str(_legacy.calls),
        )
        _reordered = _ResumeClient({})
        asyncio.run(_reordered.crawl_riven_realtime(limit=2))
        _reordered.calls.clear()
        _small.reverse()
        asyncio.run(_reordered.crawl_riven_realtime())
        check(
            "目标顺序变化：保守重扫新序列，不套用旧下标漏f/g",
            [c[0] for c in _reordered.calls] == _small
            and set(_reordered.store["rows"]) == set(_small)
            and _reordered.store["cursor_targets"] == _small,
            str(_reordered.calls),
        )
    finally:
        _AC.riven_realtime_targets = lambda *a, **k: list(_T)

    # ⑨ 退出目标集的旧行要裁掉（10-25 ZCode：「已收录 98/97」，多出的是上一轮的 dex_nikana）
    _cf = _CrawlClient({"rows": {"old_gone": {"median": 1}, "w0": {"median": 2}}, "ts": "x"})
    asyncio.run(_cf.crawl_riven_realtime())
    check(
        "★ 退出目标集的旧行被裁掉：rows ⊆ 目标集、已收录不再大于总数",
        "old_gone" not in _cf.store["rows"] and len(_cf.store["rows"]) == _cf.store["total"],
        str(len(_cf.store["rows"])),
    )
finally:
    _AC.riven_realtime_targets = _orig_targets

# ⑧ 目标集折母武器（10-25 实测 kuva_sobek / kuva_zarr / mk1-paris 被当母武器 ⇒ WM 400）：
#    母武器取 WM 原始表真实存在的成员（game_ref 非空）、家族名不分大小写、孤儿变体剔除
_WM8 = [
    {"url_name": "sobek", "en": "Sobek", "zh": "索贝克", "game_ref": "/x/Sobek"},
    {"url_name": "kuva_bramma", "en": "Kuva Bramma", "zh": "赤毒布拉玛", "game_ref": "/x/KB"},
    {"url_name": "paris", "en": "Paris", "zh": "帕里斯", "game_ref": "/x/Paris"},
    {"url_name": "kuva_sobek", "en": "Kuva Sobek", "zh": "赤毒索贝克", "game_ref": ""},
    {"url_name": "mk1-paris", "en": "Mk1-Paris", "zh": "Mk1-帕里斯", "game_ref": ""},
    {"url_name": "kuva_orphan", "en": "Kuva Orphan", "zh": "孤儿", "game_ref": ""},
]
_DISP8 = {
    "Kuva Sobek": {"url_name": "kuva_sobek", "disposition": 0.6},
    "Mk1-Paris": {"url_name": "mk1-paris", "disposition": 0.6},
    "Kuva Bramma": {"url_name": "kuva_bramma", "disposition": 0.6},
    "Kuva Orphan": {"url_name": "kuva_orphan", "disposition": 0.6},
}
_FAM8 = {
    "by_name": {"Kuva Sobek": "S", "Sobek": "S", "MK1-Paris": "P", "Paris": "P"},
    "families": {"S": ["Kuva Sobek", "Sobek"], "P": ["MK1-Paris", "Paris"]},
}
_t8 = riven_realtime_targets([], _WM8, _DISP8, _FAM8)
check(
    "★ 折母武器：Kuva Sobek→sobek、Mk1-Paris→paris（不分大小写）、kuva_bramma 保留、孤儿变体剔除",
    _t8 == ["kuva_bramma", "paris", "sobek"],
    str(_t8),
)


async def _price_reference_checks():
    """实际方法→单方向拍卖查询→httpx→真实解析/统计；不联网、不访问历史榜。"""
    import json
    import time
    import httpx

    def row(price=10, status="offline", **changes):
        return {
            "buyout_price": price,
            "visible": True,
            "closed": False,
            "owner": {"status": status},
            **changes,
        }

    async def query(rows, platform="pc", *, response_code=200, envelope=None, cached=False):
        payload = (
            envelope
            if envelope is not None
            else {
                "payload": {"auctions": [{"id": f"a{i}", **value} for i, value in enumerate(rows)]}
            }
        )
        calls = []

        def transport(request):
            calls.append(request)
            return httpx.Response(
                response_code,
                content=json.dumps(payload).encode(),
                headers={"content-type": "application/json"},
            )

        client = WarframeClient(wm_base="https://synthetic.invalid/v1")
        await client.http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(transport))

        def no_history(*args, **kwargs):
            raise AssertionError("单武器价格参考不得读写历史榜或启动全量爬取")

        client._load_json_file = client._save_json_file = no_history
        client.start_riven_rt_crawl = no_history
        before = time.time()
        try:
            result = await client.riven_price_reference("synthetic_weapon", platform)
            check(
                "低价参考只查指定slug/platform一次price_asc/" + platform,
                len(calls) == 1
                and calls[0].url.path == "/v1/auctions/search"
                and dict(calls[0].url.params)
                == {
                    "type": "riven",
                    "weapon_url_name": "synthetic_weapon",
                    "platform": platform,
                    "sort_by": "price_asc",
                },
            )
            check(
                "低价参考保留查询口径和完成时间/" + platform,
                result["platform"] == platform
                and result["weapon_url_name"] == "synthetic_weapon"
                and before <= result["fetched"] <= time.time()
                and result["cache_ttl_seconds"] == 90
                and "price_asc" in result["source"]
                and "查询返回样本" in result["scope"]
                and "非成交价" in result["scope"],
            )
            if cached:
                second = await client.riven_price_reference("synthetic_weapon", platform)
                check(
                    "低价参考复用现有拍卖缓存，不新增后台抓取",
                    len(calls) == 1 and second["median"] == result["median"],
                )
            return result
        except _AC.WarframeAPIError:
            check("错误路径只做一次price_asc请求、不快速重试", len(calls) == 1)
            raise
        finally:
            await client.close()

    result = await query([row(30, "online"), row(10), row(20, "ingame")], cached=True)
    check(
        "低价参考奇数≤20：离线低价与在线低价分开、中位数不偏在线",
        (
            result["low"],
            result["online_low"],
            result["median"],
            result["sample_count"],
            result["valid_count"],
            result["returned_count"],
        )
        == (10, 20, 20, 3, 3, 3),
    )
    result = await query([row(60), row(10), row(30), row(20)], "ps4")
    check(
        "低价参考偶数≤20：中间两价均值，无在线返回None",
        result["median"] == 25 and result["online_low"] is None,
    )
    many = [row(value, "online" if value == 125 else "offline") for value in range(125, 0, -5)]
    result = await query(many, "xbox")
    check(
        "低价参考超过20：只取最低20条中位数，在线最低价仍覆盖全部有效返回",
        (
            result["low"],
            result["online_low"],
            result["median"],
            result["sample_count"],
            result["sample_limit"],
            result["valid_count"],
            result["returned_count"],
        )
        == (5, 125, 52.5, 20, 20, 25, 25),
    )
    invalid = [
        row(value)
        for value in (
            True,
            False,
            0,
            -1,
            "12",
            None,
            float("nan"),
            float("inf"),
            -float("inf"),
            10**400,
        )
    ]
    invalid += [
        row(1, visible=False),
        row(2, closed=True),
        row(3, visible=1),
        row(4, closed=0),
        row(None, starting_price=1),
    ]
    no_buyout = row(1, starting_price=1)
    no_buyout.pop("buyout_price")
    no_visible = row(1)
    no_visible.pop("visible")
    no_closed = row(1)
    no_closed.pop("closed")
    invalid += [no_buyout, no_visible, no_closed]
    valid = [row(10), row(20, "ingame"), row(30, owner=[]), row(40, "online")]
    result = await query(invalid + valid, "switch")
    check(
        "低价参考混合坏值：只认明确可见未关闭一口价，不回退起拍、不截断小数",
        result["low"] == 10
        and result["online_low"] == 20
        and result["median"] == 25
        and result["sample_count"] == result["valid_count"] == 4
        and result["returned_count"] == len(invalid) + 4,
    )
    result = await query(invalid)
    check(
        "全无效价格：有效计数0、价格None，保留实际返回数",
        result["sample_count"] == result["valid_count"] == 0
        and result["returned_count"] == len(invalid)
        and all(result[key] is None for key in ("low", "online_low", "median")),
    )
    result = await query([])
    check(
        "真实空样本：三个价格None、三个计数0，不是假0价",
        all(result[key] is None for key in ("low", "online_low", "median"))
        and all(result[key] == 0 for key in ("sample_count", "valid_count", "returned_count")),
    )
    result = await query([row(10.25), row(10.5)])
    check("合法小数一口价不被int截断", result["low"] == 10.25 and result["median"] == 10.375)
    for code, payload in (
        (503, {}),
        (429, {}),
        (200, {"error": "bad-response"}),
        (200, {"payload": {}}),
    ):
        try:
            await query([], response_code=code, envelope=payload)
        except _AC.WarframeAPIError:
            check("接口/信封错误向上抛出而不返回空样本或0价/" + str((code, payload)), True)
        else:
            check("接口/信封错误向上抛出而不返回空样本或0价/" + str((code, payload)), False)


asyncio.run(_price_reference_checks())

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + "、".join(FAILED))
    sys.exit(1)
print("全部通过")
