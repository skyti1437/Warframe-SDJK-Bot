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
_seed = {
    "tenet": {
        "mode": "refresh_only",
        "epoch": "2026-09-12T00:00:00+00:00",
        "period_hours": 96,
        "items": [{"en": f"Tenet {i}", "cn": f"信条{i}"} for i in range(5)],
    },
    "coda": {
        "mode": "batch_cycle",
        "epoch": "2026-09-13T00:00:00+00:00",
        "period_hours": 96,
        "anchor_idx": 0,
        "batch_label": ["A", "B"],
        "batches": [
            [{"en": f"Coda {i}-{j}", "cn": f"终幕{i}-{j}"} for j in range(7)] for i in range(2)
        ],
    },
}
(_h / "core" / "data" / "rotations.json").write_text(json.dumps(_seed), encoding="utf-8")
(_h / "core" / "data" / "de" / "acrichis_week.json").write_text(
    json.dumps(
        {
            "_en_catalog": {
                "Orokin Reactor": {"name": "奥罗金反应堆蓝图", "price": 20},
                "Orokin Catalyst": {"name": "奥罗金催化剂蓝图", "price": 20},
                "Exilus Warframe Adapter": {"name": "战甲特殊功能槽连接器", "price": 20},
                "Exilus Weapon Adapter": {"name": "武器特殊功能槽连接器", "price": 20},
                "Primary Arcane Adapter": {"name": "主要武器赋能槽连接器", "price": 25},
                "Secondary Arcane Adapter": {"name": "次要武器赋能槽连接器", "price": 25},
                "Forma": {"name": "Forma 蓝图", "price": 10},
                "Melee Riven Mod": {"name": "近战裂罅 MOD", "price": 15},
                "Pistol Riven Mod": {"name": "手枪裂罅 MOD", "price": 15},
                "Rifle Riven Mod": {"name": "步枪裂罅 MOD", "price": 15},
                "Kitgun Riven Mod": {"name": "射枪裂罅 MOD", "price": 15},
                "Zaw Riven Mod": {"name": "佐作裂罅 MOD", "price": 15},
                "Shotgun Riven Mod": {"name": "霰弹枪裂罅 MOD", "price": 15},
                "Companion Weapon Riven Mod": {"name": "同伴裂罅 MOD", "price": 20},
                "10,000 Kuva": {"name": "赤毒", "qty": 10000, "price": 10},
            },
            "_catalog_price_source": "wiki《Acrithis》页 offeringsBox 标价（2026-09-21 抓取），苦栓",
            "_reset_rule": "每周一 00:00 UTC 轮换（wiki《Acrithis》页实时口径）",
            "_过期口径": "expiry 为 UTC；卡面展示换算北京时间",
            "_zh_official_notes": {
                "Amp Arcane Adapter": "增幅器赋能槽连接器",
                "Primary Arcane Adapter": "主要武器赋能槽连接器",
                "Secondary Arcane Adapter": "次要武器赋能槽连接器",
                "Melee Arcane Adapter": "近战武器赋能槽连接器",
                "Archgun Arcane Adapter": "曲翼枪械赋能槽连接器",
            },
        }
    ),
    encoding="utf-8",
)
_at = datetime(2026, 9, 21, tzinfo=timezone.utc)
_items = {i["item"]: i for i in RC.freshness_items(_h, _at)}
check("health：无值种子结构合法", _items["rotations.json::seed_structure"]["ok"], str(_items))
check("health：言录使静态目录通过", _items["de/acrichis_week.json::seed_structure"]["ok"])
check(
    "health：输出含检查时刻",
    _items["rotations.json::seed_structure"]["checked_at"] == _at.isoformat(),
)
_late = {
    i["item"]: i["ok"] for i in RC.freshness_items(_h, datetime(2026, 9, 28, tzinfo=timezone.utc))
}
check(
    "health：言录使静态目录不依赖时刻", _late["de/acrichis_week.json::seed_structure"], str(_late)
)
_seed["tenet"]["items"][0]["bonus"] = 40
(_h / "core" / "data" / "rotations.json").write_text(json.dumps(_seed), encoding="utf-8")
_bad = {i["item"]: i for i in RC.freshness_items(_h, _at)}
check("health：单个动态值残留拒绝", not _bad["rotations.json::seed_structure"]["ok"], str(_bad))
rc, out, sm = _run(_h, "health", "--now", "2026-09-21T00:00:00+00:00")
check("health CLI：有不新鲜项 ⇒ 退出非零", rc != 0, out[-300:])
check("health CLI：结果不混进 regression", "regression" not in sm and "health" in sm, str(list(sm)))
shutil.rmtree(_h, ignore_errors=True)

# ⑦ health：数据登记表 —— 未登记文件不论风险都阻断（core/data_registry 接入）
_h2 = Path(tempfile.mkdtemp(prefix="wf_registry_health_"))
(_h2 / "core" / "data" / "de").mkdir(parents=True)
(_h2 / "core" / "data" / "rotations.json").write_text(
    json.dumps(
        {
            "tenet": {
                "epoch": "2026-09-12T00:00:00+00:00",
                "period_hours": 96,
                "valence_snapshot": "2026-09-20T06:00:00+00:00",
            },
            "coda": {
                "epoch": "2026-09-12T00:00:00+00:00",
                "period_hours": 96,
                "valence_snapshot": "2026-09-20T06:00:00+00:00",
            },
        }
    ),
    encoding="utf-8",
)
(_h2 / "core" / "data" / "de" / "acrichis_week.json").write_text(
    json.dumps({"expiry": "2026-10-01T00:00:00+00:00"}), encoding="utf-8"
)
(_h2 / "core" / "data" / "arb_ratings.json").write_text(
    json.dumps({"_updated": "2026-09-01T00:00:00+00:00"}), encoding="utf-8"
)
# 现役登记表里没有「高风险 + 按年龄判」的项了（wiki_disp 2026-10 倾向取消后冻结），
# 借仲裁评级临时标高风险来走这条规则（本段末还原）。
from core import data_registry as _DR7  # noqa: E402

_arb_risk7 = _DR7.REGISTRY["arb_ratings.json"]["risk"]
_DR7.REGISTRY["arb_ratings.json"]["risk"] = "high"
(_h2 / "core" / "data" / "zzz_unregistered.json").write_text("{}", encoding="utf-8")
_at2 = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)
_hit = {i["item"]: i for i in RC.registry_items(_h2, _at2)}
check(
    "登记表：未登记文件 ⇒ 条目不通过",
    _hit.get("registry::zzz_unregistered.json", {}).get("ok") is False,
    str(sorted(_hit)[:5]),
)
check(
    "登记表：未登记条目说明写明原因",
    "未登记" in _hit.get("registry::zzz_unregistered.json", {}).get("reason", ""),
    str(_hit.get("registry::zzz_unregistered.json")),
)
check(
    "登记表：高风险过期项算警告（ok=True，不计失败）",
    _hit.get("registry::arb_ratings.json", {}).get("ok") is True
    and _hit.get("registry::arb_ratings.json", {}).get("warn") is True,
    str(_hit.get("registry::arb_ratings.json")),
)
_hlogs = Path(tempfile.mkdtemp(prefix="wf_registry_logs_"))
_hres = {r.name: r for r in RC.health(_h2, _hlogs, _at2)}
check(
    "health：登记表未登记 ⇒ 整体未通过",
    _hres.get("registry::zzz_unregistered.json", None) is not None
    and _hres["registry::zzz_unregistered.json"].status == "FAIL"
    and not all(r.status == "PASS" for r in _hres.values()),
    str({k: v.status for k, v in _hres.items() if k.startswith("registry::")}),
)
_DR7.REGISTRY["arb_ratings.json"]["risk"] = _arb_risk7
shutil.rmtree(_hlogs, ignore_errors=True)
shutil.rmtree(_h2, ignore_errors=True)

# 只读巡检真实CLI：静态种子双向校验，不联网且不写被检树。
with tempfile.TemporaryDirectory(prefix="wf_patrol_") as _patrol_td:
    _pt = Path(_patrol_td)
    (_pt / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/readonly_patrol.py", _pt / "scripts/readonly_patrol.py")
    shutil.copytree(
        ROOT / "core", _pt / "core", ignore=shutil.ignore_patterns("data", "__pycache__", "*.pyc")
    )
    (_pt / "core/data/de").mkdir(parents=True)
    _rot_seed = json.loads((ROOT / "core/data/rotations.json").read_text(encoding="utf-8"))
    _acr_seed = json.loads((ROOT / "core/data/de/acrichis_week.json").read_text(encoding="utf-8"))
    (_pt / "sitecustomize.py").write_text(
        "import socket\n"
        "def denied(*args, **kwargs): raise AssertionError('NETWORK_FORBIDDEN')\n"
        "socket.socket = denied\nsocket.create_connection = denied\n",
        encoding="utf-8",
    )
    for _case in ("clean", "valence_dynamic", "acr_dynamic", "missing_static", "bad_rule"):
        _rot = json.loads(json.dumps(_rot_seed))
        _acr = json.loads(json.dumps(_acr_seed))
        if _case == "valence_dynamic":
            _rot["tenet"]["valence_snapshot"] = None
        elif _case == "acr_dynamic":
            _acr["items"] = []
        elif _case == "missing_static":
            del _acr["_en_catalog"][next(iter(_acr["_en_catalog"]))]
        elif _case == "bad_rule":
            _rot["acrichis"]["reset_weekday"] = 6
        (_pt / "core/data/rotations.json").write_text(json.dumps(_rot), encoding="utf-8")
        (_pt / "core/data/de/acrichis_week.json").write_text(json.dumps(_acr), encoding="utf-8")

        def _patrol_files():
            return {
                f.relative_to(_pt).as_posix(): (f.read_bytes(), f.stat().st_mtime_ns)
                for f in _pt.rglob("*")
                if f.is_file()
            }

        _before = _patrol_files()
        _env = {**os.environ, "PYTHONPATH": str(_pt), "PYTHONDONTWRITEBYTECODE": "1"}
        _proc = subprocess.run(
            [sys.executable, "-B", str(_pt / "scripts/readonly_patrol.py")],
            cwd=_pt,
            env=_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        check(
            f"只读巡检CLI {_case}双向判定",
            _proc.returncode == (0 if _case == "clean" else 1),
            _proc.stdout + _proc.stderr,
        )
        check(
            f"只读巡检CLI {_case}禁网且零写入",
            "NETWORK_FORBIDDEN" not in _proc.stdout + _proc.stderr and _before == _patrol_files(),
        )
        check(
            f"只读巡检CLI {_case}不宣称运行期健康",
            "运行期健康" in _proc.stdout and "WarframeClient" not in _proc.stderr,
        )

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
