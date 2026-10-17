#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发版计划器（release_plan.py）—— 只读汇总「一次发版要动什么、基线对不对」。

用法::

    python scripts/release_plan.py [--base REV] [--head REV] [--json] [--patch-out FILE]

    --base      上次发版所在（默认 origin/main）
    --head      本次发版所在（默认 HEAD）
    --json      以 JSON 输出同一份内容（stdout 是纯 JSON，便于脚本消费）
    --patch-out FILE
                把 6 处可机械替换的版本行写成 unified diff（**不应用**，只落盘）。
                FILE 必须落在仓库之外；落在仓库内 ⇒ 拒绝并退出非 0。

输出六节：
  ① 版本落点（7 处）+ 建议下一版；
  ② base..head 改动集（按面分组，列新增 / 删除文件名 —— 它们影响基线）；
  ③ 基线（开源 stage 投影 / 两处 stage 基线 / 市场件基线）—— **只报告，不改**；
  ④ CHANGELOG 候选（分「候选 / 内部」）；
  ⑤ 待审修改草稿（`文件:行号  旧行 → 新行`）；
  ⑥ 发版前闸门清单（本地能查的当场判，其余列人工步骤）。
末行是机器可读摘要：``RELEASE_PLAN|current=…|next=…|…``。

★ 红线（本脚本的纪律，改本文件前先读）
- **只读**：不写仓库任何文件、不提交、不推送、不联网；`--patch-out` 只写你指定的仓外文件。
- **不自动把期望值设成实际值**：基线只报告「期望 vs 实际 vs 各落点是否一致」，由人审定。
- 版本号规则（维护者定）：每次 +0.0.1，每位最大 9 后进位（1.2.9 → 1.3.0；1.9.9 → 2.0.0）。
- **公开仓同步仍须用户单独授权**：本计划器只在清单里提示，绝不执行。
- 规则**不复制**：版本落点 / 版本取值 / 一致性 / 投影全部复用 ``dist/sync_opensource.py``
  （VERSION_SOURCES / version_of_file / versions_agree / expected_stage）；白名单与排除规则
  取自 ``dist/package_release.py``（它一改这里不漂）。基线数字从源码文本正则读取
  （**不 import package_release 取数**，避免副作用）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
if str(DIST) not in sys.path:
    sys.path.insert(0, str(DIST))

try:  # 复用 sync_opensource 的落点 / 投影规则
    import sync_opensource as S
except Exception as e:  # noqa: BLE001
    raise SystemExit(f"✗ 无法导入 dist/sync_opensource.py：{e}")

# 7 处版本落点 = sync_opensource.VERSION_SOURCES 的 5 处 + 两份门面模板首行
# （后两份在 dist/ 里、不进 stage，但抬版本时必须同号：它们各自是公开仓 / 自用包的
#  门面首行，见 .zcode/commands/wf/pack.md 的「版本联动真源」）
FACADE_READMES = ("dist/OPENSOURCE_README.md", "dist/SDJKwfbot_README.md")
VERSION_LANDINGS = tuple(S.VERSION_SOURCES) + FACADE_READMES
# 6 处**可机械替换**的版本行（CHANGELOG 只提示「需新增小节」，不生成文案）
MECHANICAL_LANDINGS = tuple(r for r in VERSION_LANDINGS if r != "CHANGELOG.md")

# 发版草稿（用户手写、放在工作区外）—— 只列文件名，不读内容
DRAFT_DIR = Path("/root")
DRAFT_GLOB = "CHANGELOG草稿-*.md"

# 面的分组（顺序即输出顺序；先判的优先 ⇒ dist/*.md 归「工具」而不是「文档」）
FACE_RUNTIME = "运行期面"
FACE_TESTS = "测试"
FACE_TOOLS = "工具"
FACE_OPS = "运维"
FACE_DOCS = "文档"
FACE_OTHER = "其它"
FACE_ORDER = (FACE_RUNTIME, FACE_TESTS, FACE_TOOLS, FACE_OPS, FACE_DOCS, FACE_OTHER)

# ④ 的「内部」清单（按 AGENTS.md：只改这些的提交不进更新日志）
INTERNAL_PREFIXES = ("tests/", "scripts/", "dist/", ".zcode/", "docs/")
INTERNAL_FILES = {"AGENTS.md"}


# ---------------------------------------------------------------------------
# 版本号规则（维护者定：每次 +0.0.1，每位最大 9 后进位）
# ---------------------------------------------------------------------------
def next_version(ver: str) -> str:
    """按用户规则抬一位：每位最大 9 后进位。

    1.2.1 → 1.2.2；1.2.9 → 1.3.0；1.9.9 → 2.0.0；0.0.9 → 0.1.0。
    非法输入（不是 X.Y.Z 三位、或某位不是 0-9 的单个数字）抛 ``ValueError``。
    """
    text = str(ver).strip().lstrip("vV")
    if not re.fullmatch(r"\d\.\d\.\d", text):
        raise ValueError(f"版本号必须形如 X.Y.Z 且每位 0-9：{ver!r}")
    major, minor, patch = (int(x) for x in text.split("."))
    patch += 1
    if patch > 9:
        patch, minor = 0, minor + 1
    if minor > 9:
        minor, major = 0, major + 1
    return f"{major}.{minor}.{patch}"


# ---------------------------------------------------------------------------
# 只读 git 访问
# ---------------------------------------------------------------------------
def _run_git(repo: Path, args: list[str]) -> tuple[int, str]:
    """跑一条只读 git 命令（不写仓库）。返回 ``(rc, stdout)``。"""
    proc = subprocess.run(
        ["git", "--no-pager", "-c", "core.quotepath=false", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "")


def rev_sha(repo: Path, rev: str) -> str | None:
    """解析成 commit sha；取不到返回 None。"""
    rc, out = _run_git(repo, ["rev-parse", "--verify", f"{rev}^{{commit}}"])
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[0] if rc == 0 and lines else None


def show_file(repo: Path, rev: str, rel: str) -> str | None:
    """取 ``rev:rel`` 的文本内容；不存在返回 None。"""
    rc, out = _run_git(repo, ["show", f"{rev}:{rel}"])
    return out if rc == 0 else None


def tree_paths(repo: Path, rev: str) -> list[str]:
    """``rev`` 的文件路径清单（含子目录）。"""
    rc, out = _run_git(repo, ["ls-tree", "-r", "--name-only", rev])
    return [ln for ln in out.splitlines() if ln.strip()] if rc == 0 else []


# ---------------------------------------------------------------------------
# ② 改动集：按面分组
# ---------------------------------------------------------------------------
def face_of(rel: str) -> str:
    """把仓库相对路径归到一个「面」（顺序见 :data:`FACE_ORDER`）。"""
    if rel in ("main.py", "_conf_schema.json", "metadata.yaml", "requirements.txt"):
        return FACE_RUNTIME
    if rel.startswith(("core/", "pages/", ".astrbot-plugin/")):
        return FACE_RUNTIME
    if rel.startswith("tests/"):
        return FACE_TESTS
    if rel.startswith(("scripts/", "dist/")):
        return FACE_TOOLS
    if rel.startswith(".zcode/"):
        return FACE_OPS
    if rel.endswith(".md") or rel.startswith("assets/"):
        return FACE_DOCS
    return FACE_OTHER


def section_changes(repo: Path, base: str, head: str) -> dict:
    """``git diff --name-status --no-renames base..head`` 按面分组。"""
    faces = {f: {"added": [], "deleted": [], "modified": []} for f in FACE_ORDER}
    rc, out = _run_git(repo, ["diff", "--name-status", "--no-renames", f"{base}..{head}"])
    total = 0
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) < 2 or not parts[0].strip():
            continue
        status, rel = parts[0].strip()[0], parts[-1].strip()
        key = "added" if status == "A" else "deleted" if status == "D" else "modified"
        faces[face_of(rel)][key].append(rel)
        total += 1
    return {
        "faces": faces,
        "total": total,
        "added": sorted(r for f in FACE_ORDER for r in faces[f]["added"]),
        "deleted": sorted(r for f in FACE_ORDER for r in faces[f]["deleted"]),
        "diff_rc": rc,
    }


# ---------------------------------------------------------------------------
# ① 版本落点（7 处）
# ---------------------------------------------------------------------------
_LINE_PATTERNS = {
    "metadata.yaml": re.compile(r"^version:\s*v?\d+\.\d+\.\d+\s*$"),
    "core/__init__.py": re.compile(r'__version__\s*=\s*"\d+\.\d+\.\d+"'),
    "CHANGELOG.md": re.compile(r"^##\s*v?\d+\.\d+\.\d+"),
}


def _landing_line_no(rel: str, text: str, ver: str | None) -> int | None:
    """定位「版本所在的那一行」的行号（1 起）。取不到返回 None。"""
    lines = text.split("\n")
    if rel in ("README.md",) + FACADE_READMES:
        return 1 if lines else None
    if rel == "main.py":
        i = text.find("@register(")
        if i < 0:
            return None
        base_no = text[:i].count("\n") + 1
        for off, ln in enumerate(text[i:].split("\n")):
            if ver and ver in ln:
                return base_no + off
        return None
    pat = _LINE_PATTERNS.get(rel)
    if pat is None:
        return None
    for no, ln in enumerate(lines, 1):
        if pat.search(ln):
            return no
    return None


def read_landing(repo: Path, rev: str, rel: str) -> dict:
    """读一处版本落点：版本串 / 行号 / 原行（取不到记 None）。"""
    raw = show_file(repo, rev, rel)
    if raw is None:
        return {"path": rel, "version": None, "line_no": None, "line": None}
    # 版本取值复用 sync_opensource.version_of_file（不复制规则）；两份门面 README
    # 按 README.md 的「首行取版本」分支走（rel 传 README.md 即命中该分支）。
    key = "README.md" if rel in FACADE_READMES else rel
    ver = S.version_of_file(key, raw)
    ver_str = ".".join(map(str, ver)) if ver else None
    no = _landing_line_no(rel, raw, ver_str)
    lines = raw.split("\n")
    line = lines[no - 1] if no is not None and no <= len(lines) else None
    return {"path": rel, "version": ver_str, "line_no": no, "line": line}


def section_versions(repo: Path, base: str, head: str) -> dict:
    """7 处落点 + 一致性 + 建议下一版（含 base 侧是否已抬过）。"""
    landings = [read_landing(repo, head, rel) for rel in VERSION_LANDINGS]
    five = {}
    for item in landings:
        if item["path"] in S.VERSION_SOURCES:
            ver = item["version"]
            five[item["path"]] = tuple(int(x) for x in ver.split(".")) if ver else None
    agree5, detail5 = S.versions_agree(five)
    core_ver = next((x["version"] for x in landings if x["path"] == "core/__init__.py"), None)
    current = core_ver or next((x["version"] for x in landings if x["version"]), None)
    extras_bad = [
        x["path"]
        for x in landings
        if x["path"] not in S.VERSION_SOURCES and x["version"] != current
    ]
    agree = bool(agree5 and not extras_bad)

    base_txt = show_file(repo, base, "core/__init__.py") or ""
    base_ver = S.version_of_file("core/__init__.py", base_txt)
    base_ver_str = ".".join(map(str, base_ver)) if base_ver else None
    bumped = bool(base_ver_str and current and base_ver_str != current)
    note = (
        f"head 已抬到 {current}（base {base_ver_str}），本次发版目标即 {current}，不再 +1"
        if bumped
        else ""
    )

    # 目标版本：head 已抬过（≠ base）⇒ 就是当前版本（发版提交已就位，CHANGELOG 首条应等于它）；否则 +0.0.1
    next_ver, next_err = None, None
    if current:
        try:
            next_ver = current if bumped else next_version(current)
        except ValueError as e:
            next_err = str(e)
    return {
        "landings": landings,
        "agree": agree,
        "agree_detail": detail5,
        "agree_five": agree5,
        "extras_mismatch": extras_bad,
        "current": current,
        "next": next_ver,
        "next_error": next_err,
        "base_version": base_ver_str,
        "head_bumped": bumped,
        "note": note,
    }


# ---------------------------------------------------------------------------
# ③ 基线（只报告，不改）
# ---------------------------------------------------------------------------
_NUM_RE = {
    "oss_stage": re.compile(r"^EXPECTED_OSS_STAGE_FILES\s*=\s*(\d+)", re.M),
    "stage": re.compile(r"^EXPECTED_STAGE_FILES\s*=\s*(\d+)", re.M),
    "market": re.compile(r"^EXPECTED_MARKET_ENTRIES\s*=\s*(\d+)", re.M),
}
AGENTS_STAGE_LABEL = "文件数精确基线"
AGENTS_MARKET_LABEL = "市场件条目基线"


def _number_in(path: Path, pattern: re.Pattern) -> int | None:
    """从源码文本里正则读一个基线数字（不 import，避免副作用）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = pattern.search(text)
    return int(m.group(1)) if m else None


def agents_baseline(repo: Path, label: str) -> int | None:
    """AGENTS.md 里带 ``label`` 的那一行上的「当前 **N**」。"""
    try:
        text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    except OSError:
        return None
    for ln in text.splitlines():
        if label in ln:
            m = re.search(r"当前 \*\*(\d+)\*\*", ln)
            if m:
                return int(m.group(1))
    return None


def projection_paths(repo: Path, rev: str) -> set[str] | None:
    """``rev`` 的「开源投影」路径集（规则取自 dist/package_release.py，不复制一份）。

    与 ``sync_opensource.expected_stage`` 同判据：整树去掉 ``_should_skip`` 命中的，
    再去掉 ``OSS_DROP_FILES``；变换产出（``TRANSFORM_FILES``）仍在投影内（会重算）。
    取不到规则（无 package_release / git 不可用）时返回 None。
    """
    try:
        import package_release as pr
    except Exception:  # noqa: BLE001 —— 临时仓里允许降级
        return None
    paths = tree_paths(repo, rev)
    if not paths:
        return None
    pr._OSS_MODE = True  # 与 sync_opensource.expected_stage 同口径
    out = set()
    for rel in paths:
        if rel in S.TRANSFORM_FILES or rel in pr.OSS_DROP_FILES:
            continue
        if pr._should_skip(Path(rel)):
            continue
        out.add(rel)
    return out


def section_baselines(repo: Path, base: str, head: str, head_is_head: bool, changes: dict) -> dict:
    """三处 stage 基线 + 市场件基线：期望 vs 实际 vs 是否互相一致。"""
    oss_stage = _number_in(repo / "dist" / "package_release.py", _NUM_RE["oss_stage"])
    mm_stage = _number_in(repo / "scripts" / "make_market_zip.py", _NUM_RE["stage"])
    mm_market = _number_in(repo / "scripts" / "make_market_zip.py", _NUM_RE["market"])
    agents_stage = agents_baseline(repo, AGENTS_STAGE_LABEL)
    agents_market = agents_baseline(repo, AGENTS_MARKET_LABEL)

    stage_proj, proj_err = None, None
    try:  # expected_stage 读的是 HEAD（不切分支）
        stage_proj = len(S.expected_stage(repo))
    except Exception as e:  # noqa: BLE001 —— 临时仓里降级
        proj_err = f"{type(e).__name__}: {e}"

    stage_triple = [oss_stage, mm_stage, agents_stage]
    stage_consistent = (
        stage_proj is not None
        and None not in stage_triple
        and len(set(stage_triple)) == 1
        and stage_triple[0] == stage_proj
    )
    market_pair = [mm_market, agents_market]
    market_consistent = None not in market_pair and len(set(market_pair)) == 1

    notes = []
    if not head_is_head:
        notes.append(f"基线一节按当前 HEAD 计算（--head={head} 不是 HEAD）")
    if proj_err:
        notes.append(f"无法计算开源投影（基线一节降级）：{proj_err}")
    if stage_proj is not None and not stage_consistent:
        notes.append("✗ 需人工审定：三处 stage 基线与投影不一致")
    if not market_consistent:
        notes.append("✗ 需人工审定：市场件基线与 AGENTS.md 不一致")

    proj_head = projection_paths(repo, head)
    proj_base = projection_paths(repo, base)
    stage_clues, market_clues = [], []
    for rel in changes["added"]:
        if proj_head is not None and rel in proj_head:
            stage_clues.append(f"+ {rel}（新增，在 head 投影内 ⇒ 基线该 +1）")
        if face_of(rel) == FACE_RUNTIME:
            market_clues.append(f"+ {rel}（新增，运行期面 ⇒ 可能进市场件）")
    for rel in changes["deleted"]:
        if proj_base is not None and rel in proj_base:
            stage_clues.append(f"- {rel}（删除，在 base 投影内 ⇒ 基线该 −1）")
        if face_of(rel) == FACE_RUNTIME:
            market_clues.append(f"- {rel}（删除，运行期面 ⇒ 可能出市场件）")
    return {
        "stage_proj": stage_proj,
        "stage_proj_error": proj_err,
        "stage_baselines": {
            "package_release": oss_stage,
            "make_market_zip": mm_stage,
            "agents": agents_stage,
        },
        "stage_consistent": stage_consistent,
        "market_baselines": {"make_market_zip": mm_market, "agents": agents_market},
        "market_consistent": market_consistent,
        "stage_clues": stage_clues,
        "market_clues": market_clues,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# ④ CHANGELOG 候选
# ---------------------------------------------------------------------------
def section_changelog(repo: Path, base: str, head: str, next_ver: str | None) -> dict:
    """逐个提交分类：候选（动运行期面）/ 内部（按 AGENTS 不进更新日志）/ 其他。"""
    rc, out = _run_git(repo, ["log", "--no-merges", "--format=%H%x09%s", f"{base}..{head}"])
    commits = []
    for ln in out.splitlines():
        if "\t" not in ln:
            continue
        sha, subj = ln.split("\t", 1)
        _, files_out = _run_git(repo, ["show", "--name-only", "--format=", sha])
        rels = [f for f in files_out.splitlines() if f.strip()]
        runtime = sorted({f for f in rels if face_of(f) == FACE_RUNTIME})
        if runtime:
            bucket = "candidate"
        elif rels and all(f in INTERNAL_FILES or f.startswith(INTERNAL_PREFIXES) for f in rels):
            bucket = "internal"
        else:
            bucket = "other"
        commits.append(
            {
                "sha": sha,
                "short": sha[:7],
                "subject": subj,
                "files": rels,
                "runtime_files": runtime,
                "bucket": bucket,
            }
        )
    cl_raw = show_file(repo, head, "CHANGELOG.md") or ""
    m = re.search(r"^##\s*v?(\d+\.\d+\.\d+)", cl_raw, re.M)
    first = m.group(1) if m else None
    ready = bool(first and next_ver and first == next_ver)
    try:
        drafts = sorted(p.name for p in DRAFT_DIR.glob(DRAFT_GLOB))
    except OSError:
        drafts = []
    return {
        "commits": commits,
        "candidates": [c for c in commits if c["bucket"] == "candidate"],
        "internal": [c for c in commits if c["bucket"] == "internal"],
        "others": [c for c in commits if c["bucket"] == "other"],
        "first_heading": first,
        "ready": ready,
        "needs_section": None if ready else next_ver,
        "drafts": drafts,
        "log_rc": rc,
    }


# ---------------------------------------------------------------------------
# ⑤ 待审修改草稿（只给草稿，不落笔到仓库）
# ---------------------------------------------------------------------------
def section_draft(repo: Path, head: str, versions: dict) -> dict:
    """6 处可机械替换的版本行；取不到行号的标 manual（由人补）。"""
    edits = []
    for item in versions["landings"]:
        if item["path"] == "CHANGELOG.md":
            continue
        old, no, ver, new_ver = item["line"], item["line_no"], item["version"], versions["next"]
        if ver and new_ver and ver == new_ver:
            continue  # 已是目标版本（发版提交已抬过）⇒ 无需改
        new = old.replace(ver, new_ver) if (old and ver and new_ver) else None
        edits.append(
            {
                "path": item["path"],
                "line_no": no,
                "old": old,
                "new": None if new == old else new,
                "manual": not new or new == old,
            }
        )
    return {"edits": edits, "patch_out": None}


def _split_lines(raw: str) -> tuple[list[str], bool]:
    """按行拆分并回报「原文件是否以换行结尾」。"""
    ends_nl = raw.endswith("\n")
    lines = raw.split("\n")
    if ends_nl:
        lines = lines[:-1]
    return lines, ends_nl


def _hunk(path: str, lines: list[str], ends_nl: bool, no: int, old: str, new: str) -> str:
    """单行替换的 hunk —— **上下各带 1 行上下文**。

    零上下文的 unified diff 过不了 `git apply --check`（那要 `--unidiff-zero`），
    所以这里始终尽量带上下文；文件只有一行时无上下文可用（此时由人补）。
    """
    start = max(1, no - 1)
    end = min(len(lines), no + 1)
    body = []
    for i in range(start, end + 1):
        if i == no:
            body.append("-" + lines[i - 1])
            body.append("+" + new)
        else:
            body.append(" " + lines[i - 1])
        if i == len(lines) and not ends_nl:
            body.append("\\ No newline at end of file")
    count = end - start + 1
    return (
        f"--- a/{path}\n+++ b/{path}\n@@ -{start},{count} +{start},{count} @@\n"
        + "\n".join(body)
        + "\n"
    )


def make_patch(repo: Path, head: str, edits: list[dict]) -> str:
    """把可机械替换的版本行写成 unified diff（相对仓库根 a/ b/；**不应用**）。"""
    chunks = []
    for e in edits:
        if e["manual"] or e["path"] not in MECHANICAL_LANDINGS or not e["new"]:
            continue
        raw = show_file(repo, head, e["path"]) or ""
        lines, ends_nl = _split_lines(raw)
        no = e["line_no"]
        if no is None or no > len(lines) or lines[no - 1] != e["old"]:
            continue
        chunks.append(_hunk(e["path"], lines, ends_nl, no, e["old"], e["new"]))
    return "".join(chunks)


# ---------------------------------------------------------------------------
# ⑥ 闸门清单
# ---------------------------------------------------------------------------
MANUAL_STEPS = (
    "scripts/run_checks.py regression（ruff + 全量 tests + 伤害审计；解释器须 ≥3.10）",
    # 2026-10-17 v1.2.2 教训：私有 CI 的 oss-stage 从批次 B 起一直红，没人看就发了版，公开仓随即变红
    "私有仓 CI 在 head 提交上全绿（regression 3.10 / 3.12 + **oss-stage**）——看 Actions 运行结果，不要只看徽章",
    "dist/package_release.py --opensource（Gate A/B 白名单 + 数量基线 + 种子新鲜度闸门）",
    "合 main（需维护者授权）",
    "公开仓同步 dist/sync_opensource.py（需用户**单独**授权；默认 dry-run，ZCode 执行）",
    "市场自动更新后回查（by-id + cache-buster；四件齐 = versions[] / approved / "
    "download_url 实测 206 / git_commit == 本地件 sha）",
)


def section_gates(repo: Path, versions: dict, baselines: dict, changelog: dict) -> dict:
    """本地能查的当场判；其余列人工步骤（本脚本一步都不执行）。"""
    dirty = S.repo_dirty_lines(repo)
    return {
        "gates": {
            "worktree_clean": not dirty,
            "worktree_dirty_lines": dirty[:20],
            "versions_agree": versions["agree"],
            "stage_consistent": baselines["stage_consistent"],
            "changelog_ready": changelog["ready"],
        },
        "manual_steps": list(MANUAL_STEPS),
    }


# ---------------------------------------------------------------------------
# 组装 / 渲染
# ---------------------------------------------------------------------------
def build_plan(repo: Path, base: str, head: str) -> dict:
    """把六节算成一份结构化计划（只读；不写仓库任何文件）。"""
    changes = section_changes(repo, base, head)
    versions = section_versions(repo, base, head)
    head_sha, head_of_head = rev_sha(repo, head), rev_sha(repo, "HEAD")
    baselines = section_baselines(repo, base, head, head_sha == head_of_head, changes)
    changelog = section_changelog(repo, base, head, versions["next"])
    plan = {
        "repo": str(repo),
        "base": base,
        "base_sha": rev_sha(repo, base),
        "head": head,
        "head_sha": head_sha,
        "versions": versions,
        "changes": changes,
        "baselines": baselines,
        "changelog": changelog,
        "draft": section_draft(repo, head, versions),
        "gates": section_gates(repo, versions, baselines, changelog),
    }
    plan["summary"] = summary_dict(plan)
    return plan


def summary_dict(plan: dict) -> dict:
    v, b, c = plan["versions"], plan["baselines"], plan["changelog"]
    g = plan["gates"]["gates"]
    sb, mb = b["stage_baselines"], b["market_baselines"]
    return {
        "current": v["current"],
        "next": v["next"],
        "versions_agree": v["agree"],
        "stage_proj": b["stage_proj"],
        "stage_baselines": [sb["package_release"], sb["make_market_zip"], sb["agents"]],
        "market_baseline": [mb["make_market_zip"], mb["agents"]],
        "candidates": len(c["candidates"]),
        "internal": len(c["internal"]),
        "changelog_ready": c["ready"],
        "clean": g["worktree_clean"],
    }


def _num(x) -> str:
    return "?" if x is None else str(x)


def summary_line(plan: dict) -> str:
    """末行的机器可读摘要（口径与 AGENTS.md 的基线三处一致）。"""
    s = plan["summary"]
    return (
        "RELEASE_PLAN"
        f"|current={s['current'] or '?'}"
        f"|next={s['next'] or '?'}"
        f"|versions_agree={'yes' if s['versions_agree'] else 'no'}"
        f"|stage_proj={_num(s['stage_proj'])}"
        f"|stage_baselines={'/'.join(_num(x) for x in s['stage_baselines'])}"
        f"|market_baseline={'/'.join(_num(x) for x in s['market_baseline'])}"
        f"|candidates={s['candidates']}|internal={s['internal']}"
        f"|changelog_ready={'yes' if s['changelog_ready'] else 'no'}"
        f"|clean={'yes' if s['clean'] else 'no'}"
    )


def render_text(plan: dict) -> str:
    """六节文本输出（末行 = 机器可读摘要）。"""
    v, ch, bl, dr, ga = (
        plan["versions"],
        plan["changes"],
        plan["baselines"],
        plan["draft"],
        plan["gates"],
    )
    log = plan["changelog"]
    out = [
        "=== 发版计划（只读）===",
        f"repo : {plan['repo']}",
        f"base : {plan['base']} ({(plan['base_sha'] or '?')[:7]})",
        f"head : {plan['head']} ({(plan['head_sha'] or '?')[:7]})",
        "",
        f"[1] 版本落点（{len(v['landings'])} 处）",
    ]
    for item in v["landings"]:
        ver = f"v{item['version']}" if item["version"] else "（读不出）"
        loc = f"第 {item['line_no']} 行" if item["line_no"] else "行号 ?"
        out.append(f"    {item['path']:<28} {ver:<8} （{loc}）")
    out.append(f"    五处落点一致：{'是' if v['agree_five'] else '否'} —— {v['agree_detail']}")
    out.append(
        "    两份门面 README 同号："
        + ("是" if not v["extras_mismatch"] else f"否 {v['extras_mismatch']}")
    )
    out.append(
        f"    当前版本：{v['current'] or '?'}    建议下一版：{v['next'] or '?'}"
        + (f"（{v['next_error']}）" if v["next_error"] else "")
    )
    if v["base_version"]:
        out.append(f"    base 版本（core/__init__.py @{plan['base']}）：{v['base_version']}")
    if v["note"]:
        out.append(f"    ⚠ {v['note']}")

    out += ["", f"[2] 改动集 {plan['base']}..{plan['head']}（{ch['total']} 个文件）"]
    for face in FACE_ORDER:
        d = ch["faces"][face]
        if not (d["added"] or d["deleted"] or d["modified"]):
            continue
        out.append(
            f"    {face}（新增 {len(d['added'])} / 删除 {len(d['deleted'])} /"
            f" 修改 {len(d['modified'])}）"
        )
        out += [f"        + {r}" for r in d["added"]]
        out += [f"        - {r}" for r in d["deleted"]]
        out += [f"        ~ {r}" for r in d["modified"]]

    out += ["", "[3] 基线（只报告，不改）"]
    out.append(f"    开源 stage 投影（expected_stage(HEAD)）：{_num(bl['stage_proj'])}")
    sb = bl["stage_baselines"]
    out.append(
        "    三处 stage 基线：package_release={} / make_market_zip={} / AGENTS.md={} → {}".format(
            _num(sb["package_release"]),
            _num(sb["make_market_zip"]),
            _num(sb["agents"]),
            "与投影一致 ✔" if bl["stage_consistent"] else "✗ 需人工审定",
        )
    )
    mb = bl["market_baselines"]
    out.append(
        "    市场件基线：make_market_zip={} / AGENTS.md={} → {}".format(
            _num(mb["make_market_zip"]),
            _num(mb["agents"]),
            "一致 ✔" if bl["market_consistent"] else "✗ 需人工审定",
        )
    )
    if bl["stage_clues"]:
        out.append("    线索（base..head 落在开源投影内的新增/删除）：")
        out += [f"      {c}" for c in bl["stage_clues"]]
    if bl["market_clues"]:
        out.append("    线索（运行期面新增/删除 —— 会影响市场件条目数）：")
        out += [f"      {c}" for c in bl["market_clues"]]
    out += [f"    注：{n}" for n in bl["notes"]]

    out += ["", f"[4] CHANGELOG 候选（{len(log['commits'])} 个非合并提交）"]
    out.append(
        f"    候选（动了运行期面，需人工改写成玩家能看懂的话）（{len(log['candidates'])}）："
    )
    for c in log["candidates"]:
        out.append(f"      {c['short']}  {c['subject']}")
        out += [f"        - {f}" for f in c["runtime_files"]]
    out.append(f"    内部（按 AGENTS 不进更新日志）（{len(log['internal'])}）：")
    for c in log["internal"]:
        out.append(f"      {c['short']}  {c['subject']}")
    if log["others"]:
        out.append(f"    其他（未动运行期面、也不在内部清单）（{len(log['others'])}）：")
        for c in log["others"]:
            out.append(f"      {c['short']}  {c['subject']}  [{len(c['files'])} 文件]")
    if log["ready"]:
        out.append(f"    CHANGELOG 首条：v{log['first_heading']} ⇒ 已是建议下一版 ✔")
    else:
        out.append(
            f"    CHANGELOG 首条：v{log['first_heading'] or '（读不出）'} ⇒ "
            f"需新增 ## v{log['needs_section']} 小节"
        )
    out += [f"    工作区发版草稿：{n}" for n in log["drafts"]]

    out += ["", f"[5] 待审修改草稿（{len(dr['edits'])} 处版本行，CHANGELOG 只提示）"]
    for e in dr["edits"]:
        if e["manual"]:
            out.append(f"    {e['path']}:{_num(e['line_no'])}  ⚠ 取不到可替换行，需人工改")
        else:
            out.append(f"    {e['path']}:{e['line_no']}  {e['old']} → {e['new']}")
    if not log["ready"]:
        out.append(f"    CHANGELOG.md：需新增 ## v{log['needs_section']} 小节（文案不自动生成）")

    out += ["", "[6] 发版前闸门清单"]
    g = ga["gates"]
    out.append(
        f"    {'✔' if g['worktree_clean'] else '✗'} 工作树干净"
        + ("" if g["worktree_clean"] else f"（{len(g['worktree_dirty_lines'])} 条改动）")
    )
    out.append(f"    {'✔' if g['versions_agree'] else '✗'} 版本落点一致")
    out.append(f"    {'✔' if g['stage_consistent'] else '✗'} 三处 stage 基线一致且 = 投影")
    out.append(f"    {'✔' if g['changelog_ready'] else '✗'} CHANGELOG 首条 = 建议版本")
    out.append("    需人工 / 他人执行（本脚本不执行任何一步）：")
    out += [f"      - {s}" for s in ga["manual_steps"]]
    out += ["", summary_line(plan)]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _inside_repo(repo: Path, path: Path) -> bool:
    try:
        return path == repo or path.is_relative_to(repo)
    except (OSError, ValueError):
        return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发版计划器（只读；不改仓库、不联网）")
    ap.add_argument("--base", default="origin/main", help="上次发版所在（默认 origin/main）")
    ap.add_argument("--head", default="HEAD", help="本次发版所在（默认 HEAD）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（stdout 为纯 JSON）")
    ap.add_argument(
        "--patch-out",
        metavar="FILE",
        default=None,
        help="把 6 处版本行写成 unified diff（不应用；FILE 须在仓库外）",
    )
    args = ap.parse_args(argv)

    repo = ROOT.resolve()
    if not (repo / ".git").exists():
        print(f"✗ {repo} 不是 git 仓库", file=sys.stderr)
        return 2
    patch_path = None
    if args.patch_out:
        patch_path = Path(args.patch_out).expanduser().resolve()
        if _inside_repo(repo, patch_path):
            print(f"✗ --patch-out 不得落在仓库内：{patch_path}", file=sys.stderr)
            return 2
    if rev_sha(repo, args.base) is None:
        print(f"✗ 取不到 base 版本 {args.base!r}（不存在或不是 commit）", file=sys.stderr)
        return 2
    if rev_sha(repo, args.head) is None:
        print(f"✗ 取不到 head 版本 {args.head!r}（不存在或不是 commit）", file=sys.stderr)
        return 2

    plan = build_plan(repo, args.base, args.head)
    if patch_path is not None:
        plan["draft"]["patch_out"] = str(patch_path)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(render_text(plan))
    if patch_path is not None:
        patch = make_patch(repo, args.head, plan["draft"]["edits"])
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(patch, encoding="utf-8")
        if not args.json:
            print(f"\n[草稿 diff] 已写出（**未应用**）：{patch_path}")
    return 0


if __name__ == "__main__":
    try:
        _rc = main()
        sys.stdout.flush()  # 显式 flush：管道被下游关掉时在这里就能接住
    except BrokenPipeError:
        # 下游提前关管道（如 `… --json | head -c 1500`）：静默退出，别吐 traceback
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        _rc = 0
    sys.exit(_rc)
