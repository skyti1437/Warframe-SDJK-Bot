# -*- coding: utf-8 -*-
"""统一检查入口 scripts/run_checks.py 的退出行为自测（python3 tests/test_check_runner.py）

只在临时根目录里放假脚本（--root 指过去），不自动发现真实 tests/，避免递归启动整套测试。
覆盖：成功 / 含 [SKIP] 记录 / 失败 / 超时（进程组被结束）/ 零测试 / 缺审计脚本 / health 新鲜与过期。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "run_checks.py"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import core.api_client  # noqa: E402,F401  —— 先从真实树载入，health 用例的临时根才找得到
import run_checks as RC  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


def _tree(scripts: dict[str, str], audit: bool = True) -> Path:
    root = Path(tempfile.mkdtemp(prefix="wf_runner_"))
    (root / "tests").mkdir()
    for name, body in scripts.items():
        (root / "tests" / name).write_text(body, encoding="utf-8")
    if audit:
        (root / "tests" / "audit_damage_calc.py").write_text("print('审计结论：HIGH 0')\n", "utf-8")
    return root


def _run(root: Path, *extra: str, timeout: float = 120) -> tuple[int, str, dict]:
    logs = Path(tempfile.mkdtemp(prefix="wf_runner_logs_"))
    p = subprocess.run(
        [sys.executable, str(RUNNER), *extra, "--root", str(root), "--log-dir", str(logs)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    summary = json.loads((logs / "summary.json").read_text(encoding="utf-8"))
    shutil.rmtree(logs, ignore_errors=True)
    return p.returncode, p.stdout, summary


def _statuses(summary: dict, mode: str = "regression") -> dict[str, str]:
    return {r["name"]: r["status"] for r in summary[mode]["results"]}


# ① 全部成功 + 跳过记录如实列出（不折算成用例数）
_r = _tree(
    {
        "test_ok.py": "print('[PASS] a')\n",
        "test_skip.py": "print('[SKIP] 无某能力，跳过 X')\nprint('[PASS] b')\n",
    }
)
rc, out, sm = _run(_r, "--no-lint")
check("成功：退出 0", rc == 0, out[-400:])
check(
    "成功：两个脚本 + 审计各一条 PASS",
    _statuses(sm)
    == {
        "tests/test_ok.py": "PASS",
        "tests/test_skip.py": "PASS",
        "tests/audit_damage_calc.py": "PASS",
    },
    str(_statuses(sm)),
)
_skip = [r for r in sm["regression"]["results"] if r["name"] == "tests/test_skip.py"][0]["skips"]
check("跳过记录：原文保留 1 条", _skip == ["[SKIP] 无某能力，跳过 X"], str(_skip))
check("汇总行标出 [SKIP] 记录数", "[SKIP] 记录 1 条" in out, out[-300:])
shutil.rmtree(_r, ignore_errors=True)

# ② 任一失败 ⇒ 非零，且其余照跑
_r = _tree(
    {"test_a_fail.py": "import sys\nprint('[FAIL] x')\nsys.exit(1)\n", "test_b_ok.py": "pass\n"}
)
rc, out, sm = _run(_r, "--no-lint")
check("失败：退出非零", rc != 0)
check(
    "失败：失败项 FAIL、后续脚本仍执行",
    _statuses(sm).get("tests/test_a_fail.py") == "FAIL"
    and _statuses(sm).get("tests/test_b_ok.py") == "PASS",
    str(_statuses(sm)),
)
check("失败：输出给出日志路径", "日志：" in out, out[-300:])
shutil.rmtree(_r, ignore_errors=True)

# ③ 超时 ⇒ TIMEOUT、非零、子进程（含孙进程）被结束
_pidfile = Path(tempfile.mkdtemp()) / "pids"
_hang = (
    "import os, subprocess, sys, time\n"
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    f"open({str(_pidfile)!r}, 'w').write(f'{{os.getpid()}} {{c.pid}}')\n"
    "time.sleep(60)\n"
)
_r = _tree({"test_hang.py": _hang})
rc, out, sm = _run(_r, "--no-lint", "--timeout", "2")
check("超时：退出非零", rc != 0)
check(
    "超时：状态 TIMEOUT", _statuses(sm).get("tests/test_hang.py") == "TIMEOUT", str(_statuses(sm))
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # 已退出但未被回收的僵尸不算存活
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return True


_pids = [int(x) for x in _pidfile.read_text().split()] if _pidfile.exists() else []
check(
    "超时：脚本进程与其子进程都已结束",
    len(_pids) == 2 and not any(_alive(p) for p in _pids),
    str(_pids),
)
shutil.rmtree(_r, ignore_errors=True)
shutil.rmtree(_pidfile.parent, ignore_errors=True)

# ④ 零测试不算通过
_r = _tree({})
rc, out, sm = _run(_r, "--no-lint")
check("零测试：退出非零", rc != 0)
check("零测试：明确报 ERROR", _statuses(sm).get("tests/test_*.py") == "ERROR", str(_statuses(sm)))
shutil.rmtree(_r, ignore_errors=True)

# ⑤ 缺伤害审计脚本 ⇒ 非零（README 推荐的审计不许被悄悄漏掉）
_r = _tree({"test_ok.py": "pass\n"}, audit=False)
rc, out, sm = _run(_r, "--no-lint")
check(
    "缺审计：退出非零且报 ERROR",
    rc != 0 and _statuses(sm).get(RC.AUDIT) == "ERROR",
    str(_statuses(sm)),
)
shutil.rmtree(_r, ignore_errors=True)

# ⑥ health：固定时刻判新鲜 / 过期 / 缺失（临时根只放数据文件，判据同运行期）
_h = Path(tempfile.mkdtemp(prefix="wf_health_"))
(_h / "core" / "data" / "de").mkdir(parents=True)
(_h / "core" / "data" / "rotations.json").write_text(
    json.dumps(
        {
            "tenet": {
                "epoch": "2026-09-12T00:00:00+00:00",
                "period_hours": 96,
                "valence_snapshot": "2026-09-20T06:00:00+00:00",
            },
            "coda": {"epoch": "2026-09-13T00:00:00+00:00", "period_hours": 96},
        }
    ),
    encoding="utf-8",
)
(_h / "core" / "data" / "de" / "acrichis_week.json").write_text(
    json.dumps({"expiry": "2026-09-28T00:00:00+00:00"}), encoding="utf-8"
)
_at = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)
_items = {i["item"]: i for i in RC.freshness_items(_h, _at)}
check(
    "health：信条快照在本窗口 ⇒ 新鲜",
    _items["rotations.json::tenet.valence_snapshot"]["ok"] is True,
    str(_items),
)
check(
    "health：终幕缺快照 ⇒ 不新鲜且说明原因",
    _items["rotations.json::coda.valence_snapshot"]["ok"] is False
    and "缺失" in _items["rotations.json::coda.valence_snapshot"]["reason"],
    str(_items["rotations.json::coda.valence_snapshot"]),
)
check("health：货单未到期 ⇒ 新鲜", _items["de/acrichis_week.json::expiry"]["ok"] is True)
check(
    "health：输出含检查时刻与当前窗口",
    _items["rotations.json::tenet.valence_snapshot"]["checked_at"] == _at.isoformat()
    and _items["rotations.json::tenet.valence_snapshot"]["window"].startswith(
        "2026-09-20T00:00:00"
    ),
    str(_items["rotations.json::tenet.valence_snapshot"]),
)
_late = {
    i["item"]: i["ok"] for i in RC.freshness_items(_h, datetime(2026, 9, 28, tzinfo=timezone.utc))
}
check(
    "health：换轮后 / 到期时刻 ⇒ 信条过期、货单过期",
    _late["rotations.json::tenet.valence_snapshot"] is False
    and _late["de/acrichis_week.json::expiry"] is False,
    str(_late),
)
rc, out, sm = _run(_h, "health", "--now", "2026-09-21T00:00:00+00:00")
check("health CLI：有不新鲜项 ⇒ 退出非零", rc != 0, out[-300:])
check("health CLI：结果不混进 regression", "regression" not in sm and "health" in sm, str(list(sm)))
shutil.rmtree(_h, ignore_errors=True)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
