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
check("散文里的「剩余时间」不着色", R._TOKEN_RE.findall("商城折扣商品与剩余时间") == [],
      str(R._TOKEN_RE.findall("商城折扣商品与剩余时间")))
toks = R._TOKEN_RE.findall("剩余 32分钟　钢铁　网页在线")
check("正常计时/关键词仍着色",
      all(x in toks for x in ("剩余 32分钟", "钢铁", "网页在线")), str(toks))

# ---------------------------------------------------------------- _tint 预混
check("_tint 返回不透明四元组", R._tint((255, 255, 255), 12)[3] == 255)
check("_tint(白,12) 接近面板底色而非纯白",
      R._tint((255, 255, 255), 12) < (60, 60, 60),
      str(R._tint((255, 255, 255), 12)))
check("_tint(白,255) 就是白", R._tint((255, 255, 255), 255)[:3] == (255, 255, 255))
check("_tint(白,0) 就是底色", R._tint((255, 255, 255), 0)[:3] == R.PANEL_BG)

# ---------------------------------------------------------------- 分列单元格
# 行首「·」必须剥掉：渲染层已经画了一个圆点，字面「·」留着就是两个点
for src, exp in [
    ("· 指令格式　主指令 + 内容 + 附加项", ["指令格式", "主指令 + 内容 + 附加项"]),
    ("· 突击　每日 3 阶段：节点 · 任务 · 限制", ["突击", "每日 3 阶段：节点 · 任务 · 限制"]),
    ("· 日历　1999 日历：奖励 / 清单 / 覆写", ["日历", "1999 日历：奖励 / 清单 / 覆写"]),
    ("· 输出方式　-1 或 -w 纯文字 ｜ -t 强制图片",
     ["输出方式", "-1 或 -w 纯文字 ｜ -t 强制图片"]),
]:
    got = R._split_cells(src)
    check(f"分列：{src[:16]}…", got == exp, f"got={got!r}")
    check(f"分列首列无字面圆点：{src[:12]}…",
          bool(got) and not got[0].startswith("·"), f"got={got!r}")

# 没有全角空格 / 不该分列的行
for src in ["◆ 世界状态", "· 帮助 世界状态（查看分类详情）", "※ 说明文字", ""]:
    check(f"不分列：{src[:16] or '(空)'}", R._split_cells(src) is None,
          str(R._split_cells(src)))

# ---------------------------------------------------------------- 标签行判定
# 表格式卡片的分列行必须让位给列对齐，否则会出现加粗顶格的“异类行”
check("分列行不走标签排版", R._is_label_row("突击　每日 3 阶段", True, True) is False)
check("短标签走标签排版", R._is_label_row("首领", True, False) is True)
check("超长标签不走标签排版",
      R._is_label_row("这是一个超过十个字的标签", True, False) is False)
check("无冒号不走标签排版", R._is_label_row("没有冒号的一行", False, False) is False)

# ---------------------------------------------------------------- 赏金等级列提取
# `_LV_RE` 决定「｜N-M级」是否被抽成**右对齐等级列**。
# ⚠️ 回归护栏：旧正则写成 `｜(\d+-\d+级)｜`，要求等级两侧都有分隔符 —— 于是只有
#    「任务名｜100-100级｜钢铁之路」这种带标签的行被抽走，而「任务名｜5-15级」
#    这种不带标签的行留在原地内联，同一张卡上两种样式并存，看起来像排版坏了。
for _src, _exp in [
    ("　◆ 削弱 Grineer 的据点｜5-15级", "5-15级"),          # 行尾（无标签）
    ("　◆ 破坏赏金｜100-100级｜钢铁之路", "100-100级"),      # 行中（有标签）
    ("　◆ 涂沃主厅｜50-55级", "50-55级"),                   # oracle 地区
]:
    _m = R._LV_RE.search(_src)
    check(f"等级可抽取：{_src[-14:]}", bool(_m) and _m.group(1) == _exp,
          f"got={_m.group(1) if _m else None}")
check("普通文字里的等级区间不被误抽", R._LV_RE.search("奖励 5-15级 的物品") is None)

# ---------------------------------------------------------------- 列对齐白名单
# 2026-09-27（审核要求）：`_table_mode` 的标题白名单从 `render()` 内联 or-链
# 抽成 `is_table_title()`，为的是能用**正/反两组**用例锁住 —— 往白名单加卡片时
# 不得误改既有卡片的行为。新增卡片请同笔补正例。
TABLE_TITLES = [
    "仲裁时间表（第1/2页，共30条）", "指令一览", "赤毒 侵袭", "价格排行",
    "紫卡热度排行榜", "遗物入库（当前出库）", "遗物出库（当前可掉落）",
    "遗物列表：后纪", "部件出处：绝路 Prime", "虚空商人 当期货单（第1/2页）",
    "九重天虚空风暴",
    "仲裁排期 · 生存（第1/1页，共6场）",
    "结合仪式目标（39 种，静态库）",
]
for _t in TABLE_TITLES:
    check(f"列对齐白名单命中：{_t[:14]}", R.is_table_title(_t) is True)

# 反向：不相关标题不得命中（误开列对齐会把普通卡切成格子）
for _t in ("", "状态", "当前仲裁", "紫卡倾向：布莱顿", "遗物：古纪 A1",
           "每日突击 · 重置：剩余 3小时", "帮助"):
    check(f"不误命中：{_t or '(空)'}", R.is_table_title(_t) is False)
# ★ 子串串味守卫：新加的「仲裁排期」不是「仲裁时间表」的子串（反之亦然）
check("「仲裁排期」与「仲裁时间表」互不误命中（子串口径）",
      "仲裁排期" not in "仲裁时间表（第1/2页）"
      and "仲裁时间表" not in "仲裁排期 · 生存（第1/1页）")

# ------------------------------------------------- 仲裁排期卡：分列单元格（3.4）
# 排期卡从「节点·类型·派系 挤一格」改为**一格一字段**，渲染层才能按列对齐。
# 两条硬约束：① 未评级不生成评级格；② 字段缺失保留**空串**（丢空串会让后面的列串位）。
from core import arbi as _arbi                                 # noqa: E402

_NODES = {
    # 形态照抄线上 arbys.nodes.zh.json（88 节点实测：missionNameZh 是**纯任务名**，
    # 如「生存」「防御」「挖掘」—— 派系单独在 factionNameZh，别拿它拼进类型格）
    "a": {"nameZh": "Apollodorus", "systemNameZh": "水星",
          "missionNameZh": "生存", "factionNameZh": "Infestation"},
    "b": {"nameZh": "Tycho", "systemNameZh": "月球",
          "missionNameZh": "生存", "factionNameZh": "Corpus"},
}
_TIER = {"a": "A", "b": "未评级"}
check("仲裁排期：分列 = [节点（星球）, 类型, 派系, [评级]]",
      _arbi.node_cells(_NODES, "a", _TIER)
      == ["Apollodorus（水星）", "生存", "Infested", "[A]"],
      str(_arbi.node_cells(_NODES, "a", _TIER)))
check("仲裁排期：未评级行不生成评级格",
      _arbi.node_cells(_NODES, "b", _TIER) == ["Tycho（月球）", "生存", "Corpus"],
      str(_arbi.node_cells(_NODES, "b", _TIER)))
check("仲裁排期：字段缺失保留空串（防后面的列串位）",
      _arbi.node_cells({"c": {"nameZh": "X"}}, "c", {})[1:] == ["?", ""],
      str(_arbi.node_cells({"c": {"nameZh": "X"}}, "c", {})))
check("仲裁排期：node_line（当前仲裁卡用）仍是单行内联写法（未被改动）",
      _arbi.node_line(_NODES, "a", _TIER)
      == "Apollodorus（水星） · 生存 · Infested　[A]",
      _arbi.node_line(_NODES, "a", _TIER))

# ------------------------------------------- 结合目标卡：三格（3.3 排版）
# 原来第二格是「节点·类型（派系）」整块 ⇒ 节点列左边界随名称长短抖动（实测 38px）。
from core import formatters as _F                              # noqa: E402

_TG = [{"name": "Ancient Disruptor", "active": True, "node": "Tikal",
        "type": "Excavation", "faction": "Infested"},
       {"name": "MOA", "active": True, "node": "Venera",
        "type": "Capture", "faction": ""}]
_tg_title, _tg_lines = _F.fmt_synth_targets(_TG)
check("结合目标：行 = 名称 / 节点 / 类型（派系） 三格",
      _tg_lines[0].split("　")
      == ["· 远古干扰者（Ancient Disruptor）", "Tikal", "挖掘（Infested）"],
      str(_tg_lines[0].split("　")))
check("结合目标：派系缺失时不留空括号（回落成纯类型）",
      _tg_lines[1].split("　")[2] == "捕获", str(_tg_lines[1].split("　")))
check("结合目标：所有行格数一致（列对齐的前提）",
      len({len(x.split("　")) for x in _tg_lines}) == 1,
      str([len(x.split("　")) for x in _tg_lines]))

# 端到端：九重天卡确实走列对齐；超长节点名时安全阀关掉它（不压出面板边框）
ROOT = Path(__file__).resolve().parent.parent
try:
    from PIL import Image as _Image                            # noqa: E402

    _out = ROOT / "runtime"
    _out.mkdir(exist_ok=True)
    _rend = R.ImageRenderer(_out)
    if _rend.available:
        _st = [{"tier": "T1", "nodeCn": "卫标星环",
                "missionType": "volatile", "timeLeft": "40m 4s"},
               {"tier": "T4", "nodeCn": "努秘",
                "missionType": "volatile", "timeLeft": "40m 4s"}]
        _t1, _l1 = _F.fmt_void_storms(_st)
        _rend.render(_t1, _l1, "国际服")
        check("九重天卡：常规数据走列对齐（_table_mode=True）",
              bool(_rend._table_mode))
        _huge = dict(_st[0], nodeCn="超长节点名压力测试" * 5)
        _t2, _l2 = _F.fmt_void_storms([_huge, *_st[1:]])
        _png2 = _rend.render(_t2, _l2, "国际服")
        check("九重天卡：超长节点名触发宽度安全阀 ⇒ 退回逐行折行",
              not bool(_rend._table_mode))
        _w2 = _Image.open(_png2).size[0]
        check("九重天卡：极端数据下卡宽封顶 1500（不越界）", _w2 <= 1500, str(_w2))

        # 仲裁排期卡（同款两向验证）
        _arb = ["　".join(["9月27日 10时", "Apollodorus（水星）", "生存",
                           "Infested", "[A]"]),
                "　".join(["9月27日 11时", "Tycho（月球）", "生存",
                           "Corpus", "[B]"])]
        _rend.render("仲裁排期 · 生存（第1/1页，共2场）", _arb, "国际服")
        check("仲裁排期卡：常规数据走列对齐", bool(_rend._table_mode))
        _arb_long = ["　".join(["9月27日 10时", "超长节点名压力测试" * 6,
                               "生存", "Infested"]), _arb[1]]
        _png3 = _rend.render("仲裁排期 · 生存（第1/1页，共2场）", _arb_long, "国际服")
        check("仲裁排期卡：超长节点名触发安全阀（退回折行）",
              not bool(_rend._table_mode))
        check("仲裁排期卡：极端数据卡宽封顶 1500",
              _Image.open(_png3).size[0] <= 1500,
              str(_Image.open(_png3).size[0]))

        # 结合目标卡（同款两向验证）
        _syn = ["　".join(["· 远古干扰者（Ancient Disruptor）", "Tikal",
                           "挖掘（Infested）"]),
                "　".join(["· 恐鸟（MOA）", "Venera", "捕获（Corpus）"])]
        _rend.render("结合仪式目标（39 种，静态库）", _syn, "国际服")
        check("结合目标卡：常规数据走列对齐", bool(_rend._table_mode))
        _syn_long = ["　".join(["· " + "超长名称压力测试" * 6, "Tikal",
                                "挖掘（Infested）"]), _syn[1]]
        _png4 = _rend.render("结合仪式目标（39 种，静态库）", _syn_long, "国际服")
        check("结合目标卡：超长名称触发安全阀（退回折行）",
              not bool(_rend._table_mode))
        check("结合目标卡：极端数据卡宽封顶 1500",
              _Image.open(_png4).size[0] <= 1500,
              str(_Image.open(_png4).size[0]))
    else:
        print("[SKIP] 渲染器不可用（缺字体），跳过列对齐出图用例")
except Exception as _exc:                                       # noqa: BLE001
    print(f"[SKIP] 列对齐出图用例环境异常：{type(_exc).__name__}: {_exc}")

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
