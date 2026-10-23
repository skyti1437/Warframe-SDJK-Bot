# -*- coding: utf-8 -*-
"""渲染层文本解析离线测试：python3 tests/test_render_parse.py

覆盖 render.py 的两组正则，它们决定「哪些字会被抽走、染色」：

1. `_TIMER_RE` / `_TIMER_ALT_RE`：行尾计时会被**从正文中移除**并右对齐成琥珀色列，
   所以误匹配会同时造成「原文缺字」和「多出一截没意义的右对齐文字」。
   历史 bug：单位字符集里含「时」，散文中的「剩余时间」被匹配成「剩余时」。
2. `_TOKEN_RE`：行内语义着色。

另外校验 `_tint()` —— ImageDraw 在 RGBA 上不做 alpha 混合，收尾 convert("RGB")
又会丢 alpha，所以半透明色必须预先混好，否则大面积填充会渲染成纯白块。
"""

from __future__ import annotations

import sys
from pathlib import Path
import tempfile as _tempfile_rp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import render as R  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def timer_of(text: str) -> str | None:
    m = R._TIMER_RE.search(text) or R._TIMER_ALT_RE.search(text)
    return m.group(0).strip() if m else None


# ---------------------------------------------------------------- 不该抽走的
# 「剩余时间」这类**散文**绝不能命中：单位字符集里的「时」曾导致误判
NO_TIMER = [
    "· 每日特惠　商城折扣商品与剩余时间",
    "· 时效　各周期内容的当前剩余时间总览",
    "※ 以上为各周期内容的当前剩余时间，到点自动轮换/重置",
    "· 内融核心 ×6,000",
    "· 阿耶檀识 Anasa 塑像　普通 28.0%",
    "1. Odin（水星） · 营救　限制：敌人护甲强化",
    "· 面纱裂罅密文（Veiled Riven Cipher，每周限购 1）　20 精华",
    "· 霰弹枪紫卡　75 精华",
    "◆ 本周轮换（第 8/8 周 · 每周限购 1 次）",
    # 日期里的「天」不能当计时（没有 剩余/剩/不到 标记）
    "· 1999-07-09（第190天）",
    "第 22 年 · 夏季　季末 2天14小时",
    "· 击杀 30 名卓越者。",
]
for t in NO_TIMER:
    check(f"不误抽：{t[:22]}…", timer_of(t) is None, str(timer_of(t)))

# ---------------------------------------------------------------- 应抽走的
SHOULD_TIMER = [
    ("夜灵平野 · 昼夜交替：剩余 32分钟", "剩余 32分钟"),
    ("每日突击 · 重置：剩余 7小时3分", "剩余 7小时3分"),
    ("执刑官猎杀 · 周常重置：剩余 2天15小时", "剩余 2天15小时"),
    ("虚空商人 · 抵达/离开：剩余 9天4小时", "剩余 9天4小时"),
    ("奥布山谷 · 温度周期：剩余 10分钟", "剩余 10分钟"),
    ("某内容：剩余 1小时4分", "剩余 1小时4分"),
    ("首领：Oestrus　剩余 58分钟（58m 36s）", "剩余 58分钟（58m 36s）"),
    ("· [每日] 随身火力　剩 14小时48分　1000 声望", "剩 14小时48分"),
    ("· [每周] 任务完成 V　剩 2天14小时　4500 声望", "剩 2天14小时"),
    ("某内容：4天12小时 后开始", "4天12小时 后开始"),
    ("虚空商人 · 抵达：3小时2分 后抵达", "3小时2分 后抵达"),
    # 「不到」必须能抽到（`剩余?` 那种写法会漏掉它）
    ("某内容：不到1分钟", "不到1分钟"),
    ("某内容：剩余 不到1分钟", "剩余 不到1分钟"),
    # ◆/※ 开头由调用方跳过，这里只验证正则本身不炸
    ("※ 此处不应抽（◆ 开头由调用方跳过）", None),
]
for t, exp in SHOULD_TIMER:
    got = timer_of(t)
    check(f"应抽：{t[:22]}…", got == exp, f"got={got!r} exp={exp!r}")

# ---------------------------------------------------------------- 语义着色
check(
    "散文里的「剩余时间」不着色",
    R._TOKEN_RE.findall("商城折扣商品与剩余时间") == [],
    str(R._TOKEN_RE.findall("商城折扣商品与剩余时间")),
)
toks = R._TOKEN_RE.findall("剩余 32分钟　钢铁　网页在线")
check(
    "正常计时/关键词仍着色", all(x in toks for x in ("剩余 32分钟", "钢铁", "网页在线")), str(toks)
)

# ---------------------------------------------------------------- _tint 预混
check(
    "_tint 返回不透明 RGB 三元组（扁平 RGB 画布，无 alpha）", len(R._tint((255, 255, 255), 12)) == 3
)
check(
    "_tint(白,12) 接近面板底色而非纯白",
    R._tint((255, 255, 255), 12) < (60, 60, 60),
    str(R._tint((255, 255, 255), 12)),
)
check("_tint(白,255) 就是白", R._tint((255, 255, 255), 255) == (255, 255, 255))
check("_tint(白,0) 就是底色", R._tint((255, 255, 255), 0)[:3] == R.PANEL_BG)

# ---------------------------------------------------------------- 分列单元格
# 行首「·」必须剥掉：渲染层已经画了一个圆点，字面「·」留着就是两个点
for src, exp in [
    ("· 指令格式　主指令 + 内容 + 附加项", ["指令格式", "主指令 + 内容 + 附加项"]),
    ("· 突击　每日 3 阶段：节点 · 任务 · 限制", ["突击", "每日 3 阶段：节点 · 任务 · 限制"]),
    ("· 日历　1999 日历：奖励 / 清单 / 覆写", ["日历", "1999 日历：奖励 / 清单 / 覆写"]),
    ("· 输出方式　-1 或 -w 纯文字 ｜ -t 强制图片", ["输出方式", "-1 或 -w 纯文字 ｜ -t 强制图片"]),
]:
    got = R._split_cells(src)
    check(f"分列：{src[:16]}…", got == exp, f"got={got!r}")
    check(
        f"分列首列无字面圆点：{src[:12]}…", bool(got) and not got[0].startswith("·"), f"got={got!r}"
    )

# 没有全角空格 / 不该分列的行
for src in ["◆ 世界状态", "· 帮助 世界状态（查看分类详情）", "※ 说明文字", ""]:
    check(f"不分列：{src[:16] or '(空)'}", R._split_cells(src) is None, str(R._split_cells(src)))

# ---------------------------------------------------------------- 标签行判定
# 表格式卡片的分列行必须让位给列对齐，否则会出现加粗顶格的“异类行”
check("分列行不走标签排版", R._is_label_row("突击　每日 3 阶段", True, True) is False)
check("短标签走标签排版", R._is_label_row("首领", True, False) is True)
check("超长标签不走标签排版", R._is_label_row("这是一个超过十个字的标签", True, False) is False)
check("无冒号不走标签排版", R._is_label_row("没有冒号的一行", False, False) is False)

# ---------------------------------------------------------------- 赏金等级列提取
# `_LV_RE` 决定「｜N-M级」是否被抽成**右对齐等级列**。
# ⚠️ 回归护栏：旧正则写成 `｜(\d+-\d+级)｜`，要求等级两侧都有分隔符 —— 于是只有
#    「任务名｜100-100级｜钢铁之路」这种带标签的行被抽走，而「任务名｜5-15级」
#    这种不带标签的行留在原地内联，同一张卡上两种样式并存，看起来像排版坏了。
for _src, _exp in [
    ("　◆ 削弱 Grineer 的据点｜5-15级", "5-15级"),  # 行尾（无标签）
    ("　◆ 破坏赏金｜100-100级｜钢铁之路", "100-100级"),  # 行中（有标签）
    ("　◆ 涂沃主厅｜50-55级", "50-55级"),  # oracle 地区
]:
    _m = R._LV_RE.search(_src)
    check(
        f"等级可抽取：{_src[-14:]}",
        bool(_m) and _m.group(1) == _exp,
        f"got={_m.group(1) if _m else None}",
    )
check("普通文字里的等级区间不被误抽", R._LV_RE.search("奖励 5-15级 的物品") is None)

# ---------------------------------------------------------------- 列对齐白名单
# 2026-09-27（审核要求）：`_table_mode` 的标题白名单从 `render()` 内联 or-链
# 抽成 `is_table_title()`，为的是能用**正/反两组**用例锁住 —— 往白名单加卡片时
# 不得误改既有卡片的行为。新增卡片请同笔补正例。
TABLE_TITLES = [
    "仲裁时间表（第1/2页，共30条）",
    "指令一览",
    "可蹲类型",
    "赤毒 侵袭",
    "价格排行",
    "紫卡热度排行榜",
    "遗物入库（当前出库）",
    "遗物出库（当前可掉落）",
    "遗物列表：后纪",
    "部件出处：绝路 Prime",
    "虚空商人 当期货单（第1/2页）",
    "九重天虚空风暴",
    "仲裁排期 · 生存（第1/1页，共6场）",
    "结合仪式目标（39 种，静态库）",
    "冰凇 紫卡词条范围（3正1负）",  # 2026-10-23：词条/区间两列
]
for _t in TABLE_TITLES:
    check(f"列对齐白名单命中：{_t[:14]}", R.is_table_title(_t) is True)

# 反向：不相关标题不得命中（误开列对齐会把普通卡切成格子）
for _t in (
    "",
    "状态",
    "当前仲裁",
    "紫卡倾向：布莱顿",
    "紫卡分析",  # 反向：「词条范围」与「紫卡分析」互不误命中
    "遗物：古纪 A1",
    "每日突击 · 重置：剩余 3小时",
    "帮助",
):
    check(f"不误命中：{_t or '(空)'}", R.is_table_title(_t) is False)
# ★ 子串串味守卫：新加的「仲裁排期」不是「仲裁时间表」的子串（反之亦然）
check(
    "「仲裁排期」与「仲裁时间表」互不误命中（子串口径）",
    "仲裁排期" not in "仲裁时间表（第1/2页）" and "仲裁时间表" not in "仲裁排期 · 生存（第1/1页）",
)

# ------------------------------------------------- 仲裁排期卡：分列单元格（3.4）
# 排期卡从「节点·类型·派系 挤一格」改为**一格一字段**，渲染层才能按列对齐。
# 两条硬约束：① 未评级不生成评级格；② 字段缺失保留**空串**（丢空串会让后面的列串位）。
from core import arbi as _arbi  # noqa: E402

_NODES = {
    # 形态照抄线上 arbys.nodes.zh.json（88 节点实测：missionNameZh 是**纯任务名**，
    # 如「生存」「防御」「挖掘」—— 派系单独在 factionNameZh，别拿它拼进类型格）
    "a": {
        "nameZh": "Apollodorus",
        "systemNameZh": "水星",
        "missionNameZh": "生存",
        "factionNameZh": "Infestation",
    },
    "b": {
        "nameZh": "Tycho",
        "systemNameZh": "月球",
        "missionNameZh": "生存",
        "factionNameZh": "Corpus",
    },
}
_TIER = {"a": "A", "b": "未评级"}
check(
    "仲裁排期：分列 = [节点（星球）, 类型, 派系, [评级]]",
    _arbi.node_cells(_NODES, "a", _TIER) == ["Apollodorus（水星）", "生存", "Infested", "[A]"],
    str(_arbi.node_cells(_NODES, "a", _TIER)),
)
check(
    "仲裁排期：未评级行不生成评级格",
    _arbi.node_cells(_NODES, "b", _TIER) == ["Tycho（月球）", "生存", "Corpus"],
    str(_arbi.node_cells(_NODES, "b", _TIER)),
)
check(
    "仲裁排期：字段缺失保留空串（防后面的列串位）",
    _arbi.node_cells({"c": {"nameZh": "X"}}, "c", {})[1:] == ["?", ""],
    str(_arbi.node_cells({"c": {"nameZh": "X"}}, "c", {})),
)
check(
    "仲裁排期：node_line（当前仲裁卡用）仍是单行内联写法（未被改动）",
    _arbi.node_line(_NODES, "a", _TIER) == "Apollodorus（水星） · 生存 · Infested　[A]",
    _arbi.node_line(_NODES, "a", _TIER),
)

# ------------------------------------------- 结合目标卡：三格（3.3 排版）
# 原来第二格是「节点·类型（派系）」整块 ⇒ 节点列左边界随名称长短抖动（实测 38px）。
from core import formatters as _F  # noqa: E402

_TG = [
    {
        "name": "Ancient Disruptor",
        "active": True,
        "node": "Tikal",
        "type": "Excavation",
        "faction": "Infested",
    },
    {"name": "MOA", "active": True, "node": "Venera", "type": "Capture", "faction": ""},
]
_tg_title, _tg_lines = _F.fmt_synth_targets(_TG)
# ★ 2026-10-11 第二步改版：卡首多了「◆ 共收录」答案行 / 「◆ 目标一览」标题 / 表头行；
#   下面三条断言只针对数据行（「· 」开头），断言内容不变。
_tg_lines = [x for x in _tg_lines if x.startswith("· ")]
check(
    "结合目标：行 = 名称 / 节点 / 类型（派系） 三格",
    _tg_lines[0].split("　") == ["· 远古干扰者（Ancient Disruptor）", "Tikal", "挖掘（Infested）"],
    str(_tg_lines[0].split("　")),
)
check(
    "结合目标：派系缺失时不留空括号（回落成纯类型）",
    _tg_lines[1].split("　")[2] == "捕获",
    str(_tg_lines[1].split("　")),
)
check(
    "结合目标：所有行格数一致（列对齐的前提）",
    len({len(x.split("　")) for x in _tg_lines}) == 1,
    str([len(x.split("　")) for x in _tg_lines]),
)

# 端到端：九重天卡确实走列对齐；超长节点名时安全阀关掉它（不压出面板边框）
ROOT = Path(__file__).resolve().parent.parent
try:
    from PIL import Image as _Image  # noqa: E402

    _out = Path(
        _tempfile_rp.mkdtemp(prefix="sdjk_render_parse_")
    )  # 不写 ROOT/runtime（会污染开源 stage）
    _rend = R.ImageRenderer(_out)
    if _rend.available:
        _st = [
            {"tier": "T1", "nodeCn": "卫标星环", "missionType": "volatile", "timeLeft": "40m 4s"},
            {"tier": "T4", "nodeCn": "努秘", "missionType": "volatile", "timeLeft": "40m 4s"},
        ]
        _t1, _l1 = _F.fmt_void_storms(_st)
        _rend.render(_t1, _l1, "国际服")
        check("九重天卡：常规数据走列对齐（_table_mode=True）", bool(_rend._table_mode))
        _huge = dict(_st[0], nodeCn="超长节点名压力测试" * 5)
        _t2, _l2 = _F.fmt_void_storms([_huge, *_st[1:]])
        _png2 = _rend.render(_t2, _l2, "国际服")
        check("九重天卡：超长节点名触发宽度安全阀 ⇒ 退回逐行折行", not bool(_rend._table_mode))
        _w2 = _Image.open(_png2).size[0]
        check("九重天卡：极端数据下卡宽封顶 1500（不越界）", _w2 <= 1500, str(_w2))

        # 仲裁排期卡（同款两向验证）
        _arb = [
            "　".join(["9月27日 10时", "Apollodorus（水星）", "生存", "Infested", "[A]"]),
            "　".join(["9月27日 11时", "Tycho（月球）", "生存", "Corpus", "[B]"]),
        ]
        _rend.render("仲裁排期 · 生存（第1/1页，共2场）", _arb, "国际服")
        check("仲裁排期卡：常规数据走列对齐", bool(_rend._table_mode))
        _arb_long = [
            "　".join(["9月27日 10时", "超长节点名压力测试" * 6, "生存", "Infested"]),
            _arb[1],
        ]
        _png3 = _rend.render("仲裁排期 · 生存（第1/1页，共2场）", _arb_long, "国际服")
        check("仲裁排期卡：超长节点名触发安全阀（退回折行）", not bool(_rend._table_mode))
        check(
            "仲裁排期卡：极端数据卡宽封顶 1500",
            _Image.open(_png3).size[0] <= 1500,
            str(_Image.open(_png3).size[0]),
        )

        # 结合目标卡（同款两向验证）
        _syn = [
            "　".join(["· 远古干扰者（Ancient Disruptor）", "Tikal", "挖掘（Infested）"]),
            "　".join(["· 恐鸟（MOA）", "Venera", "捕获（Corpus）"]),
        ]
        _rend.render("结合仪式目标（39 种，静态库）", _syn, "国际服")
        check("结合目标卡：常规数据走列对齐", bool(_rend._table_mode))
        _syn_long = [
            "　".join(["· " + "超长名称压力测试" * 6, "Tikal", "挖掘（Infested）"]),
            _syn[1],
        ]
        _png4 = _rend.render("结合仪式目标（39 种，静态库）", _syn_long, "国际服")
        check("结合目标卡：超长名称触发安全阀（退回折行）", not bool(_rend._table_mode))
        check(
            "结合目标卡：极端数据卡宽封顶 1500",
            _Image.open(_png4).size[0] <= 1500,
            str(_Image.open(_png4).size[0]),
        )
    else:
        print("[SKIP] 渲染器不可用（缺字体），跳过列对齐出图用例")
except Exception as _exc:  # noqa: BLE001
    print(f"[SKIP] 列对齐出图用例环境异常：{type(_exc).__name__}: {_exc}")

# ------------------------------- 仲裁时间表：含空格节点名不裂列（2026-10-02）
# 玩家报障：88 个仲裁节点里 3 个 nameZh 带 ASCII 空格（V Prime / Tyana Pass /
# Outer Terminus）→ 旧实现「ASCII 空格 join → split」把节点劈成两列、其后
# 各列整体右移。现在 handler 存**结构化单元格**、按全角空格拼行（渲染层分列
# 口径只认全角空格）。本段 ① 行为断言（假数据走真 handler）② 数据守卫
# （arbys.nodes.zh.json 是运行时拉的、不入包 ⇒ 用同域的 arb_ratings.json +
# 官方节点表做离线守卫，防「数据里没有空格名」让本测试静默失效）。
import asyncio  # noqa: E402
import inspect  # noqa: E402
import json  # noqa: E402
import time as _time  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402

from core.commands.arbitration import ArbitrationCommands as _AC  # noqa: E402


class _ArbStubClient:
    """假客户端：按 URL 返回三张表的固定数据。"""

    async def _fetch_json(self, url: str, ttl: int = 0):
        if url.endswith("arbys.schedule.v2.json"):
            return {
                "startTs": int(_time.time()) - 60,
                "stepSec": 3600,
                "seq": [0, 1, 2, 3],
                "nodes": ["vprime", "tyana", "outer", "apollo"],
            }
        if url.endswith("arbys.nodes.zh.json"):
            return {
                "nodes": {
                    "vprime": {
                        "nameZh": "V Prime",
                        "systemNameZh": "金星",
                        "missionNameZh": "生存",
                        "factionNameZh": "Corpus",
                    },
                    "tyana": {
                        "nameZh": "Tyana Pass",
                        "systemNameZh": "火星",
                        "missionNameZh": "镜像防御",
                        "factionNameZh": "Corpus",
                    },
                    "outer": {
                        "nameZh": "Outer Terminus",
                        "systemNameZh": "冥王星",
                        "missionNameZh": "防御",
                        "factionNameZh": "Corpus",
                    },
                    "apollo": {
                        "nameZh": "Apollodorus",
                        "systemNameZh": "水星",
                        "missionNameZh": "生存",
                        "factionNameZh": "Infested",
                    },
                }
            }
        return {"tierBuckets": {"S": ["apollo"]}}


_arb_obj = _NS(client=_ArbStubClient())
# ★ 2026-10-04：`_h_arbtable` 改走共用入口 `self._arb_fetch()`（三级回落后的 tier_of）
#   ⇒ 桩对象也要绑上这个方法（绑真实实现，用假 client 拉三张表）。
_arb_obj._arb_fetch = lambda: _AC._arb_fetch(_arb_obj)
_arb_reply = asyncio.run(_AC._h_arbtable(_arb_obj, _NS(page=1), None, "国际服"))
# ★ 2026-10-11 第二步改版：数据行加「· 」前缀（列表项）并有表头 / 答案行；只取数据行并剥掉前缀再按列切，
#   断言内容（列数、节点格完整、无空列、不拿 ASCII 空格当分隔）不变。
_arb_rows = [_r[2:].split("　") for _r in _arb_reply.lines if _r.startswith("· ")]
# ★ 2026-10-16（维护者要求）：凡出现评级的仲裁卡，下方都标评分范围（共用 core/arbi.py::TIER_NOTE）
from core import arbi as _arbi_mod  # noqa: E402

check(
    "仲裁时间表：评级列下方标评分范围",
    any(_arbi_mod.TIER_RANGE in ln for ln in _arb_reply.lines),
    str(_arb_reply.lines[-3:]),
)
check(
    "筛选排期 / 当前仲裁 / 时间表三张卡都引用共用评分范围说明",
    (ROOT / "core" / "commands" / "arbitration.py")
    .read_text(encoding="utf-8")
    .count("_arbi.TIER_NOTE")
    >= 3,
)
check(
    "仲裁时间表：注入 4 行（3 个含空格节点 + 1 个评级节点）",
    len(_arb_rows) == 4,
    str(len(_arb_rows)),
)
check(
    "★ V Prime 行 = 5 列，节点/星球各一列（未合并、未裂列）",
    _arb_rows[0] == [_arb_rows[0][0], "V Prime", "金星", "生存", "Corpus"],
    str(_arb_rows[0]),
)
check(
    "★ Tyana Pass 行 5 列、节点格完整",
    len(_arb_rows[1]) == 5 and _arb_rows[1][1] == "Tyana Pass",
    str(_arb_rows[1]),
)
check(
    "★ Outer Terminus 行 5 列、节点格完整",
    len(_arb_rows[2]) == 5 and _arb_rows[2][1] == "Outer Terminus",
    str(_arb_rows[2]),
)
check(
    "评级节点仍出第 6 列（评级格未被本修波及）",
    len(_arb_rows[3]) == 6 and _arb_rows[3][5] == "S",
    str(_arb_rows[3]),
)
check(
    "所有单元格非空（格子间无空列）",
    all(all(_c for _c in _r) for _r in _arb_rows),
    str([[c for c in r if not c] for r in _arb_rows]),
)

# 源码接线：死代码 dw()/pad() 已删；不再出现「ASCII 空格当列分隔」
_arb_src = inspect.getsource(_AC._h_arbtable)
check(
    "仲裁时间表：死代码 dw()/pad() 已删", "def dw(" not in _arb_src and "def pad(" not in _arb_src
)
check(
    '★ 仲裁时间表：不再把 ASCII 空格当列分隔（无 split(" ") 往返）',
    'split(" ")' not in _arb_src and 'S = "　"' in _arb_src and "S.join(" in _arb_src,
)

# 数据守卫：仓内确实存在含 ASCII 空格的节点名（≥3），否则本段会静默失效
_arb_ratings = json.loads((ROOT / "core" / "data" / "arb_ratings.json").read_text(encoding="utf-8"))
# ★ 2026-10-04：表结构换代（旧 = 顶层 `名称|类型` 键；新 = `nodes[节点ID]` 里带 name）
_spaced_rating_nodes = sorted(
    {
        v.get("name") or ""
        for v in (_arb_ratings.get("nodes") or {}).values()
        if " " in (v.get("name") or "")
    }
)
check(
    "★ 数据守卫：arb_ratings.json 含空格节点名 ≥3（含 V Prime / Tyana Pass）",
    len(_spaced_rating_nodes) >= 3 and {"V Prime", "Tyana Pass"} <= set(_spaced_rating_nodes),
    str(_spaced_rating_nodes),
)
_nodes_zh = json.loads(
    (ROOT / "core" / "data" / "de" / "nodes_zh.json").read_text(encoding="utf-8")
)
_zh_names = {v.get("name") for v in _nodes_zh.values() if isinstance(v, dict)}
check(
    "★ 数据守卫：官方节点表含三个含空格样本（V Prime / Tyana Pass / Outer Terminus）",
    {"V Prime", "Tyana Pass", "Outer Terminus"} <= _zh_names,
)

# ------------------------------------- 仲裁评级：节点 ID 键 + 三级回落（2026-10-04）
# ★ 回归对象（调研 §4.1 的真缺陷）：`_arb_now` 曾用 `中文名|英文类型` 拼 key 查实测表，
#   而 arbi 的 missionType 是**内部代号**（MT_TERRITORY → 本地表写 Interception、
#   MT_PURIFY → Infested Salvage、MT_ARTIFACT → Disruption、MT_EVACUATE → Excavation、
#   MT_EVACUATION → Defection）⇒ 17 个节点的「生息效率」行永远查不到（命中率仅 44/88）。
#   现表改按 **节点 ID**（SolNodeXXX/ClanNodeXX，与 schedule/nodes/tierlist 三表同源）。
_M = _arbi.load_measured()
check("★ 实测表已加载（core/data/arb_ratings.json 存在且非空）", bool(_M), str(len(_M)))
# ① 该命中要命中：MT_TERRITORY 的 Xini 用节点 ID 查得到（旧写法必然查不到）
check(
    "★ bug 回归（命中侧）：SolNode172(Xini, MT_TERRITORY) 按节点 ID 命中实测档",
    _M.get("SolNode172", {}).get("tier") == "A+"
    and _M.get("SolNode172", {}).get("median") is not None,
    str(_M.get("SolNode172")),
)
# ② 不该命中的不误挂：旧写法拼出的 key 不在表里（这就是旧 bug 的形态）
check(
    "★ bug 回归（旧键侧）：`Xini|Territory` 这类旧键在表里**不存在**",
    "Xini|Territory" not in _M and "Xini|Interception" not in _M and all("|" not in k for k in _M),
    "表内不应出现任何 `名称|类型` 形态的键",
)
# ③ 三级回落：arbi 优先（含 C 档原样）、实测补空白、两者都无 → 未评级
_tier_syn = {"SolNode172": "S", "SolNode167": "未评级", "SolNode999": "未评级"}
_arbi._ARBI_ONLY.clear()
_arbi._ARBI_ONLY.update({"SolNode172": "S", "SolNode167": "未评级"})  # 模拟 fetch_tables 记录
_merged = _arbi.merge_measured_tiers(_tier_syn, _M)
check(
    "★ 三级回落：arbi 有评级 ⇒ 原样保留（实测不覆盖）",
    _merged["SolNode172"] == "S",
    str(_merged.get("SolNode172")),
)
check(
    "★ 三级回落：arbi 未评级 ⇒ 用实测档补上",
    _merged["SolNode167"] == _M["SolNode167"]["tier"],
    "%s vs %s" % (_merged.get("SolNode167"), _M.get("SolNode167")),
)
check(
    "★ 三级回落：两者都无 ⇒ 维持「未评级」",
    _merged["SolNode999"] == "未评级",
    str(_merged.get("SolNode999")),
)
check(
    "★ 回落不丢输入键（列对齐依赖的不变量；允许新增实测键）",
    set(_tier_syn) <= set(_merged),
    str(set(_tier_syn) - set(_merged)),
)
# ④ 来源判定：arbi / measured / 空
check(
    "★ 档位来源：arbi 覆盖的节点报 arbi、实测补的报 measured、未评级报空",
    _arbi.tier_source("SolNode172", _merged) == "arbi"
    and _arbi.tier_source("SolNode167", _merged) == "measured"
    and _arbi.tier_source("SolNode999", _merged) == "",
    "%s / %s / %s"
    % (
        _arbi.tier_source("SolNode172", _merged),
        _arbi.tier_source("SolNode167", _merged),
        _arbi.tier_source("SolNode999", _merged),
    ),
)
check(
    "★ 实测表结构守卫：每条都是 {name,system,mission,n[,median,tier]} 且 n>=3 才带档",
    all(isinstance(v, dict) and "n" in v and "name" in v for v in _M.values())
    and all(("tier" in v) == (v["n"] >= 3) for v in _M.values()),
    "有 %d 条带档 / 共 %d 条" % (sum(1 for v in _M.values() if v.get("tier")), len(_M)),
)


# ===========================================================================
# B12（2026-10-11）：A「Orokin 精修」绘制层 —— 解析/判定层用例
# ===========================================================================
def _toks(text: str) -> list[str]:
    return [m.group(0) for m in R._TOKEN_RE.finditer(text)]


# ---- 渲染 bug ①：序号徽章不吃小数（「3.5x Zoom」不是第 3 条）
check("序号：「3. 绝路」仍是序号行", R._NUM_HEAD_RE.match("3. 绝路") is not None)
check("序号：「12. 紫卡」两位数仍是序号行", R._NUM_HEAD_RE.match("12. 紫卡") is not None)
check("序号反例：「3.5x Zoom」不画序号徽章", R._NUM_HEAD_RE.match("3.5x Zoom") is None)
check("序号反例：「0.75 倍」不画序号徽章", R._NUM_HEAD_RE.match("0.75 倍") is None)

# ---- 渲染 bug ②：价格 token 认小数（「127.5p」整染，不是只染「5p」）
check("价格：整数 120p 成 token", _toks("价格 120p") == ["120p"], str(_toks("价格 120p")))
check(
    "价格：小数 127.5p 整串成 token", _toks("价格 127.5p") == ["127.5p"], str(_toks("价格 127.5p"))
)
check(
    "价格：小数价按青色规则整染",
    R._token_style("127.5p")[3] == R.CYAN and R._token_style("120p")[3] == R.CYAN,
    str(R._token_style("127.5p")),
)
check("价格反例：「50pa」后接字母不是价格", _toks("50pa") == [], str(_toks("50pa")))
check("价格反例：「1.5.5p」不从中间截一段", _toks("1.5.5p") == [], str(_toks("1.5.5p")))

# ---- 渲染 bug ③：词表染色加右边界（不再半词染色）
for _w in ("钢铁", "执刑官"):
    check(
        f"词边界反例：「{_w}意志」不半词染色", _w not in _toks(f"{_w}意志"), str(_toks(f"{_w}意志"))
    )
    check(f"词边界正例：「{_w}」独立成词仍染", _w in _toks(f"　{_w}　"), str(_toks(f"　{_w}　")))
    check(
        f"词边界正例：「{_w}」后接标点仍染", _w in _toks(f"{_w}，剩余"), str(_toks(f"{_w}，剩余"))
    )
check(
    "词边界：「钢铁之路」四字整体仍是一个 token",
    _toks("钢铁之路") == ["钢铁之路"],
    str(_toks("钢铁之路")),
)

# ---- 渲染 bug ④：✦ 的去留以「子集字体有没有字形」为准
# 实测（2026-10-11）：NotoSansCJKsc 子集（含 charset 里写了 ✦）没有 U+2726 的字形，
# 画出来就是 .notdef 豆腐块 —— 所以渲染层**继续剥掉 ✦**，formatter 改用有字形的 ★。
_FD = Path(__file__).resolve().parent.parent / "core" / "data" / "fonts"
_SUB_R = _FD / "NotoSansCJKsc-Subset-Regular.otf"
_SUB_B = _FD / "NotoSansCJKsc-Subset-Bold.otf"
_CHARSET = (
    Path(__file__).resolve().parent.parent / "scripts" / "font_subset_charset.txt"
).read_text(encoding="utf-8")


def _has_glyph(font, ch: str) -> bool:
    nd = font.getmask("\U0001f9ff")  # 子集里必然没有的码位 = .notdef
    m = font.getmask(ch)
    return not (m.size == nd.size and bytes(m) == bytes(nd))


if R.Image is not None and _SUB_R.exists() and _SUB_B.exists():
    _fr = R.ImageFont.truetype(str(_SUB_R), 40)
    _fb = R.ImageFont.truetype(str(_SUB_B), 40)
    check(
        "✦ 子集字体无字形（渲染层剥掉它的前提）",
        not _has_glyph(_fr, "✦") and not _has_glyph(_fb, "✦"),
    )
    check("★ 子集字体有字形（玄骸幻纹标记改用它）", _has_glyph(_fr, "★") and _has_glyph(_fb, "★"))
    # 新绘制层新增/依赖的字面量：必须在 charset 里、且两档字重都有真字形
    # （▸ 是行首指令标记，绘制时被剥掉、从不上屏，子集里也没有它的字形 —— 不在此列）
    _NEW_LITERALS = "◆※⚠·★▲▼▣…：／×"
    _lit_slugs = {s for _k, s in R._SLUG_OVERRIDE} | {s for _k, (_c, s) in R.TITLE_THEME}
    _lit = set(_NEW_LITERALS) | {c for s in _lit_slugs for c in s} | set("0123456789/ ")
    _not_in_charset = sorted(c for c in _lit if c not in _CHARSET)
    check("新绘制层字面量全在 font_subset_charset.txt", not _not_in_charset, str(_not_in_charset))
    _no_glyph = sorted(
        c for c in _lit if c != " " and not (_has_glyph(_fr, c) and _has_glyph(_fb, c))
    )
    check("新绘制层字面量在子集字体里都有字形（常规+粗体）", not _no_glyph, str(_no_glyph))
else:
    print("[SKIP] 无 Pillow/子集字体，跳过 ✦/★ 与字面量缺字用例")
check("✦ 仍被 _strip_emoji 剥掉（避免豆腐）", R._strip_emoji("幻纹✦")[0] == "幻纹")
check("★ 不被 _strip_emoji 剥", R._strip_emoji("｜幻纹★")[0] == "｜幻纹★")

# ---- _LV_RE 放宽：单值「100级」也进右对齐等级列
for _src, _exp in [
    ("　▸ 捕获 Grineer 特工｜100级｜钢铁之路", "100级"),
    ("　▸ 刺杀 焦土大地｜100级", "100级"),
    ("　▸ 防御 削弱据点｜40-60级", "40-60级"),
]:
    _m = R._LV_RE.search(_src)
    check(f"等级可抽取（含单值）：{_src[-12:]}", bool(_m) and _m.group(1) == _exp, str(_m))
for _src in ("奖励 100级 的物品", "｜满级", "｜100级别", "· 川流不息 10级 在线"):
    check(f"等级反例不误抽：{_src}", R._LV_RE.search(_src) is None)


# ---- 行类型二次判定：KV / 分区标题 / 结论行 / 表头
def _mk(texts, kinds=None, cells=None):
    rows = []
    for i, tx in enumerate(texts):
        k = (kinds or {}).get(i) or ("section" if tx.startswith("◆") else "normal")
        rows.append({"text": tx, "kind": k, "cells": (cells or {}).get(i)})
    return rows


_r = _mk(["◆ 状态：在线"])
R._classify_rows(_r, bounty=False, plain=False)
check(
    "KV：「◆ 标签：值」判 kv", _r[0]["kind"] == "kv" and _r[0]["kv"] == ("状态", "在线"), str(_r[0])
)

_r = _mk(["◆ 本周轮换", "· 甲", "· 乙"])
R._classify_rows(_r, bounty=False, plain=False)
check("区块：「◆ 纯标题」仍是分区标题", _r[0]["kind"] == "section" and "kv" not in _r[0])

_r = _mk(["◆ 本周：", "· 甲"])
R._classify_rows(_r, bounty=False, plain=False)
check("区块反例：冒号后无值 = 标题，不是 KV", _r[0]["kind"] == "section")

_r = _mk(["◆ " + "长" * 31 + "：值", "· 甲"])
R._classify_rows(_r, bounty=False, plain=False)
check("区块反例：标签超 30 字 = 标题，不是 KV", _r[0]["kind"] == "section")

_r = _mk(["◆ 持续：1", "◆ DPS 持续 ≈ 48（含暴击）", "◆ 末尾：2"])
R._classify_rows(_r, bounty=False, plain=False)
check(
    "结论行：卡上有 KV 且紧跟另一个 ◆ 行 → 整句按 KV 处理",
    _r[1]["kind"] == "kv" and _r[1]["kv"] == ("DPS 持续 ≈ 48（含暴击）", ""),
    str(_r[1]),
)

_r = _mk(["◆ 持续：1", "◆ 某标题", "· 内容"])
R._classify_rows(_r, bounty=False, plain=False)
check("结论行反例：后面有内容行可装 → 仍是分区标题", _r[1]["kind"] == "section")

_r = _mk(["◆ 甲", "◆ 乙", "· 内容"])
R._classify_rows(_r, bounty=False, plain=False)
check(
    "结论行反例：整卡没有任何 KV → 连续 ◆ 都是标题",
    [x["kind"] for x in _r[:2]] == ["section", "section"],
)

_r = _mk(["◆ 地球：夜灵平原", "▸ 甲"])
R._classify_rows(_r, bounty=True, plain=False)
check("赏金卡的 ◆ 是地区横幅，不判 KV", _r[0]["kind"] == "section")

_r = _mk(["◆ 状态：在线"])
_r[0]["cont"] = True
R._classify_rows(_r, bounty=False, plain=False)
check("折行续行不参与 KV 判定", _r[0]["kind"] == "section")

_r = _mk(
    ["◆ 榜单", "名称　数量　备注", "· 甲　1　a", "· 乙　2　b"], cells={1: ["名称", "数量", "备注"]}
)
R._classify_rows(_r, bounty=False, plain=False)
check("表头：分区标题后的分列行、下一行是「·」项 → 表头", _r[1].get("thead") is True)
_r = _mk(["◆ 榜单", "名称　数量　备注", "· 甲　1　a"], cells={1: ["名称", "数量", "备注"]})
R._classify_rows(_r, bounty=False, plain=True)
check("表头反例：语法文档卡（plain）不判表头", not _r[1].get("thead"))
_r = _mk(
    ["◆ 榜单", "· 先来一行", "名称　数量　备注", "· 甲　1　a"], cells={2: ["名称", "数量", "备注"]}
)
R._classify_rows(_r, bounty=False, plain=False)
check("表头反例：前一行不是分区标题且非首行 → 不判表头", not _r[2].get("thead"))
_r = _mk(["◆ 榜单", "· 甲　1　a", "· 乙　2　b"], cells={1: ["· 甲", "1", "a"]})
R._classify_rows(_r, bounty=False, plain=False)
check("表头反例：「·」开头的数据行不是表头", not _r[1].get("thead"))

# ---- 头部英文栏目名（kicker）：本批实测发现的 11 张「落到品牌名兜底」的卡 + 1 张误命中
for _title, _slug in [
    ("本周钢铁回廊灵化", "INCARNON ROTATION"),
    ("Ergo 信条武器轮换", "TENET ROTATION"),
    ("Coda 终幕武器轮换", "CODA ROTATION"),
    ("沉沦之地 · 炼狱塔", "DESCENDIA"),
    ("九重天虚空风暴", "VOID STORMS"),
    ("钢铁精华兑换（Teshin 荣誉商店）", "STEEL ESSENCE SHOP"),
    ("言录使（Acrithis）本周货单", "ACRITHIS"),
    ("裂罅碎块兑换（Palladino · 钢铁守望）", "PALLADINO SHOP"),
    ("Prime 出库清单（本期）", "PRIME VAULT"),
    ("开核桃建议（共38场）", "RELIC CRACKING"),
    ("进行中的活动", "LIVE EVENTS"),
    ("赋能·专注（Arcane Concentration）", "CODEX ENTRY"),
    ("钢铁之路侵袭", "STEEL PATH"),  # 反例：原有映射不被新补表劫持
    ("时效总览", "TIMERS OVERVIEW"),
]:
    check(f"栏目英文名：{_title[:12]}", R._card_slug(_title) == _slug, R._card_slug(_title))
check("栏目英文名：全无命中时回落品牌名", R._card_slug("乱七八糟").startswith("WARFRAME "))


# ---- 第二步（2026-10-11）：从属行 / 标记芯片 / 列表网格宽度
def _mk_lead(texts, lead):
    rows = _mk(texts)
    for i, n in lead.items():
        rows[i]["lead"] = n
    return rows


_r = _mk_lead(["◆ 每周", "· 标题　1000 声望", "说明文字"], {2: 1})
R._classify_rows(_r, bounty=False, plain=False)
check(
    "从属行：行首缩进的普通行并入上一条（cont+sub，斑马纹不再把「标题行 + 说明行」拆成两种底色）",
    _r[2].get("cont") and _r[2].get("sub") and _r[2].get("indent") == 1,
    str(_r[2]),
)
_r = _mk_lead(["◆ 表", "列甲　列乙", "· 甲　乙", "· 丙　丁"], {1: 1})
_r[1]["cells"] = ["列甲", "列乙"]
R._classify_rows(_r, bounty=False, plain=False)
check(
    "从属行反例：表头行（紧跟分区标题、下一行是「·」项）不算从属",
    not _r[1].get("sub") and _r[1].get("thead"),
)
_r = _mk_lead(["◆ 区块", "说明文字"], {1: 1})
R._classify_rows(_r, bounty=False, plain=False)
check("从属行反例：紧跟分区标题的缩进行（无上一条可并）不判从属", not _r[1].get("sub"))
_r = _mk_lead(
    ["◆ 木星（第 660 周）", "500 分：甲", "1000 分：乙", "1500 分：丙"], {1: 2, 2: 2, 3: 2}
)
R._classify_rows(_r, bounty=False, plain=False)
check(
    "从属行反例：整排缩进的同级列表（氏族奖励）不判从属（缩进不得前后不一）",
    not any(x.get("sub") for x in _r),
    str([x.get("sub") for x in _r]),
)
_r = _mk_lead(["▸ 档位", "奖励行"], {1: 2})
_r[0]["kind"] = "bounty_head"
R._classify_rows(_r, bounty=True, plain=False)
check("从属行反例：赏金卡（自有档位框）不套用", not _r[1].get("sub"))
_r = _mk_lead(["· 标题", "折行第一段", "折行第二段"], {1: 1})
_r[2]["cont"] = True
_r[2]["subcont"] = True
R._classify_rows(_r, bounty=False, plain=False)
check("从属行折行后续行：同缩进同灰度", _r[2].get("sub") and _r[2].get("indent") == 1, str(_r[2]))

for _w in ("钢铁", "九重天", "速刷"):
    check(
        f"标记芯片：「{_w}」可切成独立 token 且有芯片色",
        _w in R._MARK_CHIPS and R._TOKEN_RE.search(f"Grineer　{_w}　剩9分钟") is not None,
    )
check(
    "标记芯片反例：「钢铁之路」整词仍是文字 token（不被拆成「钢铁」芯片 + 「之路」）",
    R._TOKEN_RE.search("钢铁之路").group(0) == "钢铁之路" and "钢铁之路" not in R._MARK_CHIPS,
)
check(
    "纪元 / 稀有度 / 本期 芯片都有色（否则静默退成朴素文字）",
    all(k in R.GROUP_CHIP_COLOR for k in ("常见", "罕见", "稀有", "本期", "常驻", "精英")),
)

try:
    _out2 = Path(
        _tempfile_rp.mkdtemp(prefix="sdjk_render_parse_")
    )  # 不写 ROOT/runtime（会污染开源 stage）
    _rend2 = R.ImageRenderer(_out2)
    if _rend2.available:
        _S = "　"
        _rows = [
            f"· [古纪]{_S}前哨战{_S}虚无神殿（地球比邻星域）{_S}Grineer{_S}九重天{_S}剩25分钟",
            f"· [前纪]{_S}拦截{_S}Callisto（木星）{_S}Corpus{_S}钢铁{_S}剩1小时9分",
            f"· [中纪]{_S}间谍{_S}Nereid（海王星）{_S}Corpus{_S}{_S}剩9分钟",
            f"· [后纪]{_S}生存{_S}初裔冰渍区（海王星比邻星域）{_S}Corpus{_S}钢铁 九重天{_S}剩3小时9分",
        ]
        _lines = ["◆ 进行中：4 条", "◆ 列表", f"{_S}纪元{_S}类型{_S}节点{_S}派系{_S}标记", *_rows]
        _png3 = _rend2.render("虚空裂隙（共4条）", _lines, "国际服")
        _im3 = _Image.open(_png3)
        check(
            "列表网格：多格列表行出图不抛、卡宽不越界（≤1500）",
            _im3.size[0] <= 1500,
            str(_im3.size),
        )
        check("列表网格：未折行（带标记的长行不被折成孤字）", _im3.size[1] < 900, str(_im3.size))
        _png4 = _rend2.render(
            "午夜电波",
            [
                "◆ 每日挑战（1 项）",
                "· 吸引力　1000 声望　剩 3小时",
                f"{_S}使用磁力伤害击杀 150 名敌人。",
            ],
            "国际服",
        )
        check("从属行：出图不抛", _png4 is not None)
except ImportError:
    pass

# ---------------------------------------------------------------- 报错卡片 err_card（B13）
from core.commands.base import err_card  # noqa: E402

_ec = err_card(
    "没有找到「xx」",
    "换英文名重发",
    ("你是不是想找", "甲、乙"),
    "· 示例　wm 母牛 蓝图",
    title="wm 查价",
)
check("err_card：标题沿用指令标题", _ec.title == "wm 查价" and _ec.raw_text is None)
check(
    "err_card：结构 = 原因 + KV + 示例行 + 怎么改（首尾固定）",
    _ec.lines[0] == "◆ 原因：没有找到「xx」"
    and _ec.lines[1] == "◆ 你是不是想找：甲、乙"
    and _ec.lines[2] == "· 示例　wm 母牛 蓝图"
    and _ec.lines[-1] == "※ ⚠ 怎么改：换英文名重发",
    str(_ec.lines),
)
check("err_card：空 kv 字符串被忽略", len(err_card("a", "b", "").lines) == 2)
_ect = R.text_card(_ec.title, _ec.lines, _ec.footer)
check(
    "文字降级（-w / render_mode=text）：原因、KV、怎么改都在",
    all(k in _ect for k in ("wm 查价", "没有找到「xx」", "甲、乙", "wm 母牛 蓝图", "换英文名重发")),
    _ect,
)


if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
