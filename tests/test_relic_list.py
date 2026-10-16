# -*- coding: utf-8 -*-
"""遗物出入库卡：中文名 / 版式 / 缺失补齐 / 刷取建议（python3 tests/test_relic_list.py）

覆盖 2026-09-17 玩家反馈的四件事：
  1. 列表里遗物名是英文（Lith A12）→ 必须全中文（古纪 A12）
  2. 入库卡只占左半边、列不对齐 → 表格模式 + 每行 9 列 + 自然序
  3. 出库清单缺遗物 → 任务掉落表之外的阿耶商店遗物要并进来
  4. 出库卡要给出「推荐刷取」并且「只在特定位置能获取」的要单独标记
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _txt(r) -> str:
    """Reply 的可读文本：raw_text 或 title + lines（报错卡片迁移后断言用）。"""
    if r is None:
        return ""
    return r.raw_text or (r.title + "\n" + "\n".join(r.lines))


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return
    pkg = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    mc_mod = types.ModuleType("astrbot.api.message_components")
    star_mod = types.ModuleType("astrbot.api.star")

    class AstrBotConfig(dict):
        pass

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def exception(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    class AstrMessageEvent:
        def __init__(self, umo: str = "group://test", sender: str = "tester"):
            self.unified_msg_origin = umo
            self._sender = sender

        def get_sender_name(self) -> str:
            return self._sender

    class MessageChain:
        def message(self, text):
            return text

    class _EventMessageType:
        ALL = "ALL"

    class _Filter:
        EventMessageType = _EventMessageType
        event_message_type = staticmethod(lambda spec: lambda fn: fn)

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: lambda fn: fn
    event_mod.filter = _Filter()

    class Image:
        pass

    class Plain:
        pass

    mc_mod.Image = Image
    mc_mod.Plain = Plain

    class Context:
        pass

    class Star:
        def __init__(self, *a, **k):
            pass

    def register(*a, **k):
        def deco(cls):
            return cls

        return deco

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register
    api.AstrBotConfig = AstrBotConfig
    api.logger = _Logger()
    sys.modules.setdefault("astrbot", pkg)
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = mc_mod
    sys.modules["astrbot.api.star"] = star_mod


_install_astrbot_stub()

import main as plugin  # noqa: E402

# 2026-09-29 D5：RELIC_USAGE 随遗物域迁 core/commands/relic.py
from core.commands.relic import RELIC_USAGE  # noqa: E402
from core import drops as drops_db  # noqa: E402
from core import formatters as fmt  # noqa: E402
from core.api_client import load_aliases  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


IDX = json.loads((ROOT / "core" / "data" / "relic_index.json").read_text(encoding="utf-8"))

# ---------------------------------------------------------------- ① 中文名归一
print("=== ① 遗物名统一成中文 ===")
cases = [
    ("古纪 Lith A1", "古纪 A1"),  # relic_index 的键
    ("后纪 Axi V12", "后纪 V12"),  # relic_inverse 的键
    ("Lith A12", "古纪 A12"),  # 掉落表键（英文）
    ("古纪 S1 遗物", "古纪 S1"),  # DE 官方简中写法
    ("后纪A2", "后纪 A2"),  # 用户输入（无空格）
    ("古纪 q3", "古纪 Q3"),  # 小写代号
    ("安魂 I", "安魂 I"),  # 安魂遗物是罗马数字
]
for src, want in cases:
    got = fmt.relic_cn(src)
    check(f"relic_cn({src!r}) == {want!r}", got == want, f"得到 {got!r}")
check("认不出来的原样返回（不猜）", fmt.relic_cn("绝路 Prime 枪机") == "绝路 Prime 枪机")

# ------------------------------------------------------------- ② 自然序与版式
print("\n=== ② 自然序 + 每行 12 列（格内只写代号）+ 中文 ===")
check(
    "自然序 A1 < A2 < A10（不是字符串序）",
    [
        n.split()[-1]
        for n in sorted(["古纪 A10", "古纪 A2", "古纪 A1", "古纪 B1"], key=fmt._relic_sort_key)
    ]
    == ["A1", "A2", "A10", "B1"],
)
# ★ 2026-10-11 第二步改版：纪元已写在 ◆ 分区标题里，格内只写代号（「A1」而非「古纪 A1」，
#   全行相同的列不写）⇒ 单格实测宽度 119px → 约 60px；按 12 列重测：12×60+11×24 ≈ 980px，
#   远在列对齐安全阀（≤1322）之内，所以每行由 9 列放宽到 12 列。
check("每行 12 列（实测宽度算出来的，改动需重测）", fmt._TIER_PER_LINE == 12)

_sample = [
    {"cn": k, "tier_cn": k.split()[0], "unvaulted": True} for k in IDX if k.startswith("古纪 ")
]
_t, _l = fmt.fmt_relic_by_tier(_sample, "遗物出库（当前可掉落）")
_body = "\n".join(_l)
check("卡面标题走表格模式（渲染层才会列对齐）", "遗物出库" in _t)
check(
    "卡面不再出现英文纪元（Lith/Meso/Neo/Axi）",
    not any(w in _body for w in ("Lith", "Meso", "Neo", "Axi")),
    _body[:200],
)
_row = [x for x in _l if x.startswith("· ")][0]
check("首行 12 个条目", len([c for c in _row[2:].split("　") if c]) == 12, _row)
check(
    "格内只写代号（不重复纪元）", all(not c.startswith("古纪") for c in _row[2:].split("　")), _row
)
check(
    "注脚行不含全角空格（会污染渲染层的列宽计算）",
    all("　" not in x for x in _l if x.startswith("※")),
)

# ------------------------------------------------- ③ 出库补齐：阿耶商店遗物
print("\n=== ③ 出库并入阿耶商店遗物 ===")
_live = drops_db.unvaulted_relics()
_uv = {f"{k.split()[0]} {k.split()[1]}" for k in _live}
for code in ("lith k5", "lith m7", "meso e5", "neo b6", "axi h5", "axi a12"):
    check(f"★ {code} 确实不在任务掉落表（所以要靠阿耶补齐）", code not in _uv)


class _FakeClient:
    """只回阿耶商店的两个遗物，用来验证 handler 会把它们并进出库。"""

    def __init__(self, names):
        self._names = names

    async def prime_vault(self, platform):
        return {
            "items": [
                {"kind": "relic", "name": n, "price": 1, "prime_currency": "阿耶精华"}
                for n in self._names
            ]
        }

    async def ducats_price_map(self):
        return {}


class _Parsed:
    def __init__(self, content="", preset="", page=1):
        self.content_str = content
        self.preset = preset
        self.page = page


def _make_obj(names):
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj._relic_cache = None
    obj.client = _FakeClient(names)
    obj.client._aliases = load_aliases()  # ②-b 黑话兜底需要（真实 client 自带）
    obj.page_size = 12
    obj._dir = Path(tempfile.mkdtemp())
    return obj


_reply = asyncio.run(
    _make_obj(["古纪 K5 遗物", "后纪 A12 遗物", "赫利俄斯 Prime 蓝图"])._h_relic(
        _Parsed(content="出库"), None, "pc"
    )
)
_out = "\n".join(_reply.lines)
check("出库卡包含阿耶商店的遗物（古纪 K5）", "古纪 K5" in _out, _reply.title)
check("出库卡包含阿耶商店的遗物（后纪 A12）", "后纪 A12" in _out)
check("阿耶来源的遗物标了「仅阿耶兑换」", "仅阿耶兑换" in _out, _out[:300])
check("非遗物商品（赫利俄斯 Prime 蓝图）没被当成遗物", "赫利俄斯 Prime 蓝图" not in _out)

# --------------------------------------------------- ④ 特殊渠道 + 推荐刷取
print("\n=== ④ 特殊渠道标记与推荐刷取 ===")
check(
    "出处极窄的遗物被标出（古纪 C7 只在比邻星域储藏库）",
    "储藏库" in drops_db.special_source("lith c7 relic"),
    drops_db.special_source("lith c7 relic"),
)
check(
    "常规遗物（出处遍地）不标",
    drops_db.special_source("lith a12 relic") == "",
    drops_db.special_source("lith a12 relic"),
)
_hints = drops_db.farm_hints()
check(
    "每个纪元都给推荐刷取点",
    all(_hints.get(t) for t in ("古纪", "前纪", "中纪", "后纪")),
    str(_hints),
)
check(
    "推荐点里没有「储藏库／比邻星域」这类非刷取目标",
    all("储藏库" not in x and "比邻星域" not in x for v in _hints.values() for x in v),
    str(_hints),
)
check(
    "推荐点不带轮次后缀（只给星球·节点·任务）",
    all(len(x.split(" · ")) == 3 for v in _hints.values() for x in v),
    str(_hints),
)
check(
    "推荐行裁剪函数保证不折行（按显示宽度）",
    all(
        fmt._hint_cells("※ 推荐刷取：" + " ｜ ".join(fmt._fit_hints(v)))
        <= fmt._HINT_MAX_CELLS * 2 + 20
        for v in _hints.values()
    ),
)

# ------------------------------------------- ⑤ 单查卡与部件出处也要中文
print("\n=== ⑤ 单查 / 部件出处 ===")
_obj = _make_obj([])
_one = asyncio.run(_obj._h_relic(_Parsed(content="古纪A1"), None, "pc"))
check("单查卡标题是中文（遗物：古纪 A1）", _one.title == "遗物：古纪 A1", _one.title)
check("单查卡给出可掉落状态", any("遗物" in x for x in _one.lines), str(_one.lines[:3]))

# 部件反查：relic_inverse 里存的是「后纪 Axi V12」这种带英文的键
_inv = json.loads((ROOT / "core" / "data" / "relic_inverse.json").read_text(encoding="utf-8"))
_piece = next(k for k, v in _inv.items() if v)
_two = asyncio.run(_make_obj([])._h_relic(_Parsed(content=_piece), None, "pc"))
check(
    f"部件出处行里的遗物名是中文（{_piece}）",
    all(
        "Lith" not in x and "Meso" not in x and "Neo" not in x and "Axi" not in x
        for x in _two.lines
    ),
    str(_two.lines[:3]),
)

# 自动补 Prime：用户输入通常不带 Prime。旧实现把 Prime 插在「第一个部件类型字」
# 之前 —— 而武器名里也可能含这些字（席尔**火枪**枪管）→ 插成
# 「席尔火Prime枪枪管」永远查不到。现在穷举插入位置。
for _name, _want in (
    ("席尔火枪枪管", "席尔火枪Prime枪管"),
    ("布莱顿枪机", "布莱顿Prime枪机"),
    ("绝路枪管", "绝路Prime枪管"),
):
    _r = asyncio.run(_make_obj([])._h_relic(_Parsed(content=_name), None, "pc"))
    # 键形态与拼写一致即可（2026-09-24 起部件名统一为官方形态「席尔火枪 Prime 枪管」，带空格）
    check(
        f"部件反查自动补 Prime（{_name} → {_want}）",
        _want.replace(" ", "") in str(_r.title).replace(" ", ""),
        str(_r.title),
    )

# ---------------------------------------------------------------------------
# ⑥ 中文黑话 + 部件词（2026-09-25 玩家反馈：「遗物 水晶p 蓝图」无结果）
#    aliases 词库（水晶甲/水晶/水晶p → citrine_prime_set）+ 部件词 →
#    拼「Citrine Prime <部件词>」查 inverse；p/prime 允许跟在词库键后面。
# ---------------------------------------------------------------------------
_hl_cases = [
    ("水晶p 蓝图", "Citrine Prime 蓝图"),
    ("水晶p蓝图", "Citrine Prime 蓝图"),
    ("水晶甲 系统蓝图", "Citrine Prime 系统蓝图"),
    ("水晶甲p 蓝图", "Citrine Prime 蓝图"),
    ("水晶甲prime 机体蓝图", "Citrine Prime 机体蓝图"),
    ("水晶 头部神经光元蓝图", "Citrine Prime 头部神经光元蓝图"),
]
for _q, _want in _hl_cases:
    _r = asyncio.run(_make_obj([])._h_relic(_Parsed(content=_q), None, "pc"))
    check(
        f"黑话部件反查（{_q} → {_want}）",
        _want.replace(" ", "") in str(_r.title).replace(" ", ""),
        str(_r.title),
    )
_r = asyncio.run(_make_obj([])._h_relic(_Parsed(content="水晶p 枪管"), None, "pc"))
_blob = str(_r.title) + "".join(_r.lines) + str(getattr(_r, "raw_text", "") or "")
check("黑话 + 不存在的部件词 → 不误报（提示未找到）", "未找到" in _blob, _blob[:80])

# ---------------------------------------------------------------------------
# ⑦ 档位归一与单档位列表（2026-09-25 资料会话交接：「遗物 先锋 C1 / 遗物 先锋」
#    查不出）。根因：_norm_relic 档位表本地硬编 5 档（缺先锋/全能），且代号只认
#    字母+数字——安魂档的罗马数字（I..IV）与词式（Eterna）同样全查不出。
#    修法：档位统一取 core/parser.TIER_CN（_relic_tier_en）。
# ---------------------------------------------------------------------------
for _src, _want in (
    ("先锋C1", "先锋 Vanguard C1"),
    ("先锋 C1", "先锋 Vanguard C1"),
    ("全能S1", "全能 Omnia S1"),
    ("安魂I", "安魂 Requiem I"),
    ("安魂 I", "安魂 Requiem I"),
    ("安魂 IV", "安魂 Requiem IV"),
    ("安魂 Eterna", "安魂 Requiem Eterna"),
    ("后纪A2", "后纪 Axi A2"),
    ("古纪 A1", "古纪 Lith A1"),
):
    _got = plugin.WarframeSDJK._norm_relic(_src)
    check(f"_norm_relic({_src!r}) == {_want!r}", _got == _want, _got)

for _q, _want in (
    ("先锋 C1", "遗物：先锋 C1"),
    ("先锋C1", "遗物：先锋 C1"),
    ("安魂 I", "遗物：安魂 I"),
    ("安魂 Eterna", "遗物：安魂 ETERNA"),
):
    _r = asyncio.run(_make_obj([])._h_relic(_Parsed(content=_q), None, "pc"))
    check(
        f"单查（{_q}）→ {_want}",
        _want.replace(" ", "") in str(_r.title).replace(" ", ""),
        str(_r.title),
    )

# 先锋不在官方任务掉落表 → 不给「已入库」误导（数据事实：drops.json 无 vanguard）
_r = asyncio.run(_make_obj([])._h_relic(_Parsed(content="先锋 C1"), None, "pc"))
_body = "\n".join(_r.lines)
check("先锋 C1 状态：不判「已入库」", "已入库" not in _body, _body[-90:])
check("先锋 C1 状态：写明「不在官方任务掉落表」", "不在官方任务掉落表" in _body, _body[-90:])
# 真入库的遗物仍显示「已入库」（回归）
_r = asyncio.run(_make_obj([])._h_relic(_Parsed(content="古纪 A1"), None, "pc"))
check("古纪 A1（真入库）仍显示「已入库」", "已入库" in "\n".join(_r.lines), str(_r.lines[-1]))

# 单档位词 → 该档位遗物一览（复用列表卡）
_r = asyncio.run(_make_obj([])._h_relic(_Parsed(content="先锋"), None, "pc"))
_blob2 = str(_r.title) + "\n".join(_r.lines)
check(
    "「遗物 先锋」→ 列表中含 C1/E1/M1/P1 四把",
    all(x in _blob2 for x in ("C1", "E1", "M1", "P1")),
    str(_r.title),
)
_r = asyncio.run(_make_obj([])._h_relic(_Parsed(content="后纪"), None, "pc"))
check("「遗物 后纪」→ 列表卡（既有单档位词同样受益）", "遗物列表" in str(_r.title), str(_r.title))

# ---------------------------------------------------------------------------
# 部件反查卡的「能不能获取 + 推荐位置」（2026-09-18 玩家反馈）
# ---------------------------------------------------------------------------

# 归一必须把 relic_inverse 的中文纪元写法（「古纪 Lith A12」）翻成掉落库键
# （「lith a12」）。直接拿名字去比对会把 34 把可掉落遗物全判成「已入库」。
check(
    "relic_en_key：中文纪元写法能对上掉落库键 古纪 Lith A12 → lith a12",
    fmt.relic_en_key("古纪 Lith A12") == "lith a12",
    fmt.relic_en_key("古纪 Lith A12"),
)
check(
    "relic_en_key：纯中文「古纪 A12」同样归一",
    fmt.relic_en_key("古纪 A12") == "lith a12",
    fmt.relic_en_key("古纪 A12"),
)
check(
    "relic_en_key：英文写法「Axi S20 Relic」归一",
    fmt.relic_en_key("Axi S20 Relic") == "axi s20",
    fmt.relic_en_key("Axi S20 Relic"),
)
check(
    "relic_en_key：认不出的返回空串（不猜）",
    fmt.relic_en_key("Requiem Eterna Relic") == "" and fmt.relic_en_key("垃圾") == "",
    fmt.relic_en_key("垃圾"),
)

_uv_keys = drops_db.droppable_keys()
check("可掉落遗物键集合非空（34 把左右）", 30 <= len(_uv_keys) <= 40, str(len(_uv_keys)))


def _piece_rows(piece: str) -> list[dict]:
    """与 main._h_relic 部件分支同口径：掉落表三态。"""
    out = []
    for o in _inv.get(piece) or []:
        key = fmt.relic_en_key(o.get("relic") or "")
        out.append(
            {
                "relic": o.get("relic"),
                "rarity": o.get("rarity"),
                "state": "drop" if key and key in _uv_keys else "vaulted",
            }
        )
    return out


# 混合状态部件：**动态挑**「部分遗物可掉落」的样本——每轮官方轮换后自动适配，
# 不再硬编码「某某部件 1+2」（2026-09-24 重建器上线后改;旧夹具随轮换反复失效）。
def _state_counts(piece: str) -> tuple[int, int]:
    rows = _piece_rows(piece)
    drop = sum(1 for r in rows if r["state"] == "drop")
    return drop, len(rows) - drop


_sample = next(
    p
    for p in sorted(_inv)
    if len(_inv[p]) <= 6 and _state_counts(p)[0] >= 1 and _state_counts(p)[1] >= 1
)
_drop_n, _vault_n = _state_counts(_sample)
_srows = _piece_rows(_sample)
_stitle, _slines = fmt.fmt_relic_piece(_sample, _srows, farm_hints=_hints)
check(
    f"部件卡标题带可获取/已入库计数（{_sample} = {_drop_n}+{_vault_n}）",
    # ★ 改版：计数在卡首答案行「◆ 可获取：N 把｜已入库 M 把」
    f"可获取：{_drop_n} 把" in _slines[0] and f"已入库 {_vault_n} 把" in _slines[0],
    _slines[0],
)
check(
    "★★ 卡面写明「能不能获取」：可掉落的行出现「可掉落」",
    any("可掉落" in x for x in _slines),
    str(_slines),
)
check(
    "★★ 卡面写明「能不能获取」：入库的行出现「已入库」",
    any("已入库" in x for x in _slines),
    str(_slines),
)
_srow0 = [x for x in _slines if x.startswith("· ")][0]  # 改版：首条「· 遗物　槽位　状态」数据行
check("可掉落的遗物排在最前（用户先看到能刷的）", "可掉落" in _srow0, _srow0)
check(
    "★★ 给出「推荐刷取」位置（只对可掉落的纪元给）",
    any("推荐刷取" in x for x in _slines),
    str([x for x in _slines if x.startswith("※")]),
)
check(
    "推荐刷取行不带全角空格（会污染列宽、导致放弃列对齐）",
    all("　" not in x for x in _slines if x.startswith("※")),
    str([x for x in _slines if x.startswith("※")]),
)
check(
    "推荐刷取点来自数据（排除储藏库等非刷取目标）",
    all("储藏库" not in x and "比邻星域" not in x for x in _slines),
    str(_slines),
)

# 全部入库的部件：只给原因，不给推荐位置（给了是误导）
_allvaulted = next(
    p for p, v in _inv.items() if v and not any(fmt.relic_en_key(o["relic"]) in _uv_keys for o in v)
)
_arows = _piece_rows(_allvaulted)
_atitle, _alines = fmt.fmt_relic_piece(_allvaulted, _arows, farm_hints=_hints)
check(f"全部入库的部件（{_allvaulted}）卡首写明 可获取 0", "可获取：0 把" in _alines[0], _alines[0])
check(
    "全部入库时**不**出现推荐刷取（刷不到，给了是误导）",
    not any("推荐刷取" in x for x in _alines),
    str(_alines),
)
check(
    "全部入库时给出「都已入库…刷不到」的说明",
    any("已入库" in x and "刷不到" in x for x in _alines),
    str([x for x in _alines if x.startswith("※")]),
)

# 超大列表（Forma 蓝图 545 把）：截断提示必须说实话 ——
# 隐藏的那批里可能还有可掉落的，不能一律说成「已入库」。
_big = max(_inv, key=lambda k: len(_inv[k]))
_brows = _piece_rows(_big)
_btitle, _blines = fmt.fmt_relic_piece(_big, _brows, farm_hints=_hints)
# ★ 不在测试里重写排序逻辑（排序规则一改这里就会假失败）：只校验提示文本
#   自己说得通 —— 隐藏总数对得上，且「还能掉落」的把数在 1..隐藏 之间。
_m = None
_hidden = len(_brows) - fmt._PIECE_MAX_ROWS
for _x in _blines:
    _m = _m or re.search(r"还有 (\d+) 把未列出（其中 (\d+) 把现在就能掉落）", _x)
check(
    f"超大部件（{_big}，{len(_brows)} 把）行数被截断到 {fmt._PIECE_MAX_ROWS}",
    sum(1 for x in _blines if x.startswith("· ")) == fmt._PIECE_MAX_ROWS,
    str(sum(1 for x in _blines if x.startswith("· "))),
)
check(
    "截断提示不会把可掉落的藏起来（写明还有几把能掉落）",
    bool(_m) and int(_m.group(1)) == _hidden and 1 <= int(_m.group(2)) <= _hidden,
    str([x for x in _blines if x.startswith("※")]),
)

# 全量：607 个部件的卡面都不能出现折行（注脚宽度按 25px 实测）
# ★ 字体缺失时优雅跳过：开源包**不带** Noto 字库（40MB 会让 zip 超过插件市场
#   16MB 上限，改为 scripts/fetch_font.py 按需下载）。少了这个判断，
#   别人 clone 后跑测试会直接 OSError: cannot open resource。
from PIL import ImageFont, ImageDraw, Image  # noqa: E402

_TTC = ROOT / "core" / "data" / "fonts" / "NotoSansCJK-Regular.ttc"
if not _TTC.exists():
    print(
        "[SKIP] 未找到渲染字体（开源包默认不带）："
        "跑 `python scripts/fetch_font.py` 可下载；"
        "本次跳过「注脚折行宽度」断言（该断言依赖字体度量）"
    )
else:
    _nf = ImageFont.truetype(str(_TTC), 25)
    _nd = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    _over = []
    for _p, _orig in _inv.items():
        _rows = [
            {
                "relic": o["relic"],
                "rarity": o["rarity"],
                "state": "drop" if fmt.relic_en_key(o["relic"]) in _uv_keys else "vaulted",
            }
            for o in _orig
        ]
        _t, _ls = fmt.fmt_relic_piece(_p, _rows, farm_hints=_hints)
        for _ln in _ls:
            if _ln.startswith("※"):
                _w = _nd.textlength(_ln.lstrip("※").strip(), font=_nf)
                if _w > 1500 - 178:
                    _over.append((_p, round(_w), _ln[:50]))
    check(f"全量 {len(_inv)} 个部件：注脚没有一行会折行", not _over, str(_over[:3]))

# ★★ 维护者明确要的行为（2026-09-18）：「出库的时候另外两个多半入库了，到时候
#   下面还是一个推荐，保持底下永远是出库的那个推荐刷新位置就行」。
#   → 推荐行必须**恰好**对应「可掉落遗物所属的纪元」：
#     已入库的纪元不给（刷不到，给了是误导），可掉落的纪元不能漏。
#   对全部 607 个部件逐条核对，防止以后有人改成「按第一个纪元给」或「都给」。
_bad_pairs = []
_mixed = 0
for _p, _orig in _inv.items():
    _rows = [
        {
            "relic": o["relic"],
            "rarity": o["rarity"],
            "state": "drop" if fmt.relic_en_key(o["relic"]) in _uv_keys else "vaulted",
        }
        for o in _orig
    ]
    if not _rows:
        continue
    _t, _ls = fmt.fmt_relic_piece(_p, _rows, farm_hints=_hints)
    _tiers_drop = {fmt.relic_cn(o["relic"]).split()[0] for o in _rows if o["state"] == "drop"}
    # 从「※ 推荐刷取（XX）：」里取出纪元名
    _tiers_hint = {
        x[len("※ 推荐刷取（") :].split("）")[0] for x in _ls if x.startswith("※ 推荐刷取（")
    }
    if _tiers_drop != _tiers_hint:
        _bad_pairs.append((_p, sorted(_tiers_drop), sorted(_tiers_hint)))
    if 0 < len(_tiers_drop) < len({fmt.relic_cn(o["relic"]).split()[0] for o in _rows}):
        _mixed += 1
check(
    f"★★ 推荐行只给「可掉落」的纪元，且一个不漏（全量 {len(_inv)} 个部件）",
    not _bad_pairs,
    str(_bad_pairs[:3]),
)
check(f"抽检到 {_mixed} 个「部分纪元出库」的部件（就是玩家说的那种情况）", _mixed > 0, str(_mixed))

# 已入库纪元的点位绝不能出现（构造一个：把某部件全标成入库再对比）
_fake_allvault = [
    {"relic": o["relic"], "rarity": o["rarity"], "state": "vaulted"} for o in _inv[_allvaulted]
]
_ft, _fls = fmt.fmt_relic_piece(_allvaulted, _fake_allvault, farm_hints=_hints)
check(
    "全标入库时，即使传了 farm_hints 也一条推荐都不给",
    not any("推荐刷取" in x for x in _fls),
    str(_fls),
)

# 渲染期：列对齐（_table_mode）不能被「卡宽安全阀」关掉。
# 卡宽按注脚实际字号（25px）量之后整体收窄过（部件卡 1416→1211px），
# 必须确认收窄没有把安全阀挤爆 —— 关掉就退回「单元格顺序拼接=不对齐」。
try:
    from core import render as _R  # noqa: E402

    _out = ROOT / "runtime"
    _out.mkdir(exist_ok=True)
    _idx = json.loads((ROOT / "core" / "data" / "relic_index.json").read_text(encoding="utf-8"))
    _out_rows = [
        {"cn": k, "tier_cn": k.split()[0]}
        for k in _idx
        if len(k.split()) >= 3 and fmt.relic_en_key(k) in _uv_keys
    ][:40]
    for _name, _t, _ls in (
        ("部件卡", f"部件出处：{_sample}", _slines),
        (
            "出库卡",
            "遗物出库（当前可掉落）",
            fmt.fmt_relic_by_tier(
                _out_rows, "遗物出库（当前可掉落）", page=1, page_size=90, farm_hints=_hints
            )[1],
        ),
    ):
        _r = _R.ImageRenderer(_out)
        if not _r.available:
            continue
        _r.render(_t, _ls, "国际服")
        check(
            f"{_name}渲染后仍走列对齐（_table_mode 未被安全阀关掉）",
            bool(getattr(_r, "_table_mode", False)),
            "",
        )
except Exception as _exc:  # noqa: BLE001
    check("渲染期列对齐检查（渲染器不可用时跳过）", True, f"{type(_exc).__name__}: {_exc}")

# ---------------------------------------------------------------------------
# 未命中分支（交接稿 §二-2）：文案必须以「未找到「」开头且带用法常量
# ---------------------------------------------------------------------------
# ★ 样本必须**结构性不可能命中**：旧样本「遗物 虚无」后来撞上了真条目
#   （虚无 = Void 系），断言随之静默失效 —— 别再拿像真名的词当样本。
_nf = asyncio.run(_make_obj([])._h_relic(_Parsed(content="乱写zzq9"), None, "pc"))
_nf_txt = _txt(_nf)
check(
    "未命中：走报错卡（原因 + 怎么改）",
    _nf.raw_text is None and "原因" in _nf_txt and "怎么改" in _nf_txt,
    repr(_nf)[:80],
)
check("未命中：原因行含「未找到「」", "未找到「" in _nf_txt, _nf_txt[:60])
check(
    "未命中：带上用法常量 RELIC_USAGE（告诉用户下一步能发什么）",
    RELIC_USAGE.removeprefix("用法：") in _nf_txt,
    _nf_txt[:160],
)
check(
    "未命中：顺序固定「未找到 → 你是不是想找 → 用法」",
    _nf_txt.index("未找到") < _nf_txt.index(RELIC_USAGE.removeprefix("用法：")),
    _nf_txt[:160],
)
# 反向：命中的卡不能捎带用法串（否则每次查遗物都被塞一段用法）
_ok = asyncio.run(_make_obj([])._h_relic(_Parsed(content="古纪 A1"), None, "pc"))
_ok_txt = str(getattr(_ok, "raw_text", "") or "") + "\n".join(_ok.lines)
check("反向守卫：命中时不出用法串", RELIC_USAGE not in _ok_txt, _ok_txt[:120])


# ---------------------------------------------------------------------------
# ⑨ C1 名称归一化（2026-10-03）：大小写/空格/中点无关 + 英文名→中文名。
#   线上实证：`部件 阿索代prime` → 未找到（库里是「阿索代 Prime 蓝图」）；
#   `部件 afentis prime 蓝图` 亦查不到（索引键全中文名，英文名不在表内）。
# ---------------------------------------------------------------------------
def _blob(r):
    return (
        str(getattr(r, "title", "") or "")
        + "|"
        + "|".join(getattr(r, "lines", None) or [])
        + "|"
        + str(getattr(r, "raw_text", "") or "")
    )


_r9a = asyncio.run(_make_obj([])._h_relic(_Parsed(content="阿索代prime"), None, "pc"))
_r9b = asyncio.run(_make_obj([])._h_relic(_Parsed(content="阿索代 Prime"), None, "pc"))
_r9c = asyncio.run(_make_obj([])._h_relic(_Parsed(content="athodai"), None, "pc"))


def _cand(r):
    """取候选段（「你是不是想找：…」之后）——回显的原始输入本就该各自不同。"""
    b = _blob(r)
    return b.split("你是不是想找：")[-1] if "你是不是想找" in b else b


check(
    "⑨ 阿索代prime ≡ 阿索代 Prime ≡ athodai（三者候选集一致）",
    _cand(_r9a) == _cand(_r9b) == _cand(_r9c),
    f"{_cand(_r9a)[:80]} / {_cand(_r9b)[:80]} / {_cand(_r9c)[:80]}",
)
check(
    "⑨ 三者都给「阿索代 Prime」候选（不静默）",
    all("阿索代 Prime" in _blob(r) for r in (_r9a, _r9b, _r9c)),
    _blob(_r9a)[:120],
)

_r9d = asyncio.run(_make_obj([])._h_relic(_Parsed(content="afentis prime 蓝图"), None, "pc"))
_r9e = asyncio.run(_make_obj([])._h_relic(_Parsed(content="圣英 Prime 蓝图"), None, "pc"))
check(
    "⑨ afentis prime 蓝图 ≡ 圣英 Prime 蓝图（英文名→中文名解析）",
    _blob(_r9d) == _blob(_r9e) and "圣英 Prime 蓝图" in _blob(_r9d),
    f"{_blob(_r9d)[:100]} vs {_blob(_r9e)[:100]}",
)

_r9f = asyncio.run(_make_obj([])._h_relic(_Parsed(content="阿索代 蓝图"), None, "pc"))
check(
    "⑨ 回归：既有中文查询「阿索代 蓝图」仍出部件卡",
    "阿索代 Prime 蓝图" in _blob(_r9f) and "未找到" not in _blob(_r9f),
    _blob(_r9f)[:120],
)

# ---------------------------------------------------------------------------
# ⑩ 第十五批 R1–R7：真实 parse → handler、官方黄金样本与构建器离线护栏。
# ---------------------------------------------------------------------------
from collections import defaultdict as _defaultdict  # noqa: E402
from contextlib import redirect_stdout as _redirect_stdout  # noqa: E402
from io import StringIO as _StringIO  # noqa: E402
from unittest.mock import patch as _patch  # noqa: E402
from core.parser import parse as _parse_command, MISSION_CN as _MISSION_CN  # noqa: E402
from scripts.kb import build_relic_index as _ri  # noqa: E402
from scripts import build_drops as _bd  # noqa: E402

print("\n=== ⑩ 第十五批：奖励 / 数量 / 英文精确匹配 / 来源 ===")
# 黄金样本：DE 2026-10-07 完整档，独立冻结期望，不从生成器计算期望。
_EXPECTED_B15 = {
    "后纪 Axi B10": {
        "常见": ["Forma 蓝图", "伯斯顿 Prime 枪托", "鹦鹉螺 Prime 外壳"],
        "罕见": ["伯斯顿 Prime 枪管", "海波斯库拉对剑 Prime 握柄"],
        "稀有": ["布里斯提卡 Prime 蓝图"],
    },
    "后纪 Axi N14": {
        "常见": ["Forma 蓝图", "Hydroid Prime 机体蓝图", "野马 Prime 蓝图"],
        "罕见": ["海波斯库拉对剑 Prime 刀刃", "野马 Prime 枪管"],
        "稀有": ["鹦鹉螺 Prime 头部"],
    },
    "古纪 Lith S20": {
        "常见": ["Forma 蓝图", "布里斯提卡 Prime 下弓臂", "野马 Prime 枪机"],
        "罕见": ["Sevagoth Prime 蓝图", "葬铭 Prime 枪管"],
        "稀有": ["Sevagoth Prime 机体蓝图"],
    },
    "前纪 Meso E8": {
        "常见": ["Forma 蓝图", "Sevagoth Prime 头部神经光元蓝图", "海波斯库拉对剑 Prime 蓝图"],
        "罕见": ["Hydroid Prime 蓝图", "鹦鹉螺 Prime 系统"],
        "稀有": ["葬铭 Prime 枪机"],
    },
    "中纪 Neo H5": {
        "常见": ["Forma 蓝图", "伯斯顿 Prime 枪机", "葬铭 Prime 蓝图"],
        "罕见": ["布里斯提卡 Prime 上弓臂", "布里斯提卡 Prime 弓弦"],
        "稀有": ["Hydroid Prime 系统蓝图"],
    },
    "中纪 Neo K10": {
        "常见": ["Forma 蓝图", "Gyre Prime 系统蓝图", "野马 Prime 蓝图"],
        "罕见": ["大久和弓 Prime 下弓臂", "脉纹 Prime 蓝图"],
        "稀有": ["卡帕压力枪 Prime 枪管"],
    },
    "中纪 Neo N25": {
        "常见": ["Forma 蓝图", "Sevagoth Prime 系统蓝图", "伯斯顿 Prime 蓝图"],
        "罕见": ["Hydroid Prime 头部神经光元蓝图", "布里斯提卡 Prime 枪机"],
        "稀有": ["鹦鹉螺 Prime 蓝图"],
    },
}
for _key, _expected in _EXPECTED_B15.items():
    check(f"R1 官方黄金样本 {_key}", IDX[_key] == _expected)
check(
    "R1 普通遗物均为 3/2/1 槽位",
    all(
        [len(v.get(r, [])) for r in ("常见", "罕见", "稀有")] == [3, 2, 1]
        for k, v in IDX.items()
        if not k.startswith("安魂 ")
    ),
)
_expected_inverse = _defaultdict(list)
for _key, _slots in IDX.items():
    for _rarity, _rewards in _slots.items():
        for _name in _rewards:
            _expected_inverse[_name].append({"relic": _key, "rarity": _rarity})
check("R1 正反表逐项闭合", dict(_expected_inverse) == _inv)
check("R3 双份 Forma 与单张蓝图分键", "2X Forma 蓝图" in _inv and "Forma 蓝图" in _inv)
check(
    "R3 Axi A7 双份 Forma 不混入单张反查",
    any(x["relic"] == "后纪 Axi A7" for x in _inv["2X Forma 蓝图"])
    and not any(x["relic"] == "后纪 Axi A7" for x in _inv["Forma 蓝图"]),
)
for _query in ("Axi A2", "axi a2", "AXI A 02", "Axi A2 Relic", "后纪 Axi A2"):
    _result = asyncio.run(_make_obj([])._h_relic(_parse_command("遗物 " + _query), None, "pc"))
    check(
        f"R4 真实入口 {_query} 精确命中 A2", _result.title == "遗物：后纪 A2", _txt(_result)[:100]
    )
_query = asyncio.run(_make_obj([])._h_relic(_parse_command("遗物 Axi A2000"), None, "pc"))
check("R4 不存在的完整代号不误命中 A2", "未找到" in _txt(_query))
_query = asyncio.run(_make_obj([])._h_relic(_parse_command("遗物 2X Forma 蓝图"), None, "pc"))
check("R3 双份 Forma 真实反查入口", _query.title == "部件出处：2X Forma 蓝图", _txt(_query)[:100])
for _code in ("Axi A2", "Axi A5", "Axi M5", "Axi V8", "Neo O1"):
    _result = asyncio.run(_make_obj([])._h_relic(_parse_command("遗物 " + _code), None, "pc"))
    _text = _txt(_result)
    check(
        f"R6 {_code} 卡面显示奸商轮换来源",
        "奸商" in _text
        and "以当期货单为准" in _text
        and "已入库" not in _text
        and "◆ 获取来源" in _text,
        _text[:100],
    )
_result = asyncio.run(_make_obj([])._h_relic(_parse_command("遗物 Lith A13"), None, "pc"))
check("R6 Hemocyte 活动来源进入卡面", "Hemocyte" in _txt(_result) and "活动开放时" in _txt(_result))
_event_reply = asyncio.run(_make_obj([])._h_relic(_parse_command("遗物 Axi A21"), None, "pc"))
_event_text = _txt(_event_reply)
check("R6 Event 前缀进入卡面", "活动 · " in _event_text)
_regular_example = next(
    x
    for x in drops_db.relic_source_lines("axi a21 relic")
    if not x.startswith(("活动 · ", "奸商 · ", "敌人 · "))
)
check("R6 常规可刷地点不会被历史活动挤掉", _regular_example in _event_text)
check(
    "R5 生成器任务译名与 MISSION_CN 同源",
    all(_bd.mode_zh(k.title()) == v for k, v in _MISSION_CN.items()),
)

# 构建器微型夹具：验证官方覆盖、异常源拒写、数量保留与 --check 的双向行为。
_test_rewards = [
    ("Part" + str(i), chance, "Uncommon" if chance > 5 else "Rare")
    for i, chance in enumerate((25.33, 25.33, 25.33, 11, 11, 2))
]
_test_names = {
    "/test/" + name.lower(): "奖励" + str(i) for i, (name, _, _) in enumerate(_test_rewards)
}
_test_items = [
    {
        "name": "Axi Z999 Intact",
        "rewards": [
            {
                "item": {"name": name, "uniqueName": "/test/" + name.lower()},
                "chance": chance,
                "rarity": rar,
            }
            for name, chance, rar in _test_rewards
        ],
    }
]
_test_html = (
    '<h3 id="relicRewards">Relics:</h3><table><tr><th>Axi Z999 Relic (Intact)</th></tr>'
    + "".join(
        f"<tr><td>{name}</td><td>{rar} ({chance}%)</td></tr>" for name, chance, rar in _test_rewards
    )
    + "</table>"
)
_mini_index, _, _audit = _ri.build(_test_items, _test_html, _test_names)
# 交换两个部件的概率，只改 WFCD：结果仍须按官方页，且冲突有记录。
_wrong = json.loads(json.dumps(_test_items))
_wrong[0]["rewards"][0]["chance"], _wrong[0]["rewards"][-1]["chance"] = 2, 25.33
_official_index, _, _audit = _ri.build(_wrong, _test_html, _test_names)
check("R1 两源冲突按官方覆盖并记录", _official_index == _mini_index and bool(_audit["conflicts"]))
check(
    "R3 数量基于奖励原名，不被相同路径抹掉",
    _ri.translated("2X Part0", {"Part0": "/test/part0"}, _test_names) == "2X 奖励0",
)
for _bad_html in (
    "<html>blocked</html>",
    _test_html.replace("Part5</td><td>Rare (2%)", "Part5</td><td>invalid"),
):
    try:
        _ri.build(_test_items, _bad_html, _test_names)
        _rejected = False
    except ValueError:
        _rejected = True
    check("R1 缺表/缺奖励拒绝生成", _rejected)
with tempfile.TemporaryDirectory() as _tmp:
    _src = Path(_tmp) / "Relics.json"
    _html_file = Path(_tmp) / "official.html"
    _src.write_text(json.dumps(_test_items), encoding="utf-8")
    _html_file.write_text(_test_html, encoding="utf-8")
    _args = [
        "build_relic_index.py",
        "--src",
        str(_src),
        "--official",
        str(_html_file),
        "--out-dir",
        _tmp,
    ]
    with _patch.object(_ri, "load_names", return_value=_test_names), _redirect_stdout(_StringIO()):
        with _patch.object(sys, "argv", [*_args, "--dry-run"]):
            _dry_rc = _ri.main()
        _dry_kept = not (Path(_tmp) / "relic_index.json").exists()
        with _patch.object(sys, "argv", _args):
            _write_rc = _ri.main()
        with _patch.object(sys, "argv", [*_args, "--check"]):
            _equal_rc = _ri.main()
        _tamper_file = Path(_tmp) / "relic_inverse.json"
        _tamper_file.write_text("{}", encoding="utf-8")
        with _patch.object(sys, "argv", [*_args, "--check"]):
            _diff_rc = _ri.main()
    check("R1 --dry-run 不写产物", _dry_rc == 0 and _dry_kept)
    check("R1 --check 相等返回 0", _write_rc == 0 and _equal_rc == 0)
    check("R1 --check 不等返回非零且不覆盖", _diff_rc != 0 and _tamper_file.read_text() == "{}")

# 删除/替换逻辑双向守卫：任务旧值该移除；独立模式/非任务来源不能被误删。
_drop_html = (
    '<b>Last Update:</b> 07 October, 2026<h3 id="missionRewards">Missions:</h3>'
    "<table><tr><th>Event: Europa/Cryotic Front (Excavation)</th></tr>"
    "<tr><th>Rotation C</th></tr><tr><td>Axi Z999 Relic</td><td>Rare (6.67%)</td></tr>"
    "<tr><th>Venus/Shared (Caches)</th></tr><tr><th>Rotation B</th></tr>"
    "<tr><td>Cache Part</td><td>Rare (6.45%)</td></tr></table>"
    '<h3 id="relicByAvatar">Enemies:</h3><table><tr><th>Hemocyte</th>'
    "<th>Relic Drop Chance: 20.00%</th></tr><tr><td></td><td>Axi Z999 Relic</td>"
    "<td>Uncommon (12.91%)</td></tr></table>"
)
_drop_rows = [
    {
        "place": "Europa/Cryotic Front (Excavation), Rotation C",
        "item": "Stale Part",
        "rarity": "Rare",
        "chance": 1,
    },
    {
        "place": "Venus/Shared (Skirmish), Rotation B",
        "item": "Other Mode Part",
        "rarity": "Rare",
        "chance": 2,
    },
    {"place": "Independent Source", "item": "Independent Part", "rarity": "Rare", "chance": 3},
]
with _redirect_stdout(_StringIO()):
    _payload = _bd.build(
        _drop_rows,
        _drop_html,
        {
            "items": {"stale part": [[99, "Rare", 1]]},
            "places": {"0": "金星 · Shared · 遭遇战 · B轮"},
        },
    )
check("R2 过期来源行移除，不续传旧 items 值", _payload["items"]["stale part"] == [])
check("R2 非任务来源保留", len(_payload["items"]["independent part"]) == 1)
_pids = {rows[0][0] for name, rows in _payload["items"].items() if rows}
check("R2 不同模式/新地点不共用 pid", len(_pids) == 4 and len(_payload["places"]) == 4)
check(
    "R6 官方 Event 前缀保留且任务补丁幂等",
    "活动 · 木卫二 · Cryotic Front · 挖掘 · C轮" in _payload["places"].values(),
)
with _redirect_stdout(_StringIO()):
    _again = _bd.build(_drop_rows, _drop_html, _payload)
# 不同上游键可能译成同一显示标签（如 Exterminate/Extermination）；仍需稳定保留各自 pid。
_duplicate_modes = [
    {
        "place": f"Venus/Twin ({mode}), Rotation B",
        "item": f"Part {i}",
        "rarity": "Rare",
        "chance": 1,
    }
    for i, mode in enumerate(("Exterminate", "Extermination"))
]
with _redirect_stdout(_StringIO()):
    _duplicates = _bd.build(_duplicate_modes, _drop_html, {})
    _duplicates_again = _bd.build(_duplicate_modes, _drop_html, _duplicates)
check("R2 重复显示标签的不同模式编号仍幂等", _duplicates == _duplicates_again)
check("R2 重跑编号与数据幂等", _again == _payload)
check("R6 Hemocyte 补充来源来自官方表", "axi z999 relic" in _payload["relic_special_sources"])
_saved_db = drops_db._DB
try:
    drops_db._DB = {
        "places": {"0": "活动 · 木卫二 · Cryotic Front · 挖掘 · C轮"},
        "relic_drops": {"axi z999 relic": [[0, "Rare", 6.67]]},
    }
    check("R6 仅活动来源不当作常驻出库", "axi z999 relic" not in drops_db.unvaulted_relics())
    drops_db._DB["places"]["1"] = "虚空 · Mot · 生存 · C轮"
    drops_db._DB["relic_drops"]["axi z999 relic"].append([1, "Rare", 6.67])
    check("R6 活动+常规双来源仍可常驻出库", "axi z999 relic" in drops_db.unvaulted_relics())
finally:
    drops_db._DB = _saved_db
check("R2 辐射遗物合并到基名", _bd.base_relic_key("Lith Q3 Relic (Radiant)") == "lith q3 relic")
check(
    "R2 非遗物条目不混入遗物池",
    _bd.base_relic_key("Syndicate Relic Pack") is None
    and _bd.base_relic_key("Eidolon Relic") is None,
)
check(
    "R7 安魂 Eterna 保留罕见分槽等待裁决",
    IDX["安魂 Requiem Eterna"]
    == {"罕见": ["Fass", "Jahu", "Khra", "Lohk", "Netra", "Ris", "Vome", "Xata"]},
)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
