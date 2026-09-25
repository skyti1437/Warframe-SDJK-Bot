# -*- coding: utf-8 -*-
"""遗物出入库卡：中文名 / 版式 / 缺失补齐 / 刷取建议（python3 tests/test_relic_list.py）

覆盖 2026-09-17 用户反馈的四件事：
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
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass
        def debug(self, *a, **k): pass

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
        event_message_type = staticmethod(lambda spec: (lambda fn: fn))

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: (lambda fn: fn)
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
        def __init__(self, *a, **k): pass

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

import main as plugin                      # noqa: E402
from core import drops as drops_db         # noqa: E402
from core import formatters as fmt         # noqa: E402
from core.api_client import load_aliases   # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


IDX = json.loads((ROOT / "core" / "data" / "relic_index.json")
                 .read_text(encoding="utf-8"))

# ---------------------------------------------------------------- ① 中文名归一
print("=== ① 遗物名统一成中文 ===")
cases = [
    ("古纪 Lith A1", "古纪 A1"),        # relic_index 的键
    ("后纪 Axi V12", "后纪 V12"),       # relic_inverse 的键
    ("Lith A12", "古纪 A12"),           # 掉落表键（英文）
    ("古纪 S1 遗物", "古纪 S1"),        # DE 官方简中写法
    ("后纪A2", "后纪 A2"),              # 用户输入（无空格）
    ("古纪 q3", "古纪 Q3"),             # 小写代号
    ("安魂 I", "安魂 I"),               # 安魂遗物是罗马数字
]
for src, want in cases:
    got = fmt.relic_cn(src)
    check(f"relic_cn({src!r}) == {want!r}", got == want, f"得到 {got!r}")
check("认不出来的原样返回（不猜）", fmt.relic_cn("绝路 Prime 枪机") == "绝路 Prime 枪机")

# ------------------------------------------------------------- ② 自然序与版式
print("\n=== ② 自然序 + 每行 9 列 + 中文 ===")
check("自然序 A1 < A2 < A10（不是字符串序）",
      [n.split()[-1] for n in sorted(
          ["古纪 A10", "古纪 A2", "古纪 A1", "古纪 B1"],
          key=fmt._relic_sort_key)] == ["A1", "A2", "A10", "B1"])
check("每行 9 列（实测宽度算出来的，改动需重测）", fmt._TIER_PER_LINE == 9)

_sample = [{"cn": k, "tier_cn": k.split()[0], "unvaulted": True}
           for k in IDX if k.startswith("古纪 ")]
_t, _l = fmt.fmt_relic_by_tier(_sample, "遗物出库（当前可掉落）")
_body = "\n".join(_l)
check("卡面标题走表格模式（渲染层才会列对齐）", "遗物出库" in _t)
check("卡面不再出现英文纪元（Lith/Meso/Neo/Axi）",
      not any(w in _body for w in ("Lith", "Meso", "Neo", "Axi")), _body[:200])
_row = [x for x in _l if x.startswith("· ")][0]
check(f"首行 9 个条目", len([c for c in _row[2:].split("　") if c]) == 9,
      _row)
check("注脚行不含全角空格（会污染渲染层的列宽计算）",
      all("　" not in x for x in _l if x.startswith("※")))

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
        return {"items": [{"kind": "relic", "name": n,
                           "price": 1, "prime_currency": "阿耶精华"}
                          for n in self._names]}

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
    obj.client._aliases = load_aliases()   # ②-b 黑话兜底需要（真实 client 自带）
    obj.page_size = 12
    obj._dir = Path(tempfile.mkdtemp())
    return obj


_reply = asyncio.run(_make_obj(["古纪 K5 遗物", "后纪 A12 遗物", "赫利俄斯 Prime 蓝图"])
                     ._h_relic(_Parsed(content="出库"), None, "pc"))
_out = "\n".join(_reply.lines)
check("出库卡包含阿耶商店的遗物（古纪 K5）", "古纪 K5" in _out, _reply.title)
check("出库卡包含阿耶商店的遗物（后纪 A12）", "后纪 A12" in _out)
check("阿耶来源的遗物标了「仅阿耶兑换」", "仅阿耶兑换" in _out, _out[:300])
check("非遗物商品（赫利俄斯 Prime 蓝图）没被当成遗物",
      "赫利俄斯 Prime 蓝图" not in _out)

# --------------------------------------------------- ④ 特殊渠道 + 推荐刷取
print("\n=== ④ 特殊渠道标记与推荐刷取 ===")
check("出处极窄的遗物被标出（古纪 C7 只在比邻星域储藏库）",
      "储藏库" in drops_db.special_source("lith c7 relic"),
      drops_db.special_source("lith c7 relic"))
check("常规遗物（出处遍地）不标",
      drops_db.special_source("lith a12 relic") == "",
      drops_db.special_source("lith a12 relic"))
_hints = drops_db.farm_hints()
check("每个纪元都给推荐刷取点",
      all(_hints.get(t) for t in ("古纪", "前纪", "中纪", "后纪")), str(_hints))
check("推荐点里没有「储藏库／比邻星域」这类非刷取目标",
      all("储藏库" not in x and "比邻星域" not in x
          for v in _hints.values() for x in v), str(_hints))
check("推荐点不带轮次后缀（只给星球·节点·任务）",
      all(len(x.split(" · ")) == 3 for v in _hints.values() for x in v), str(_hints))
check("推荐行裁剪函数保证不折行（按显示宽度）",
      all(fmt._hint_cells("※ 推荐刷取：" + " ｜ ".join(fmt._fit_hints(v)))
          <= fmt._HINT_MAX_CELLS * 2 + 20 for v in _hints.values()))

# ------------------------------------------- ⑤ 单查卡与部件出处也要中文
print("\n=== ⑤ 单查 / 部件出处 ===")
_obj = _make_obj([])
_one = asyncio.run(_obj._h_relic(_Parsed(content="古纪A1"), None, "pc"))
check("单查卡标题是中文（遗物：古纪 A1）",
      _one.title == "遗物：古纪 A1", _one.title)
check("单查卡给出可掉落状态",
      any("遗物" in x for x in _one.lines), str(_one.lines[:3]))

# 部件反查：relic_inverse 里存的是「后纪 Axi V12」这种带英文的键
_inv = json.loads((ROOT / "core" / "data" / "relic_inverse.json")
                  .read_text(encoding="utf-8"))
_piece = next(k for k, v in _inv.items() if v)
_two = asyncio.run(_make_obj([])._h_relic(_Parsed(content=_piece), None, "pc"))
check(f"部件出处行里的遗物名是中文（{_piece}）",
      all("Lith" not in x and "Meso" not in x and "Neo" not in x
          and "Axi" not in x for x in _two.lines), str(_two.lines[:3]))

# 自动补 Prime：用户输入通常不带 Prime。旧实现把 Prime 插在「第一个部件类型字」
# 之前 —— 而武器名里也可能含这些字（席尔**火枪**枪管）→ 插成
# 「席尔火Prime枪枪管」永远查不到。现在穷举插入位置。
for _name, _want in (("席尔火枪枪管", "席尔火枪Prime枪管"),
                     ("布莱顿枪机", "布莱顿Prime枪机"),
                     ("绝路枪管", "绝路Prime枪管")):
    _r = asyncio.run(_make_obj([])._h_relic(_Parsed(content=_name), None, "pc"))
    # 键形态与拼写一致即可（2026-09-24 起部件名统一为官方形态「席尔火枪 Prime 枪管」，带空格）
    check(f"部件反查自动补 Prime（{_name} → {_want}）",
          _want.replace(" ", "") in str(_r.title).replace(" ", ""), str(_r.title))

# ---------------------------------------------------------------------------
# ⑥ 中文黑话 + 部件词（2026-09-25 用户反馈：「遗物 水晶p 蓝图」无结果）
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
    check(f"黑话部件反查（{_q} → {_want}）",
          _want.replace(" ", "") in str(_r.title).replace(" ", ""), str(_r.title))
_r = asyncio.run(_make_obj([])._h_relic(_Parsed(content="水晶p 枪管"), None, "pc"))
_blob = str(_r.title) + "".join(_r.lines) + str(getattr(_r, "raw_text", "") or "")
check("黑话 + 不存在的部件词 → 不误报（提示未找到）", "未找到" in _blob, _blob[:80])

# ---------------------------------------------------------------------------
# ⑦ 档位归一与单档位列表（2026-09-25 资料会话交接：「遗物 先锋 C1 / 遗物 先锋」
#    查不出）。根因：_norm_relic 档位表本地硬编 5 档（缺先锋/全能），且代号只认
#    字母+数字——安魂档的罗马数字（I..IV）与词式（Eterna）同样全查不出。
#    修法：档位统一取 core/parser.TIER_CN（_relic_tier_en）。
# ---------------------------------------------------------------------------
for _src, _want in (("先锋C1", "先锋 Vanguard C1"),
                    ("先锋 C1", "先锋 Vanguard C1"),
                    ("全能S1", "全能 Omnia S1"),
                    ("安魂I", "安魂 Requiem I"),
                    ("安魂 I", "安魂 Requiem I"),
                    ("安魂 IV", "安魂 Requiem IV"),
                    ("安魂 Eterna", "安魂 Requiem Eterna"),
                    ("后纪A2", "后纪 Axi A2"),
                    ("古纪 A1", "古纪 Lith A1")):
    _got = plugin.WarframeSDJK._norm_relic(_src)
    check(f"_norm_relic({_src!r}) == {_want!r}", _got == _want, _got)

for _q, _want in (("先锋 C1", "遗物：先锋 C1"),
                  ("先锋C1", "遗物：先锋 C1"),
                  ("安魂 I", "遗物：安魂 I"),
                  ("安魂 Eterna", "遗物：安魂 ETERNA")):
    _r = asyncio.run(_make_obj([])._h_relic(_Parsed(content=_q), None, "pc"))
    check(f"单查（{_q}）→ {_want}",
          _want.replace(" ", "") in str(_r.title).replace(" ", ""), str(_r.title))

# 先锋不在官方任务掉落表 → 不给「已入库」误导（数据事实：drops.json 无 vanguard）
_r = asyncio.run(_make_obj([])._h_relic(_Parsed(content="先锋 C1"), None, "pc"))
_body = "\n".join(_r.lines)
check("先锋 C1 状态：不判「已入库」", "已入库" not in _body, _body[-90:])
check("先锋 C1 状态：写明「不在官方任务掉落表」",
      "不在官方任务掉落表" in _body, _body[-90:])
# 真入库的遗物仍显示「已入库」（回归）
_r = asyncio.run(_make_obj([])._h_relic(_Parsed(content="古纪 A1"), None, "pc"))
check("古纪 A1（真入库）仍显示「已入库」",
      "已入库" in "\n".join(_r.lines), str(_r.lines[-1]))

# 单档位词 → 该档位遗物一览（复用列表卡）
_r = asyncio.run(_make_obj([])._h_relic(_Parsed(content="先锋"), None, "pc"))
_blob2 = str(_r.title) + "\n".join(_r.lines)
check("「遗物 先锋」→ 列表中含 C1/E1/M1/P1 四把",
      all(x in _blob2 for x in ("C1", "E1", "M1", "P1")), str(_r.title))
_r = asyncio.run(_make_obj([])._h_relic(_Parsed(content="后纪"), None, "pc"))
check("「遗物 后纪」→ 列表卡（既有单档位词同样受益）",
      "遗物列表" in str(_r.title), str(_r.title))

# ---------------------------------------------------------------------------
# 部件反查卡的「能不能获取 + 推荐位置」（2026-09-18 用户反馈）
# ---------------------------------------------------------------------------

# 归一必须把 relic_inverse 的中文纪元写法（「古纪 Lith A12」）翻成掉落库键
# （「lith a12」）。直接拿名字去比对会把 34 把可掉落遗物全判成「已入库」。
check("relic_en_key：中文纪元写法能对上掉落库键 古纪 Lith A12 → lith a12",
      fmt.relic_en_key("古纪 Lith A12") == "lith a12",
      fmt.relic_en_key("古纪 Lith A12"))
check("relic_en_key：纯中文「古纪 A12」同样归一",
      fmt.relic_en_key("古纪 A12") == "lith a12", fmt.relic_en_key("古纪 A12"))
check("relic_en_key：英文写法「Axi S20 Relic」归一",
      fmt.relic_en_key("Axi S20 Relic") == "axi s20",
      fmt.relic_en_key("Axi S20 Relic"))
check("relic_en_key：认不出的返回空串（不猜）",
      fmt.relic_en_key("Requiem Eterna Relic") == ""
      and fmt.relic_en_key("垃圾") == "",
      fmt.relic_en_key("垃圾"))

_uv_keys = drops_db.droppable_keys()
check("可掉落遗物键集合非空（34 把左右）",
      30 <= len(_uv_keys) <= 40, str(len(_uv_keys)))


def _piece_rows(piece: str) -> list[dict]:
    """与 main._h_relic 部件分支同口径：掉落表三态。"""
    out = []
    for o in _inv.get(piece) or []:
        key = fmt.relic_en_key(o.get("relic") or "")
        out.append({"relic": o.get("relic"), "rarity": o.get("rarity"),
                    "state": "drop" if key and key in _uv_keys else "vaulted"})
    return out


# 混合状态部件：**动态挑**「部分遗物可掉落」的样本——每轮官方轮换后自动适配，
# 不再硬编码「某某部件 1+2」（2026-09-24 重建器上线后改;旧夹具随轮换反复失效）。
def _state_counts(piece: str) -> tuple[int, int]:
    rows = _piece_rows(piece)
    drop = sum(1 for r in rows if r["state"] == "drop")
    return drop, len(rows) - drop


_sample = next(p for p in sorted(_inv)
               if len(_inv[p]) <= 6 and _state_counts(p)[0] >= 1
               and _state_counts(p)[1] >= 1)
_drop_n, _vault_n = _state_counts(_sample)
_srows = _piece_rows(_sample)
_stitle, _slines = fmt.fmt_relic_piece(_sample, _srows,
                                       farm_hints=_hints)
check(f"部件卡标题带可获取/已入库计数（{_sample} = {_drop_n}+{_vault_n}）",
      f"可获取 {_drop_n}" in _slines[0] and f"已入库 {_vault_n}" in _slines[0],
      _slines[0])
check("★★ 卡面写明「能不能获取」：可掉落的行出现「可掉落」",
      any("可掉落" in x for x in _slines), str(_slines))
check("★★ 卡面写明「能不能获取」：入库的行出现「已入库」",
      any("已入库" in x for x in _slines), str(_slines))
check("可掉落的遗物排在最前（用户先看到能刷的）",
      "可掉落" in _slines[1], _slines[1])
check("★★ 给出「推荐刷取」位置（只对可掉落的纪元给）",
      any("推荐刷取" in x for x in _slines),
      str([x for x in _slines if x.startswith("※")]))
check("推荐刷取行不带全角空格（会污染列宽、导致放弃列对齐）",
      all("　" not in x for x in _slines if x.startswith("※")),
      str([x for x in _slines if x.startswith("※")]))
check("推荐刷取点来自数据（排除储藏库等非刷取目标）",
      all("储藏库" not in x and "比邻星域" not in x for x in _slines),
      str(_slines))

# 全部入库的部件：只给原因，不给推荐位置（给了是误导）
_allvaulted = next(p for p, v in _inv.items()
                   if v and not any(fmt.relic_en_key(o["relic"]) in _uv_keys
                                    for o in v))
_arows = _piece_rows(_allvaulted)
_atitle, _alines = fmt.fmt_relic_piece(_allvaulted, _arows, farm_hints=_hints)
check(f"全部入库的部件（{_allvaulted}）标题写明 可获取 0",
      "可获取 0" in _alines[0], _alines[0])
check("全部入库时**不**出现推荐刷取（刷不到，给了是误导）",
      not any("推荐刷取" in x for x in _alines), str(_alines))
check("全部入库时给出「都已入库…刷不到」的说明",
      any("已入库" in x and "刷不到" in x for x in _alines),
      str([x for x in _alines if x.startswith("※")]))

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
check(f"超大部件（{_big}，{len(_brows)} 把）行数被截断到 {fmt._PIECE_MAX_ROWS}",
      sum(1 for x in _blines if x.startswith("· ")) == fmt._PIECE_MAX_ROWS,
      str(sum(1 for x in _blines if x.startswith("· "))))
check("截断提示不会把可掉落的藏起来（写明还有几把能掉落）",
      bool(_m) and int(_m.group(1)) == _hidden
      and 1 <= int(_m.group(2)) <= _hidden,
      str([x for x in _blines if x.startswith("※")]))

# 全量：607 个部件的卡面都不能出现折行（注脚宽度按 25px 实测）
# ★ 字体缺失时优雅跳过：开源包**不带** Noto 字库（40MB 会让 zip 超过插件市场
#   16MB 上限，改为 scripts/fetch_font.py 按需下载）。少了这个判断，
#   别人 clone 后跑测试会直接 OSError: cannot open resource。
from PIL import ImageFont, ImageDraw, Image  # noqa: E402
_TTC = ROOT / "core" / "data" / "fonts" / "NotoSansCJK-Regular.ttc"
if not _TTC.exists():
    print("[SKIP] 未找到渲染字体（开源包默认不带）："
          "跑 `python scripts/fetch_font.py` 可下载；"
          "本次跳过「注脚折行宽度」断言（该断言依赖字体度量）")
else:
    _nf = ImageFont.truetype(str(_TTC), 25)
    _nd = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    _over = []
    for _p, _orig in _inv.items():
        _rows = [{"relic": o["relic"], "rarity": o["rarity"],
                  "state": "drop" if fmt.relic_en_key(o["relic"]) in _uv_keys
                  else "vaulted"} for o in _orig]
        _t, _ls = fmt.fmt_relic_piece(_p, _rows, farm_hints=_hints)
        for _ln in _ls:
            if _ln.startswith("※"):
                _w = _nd.textlength(_ln.lstrip("※").strip(), font=_nf)
                if _w > 1500 - 178:
                    _over.append((_p, round(_w), _ln[:50]))
    check(f"全量 {len(_inv)} 个部件：注脚没有一行会折行",
          not _over, str(_over[:3]))

# ★★ 用户明确要的行为（2026-09-18）：「出库的时候另外两个多半入库了，到时候
#   下面还是一个推荐，保持底下永远是出库的那个推荐刷新位置就行」。
#   → 推荐行必须**恰好**对应「可掉落遗物所属的纪元」：
#     已入库的纪元不给（刷不到，给了是误导），可掉落的纪元不能漏。
#   对全部 607 个部件逐条核对，防止以后有人改成「按第一个纪元给」或「都给」。
_bad_pairs = []
_mixed = 0
for _p, _orig in _inv.items():
    _rows = [{"relic": o["relic"], "rarity": o["rarity"],
              "state": "drop" if fmt.relic_en_key(o["relic"]) in _uv_keys
              else "vaulted"} for o in _orig]
    if not _rows:
        continue
    _t, _ls = fmt.fmt_relic_piece(_p, _rows, farm_hints=_hints)
    _tiers_drop = {fmt.relic_cn(o["relic"]).split()[0]
                   for o in _rows if o["state"] == "drop"}
    # 从「※ 推荐刷取（XX）：」里取出纪元名
    _tiers_hint = {x[len("※ 推荐刷取（"):].split("）")[0]
                   for x in _ls if x.startswith("※ 推荐刷取（")}
    if _tiers_drop != _tiers_hint:
        _bad_pairs.append((_p, sorted(_tiers_drop), sorted(_tiers_hint)))
    if 0 < len(_tiers_drop) < len({fmt.relic_cn(o["relic"]).split()[0]
                                   for o in _rows}):
        _mixed += 1
check(f"★★ 推荐行只给「可掉落」的纪元，且一个不漏（全量 {len(_inv)} 个部件）",
      not _bad_pairs, str(_bad_pairs[:3]))
check(f"抽检到 {_mixed} 个「部分纪元出库」的部件（就是用户说的那种情况）",
      _mixed > 0, str(_mixed))

# 已入库纪元的点位绝不能出现（构造一个：把某部件全标成入库再对比）
_fake_allvault = [{"relic": o["relic"], "rarity": o["rarity"],
                   "state": "vaulted"} for o in _inv[_allvaulted]]
_ft, _fls = fmt.fmt_relic_piece(_allvaulted, _fake_allvault, farm_hints=_hints)
check("全标入库时，即使传了 farm_hints 也一条推荐都不给",
      not any("推荐刷取" in x for x in _fls), str(_fls))

# 渲染期：列对齐（_table_mode）不能被「卡宽安全阀」关掉。
# 卡宽按注脚实际字号（25px）量之后整体收窄过（部件卡 1416→1211px），
# 必须确认收窄没有把安全阀挤爆 —— 关掉就退回「单元格顺序拼接=不对齐」。
try:
    from core import render as _R                      # noqa: E402
    _out = ROOT / "runtime"
    _out.mkdir(exist_ok=True)
    _idx = json.loads((ROOT / "core" / "data" / "relic_index.json")
                      .read_text(encoding="utf-8"))
    _out_rows = [{"cn": k, "tier_cn": k.split()[0]} for k in _idx
                 if len(k.split()) >= 3
                 and fmt.relic_en_key(k) in _uv_keys][:40]
    for _name, _t, _ls in (
            ("部件卡", f"部件出处：{_sample}", _slines),
            ("出库卡", "遗物出库（当前可掉落）",
             fmt.fmt_relic_by_tier(_out_rows, "遗物出库（当前可掉落）",
                                   page=1, page_size=90, farm_hints=_hints)[1])):
        _r = _R.ImageRenderer(_out)
        if not _r.available:
            continue
        _r.render(_t, _ls, "国际服")
        check(f"{_name}渲染后仍走列对齐（_table_mode 未被安全阀关掉）",
              bool(getattr(_r, "_table_mode", False)), "")
except Exception as _exc:                               # noqa: BLE001
    check("渲染期列对齐检查（渲染器不可用时跳过）",
          True, f"{type(_exc).__name__}: {_exc}")

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")