# -*- coding: utf-8 -*-
"""市场规范合规守卫（python3 tests/test_market_compliance.py）

AstrBot 插件市场（cloud.astrbot.app）的自动安全检查（LLM Guard）曾以此理由
把 v1.0.0 / v1.0.1 判为 **Rejected**：

1. **日志**：日志器必须从 ``astrbot.api.logger`` 获取，禁止自行
   ``logging.getLogger(...)``（当时 core/api_client.py 与 core/render.py 违规）；
2. **数据位置**：用户数据与运行期快照必须写 ``data/plugin_data/<插件名>``，
   不得落在插件包目录（插件包对安装用户只读，升级还会整包覆盖，写进去必然丢）。

这里把两条钉死，并顺带守住插件身份三处一致，免得下次改一处忘一处又被打回。
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 1. 日志：一律走 astrbot.api.logger（禁止自行 logging.getLogger）
# ---------------------------------------------------------------------------
runtime_sources = [ROOT / "main.py"] + sorted((ROOT / "core").glob("*.py"))
offenders = []
for p in runtime_sources:
    if not p.is_file():
        continue
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "logging.getLogger(" in line or re.match(r"^import logging\b", stripped):
            offenders.append(f"{p.relative_to(ROOT)}:{i}")
check("运行期代码无 logging.getLogger / import logging",
      not offenders, "、".join(offenders))

lc = ROOT / "core" / "logging_compat.py"
check("日志出口收敛在 core/logging_compat.py",
      lc.is_file() and "from astrbot.api import logger" in lc.read_text(encoding="utf-8"))
check("main.py 的日志同样来自 astrbot.api.logger",
      "from astrbot.api import AstrBotConfig, logger" in
      (ROOT / "main.py").read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# 2. 数据位置：写盘一律落到运行期目录（data/plugin_data/<插件名>）
# ---------------------------------------------------------------------------
main_src = (ROOT / "main.py").read_text(encoding="utf-8")
api_src = (ROOT / "core" / "api_client.py").read_text(encoding="utf-8")

check("main.py 用 AstrBot 官方数据目录 API（StarTools.get_data_dir）",
      "StarTools.get_data_dir(" in main_src and "_resolve_data_dir" in main_src)
check("用户数据（组设置/订阅/卡片缓存）挂在运行期数据目录下",
      'GroupStore(data_dir / "groups.json"' in main_src
      and 'SubscriptionStore(data_dir / "subscriptions.json")' in main_src
      and 'ImageRenderer(data_dir / "cards")' in main_src)
check("不再往插件目录 runtime/ 写数据",
      'runtime = PLUGIN_DIR / "runtime"' not in main_src,
      "main.py 仍出现 runtime = PLUGIN_DIR / 'runtime'")

# 运行期快照（排行落盘 / 紫卡周报 / 倾向表 / 轮换）必须经 core.paths
stale = [pat for pat in ('DATA_DIR / "wm_ranks.json"', 'DATA_DIR / "riven_weekly.json"',
                         'DATA_DIR / "de" / "wiki_disp.json"',
                         'DATA_DIR / "rotations.json"') if pat in api_src]
check("core/api_client.py 的运行期快照不再指向包内 core/data",
      not stale, "、".join(stale))
check("core/api_client.py 写盘统一经 core.paths.write_path",
      api_src.count("paths.write_path(") >= 4 and "paths.read_path(" in api_src)

# 行为验证：注入运行目录后，写路径必须落在注入目录内
import core.paths as P  # noqa: E402

tmp = Path(tempfile.mkdtemp())
P.set_run_dir(tmp)
w = P.write_path("de/wiki_disp.json")
check("write_path 落在注入的运行期目录内", str(w).startswith(str(tmp)), str(w))
r = P.read_path("rotations.json")           # 运行目录没有 → 回退包内种子
check("read_path 缺文件时回退包内种子",
      r.exists() and str(r).startswith(str(P.PKG_DATA)), str(r))
P.set_run_dir(None)

# ---------------------------------------------------------------------------
# 3. 插件身份三处一致（metadata.yaml / PLUGIN_NAME / @register）
# ---------------------------------------------------------------------------
meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
meta_name = ""
for line in meta.splitlines():
    if line.startswith("name:"):
        meta_name = line.split(":", 1)[1].strip()
        break
m_reg = re.search(r'@register\(\s*"([^"]+)"', main_src)
reg_name = m_reg.group(1) if m_reg else ""
m_const = re.search(r'^PLUGIN_NAME = "([^"]+)"', main_src, re.M)
const_name = m_const.group(1) if m_const else ""

check("metadata.yaml 的 name 是合法 Python 标识符",
      bool(meta_name) and meta_name.isidentifier(), meta_name or "缺 name")
check("metadata.yaml.name == main.PLUGIN_NAME", meta_name == const_name,
      f"{meta_name} vs {const_name}")
check("@register 的插件名 == metadata.yaml.name", reg_name == meta_name,
      f"{reg_name} vs {meta_name}")
check("市场展示名放 display_name（不占用 name）", "display_name:" in meta)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 市场规范合规守卫全部通过")
