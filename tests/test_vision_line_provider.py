# -*- coding: utf-8 -*-
"""紫卡窄读路独立渠道（vision_provider_id_line）回归。

2026-10-15 线上报障：「紫卡分析 + 裁切卡（Sati-ignitis）」识别不了。
根因是**窄读路与 OCR 专用模型错配** —— 窄读提示词要求「逐行念出来」，而
qwen-vl-ocr 会退回它带 bbox 坐标的原生输出（`x,y,w,h,angle,文本`），
`_riven_lines_legal` 认不出极性符号 ⇒ 整卡判不合法。

三路基准 20 图实测（output/报告-视觉模型三路基准-20261015.md）：
  · qwen-vl-ocr        窄读 15/20（5 张被坐标打穿）｜ 不发提示词 19/20
  · qwen3.5-omni-flash 窄读 20/20 ｜ 不发提示词 0/20（输出散文）
⇒ 两路对模型的要求不同，故窄读路支持独立配置渠道。

本测试冻死这条配置链（不联网）：
  ① 未配置        → 窄读路与主渠道同源
  ② 配置了且可用  → 窄读路只用该渠道
  ③ 配置了但取不到 → 回落主渠道，不抛
  ④ 配置读取抛错  → 当空串处理，不炸识别链
  ⑤ 端到端        → _extract_riven_from_image 两路各走各的渠道
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.commands.vision import VisionCommands  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class _Prov:
    """假 vision 渠道：只用于 id 与身份判定。"""

    def __init__(self, pid: str):
        self._pid = pid

    def meta(self):
        class _M:
            id = self._pid

        return _M()


class _Ctx:
    def __init__(self, provs: dict[str, _Prov]):
        self._provs = provs

    def get_provider_by_id(self, pid: str):
        return self._provs.get(pid)

    def get_all_providers(self):
        return list(self._provs.values())


class _Cfg(dict):
    """可编程配置：boom_keys 里的键读取即抛（模拟配置读取异常）。"""

    def __init__(self, *a, boom_keys=(), **k):
        super().__init__(*a, **k)
        self._boom = set(boom_keys)

    def get(self, key, default=None):
        if key in self._boom:
            raise RuntimeError(f"cfg boom: {key}")
        return super().get(key, default)


MAIN = _Prov("main/json-ok")
LINE = _Prov("line/only")


def mk(cfg: dict, provs: dict[str, _Prov]) -> VisionCommands:
    inst = VisionCommands.__new__(VisionCommands)
    inst.cfg = _Cfg(cfg, boom_keys=cfg.pop("__boom__", []) if "__boom__" in cfg else ())
    inst.context = _Ctx(provs)
    return inst


# ---------------------------------------------------------------- ① 未配置
inst = mk({"vision_provider_id": "main/json-ok"}, {"main/json-ok": MAIN})
main_list = inst._vision_providers()
line_list = inst._line_vision_providers()
check(
    "① 未配置 vision_provider_id_line ⇒ 窄读路与主渠道同源",
    line_list == main_list,
    f"main={[p.meta().id for p in main_list]} line={[p.meta().id for p in line_list]}",
)

# ------------------------------------------------------------ ② 配置且可用
inst = mk(
    {"vision_provider_id": "main/json-ok", "vision_provider_id_line": "line/only"},
    {"main/json-ok": MAIN, "line/only": LINE},
)
main_list = inst._vision_providers()
line_list = inst._line_vision_providers()
check(
    "② 配置了窄读渠道 ⇒ 窄读路只用它",
    line_list == [LINE],
    f"line={[p.meta().id for p in line_list]}",
)
check(
    "② 主渠道不受窄读配置影响",
    main_list and main_list[0] is MAIN,
    f"main={[p.meta().id for p in main_list]}",
)

# ------------------------------------------------------- ③ 配置了但取不到
inst = mk(
    {"vision_provider_id": "main/json-ok", "vision_provider_id_line": "ghost/nope"},
    {"main/json-ok": MAIN},
)
line_list = inst._line_vision_providers()
check(
    "③ 窄读渠道取不到 ⇒ 回落主渠道（不抛）",
    line_list and line_list[0] is MAIN,
    f"line={[p.meta().id for p in line_list]}",
)

# --------------------------------------------------------- ④ 配置读取抛错
inst = mk(
    {"__boom__": ["vision_provider_id_line"], "vision_provider_id": "main/json-ok"},
    {"main/json-ok": MAIN},
)
try:
    line_list = inst._line_vision_providers()
    ok = line_list and line_list[0] is MAIN
    detail = f"line={[p.meta().id for p in line_list]}"
except Exception as exc:  # noqa: BLE001
    ok, detail = False, f"抛了 {type(exc).__name__}: {exc}"
check("④ 配置读取抛错 ⇒ 当空串、回落主渠道、不炸", ok, detail)

# ------------------------------------------------------------ ⑤ 端到端两路
inst = mk(
    {"vision_provider_id": "main/json-ok", "vision_provider_id_line": "line/only"},
    {"main/json-ok": MAIN, "line/only": LINE},
)
seen: list[tuple[str, list[str]]] = []


async def _fake_race(prompt, image_url, provs, *, k=2, tag="识别", window=None, parse=None):
    seen.append((tag, [p.meta().id for p in provs]))
    if tag == "紫卡行读":
        return {
            "lines": ["+189.9% 暴击几率", "+118% 冰冻伤害", "+142.8% 暴击伤害", "-121.6% 切割伤害"]
        }
    return {"weapon": "伯斯顿", "positive": [], "negative": []}


inst._vision_race_json = _fake_race
inst.RACE_WINDOW_S = 5.0
out = asyncio.run(inst._extract_riven_from_image("data:image/png;base64,x"))
by_tag = dict(seen)
check("⑤ 语义 JSON 路走主渠道", by_tag.get("紫卡识别") == ["main/json-ok"], f"seen={seen}")
check("⑤ 窄读路走独立渠道", by_tag.get("紫卡行读") == ["line/only"], f"seen={seen}")
check("⑤ 两路结果合并（lines 来自窄读路）", bool(out and out.get("lines")), f"out={out}")

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("ALL PASS")
