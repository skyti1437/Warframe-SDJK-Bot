# -*- coding: utf-8 -*-
"""加载/刷新路径健壮性（python3 tests/test_load_resilience.py）

来源：2026-09-25 服务器面板「加载失败 (1)：No module named 'core'」事故 ——
从市场安装 v1.0.7 时，**新 main.py 已落地、core/render.py 还是旧的**
（新 main 要 `migrate_legacy_user_fonts`，旧 render 没有）→ 第一步 ImportError
→ `except ImportError` 兼容分支再去 `from core import ...` → 抛出的就变成
「No module named 'core'」。**真错误被兜底伪装成"模块结构坏了"**，面板与日志
第一眼全被误导（实际插件随后自己恢复、一直在服务）。

这里钉住三件事：
1. 兼容分支只在**非包上下文**（离线脚本）才回退，包内失败必须原样抛出；
2. 后台检查点对齐到 **XX:05 UTC**（换轮都在 00:00 UTC，旧 sleep(6h) 最坏等 6h）；
3. 社区快照有**独立的 30 分钟轻轮询**（FS 关闭/不可达的用户才走它）。
"""
from __future__ import annotations

import importlib
import re
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 1. 兼容分支：只在非包上下文回退
# ---------------------------------------------------------------------------
GUARDED = {
    "main.py": ("except ImportError as _imp_err:", "import core 相关"),
    "core/api_client.py": None,
    "core/damage_calc.py": None,
    "core/wiki_intro.py": None,
}
for rel in GUARDED:
    src = (ROOT / rel).read_text(encoding="utf-8")
    # 「except ImportError」之后（允许中间有注释/空行）出现 `if __package__:` + `raise`
    # 注：不要求 except 行以冒号结尾 —— 行尾可能跟着 `# pragma: no cover`，
    #     里面那个冒号会让贪婪匹配回溯失败（实测踩到）。
    ok = bool(re.search(
        r"except ImportError[^\n]*\n(?:[^\n]*\n){0,12}?\s+if __package__:\n\s+raise", src))
    check(f"★ {rel}：包内 ImportError 原样抛出（不再被兜底掩盖）", ok,
          "未找到 `if __package__: raise` 守卫")

# 行为验证：一个模拟「包内导入失败」的临时包，必须抛出**真错误**
with tempfile.TemporaryDirectory() as tmp:
    pkg = Path(tmp) / "tmppkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "guarded.py").write_text(textwrap.dedent('''
        try:
            from .missing_module import NEEDED   # 模拟「新代码要的东西还没落地」
        except ImportError:
            if __package__:
                raise
            NEEDED = "fallback"
    '''), encoding="utf-8")
    sys.path.insert(0, tmp)
    try:
        importlib.import_module("tmppkg.guarded")
        check("★ 行为：包内导入失败必须抛真错误（提到缺的那个模块）", False, "竟然导入成功了")
    except ImportError as exc:
        check("★ 行为：包内导入失败必须抛真错误（提到缺的那个模块）",
              "missing_module" in str(exc), str(exc))
    finally:
        sys.path.remove(tmp)
        for m in [k for k in sys.modules if k.startswith("tmppkg")]:
            del sys.modules[m]

check("★ 行为：非包上下文（__package__ 为空）仍可回退",
      bool(re.search(r"if __package__:\n\s+raise\n\s+(import|from) ", MAIN)),
      "main.py 的兜底导入不见了")

# ---------------------------------------------------------------------------
# 2. 检查点对齐 XX:05 UTC（换轮 00:00 UTC → 最坏 6h 变 ~5min）
# ---------------------------------------------------------------------------
check("★ 主循环用对齐助手而不是裸 sleep(6*3600)",
      "_secs_to_aligned_tick" in MAIN
      and "await asyncio.sleep(self._secs_to_aligned_tick())" in MAIN
      and "asyncio.sleep(6 * 3600)" not in MAIN,
      f"仍有 {MAIN.count('asyncio.sleep(6 * 3600)')} 处裸 sleep")
check("对齐助手有步长/分钟/抖动三个参数（抖动防扎堆）",
      bool(re.search(
          r"def _secs_to_aligned_tick\((?:self, )?step_hours: int = 6, minute: int = 5,"
          r"\s*\n?\s*jitter_min: int = 15\)", MAIN)))
check("主循环有**两处**用它（非 ready 分支与 ready 分支各一）",
      MAIN.count("await asyncio.sleep(self._secs_to_aligned_tick())") == 2,
      str(MAIN.count("await asyncio.sleep(self._secs_to_aligned_tick())")))

# ---------------------------------------------------------------------------
# 3. 社区快照轻轮询（没装 FS 的用户才走）
# ---------------------------------------------------------------------------
check("★ 有独立的 _community_autoloop（30 分钟一轮）",
      "async def _community_autoloop" in MAIN and "await asyncio.sleep(30 * 60)" in MAIN)
check("★ 它是被 create_task 起的（否则永不运行）",
      "asyncio.create_task(self._community_autoloop())" in MAIN)
check("★ 仅在 FS 不是 ready 时才读社区快照（有 FS 的不必读）",
      'if await self._flare_phase() == "ready":' in MAIN
      and MAIN.index("_community_autoloop") < MAIN.index('if await self._flare_phase() == "ready":'))
check("轻轮询失败只静默重试（不打扰不用该功能的用户）",
      "except Exception:  # noqa: BLE001 - 轻轮询失败不打扰" in MAIN)

# ---------------------------------------------------------------------------
# 4. 一致性：三个 refresh 出口名对得上（消费侧/发布侧共用词表）
# ---------------------------------------------------------------------------
api = (ROOT / "core" / "api_client.py").read_text(encoding="utf-8")
check("refresh_valence 会返回 no-community（取不到就静默跳过）",
      'return "no-community"' in api)
check("_refresh_from_community 只认 refreshed* 为成功（其余静默）",
      'st.startswith("refreshed")' in MAIN)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 加载/刷新路径健壮性守卫全部通过")
