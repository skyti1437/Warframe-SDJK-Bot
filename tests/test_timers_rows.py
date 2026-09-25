# -*- coding: utf-8 -*-
"""时效总览不许静默吞行 + 仲裁锚点死代码清理守卫（2026-09-25）。

事故：`fmt_timers` 遇到取不到数据的项直接 ``continue``，而 `_h_timers` 更早一步
用 ``isinstance(r, dict)`` 把源恒抛的条目（仲裁 / 钢铁侵蚀——DE 源没有这两个键）
过滤掉了 ⇒ 两行自 10o.io 停摆后从「时效总览」卡里静默消失，用户以为看全了。

铁律：**合法空值≠故障，但不允许静默错槽** —— 取不到就显式打
「暂无时效数据（源未下发）」，行位不丢、顺序不乱。

第二个守卫：`锚点` 指令相关的死代码（fmt_arbitration / fetch_arbseq /
arb_from_anchor / load_arb_slots / core/data/arb_slots.json）已清理，防复发。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core import formatters as fmt  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


MISSING = "暂无时效数据（源未下发）"


# ---------------------------------------------------------------- 1. 行位不丢
def t_no_silent_drop():
    timers = [("夜灵平野", {"expiry": "2030-01-01T00:00:00+00:00"}),
              ("仲裁", None),                      # 源恒抛（事故现场）
              ("钢铁侵蚀", {}),                     # 空 dict
              ("每日突击", {"foo": 1, "bar": 2}),   # 有字段但两个键都没有
              ("虚空奸商", {"activation": "2030-01-01T00:00:00+00:00"})]
    _title, lines = fmt.fmt_timers(timers)
    body = [ln for ln in lines if not ln.startswith("※")]
    check("5 项输入 → 5 行输出（一行都不许吞）", len(body) == 5, str(body))
    check("仲裁 行显式提示源未下发",
          any("仲裁 · 每小时轮换" in ln and MISSING in ln for ln in body), str(body))
    check("钢铁侵蚀 行显式提示源未下发（描述文案=钢铁之路 · 每日重置）",
          any("钢铁之路 · 每日重置" in ln and MISSING in ln for ln in body), str(body))
    check("空 dict / 无关键 dict 也显式提示",
          sum(1 for ln in body if MISSING in ln) == 3, str(body))
    check("正常 expiry/activation 行仍出倒计时",
          any("剩余" in ln for ln in body) and any("后开始" in ln for ln in body), str(body))
    check("顺序不乱（行序=输入序）",
          [ln.split("：")[0] for ln in body] ==
          ["夜灵平野 · 昼夜交替", "仲裁 · 每小时轮换", "钢铁之路 · 每日重置",
           "每日突击 · 重置", "虚空商人 · 抵达/离开"], str(body))


# ---------------------------------------------------------------- 2. 全空也不崩
def t_all_missing():
    _title, lines = fmt.fmt_timers([("仲裁", None), ("钢铁侵蚀", None)])
    check("全缺失时不抛且每项都有行", len([ln for ln in lines if not ln.startswith("※")]) == 2,
          str(lines))


# ---------------------------------------------------------------- 3. 源码级守卫
def t_source_guards():
    src_fmt = (ROOT / "core/formatters.py").read_text(encoding="utf-8")
    src_main = (ROOT / "main.py").read_text(encoding="utf-8")
    check("fmt_timers 不再有 `if not data: continue` 静默分支",
          "if not data:\n            continue" not in src_fmt)
    # 只在 _h_timers 函数体内断言（别处 `isinstance(r, dict)` 过滤仍是合法用法）
    _i = src_main.index("async def _h_timers")
    _body = src_main[_i:_i + 2600]
    check("_h_timers 把非 dict 结果透传为 None（不再 isinstance 过滤丢行）",
          "r if isinstance(r, dict) else None" in _body and
          "if isinstance(r, dict)]" not in _body)
    # 死代码清理守卫
    check("formatters.fmt_arbitration 已清理", "def fmt_arbitration" not in src_fmt)
    check("api_client.fetch_arbseq 已清理",
          "def fetch_arbseq" not in (ROOT / "core/api_client.py").read_text(encoding="utf-8"))
    src_dw = (ROOT / "core/de_worldstate.py").read_text(encoding="utf-8")
    check("de_worldstate 锚点推算器已清理",
          "arb_from_anchor" not in src_dw and "load_arb_slots" not in src_dw)
    check("core/data/arb_slots.json 已删除", not (ROOT / "core/data/arb_slots.json").exists())
    check("「.锚点」废弃提示仍保留（按铁律给替代指令）",
          "已废弃" in src_main and "仲裁表" in src_main)


def main():
    t_no_silent_drop()
    t_all_missing()
    t_source_guards()
    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} -> {FAILED}")
        sys.exit(1)
    print("全部通过（时效总览不静默吞行 + 死代码清理守卫）")


if __name__ == "__main__":
    main()
