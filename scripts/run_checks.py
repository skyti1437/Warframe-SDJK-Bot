# -*- coding: utf-8 -*-
"""统一检查入口（本地与 CI 同一条命令）。

用法::

    python scripts/run_checks.py               # = regression（默认）
    python scripts/run_checks.py regression    # 代码回归：ruff + tests/test_*.py + 伤害审计
    python scripts/run_checks.py health        # 数据新鲜度：随包效价快照 / 言录使货单（只读）
    python scripts/run_checks.py all           # 两类都跑，分别汇总，任一失败即非零

两类结果分开报告：regression 失败 = 代码逻辑回归；health 失败 = 随包时效数据已过期
（需按 AGENTS / 发版流程回写种子），**不是**算法回归。health 只读本地
``core/data``，不联网、不写运行目录、不刷新数据。

regression 每个脚本用 ``sys.executable`` 独立子进程、固定工作目录（仓库根）运行，
互不共享桩对象与全局状态；失败 / 超时 / 起不来都计非零，超时会结束整个子进程组。
脚本内部的跳过只能从输出里的 ``[SKIP]`` 行识别，汇总里如实标为「跳过记录」，
不折算成用例数。日志写在被测树之外（默认系统临时目录），ruff 不写缓存。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUDIT = "tests/audit_damage_calc.py"


@dataclass
class Result:
    name: str
    cmd: list[str]
    status: str  # PASS / FAIL / TIMEOUT / ERROR
    rc: int | None = None
    seconds: float = 0.0
    log: str = ""
    skips: list[str] = field(default_factory=list)
    detail: str = ""


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["RUFF_NO_CACHE"] = "true"
    return env


def run_one(name: str, cmd: list[str], cwd: Path, log_dir: Path, timeout: float) -> Result:
    """跑一个子进程检查；stdout+stderr 原样落盘，超时结束整个进程组。"""
    log = log_dir / (name.replace("/", "__") + ".log")
    t0 = time.monotonic()
    res = Result(name=name, cmd=cmd, status="ERROR", log=str(log))
    try:
        with open(log, "wb") as fh:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=_env(),
                stdout=fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                res.rc = proc.wait(timeout=timeout)
                res.status = "PASS" if res.rc == 0 else "FAIL"
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait()
                res.status = "TIMEOUT"
                res.detail = f"超过 {timeout:g}s，已结束进程组"
    except OSError as exc:
        res.status, res.detail = "ERROR", f"启动失败：{exc}"
    res.seconds = round(time.monotonic() - t0, 2)
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
        res.skips = [ln.strip() for ln in text.splitlines() if ln.lstrip().startswith("[SKIP]")]
    except OSError:
        pass
    return res


def discover_tests(root: Path) -> list[Path]:
    return sorted((root / "tests").glob("test_*.py"))


def regression(root: Path, log_dir: Path, timeout: float, lint: bool = True) -> list[Result]:
    out: list[Result] = []
    py = sys.executable
    if lint:
        if (
            shutil.which("ruff") is None
            and subprocess.run([py, "-m", "ruff", "--version"], capture_output=True).returncode
        ):
            out.append(
                Result(
                    "ruff",
                    [py, "-m", "ruff"],
                    "ERROR",
                    detail="未安装 ruff（开发依赖，见 scripts/requirements-dev.txt）",
                )
            )
        else:
            out.append(
                run_one(
                    "ruff-check",
                    [py, "-m", "ruff", "check", "--no-cache", "."],
                    root,
                    log_dir,
                    timeout,
                )
            )
            out.append(
                run_one(
                    "ruff-format",
                    [py, "-m", "ruff", "format", "--check", "--no-cache", "."],
                    root,
                    log_dir,
                    timeout,
                )
            )
    tests = discover_tests(root)
    if not tests:
        out.append(
            Result(
                "tests/test_*.py",
                [],
                "ERROR",
                detail="未发现任何 tests/test_*.py（零测试不算通过）",
            )
        )
    for t in tests:
        rel = t.relative_to(root).as_posix()
        out.append(run_one(rel, [py, rel], root, log_dir, timeout))
    audit = root / AUDIT
    if audit.is_file():
        out.append(run_one(AUDIT, [py, AUDIT], root, log_dir, timeout))
    else:
        out.append(Result(AUDIT, [], "ERROR", detail="缺少伤害审计脚本"))
    return out


# --------------------------------------------------------------------- health
def freshness_items(root: Path, now=None) -> list[dict]:
    """随包时效种子逐项判定（判据同运行期：``WarframeClient.valence_is_stale`` 的窗口算法、
    言录使 ``expiry <= now`` 即过期）。只读 ``root/core/data``。"""
    from datetime import datetime, timedelta, timezone

    sys.dont_write_bytecode = True  # 进程内导入 core，不往被测树写 __pycache__
    sys.path.insert(0, str(root))
    from core.api_client import WarframeClient

    now = now or datetime.now(timezone.utc)
    items: list[dict] = []
    rot_path = root / "core" / "data" / "rotations.json"
    try:
        rot = json.loads(rot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        rot = {}
        items.append({"item": "rotations.json", "ok": False, "reason": f"不可读/损坏：{exc}"})
    for sec in ("tenet", "coda"):
        d = rot.get(sec) or {}
        row = {
            "item": f"rotations.json::{sec}.valence_snapshot",
            "snapshot": d.get("valence_snapshot"),
        }
        try:
            epoch = datetime.fromisoformat(d["epoch"])
            period = timedelta(hours=int(d.get("period_hours", 96)))
            win = epoch + (now - epoch) // period * period
            row["window"] = f"{win.isoformat()} ~ {(win + period).isoformat()}"
            row["ok"] = not WarframeClient.valence_is_stale(d, now)
            if not row["ok"]:
                row["reason"] = (
                    "快照缺失"
                    if not d.get("valence_snapshot")
                    else "快照早于当前窗口起点（上一轮数据）"
                )
        except (KeyError, TypeError, ValueError) as exc:
            row["ok"], row["reason"] = False, f"epoch/周期字段畸形：{exc!r}"
        items.append(row)
    acr_path = root / "core" / "data" / "de" / "acrichis_week.json"
    row = {"item": "de/acrichis_week.json::expiry"}
    try:
        acr = json.loads(acr_path.read_text(encoding="utf-8"))
        row["snapshot"] = acr.get("expiry")
        row["ok"] = datetime.fromisoformat(acr.get("expiry") or "") > now
        if not row["ok"]:
            row["reason"] = "货单已到期（expiry <= 检查时刻）"
    except (OSError, ValueError) as exc:
        row["ok"], row["reason"] = False, f"不可读或 expiry 畸形：{exc}"
    items.append(row)
    for r in items:
        r["checked_at"] = now.isoformat()
    return items


def health(root: Path, log_dir: Path, now=None) -> list[Result]:
    log = log_dir / "health.json"
    try:
        items = freshness_items(root, now)
    except Exception as exc:  # noqa: BLE001
        return [Result("health", [], "ERROR", detail=f"新鲜度检查起不来：{exc!r}")]
    log.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    out = []
    for it in items:
        detail = (
            f"快照={it.get('snapshot')} 检查时刻={it['checked_at']}"
            + (f" 当前窗口={it['window']}" if it.get("window") else "")
            + (f" 原因={it['reason']}" if it.get("reason") else "")
        )
        out.append(
            Result(it["item"], [], "PASS" if it.get("ok") else "FAIL", log=str(log), detail=detail)
        )
    return out


# ------------------------------------------------------------------- 汇总输出
def report(title: str, results: list[Result]) -> bool:
    print(f"\n==== {title} ====")
    for r in results:
        line = f"[{r.status}] {r.name}"
        if r.seconds:
            line += f"  ({r.seconds}s)"
        if r.skips:
            line += f"  · 含 {len(r.skips)} 条 [SKIP] 记录"
        print(line)
        if r.cmd:
            print(f"    命令：{' '.join(r.cmd)}")
        if r.detail:
            print(f"    {r.detail}")
        if r.log and r.status != "PASS":
            print(f"    日志：{r.log}")
        for s in r.skips:
            print(f"    {s}")
    bad = [r for r in results if r.status != "PASS"]
    counts = {
        s: sum(1 for r in results if r.status == s) for s in ("PASS", "FAIL", "TIMEOUT", "ERROR")
    }
    n_skip = sum(len(r.skips) for r in results)
    print(
        f"---- {title} 汇总：{len(results)} 项｜通过 {counts['PASS']}｜失败 {counts['FAIL']}"
        f"｜超时 {counts['TIMEOUT']}｜起不来 {counts['ERROR']}｜[SKIP] 记录 {n_skip} 条"
        f"（出现在 {sum(1 for r in results if r.skips)} 个脚本）"
    )
    return not bad and bool(results)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="统一检查入口：regression / health / all")
    ap.add_argument(
        "mode", nargs="?", default="regression", choices=("regression", "health", "all")
    )
    ap.add_argument("--root", type=Path, default=ROOT, help="被测树根目录（默认本仓库）")
    ap.add_argument("--timeout", type=float, default=600, help="单个检查超时秒数（默认 600）")
    ap.add_argument("--log-dir", type=Path, help="日志目录（默认系统临时目录下新建，不写被测树）")
    ap.add_argument("--no-lint", action="store_true", help="跳过 ruff（仅供入口自测用）")
    ap.add_argument(
        "--now",
        type=lambda v: __import__("datetime").datetime.fromisoformat(v),
        help="health 的检查时刻（ISO 8601 带时区；默认当前 UTC，用于复核某一时刻的新鲜度）",
    )
    args = ap.parse_args(argv)

    root = args.root.resolve()
    log_dir = (args.log_dir or Path(tempfile.mkdtemp(prefix="wf-checks-"))).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"被测树：{root}\nPython：{sys.version.split()[0]} ({sys.executable})\n日志目录：{log_dir}"
    )

    summary: dict[str, dict] = {}
    ok = True
    if args.mode in ("regression", "all"):
        rs = regression(root, log_dir, args.timeout, lint=not args.no_lint)
        good = report("regression（代码回归）", rs)
        summary["regression"] = {"ok": good, "results": [asdict(r) for r in rs]}
        ok &= good
    if args.mode in ("health", "all"):
        hs = health(root, log_dir, args.now)
        good = report("health（随包数据新鲜度，非代码回归）", hs)
        summary["health"] = {"ok": good, "results": [asdict(r) for r in hs]}
        ok &= good
    (log_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    parts = "，".join(f"{k}={'通过' if v['ok'] else '未通过'}" for k, v in summary.items())
    print(f"\n结论：{'全部通过' if ok else '有未通过项'}（{parts}）")
    print(f"结构化结果：{log_dir / 'summary.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
