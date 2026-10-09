# -*- coding: utf-8 -*-
"""wm 指令的部件关键词（2026-09-19 用户反馈「wm 母牛 蓝图」出的是整套）。

覆盖两层：
1. core.parser.parse_wm：部件词抽取（连写 / 分写 / 限定词剥离 / 优先级）
2. main._pick_wm_set_part：部件词 → 套装部件物品的挑选（蓝图=总图）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f"　[{extra}]" if extra and not cond else ""))
    if not cond:
        _fails.append(name)


print("=== 一、parse_wm 部件词抽取 ===")
from core.parser import parse_wm  # noqa: E402

cases = [
    (["母牛", "蓝图"], "母牛", "蓝图"),  # 用户原始写法
    (["母牛蓝图"], "母牛", "蓝图"),  # 连写
    (["电男", "机体蓝图"], "电男", "机体"),  # 具体部件 + 蓝图
    (["母牛", "头部神经光元", "蓝图"], "母牛", "头部"),  # 官方全称分写
    (["母牛", "头部神经光元蓝图"], "母牛", "头部"),  # 官方全称连写
    (["母牛", "配件"], "母牛", "配件"),  # 泛指
    (["母牛", "部件"], "母牛", "配件"),
    (["母牛"], "母牛", ""),  # 无部件词不误伤
    (["席瓦"], "席瓦", ""),
    (["母牛", "蓝图", "收购"], "母牛", "蓝图"),  # 与其它开关共存
    (["Mesa", "系统"], "Mesa", "系统"),  # 英文名 + 部件
    (["母牛", "总图"], "母牛", "蓝图"),  # 总图 = 蓝图
    (["弓", "枪管"], "弓", "枪管"),  # 武器部件
]
for toks, item, part in cases:
    q = parse_wm(list(toks))
    check(
        f"{toks} -> item={item!r} part={part!r}",
        q.item == item and q.part == part,
        f"实际 item={q.item!r} part={q.part!r}",
    )

print()
print("=== 二、_pick_wm_set_part（部件挑选）===")
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("wfq_main", ROOT / "main.py")
plugin = importlib.util.module_from_spec(_spec)
sys.modules["wfq_main"] = plugin
try:
    _spec.loader.exec_module(plugin)
except Exception:
    # 私有版依赖 astrbot 桩；复用 test_admin 的安装逻辑
    sys.path.insert(0, str(ROOT / "tests"))
    import test_admin as _t  # noqa: F401

    _spec2 = importlib.util.spec_from_file_location("wfq_main2", ROOT / "main.py")
    plugin = importlib.util.module_from_spec(_spec2)
    sys.modules["wfq_main2"] = plugin
    _spec2.loader.exec_module(plugin)

pick = plugin.WarframeSDJK._pick_wm_set_part
# Hildryn Prime 真实部件名（WM zh，2026-09-19 实测）
parts = [
    {
        "zh": "Hildryn Prime 蓝图",
        "en": "Hildryn Prime Blueprint",
        "url_name": "hildryn_prime_blueprint",
    },
    {
        "zh": "Hildryn Prime 机体蓝图",
        "en": "Hildryn Prime Chassis Blueprint",
        "url_name": "hildryn_prime_chassis_blueprint",
    },
    {
        "zh": "Hildryn Prime 头部神经光元 蓝图",
        "en": "Hildryn Prime Neuroptics Blueprint",
        "url_name": "hildryn_prime_neuroptics_blueprint",
    },
    {
        "zh": "Hildryn Prime 系统蓝图",
        "en": "Hildryn Prime Systems Blueprint",
        "url_name": "hildryn_prime_systems_blueprint",
    },
]
check(
    "蓝图 → 总图（不是机体/系统蓝图）",
    (pick(parts, "蓝图") or {}).get("url_name") == "hildryn_prime_blueprint",
)
check("总图 与 蓝图 等价", (pick(parts, "总图") or {}).get("url_name") == "hildryn_prime_blueprint")
check(
    "机体 → 机体蓝图",
    (pick(parts, "机体") or {}).get("url_name") == "hildryn_prime_chassis_blueprint",
)
check(
    "头部 → 头部神经光元蓝图",
    (pick(parts, "头部") or {}).get("url_name") == "hildryn_prime_neuroptics_blueprint",
)
check(
    "系统 → 系统蓝图",
    (pick(parts, "系统") or {}).get("url_name") == "hildryn_prime_systems_blueprint",
)
check("不存在的部件 → None（上层给准确提示）", pick(parts, "枪管") is None)

print()
print("=== 三、「头」部件黑话消歧（前缀精确命中才剥，2026-10-02 二修）===")
import asyncio  # noqa: E402
from core.commands.market import _head_part_resolve  # noqa: E402

_exact = {
    "水晶": {"url_name": "citrine_prime_set", "tags": ["set"]},
    "水晶p": {"url_name": "citrine_prime_set", "tags": ["set"]},
    "白霜弹头": {"url_name": "rime_rounds", "tags": ["mod"]},
    "石头人": {"url_name": "atlas_prime_set", "tags": ["set"]},
}


class _StubClient:
    async def resolve_wm_exact(self, q):
        return _exact.get(q)


def _head(item):
    return asyncio.run(_head_part_resolve(_StubClient(), item))


_cit = _exact["水晶"]
check("wm 水晶头 → 剥头", _head("水晶头") == ("水晶", _cit), str(_head("水晶头")))
check("wm 水晶p头 → 剥头", _head("水晶p头") == ("水晶p", _cit), str(_head("水晶p头")))
check("wm 水晶 头（空格头）→ 剥头", _head("水晶 头") == ("水晶", _cit), str(_head("水晶 头")))
check("wm 水晶p 头 → 剥头", _head("水晶p 头") == ("水晶p", _cit), str(_head("水晶p 头")))
check("整名精确存在不剥：白霜弹头", _head("白霜弹头") is None, str(_head("白霜弹头")))
check("整名精确存在不剥：石头人", _head("石头人") is None, str(_head("石头人")))
check("剥头后前缀不存在不剥：狗头", _head("狗头") is None, str(_head("狗头")))
check("不以头结尾不处理：水晶", _head("水晶") is None)

print()
print("=== 四、2026-10-07 扩充的 8 个部件词（T1）与「蓝图」分支同步 ===")
# 报障：`wm 玻之武杖p 饰物` 出「一套」（「饰物」不在表里 ⇒ 整串丢给解析 ⇒ 落 set）。
# T1 = 饰物/引擎/锤头/机舱/圆盘/机翼/外甲/握把 —— 选自 WM `/v2/items`（zh-hans）
# 的 component/blueprint 中文名 843 条尾词统计，每个覆盖 ≥2 条真实部件名；
# 实测在**非部件物品名（3049 条）里 0 撞名**。
_T1 = ("饰物", "引擎", "锤头", "机舱", "圆盘", "机翼", "外甲", "握把")
for _w in _T1:
    _q1 = parse_wm(["玻之武杖p", _w])
    check(
        f"T1：`{_w}` 被拆成部件词（p 后缀不失真）",
        _q1.part == _w and _q1.item.endswith("p"),
        f"{_q1.item!r}/{_q1.part!r}",
    )
    _q2 = parse_wm([f"提佩多{_w}"])
    check(
        f"T1：`{_w}` 连写也能拆",
        _q2.part == _w and _q2.item == "提佩多",
        f"{_q2.item!r}/{_q2.part!r}",
    )

_q3 = parse_wm(["玻之武杖", "蓝图"])
check(
    "回归：加词后 `玻之武杖 蓝图` 仍是总图",
    _q3.item == "玻之武杖" and _q3.part == "蓝图",
    f"{_q3.item!r}/{_q3.part!r}",
)

# ★ skip 同步守卫（_pick_wm_set_part 的 skip 元组必须与 _PART_SPECIFIC 同步）：
#   「引擎 蓝图」这类**部件蓝图**不能被「蓝图=总图」分支挑走。
_pick_engine = [
    {"zh": "Scimitar 引擎", "url_name": "scimitar_engines"},
    {"zh": "Scimitar 引擎 蓝图", "url_name": "scimitar_engines_blueprint"},
]
_pick_bp = [
    {"zh": "Scimitar 引擎 蓝图", "url_name": "scimitar_engines_blueprint"},
    {"zh": "Scimitar 蓝图", "url_name": "scimitar_blueprint"},
]
check(
    "新词入表后：引擎 按部件命中",
    (pick(_pick_engine, "引擎") or {}).get("url_name") == "scimitar_engines",
)
check(
    "★ skip 同步：总图只挑真总图（引擎 蓝图 不被当总图）",
    (pick(_pick_bp, "蓝图") or {}).get("url_name") == "scimitar_blueprint",
)

# ★ 结构断言（2026-10-07）：skip 与 _PART_SPECIFIC 必须同步 —— 差集只允许
#   「头部神经」（parser 侧已被 _PART_STRENGTH 归并成「头部」）。比逐词断言强：
#   将来任何一侧加词而另一侧漏加，这里立刻红（历史两次漏同步都是靠人记）。
from core.commands.market import _SET_BLUEPRINT_SKIP as _SKIP  # noqa: E402
from core.parser import _PART_SPECIFIC as _PS  # noqa: E402

check(
    "★ 结构：_SET_BLUEPRINT_SKIP == _PART_SPECIFIC − {头部神经}",
    set(_SKIP) == set(_PS) - {"头部神经"},
    f"缺={sorted((set(_PS) - {'头部神经'}) - set(_SKIP))} 多={sorted(set(_SKIP) - set(_PS))}",
)

# 双向护栏（纪律 9）：T2 是**本轮有意未收录**的词（每个只覆盖 1 条部件名），
# 这里钉住它们「仍不被消费」。将来决定收录时，必须把词从这份白名单挪走 ——
# 否则本断言会红，提醒同步改测试（防止「加了词却没人验」）。
_T2 = (
    "链条",
    "星镖",
    "爪刃",
    "铆钉",
    "锯片",
    "散热片",
    "下皮层",
    "弓臂",
    "马达",
    "刀片",
    "剑刃",
    "手套",
    "靴子",
)
for _w in _T2:
    _q = parse_wm(["降灵追猎者p", _w])
    check(
        f"白名单：`{_w}` 本轮有意未收录（part 仍为空）",
        _q.part == "" and _w in _q.item,
        f"{_q.item!r}/{_q.part!r}",
    )

print()
if _fails:
    print(f"✗ {len(_fails)} 项失败: {_fails}")
    raise SystemExit(1)
print("✓ wm 部件关键词全部通过")
