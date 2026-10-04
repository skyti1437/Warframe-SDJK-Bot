# -*- coding: utf-8 -*-
"""wm 指令的部件关键词（2026-09-19 玩家反馈「wm 母牛 蓝图」出的是整套）。

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
if _fails:
    print(f"✗ {len(_fails)} 项失败: {_fails}")
    raise SystemExit(1)
print("✓ wm 部件关键词全部通过")
