# -*- coding: utf-8 -*-
"""发版计划器 scripts/release_plan.py 的回归（python3 tests/test_release_plan.py）

不联网；临时仓夹具只在 tempfile 目录里做 git 写操作，**本仓库零写操作**
（真实仓库侧只跑只读 CLI，并在运行前后比对 `git status --porcelain`）。

覆盖：next_version 进位规则 / 7 处版本落点一致与「制造一处不一致」两种 /
建议下一版 / base 侧已抬过的提示 / 改动集分组（新增、删除）/ 三处基线只报告不改 +
临时仓里投影降级不抛异常 / CHANGELOG 候选与内部分类 + 首条小节判定 /
待审草稿行号 / --patch-out 可被 `git apply --check`（含「落在仓库内被拒」）/
真实仓库 --json 可解析 / 只读。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_plan.py"
# dist/ 不进分发包（开源 stage / 公开仓里没有它）：与 tests/test_sync_guard.py 同口径跳过
if not (ROOT / "dist" / "sync_opensource.py").is_file():
    print("[SKIP] dist/sync_opensource.py 不在当前副本内（开源包），跳过")
    sys.exit(0)
sys.path.insert(0, str(ROOT / "scripts"))

import release_plan as RP  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _status() -> str:
    return _git(ROOT, "status", "--porcelain").stdout


# ---------------------------------------------------------------------------
# ① 版本号进位规则（用户口径：每次 +0.0.1，每位最大 9 后进位）
# ---------------------------------------------------------------------------
check("next_version：1.2.1 → 1.2.2", RP.next_version("1.2.1") == "1.2.2")
check("next_version：1.2.9 → 1.3.0（个位满 9 进位）", RP.next_version("1.2.9") == "1.3.0")
check("next_version：1.9.9 → 2.0.0（两级连进）", RP.next_version("1.9.9") == "2.0.0")
check("next_version：0.0.9 → 0.1.0", RP.next_version("0.0.9") == "0.1.0")

for bad in ("1.2", "1.2.3.4", "abc", "", "1.2.10", "1.2.-1", "v"):
    try:
        RP.next_version(bad)
        ok, why = False, f"{bad!r} 未抛 ValueError"
    except ValueError:
        ok, why = True, ""
    check(f"next_version：非法输入 {bad!r} 抛 ValueError", ok, why)


# ---------------------------------------------------------------------------
# 临时仓夹具（git 写操作只发生在 tempfile 目录里）
# ---------------------------------------------------------------------------
LANDING_TEXTS = {
    "README.md": "# Warframe SDJKBOT · AstrBot 插件 v{ver}\n\n给 AstrBot 用的 Warframe 工具集。\n",
    "metadata.yaml": "name: x\ndisplay_name: X\nversion: v{ver}\nauthor: a\ncategory: 工具\n",
    "core/__init__.py": '# -*- coding: utf-8 -*-\n"""x"""\n\n__version__ = "{ver}"\n',
    "main.py": (
        '# -*- coding: utf-8 -*-\nBRAND = "X"\n\n\n@register(\n'
        '    "x", "SDJK", f"{BRAND}：说明", "{ver}"\n)\n'
    ),
    "CHANGELOG.md": "# 更新日志\n\n本文件记录主要版本更新。\n\n## v1.2.1\n\n- 内容\n",
    "dist/OPENSOURCE_README.md": "# AstrBot 插件 · Warframe SDJKBOT v{ver}（`x`）\n\n说明\n",
    "dist/SDJKwfbot_README.md": "# Warframe SDJKBOT · AstrBot 插件 v{ver}\n\n说明\n",
}


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(root: Path, msg: str) -> str:
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _fixture(mismatch: bool = False) -> tuple[Path, str]:
    """最小仓：7 个版本落点 + 几个提交（内部 / 候选 / 删除各一）。

    ``mismatch=True`` 时把 ``core/__init__.py`` 写成 1.2.0（制造一处不一致）。
    """
    root = Path(tempfile.mkdtemp(prefix="wf_relplan_fixture_"))
    _git(root, "init", "-q")
    for rel, tpl in LANDING_TEXTS.items():
        ver = "1.2.0" if (mismatch and rel == "core/__init__.py") else "1.2.1"
        # 用 replace 而不是 format：main.py 模板里有 f-string 的花括号
        _write(root, rel, tpl.replace("{ver}", ver))
    _write(root, "tests/test_a.py", "print('a')\n")
    _write(root, "scripts/old.py", "print('old')\n")
    base = _commit(root, "init: 最小仓")
    _write(root, "tests/test_a.py", "print('b')\n")
    _commit(root, "test: 只改测试")  # 内部
    _write(root, "core/x.py", "X = 1\n")
    _commit(root, "feat(core): 新增 x")  # 候选（运行期面）
    _write(root, "tests/test_a.py", "print('c')\n")
    _commit(root, "test: 再改测试")  # 内部
    (root / "scripts" / "old.py").unlink()
    _commit(root, "chore(scripts): 删旧脚本")  # 内部（删除项）
    return root, base


# ---------------------------------------------------------------------------
# ② 夹具：版本落点一致 + 改动集分组 + CHANGELOG 分类 + 草稿行号
# ---------------------------------------------------------------------------
_fx, _base = _fixture()
plan = RP.build_plan(_fx, _base, "HEAD")
ver, chg, log, bl, dr = (
    plan["versions"],
    plan["changes"],
    plan["changelog"],
    plan["baselines"],
    plan["draft"],
)

check(
    "夹具：7 处版本落点都读出来且一致",
    ver["agree"] is True and all(x["version"] for x in ver["landings"]),
    str(ver["landings"]),
)
check("夹具：建议下一版 1.2.2", ver["next"] == "1.2.2", str(ver))
check(
    "夹具：base 与 head 同版 ⇒ 无「已抬过」提示",
    ver["head_bumped"] is False and ver["note"] == "",
    str(ver["note"]),
)
check(
    "夹具：新增 / 删除分类正确",
    chg["added"] == ["core/x.py"] and chg["deleted"] == ["scripts/old.py"],
    str(chg["added"]) + str(chg["deleted"]),
)
check(
    "夹具：按面分组（运行期面新增 / 测试改 / 工具删）",
    chg["faces"]["运行期面"]["added"] == ["core/x.py"]
    and chg["faces"]["测试"]["modified"] == ["tests/test_a.py"]
    and chg["faces"]["工具"]["deleted"] == ["scripts/old.py"],
    str(chg["faces"]),
)
check("夹具：候选=1（动了运行期面）", len(log["candidates"]) == 1, str(log["candidates"]))
check(
    "夹具：候选提交列出运行期文件",
    log["candidates"][0]["runtime_files"] == ["core/x.py"],
    str(log["candidates"][0]),
)
check("夹具：内部=3（只改 tests/ 或 scripts/）", len(log["internal"]) == 3, str(log["internal"]))
check(
    "夹具：CHANGELOG 首条 v1.2.1 ≠ 建议 1.2.2 ⇒ 需新增小节",
    log["ready"] is False and log["needs_section"] == "1.2.2",
    str(log),
)
check(
    "夹具：草稿 6 处可机械替换（CHANGELOG 只提示）",
    sum(1 for e in dr["edits"] if not e["manual"]) == 6,
    str(dr["edits"]),
)
check(
    "夹具：草稿行号正确（core/__init__.py 第 4 行 / README 第 1 行）",
    next(e["line_no"] for e in dr["edits"] if e["path"] == "core/__init__.py") == 4
    and next(e["line_no"] for e in dr["edits"] if e["path"] == "README.md") == 1,
    str(dr["edits"]),
)
check(
    "夹具：基线一节降级为「无法计算投影」且不抛异常",
    bl["stage_proj"] is None and any("降级" in n for n in bl["notes"]),
    f"stage_proj={bl['stage_proj']} notes={bl['notes']}",
)
check("夹具：三处基线不一致 ⇒ 标需人工审定", bl["stage_consistent"] is False, str(bl))
check(
    "夹具：市场件基线（AGENTS.md 不在）不一致 ⇒ 标需人工审定",
    bl["market_consistent"] is False,
    str(bl["market_baselines"]),
)
check(
    "夹具：文本渲染可出（含末行摘要）",
    RP.summary_line(plan).startswith("RELEASE_PLAN|")
    and "|next=1.2.2" in RP.summary_line(plan)
    and "[6] 发版前闸门清单" in RP.render_text(plan),
)

# ---------------------------------------------------------------------------
# ③ 夹具：制造一处不一致（core/__init__.py = 1.2.0）
# ---------------------------------------------------------------------------
_fx2, _base2 = _fixture(mismatch=True)
plan2 = RP.build_plan(_fx2, _base2, "HEAD")
ver2 = plan2["versions"]
check("夹具（不一致）：五处落点判定为否", ver2["agree_five"] is False, str(ver2))
check(
    "夹具（不一致）：两份门面 README 报不同号",
    ver2["extras_mismatch"] == ["dist/OPENSOURCE_README.md", "dist/SDJKwfbot_README.md"],
    str(ver2["extras_mismatch"]),
)
check(
    "夹具（不一致）：主版本取 core/__init__.py=1.2.0，下一版 1.2.1",
    ver2["current"] == "1.2.0" and ver2["next"] == "1.2.1",
    str(ver2),
)
check(
    "夹具（不一致）：CHANGELOG 首条已 = 建议下一版 ⇒ ready",
    plan2["changelog"]["ready"] is True and plan2["changelog"]["needs_section"] is None,
    str(plan2["changelog"]),
)

# ---------------------------------------------------------------------------
# ④ --patch-out：夹具上生成 → git apply --check / apply 都通过
# ---------------------------------------------------------------------------
_out = Path(tempfile.mkdtemp(prefix="wf_relplan_out_"))
_edits = RP.section_draft(_fx, "HEAD", RP.section_versions(_fx, _base, "HEAD"))["edits"]
_patch = RP.make_patch(_fx, "HEAD", _edits)
_pf = _out / "fixture.diff"
_pf.write_text(_patch, encoding="utf-8")
check(
    "patch：含 6 个文件的 hunk", _patch.count("--- a/") == 6 and _patch.count("+++ b/") == 6, _patch
)
_r = _git(_fx, "apply", "--check", str(_pf))
check("patch：夹具仓 git apply --check 通过", _r.returncode == 0, _r.stderr)
_r = _git(_fx, "apply", str(_pf))
check(
    "patch：apply 后 core/__init__.py 抬到 1.2.2",
    _r.returncode == 0 and "1.2.2" in (_fx / "core" / "__init__.py").read_text("utf-8"),
    _r.stderr,
)
_heads = [ln for ln in _patch.splitlines() if ln.startswith("@@")]
_counts = [
    int(m.group(1)) for m in (re.fullmatch(r"@@ -\d+,(\d+) \+\d+,(\d+) @@", h) for h in _heads) if m
]
check(
    "patch：每个 hunk 都带上下文（零上下文过不了 git apply）",
    len(_heads) == 6 and len(_counts) == 6 and all(c >= 2 for c in _counts),
    str(_heads),
)
# ④b 发版提交已就位（head 已抬到 1.2.2、CHANGELOG 首条 v1.2.2）：目标版本即 1.2.2，不再 +1
#     （2026-10-17 实战发现：抬版后计划器仍拿 1.2.3 对 CHANGELOG，闸门误报）
_fx3, _base3 = _fixture()
for _rel, _tpl in LANDING_TEXTS.items():
    if _rel != "CHANGELOG.md":
        _write(_fx3, _rel, _tpl.replace("{ver}", "1.2.2"))
_write(_fx3, "CHANGELOG.md", "# 更新日志\n\n## v1.2.2\n\n- 新\n\n## v1.2.1\n\n- 内容\n")
_commit(_fx3, "release: v1.2.2")
plan3 = RP.build_plan(_fx3, _base3, "HEAD")
check(
    "已抬版：目标版本 = 当前 1.2.2（不再建议 1.2.3）",
    plan3["versions"]["head_bumped"] is True and plan3["versions"]["next"] == "1.2.2",
    str(plan3["versions"].get("next")),
)
check("已抬版：CHANGELOG 首条 = 目标 ⇒ ready", plan3["changelog"]["ready"] is True)
check("已抬版：草稿无待改行", plan3["draft"]["edits"] == [], str(plan3["draft"]["edits"]))
shutil.rmtree(_fx, ignore_errors=True)
shutil.rmtree(_fx2, ignore_errors=True)
shutil.rmtree(_fx3, ignore_errors=True)


# ---------------------------------------------------------------------------
# ⑤ 真实仓库：CLI 只读跑通 + --json 可解析 + patch-out 拒绝仓内路径
# ---------------------------------------------------------------------------
_before = _status()
_p = _run()
check("CLI：文本模式退出 0", _p.returncode == 0, _p.stderr[-300:])
_lines = _p.stdout.strip().splitlines()
check(
    "CLI：末行是机器可读摘要",
    _lines[-1].startswith("RELEASE_PLAN|") and "|stage_proj=" in _lines[-1],
    _lines[-1] if _lines else "",
)
check("CLI：六节齐全", all(f"[{i}]" in _p.stdout for i in range(1, 7)), _p.stdout[:200])

_pj = _run("--json")
check("CLI：--json 退出 0 且 stdout 是纯 JSON", _pj.returncode == 0, _pj.stderr[-300:])
try:
    data = json.loads(_pj.stdout)
    parsed = True
except ValueError as e:  # noqa: BLE001
    data, parsed = {}, str(e)
check("CLI：--json 可解析", parsed, str(data))
check(
    "CLI：真实仓版本落点一致（versions_agree 为真）",
    data.get("versions", {}).get("agree") is True,
    str(data.get("versions", {}).get("agree")),
)
_proj = data.get("baselines", {}).get("stage_proj")
check("CLI：真实仓 stage_proj 是正整数", isinstance(_proj, int) and _proj > 0, str(_proj))
check("CLI：--json 里带 7 处落点", len(data.get("versions", {}).get("landings", [])) == 7)

_pp = _out / "real.diff"
_p = _run("--patch-out", str(_pp))
# 真实仓库状态随发版周期变：未抬版 ⇒ 6 个 hunk 且能 apply；发版提交已就位（head ≠ base）⇒ 无待改行
_bumped = data.get("versions", {}).get("head_bumped") is True
_nh = _pp.read_text("utf-8").count("--- a/") if _pp.is_file() else -1
check(
    f"CLI：--patch-out 退出 0（{'已抬版 ⇒ 0' if _bumped else '未抬版 ⇒ 6'} 个 hunk）",
    _p.returncode == 0 and _nh == (0 if _bumped else 6),
    f"hunks={_nh} {_p.stderr[-300:]}",
)
if not _bumped:
    _r = _git(ROOT, "apply", "--check", str(_pp))
    check("CLI：真实仓库 git apply --check 通过", _r.returncode == 0, _r.stderr[-300:])

_inrepo = ROOT / "zz_relplan_should_not_exist.diff"
_p = _run("--patch-out", str(_inrepo))
check(
    "CLI：--patch-out 落在仓库内被拒（rc≠0 且不落盘）",
    _p.returncode != 0 and not _inrepo.exists() and "--patch-out 不得落在仓库内" in _p.stderr,
    _p.stderr[-200:],
)
_p = _run("--base", "no_such_rev_zzz")
check("CLI：base 不存在 ⇒ rc≠0", _p.returncode != 0, _p.stdout[-200:])

check(
    "只读：运行前后真实仓库 git status --porcelain 不变",
    _before == _status(),
    f"before={_before!r} after={_status()!r}",
)

shutil.rmtree(_out, ignore_errors=True)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
