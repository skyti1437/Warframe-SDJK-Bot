# -*- coding: utf-8 -*-
"""「识卡伤害」复算回归（python3 tests/test_scan_damage.py）

背景（2026-10-09）：
  ① `_h_scan_damage` 把 `parsed.content_str`（字符串）传给 `dc.parse_args`，
     而后者要的是 token 列表 —— 字符串被逐字符拆开，「150级 / 爆头 / 重击」
     全部失效，单字还进了卡面「未识别」行；
  ② `dc.calculate` 直接往入参 spec 写 note / `_armor` / `evo_headshot`，
     识卡缓存的 spec 每复算一次，notes 就多一份。

走真实链路：`core.parser.parse` → `lo.analyze`（无视觉调用，手写识别产物）
→ `lo.to_damage_spec` → `ScanCommands._h_scan_damage`。内嵌桩，不联网。
"""

from __future__ import annotations

import asyncio
import copy
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import damage_calc as dc  # noqa: E402
from core import loadout_ocr as lo  # noqa: E402
from core.commands.scan import ScanCommands  # noqa: E402
from core.parser import parse  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"  —— {detail}"))
    if not cond:
        FAILED.append(name)


def _scan_cache(weapon: str, mods: list[dict]) -> tuple[dict, dict]:
    """识卡缓存（与 _h_scan 同源：analyze → to_damage_spec）。"""
    an = lo.analyze({"weapon": weapon, "mods": mods, "panel": {}})
    assert an["ok"], an.get("errors")
    return an["weapon"], lo.to_damage_spec(an)


def _run(weapon: dict, cached: dict, text: str) -> list[str]:
    host = types.SimpleNamespace(
        _last_scan={"umo|u1": (time.time(), weapon, cached)},
        _safe_sender=lambda _e: "u1",
    )
    event = types.SimpleNamespace(unified_msg_origin="umo")
    parsed = parse(text)
    assert parsed.command == "scandamage", parsed.command
    reply = asyncio.run(ScanCommands._h_scan_damage(host, parsed, event, None))
    return list(reply.lines or [])


# ---------------------------------------------------------------------------
# ① 参数真正生效（近战：重击段才有数据）
# ---------------------------------------------------------------------------
print("① 识卡伤害：参数生效")
_w_mel, _c_mel = _scan_cache("迦伦提恩", [{"name": "压迫点", "drain": "9", "color": "白"}])
_lines = _run(_w_mel, _c_mel, "识卡伤害 对 重机枪手 150级 爆头 重击")
_target = next((ln for ln in _lines if ln.startswith("◆ 目标")), "")
check("等级 150级 生效（目标行）", "150级" in _target, _target)
check("敌人 重型机枪手 生效", any("敌人：重型机枪手" in ln for ln in _lines))
# 「爆头期望」行恒显示；爆头开关只体现在 DoT（继承爆头倍率）上 ⇒ 对照组比 DoT 行
_no_hs = _run(_w_mel, _c_mel, "识卡伤害 对 重机枪手 150级 重击")


def _dot(ls: list[str]) -> str:
    return next((ln for ln in ls if ln.startswith("◆ 异常 DoT")), "")


check(
    "爆头 生效（DoT 行随爆头放大，与不带爆头的对照组不同）",
    bool(_dot(_lines)) and _dot(_lines) != _dot(_no_hs),
    f"{_dot(_lines)} vs {_dot(_no_hs)}",
)
check("重击 生效（出现重击段）", any(ln.startswith("◆ 重击") for ln in _lines))
check(
    "无「未识别」行（不再逐字符拆分）",
    not any("未识别" in ln for ln in _lines),
    str([ln for ln in _lines if "未识别" in ln]),
)

# 不带参数：沿用识卡缓存，默认 100 级
_plain = _run(_w_mel, _c_mel, "识卡伤害")
check(
    "无参数 → 默认 100级",
    any(ln.startswith("◆ 目标") and "100级" in ln for ln in _plain),
)

# ---------------------------------------------------------------------------
# ② 连续复算 notes 不累积（非近战 + 重击 → calculate 会写「没有重击数据」）
# ---------------------------------------------------------------------------
print("② 识卡伤害：连续两次复算，缓存不被污染")
_w_gun, _c_gun = _scan_cache("雷克斯", [{"name": "弹头撞击", "drain": "9", "color": "白"}])
_snap = copy.deepcopy(_c_gun)
_l1 = _run(_w_gun, _c_gun, "识卡伤害 重击")
_l2 = _run(_w_gun, _c_gun, "识卡伤害 重击")
_n1 = sum("没有重击数据" in ln for ln in _l1)
_n2 = sum("没有重击数据" in ln for ln in _l2)
check("第一次卡面含「没有重击数据」1 次", _n1 == 1, str(_n1))
check("第二次卡面仍只 1 次（不累积）", _n2 == 1, str(_n2))
check("两次卡面完全一致", _l1 == _l2)
check("识卡缓存 spec 未被改动", _c_gun == _snap, str(_c_gun.get("notes")))

# ---------------------------------------------------------------------------
# ③ calculate 不改入参（#4 根因）：note 走 res["notes"] 返回
# ---------------------------------------------------------------------------
print("③ calculate：入参 spec 不被改动")
_s, _ = dc.parse_args(["重击"])
_s_snap = copy.deepcopy(_s)
_r1 = dc.calculate(_s, _w_gun)
_r2 = dc.calculate(_s, _w_gun)
check("入参 spec 前后相等", _s == _s_snap, str(sorted(k for k in _s if _s[k] != _s_snap.get(k))))
check("不再泄漏 _armor 到入参", _s.get("_armor") == _s_snap.get("_armor"))
check("note 经 res 返回", any("没有重击数据" in n for n in _r1["notes"]), str(_r1["notes"]))
check("两次 res 的 notes 相同", _r1["notes"] == _r2["notes"])
check("两次数值相同", _r1["health"] == _r2["health"] and _r1["dps"] == _r2["dps"])

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
