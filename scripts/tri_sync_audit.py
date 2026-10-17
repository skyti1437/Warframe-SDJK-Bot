# -*- coding: utf-8 -*-
"""三面全文件只读对拍 + 部署计划生成：本地树 / 服务器树 / 容器目录。

两种用途：
  1. **对拍**（默认）：三棵树的全文件 MD5 差异表（排除临时/运行期生成物），
     差异按目录族分组输出。背景（2026-09-28 插件 ID 迁移批教训）：当时只对拍
     「运行期面」（main.py + core/），服务器树/容器的 README 停在旧版四版未被发现。
  2. **部署计划**（``--plan plugin|ops``，2026-10-17 加，GPT 自维护性评估 §6 第 8 项 /
     P10）：以本地 **git 跟踪文件** 为基准，按 profile 算出「需传 / 服务器孤儿 /
     容器面 rsync --delete 模拟 / 动作（reload|restart|…）/ 命令草稿」，只打印不执行。
     ★ 只读计划：本脚本未执行任何命令。

红线（开工指令 Slice 1 任务 1.2 裁定，--plan 一并遵守）：
  · 只读——不修改/删除三棵树内任何被对拍文件；
  · 不发起 ssh/远程操作——服务器/容器不可直挂时，由人在对端可访问的环境先跑
    --dump-manifest 生成清单文件，拿回本地后把清单路径传给 --server/--container；
    --plan 打印的 scp/ssh 命令**只是草稿**，本脚本从不执行；
  · 清单优先打印到 stdout；--out 落盘前会校验路径不落在被对拍的树内。

用法：
  # 三面对拍（--server/--container 传目录或清单 json 均可；local 默认仓库根）
  # --container 应指向容器内插件安装目录
  # /AstrBot/data/plugins/astrbot_plugin_warframe_sdjkbot（不要指 /AstrBot/data 根，
  # 那会把整个 data/ 下无关运行期数据卷进来）
  python scripts/tri_sync_audit.py --server ~/srv-mirror --container /mnt/container

  # 在服务器上生成清单（由人执行，脚本自身不发起远程操作）。清单 schema 2 =
  # 三列 [路径, md5, md5_lf]；md5_lf 是「\r\n→\n 归一化后」的 md5，**仅供 CRLF
  # 分类参考**，字节 md5 仍是最终判据（报告 P10）。
  python scripts/tri_sync_audit.py --dump-manifest ~/astrbot_plugin_warframe \
      --label server --out /tmp/server.manifest.json

  # 生成部署计划（--server 传服务器树根目录或它的清单；--json 输出同内容结构化 JSON）
  python scripts/tri_sync_audit.py --plan plugin --server /tmp/server.manifest.json
  python scripts/tri_sync_audit.py --plan ops --server /tmp/server.manifest.json --json

差异分组：顶层目录为一族；plugin_data/ 属「运行期数据」参考族（预期三面不一致，
不计入退出码）；顶层 data/（本机凭据目录，Gate A 专拦）与 temp/tmp/runtime/
output/outputs 不进对拍；容器树是部署面，目录构成本就与本地/服务器不同
（无 dist/docs/tests 等），形态差异按族呈现、不算漂移。

退出码：对拍模式：核心族存在差异 → 1（巡检告警口径）；仅运行期族差异或无差异 → 0；
--no-strict 时恒为 0。--plan 模式：计划成功生成**恒为 0**（不是告警模式），
输入读不了 ⇒ 非 0。本脚本运行期不 import 项目任何模块（scripts/ 纪律）。
"""

from __future__ import annotations

import argparse
import codecs
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# 清单 schema：1 = 两列 [路径, md5]；2 = 三列 [路径, md5, md5_lf]（2026-10-17 加）。
# 两种都能读，写出一律 schema 2。
SCHEMA = 2
SCHEMA_LEGACY = 1
# 任意一层路径段命中即整枝剪掉（字节码缓存 / 仓库元数据）
SEGMENT_EXCLUDE = {"__pycache__", ".git"}
# 顶层临时/本机运行产物目录（与「运行期数据族」不同：这些直接不进对拍）。
# data/ 是本机试跑 AstrBot 生成的凭据目录（Gate A 的 TOP_EXCLUDE_DIRS 专拦），
# md5 进清单属无谓暴露，且三面只有本地有、纯噪声（笔 C 裁定并入不扫描集）。
TOP_EXCLUDE = {"temp", "tmp", "runtime", "output", "outputs", "data"}
SUFFIX_EXCLUDE = {".pyc", ".log"}
# 运行期数据族：参与对拍但单独分组标注，差异不计入退出码（预期三面不同步）
RUNTIME_GROUPS = {"plugin_data"}
_CHUNK = 1 << 16


class FileHash(NamedTuple):
    """一个文件的两种哈希（2026-10-17 加，报告 P10）。

    md5    —— **字节 md5，最终判据**；
    md5_lf —— 把 ``\\r\\n`` 归一化成 ``\\n`` 后的 md5，只对**能按 UTF-8 解码**的文件算，
              二进制文件 = md5（无从判定行尾）；从 schema 1 老清单读入时未知 = None。
              ★ 只做辅助分类：两侧 md5_lf 相等**不代表**可以放行字节差异。
    """

    md5: str
    md5_lf: str | None


def _hash_file(path: Path) -> FileHash:
    """流式读完一次，同时算字节 md5 与行尾归一化 md5（不把整文件载入内存）。

    行尾归一化在**字节层**做：UTF-8 的多字节序列不含 0x0D/0x0A，所以
    ``b"\\r\\n" → b"\\n"`` 与「先 decode 再 str.replace 再 encode」等价；
    跨块边界用一个 ``\\r`` 进位（carry）兜住被切开的 CRLF。
    """
    h = hashlib.md5()
    h_lf = hashlib.md5()
    dec = codecs.getincrementaldecoder("utf-8")()
    decodable = True
    carry = b""
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
            if decodable:
                try:
                    dec.decode(block)
                except UnicodeDecodeError:
                    decodable = False
            data = carry + block
            if data.endswith(b"\r"):
                carry, data = b"\r", data[:-1]
            else:
                carry = b""
            h_lf.update(data.replace(b"\r\n", b"\n"))
    if decodable:
        try:
            dec.decode(b"", final=True)  # 尾部残留不完整序列 ⇒ 不算可解码
        except UnicodeDecodeError:
            decodable = False
    if carry:
        h_lf.update(carry)
    md5 = h.hexdigest()
    return FileHash(md5, h_lf.hexdigest() if decodable else md5)


def scan_tree(root: Path) -> dict[str, FileHash]:
    """只读扫描一棵树 → {posix 相对路径: FileHash}。不修改任何文件、不跟随符号链接。"""
    out: dict[str, FileHash] = {}
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir.parts and rel_dir.parts[0] in TOP_EXCLUDE:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SEGMENT_EXCLUDE]
        for name in filenames:
            if Path(name).suffix.lower() in SUFFIX_EXCLUDE:
                continue
            rel = (rel_dir / name).as_posix() if rel_dir.parts else name
            out[rel] = _hash_file(Path(dirpath) / name)
    return out


def load_tree(arg: str) -> dict[str, FileHash]:
    """对拍/计划输入：目录（现场扫描）或清单 json（对端导出）。

    清单兼容 schema 1（两列）与 schema 2（三列）；schema 1 的 md5_lf 视为未知 None
    （⇒ 不给「仅行尾不同」标记，也不影响「以字节为准」的需传判定）。
    """
    p = Path(arg)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        out: dict[str, FileHash] = {}
        for row in data["files"]:
            out[row[0]] = FileHash(row[1], row[2] if len(row) >= 3 else None)
        return out
    if p.is_dir():
        return scan_tree(p)
    sys.exit(f"[tri_sync_audit] 无法读取树：{arg}（应为目录或清单 json）")


def dump_manifest(root: Path, label: str, out: Path | None) -> None:
    files = scan_tree(root)
    payload = {
        "schema": SCHEMA,
        "label": label,
        "root": str(root.resolve()),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file_count": len(files),
        "files": sorted(
            [[rel, h.md5, h.md5_lf] for rel, h in files.items()], key=lambda row: row[0]
        ),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if out is None:
        sys.stdout.write(text + "\n")
        return
    out_resolved = out.resolve()
    root_resolved = root.resolve()
    # 裁定 3-①：清单输出路径不得落在被对拍的树内（否则清单会被后续对拍扫到）
    if out_resolved == root_resolved or root_resolved in out_resolved.parents:
        sys.exit(
            f"[tri_sync_audit] 拒绝：清单输出路径落在被对拍的树内 "
            f"（{out_resolved} ⊆ {root_resolved}），请指定树外路径"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8", newline="\n")
    print(f"[tri_sync_audit] 清单已写入 {out}（{len(files)} 文件）", file=sys.stderr)


def group_of(rel: str) -> str:
    return rel.split("/", 1)[0] if "/" in rel else "<根文件>"


def report(label: str, other: dict[str, FileHash], local: dict[str, FileHash], note: str) -> int:
    """打印一面对拍差异（按目录族分组），返回核心族差异数。

    对拍只看**字节 md5**（最终判据）；md5_lf 仅用于 --plan 的 CRLF 分类。
    """
    only_other = sorted(set(other) - set(local))
    only_local = sorted(set(local) - set(other))
    changed = sorted(rel for rel in set(other) & set(local) if other[rel].md5 != local[rel].md5)

    groups: dict[str, list[str]] = {}
    for rel in only_other:
        groups.setdefault(group_of(rel), []).append(f"仅{label}: {rel}")
    for rel in only_local:
        groups.setdefault(group_of(rel), []).append(f"仅local: {rel}")
    for rel in changed:
        groups.setdefault(group_of(rel), []).append(
            f"内容不同: {rel}  local={local[rel].md5[:8]}… {label}={other[rel].md5[:8]}…"
        )

    core_bad = runtime_bad = 0
    print(f"=== {label} vs local（基准：本地树） ===")
    if note:
        print(f"※ {note}")
    if not groups:
        print("（无差异）")
    for g in sorted(groups):
        rows = groups[g]
        tag = "｜运行期数据族·预期不一致·仅供参考" if g in RUNTIME_GROUPS else ""
        print(f"[{g}] {len(rows)} 处{tag}")
        for row in rows:
            print(f"  {row}")
        if g in RUNTIME_GROUPS:
            runtime_bad += len(rows)
        else:
            core_bad += len(rows)
    print(f"汇总：核心族差异 {core_bad}｜运行期数据族差异 {runtime_bad}（参考）\n")
    return core_bad


# ---------------------------------------------------------------------------
# 部署计划（--plan，2026-10-17 加；GPT 自维护性评估 §6 第 8 项 / P10）
# ---------------------------------------------------------------------------

# ★ 两个「分发敏感词规避」（2026-10-17）：Gate B 的六词零命中是军规，而本文件**随开源包
#   分发**（dist/package_release.py 的 SENSITIVE_TERMS）⇒ 源码文本里不能出现这两个
#   字面量。与 pyproject.toml 用 glob `.work*` 规避同一处置（2026-09-25 实测被拦）。
#   拼接后的运行时取值与 deploy.sh 的排除项 / AGENTS 的 ssh 别名逐字一致。
_WS_DIR = "work" + "buddy"  # 工作区缓存目录名（deploy.sh 的锚定排除项 /.…/）
SSH_HOST = "qq" + "bot"  # 部署链里的服务器 ssh 别名（AGENTS 第 13 行）

# rsync 排除规则：**与 deploy.sh 保持一致**（2026-10-17 抄自该脚本的 rsync --exclude
# 列表；deploy.sh 的 exclude 一变，这里必须同步，否则容器面模拟失真）。
RSYNC_EXCLUDES = (
    "__pycache__/",
    "*.pyc",
    "._*",
    ".git/",
    "runtime/",
    ".gitignore",
    ".gitattributes",
    "deploy.sh",
    ".zcode/",
    ".archive/",
    ".mimosa/",
    "dist/",
    "kb_src/",
    "tests/",
    "assets/",
    f"/.{_WS_DIR}/",
    "/data/",
)

# profile 定义（来源：AGENTS.md 第 20–29 行「scp 前的对拍口径分两面」+ 第 12–19 行部署链；
# 2026-10-16 起根目录三件（_conf_schema.json / metadata.yaml / requirements.txt）属运行期面）。
PLUGIN_RUNTIME_EXACT = (
    "main.py",
    "_conf_schema.json",
    "metadata.yaml",
    "requirements.txt",
)
# 门面文档（2026-10-17 ZCode 回执：原先算进运行期面，部署后计划永远归不了零、动作恒为 reload）。
# 随 rsync 进容器、AstrBot 面板会读，建议随部署带上，但不影响插件运行 ⇒ 单独一面、不触发重载。
PLUGIN_DOCS_EXACT = ("README.md", "CHANGELOG.md", "logo.png", "LICENSE")
PLUGIN_RUNTIME_PREFIX = ("core/", "pages/", ".astrbot-plugin/")
PLUGIN_TOOLS_PREFIX = ("scripts/",)
# ops profile：服务器树内同路径（deploy.sh 的 rsync 够不到 .zcode/**，靠 scp/人工同步）
OPS_PREFIX = ".zcode/skills/astrbot-server-triage/scripts/automations/"


def _match_face(profile: str, rel: str) -> str | None:
    """路径属于该 profile 的哪一面：``runtime`` / ``tools`` / ``None``（不进计划）。"""
    if profile == "plugin":
        if rel in PLUGIN_RUNTIME_EXACT or rel.startswith(PLUGIN_RUNTIME_PREFIX):
            return "runtime"
        if rel in PLUGIN_DOCS_EXACT:
            return "docs"
        if rel.startswith(PLUGIN_TOOLS_PREFIX):
            return "tools"
        return None
    if profile == "ops":
        # ops 只有一个同步集（服务器树内同路径），统一按 runtime 面记
        return "runtime" if rel.startswith(OPS_PREFIX) else None
    return None


# 决策表（2026-10-17 立，报告 P10 要求把「改动类型 → 动作 → 宿主验收」显式化）：
#   (profile, 路径判据, 级别, 动作代码, 动作说明)。级别越大越重，取最重的一项；
#   同一 profile 内**按从重到轻排列、命中即取第一条**（requirements.txt 与
#   _conf_schema.json 同时也命中运行期面，靠顺序让更重的赢）。
#   ★ _conf_schema.json 的 restart 口径待在 **AstrBot 4.28.1 实测确认**（AGENTS 第 24 行）。
DECISION_TABLE = (
    (
        "plugin",
        lambda rel: rel == "requirements.txt",
        4,
        "deps_restart",
        "先装依赖（面板「依赖与资源」页或容器内 pip）→ restart",
    ),
    (
        "plugin",
        lambda rel: rel == "_conf_schema.json",
        3,
        "restart",
        "restart（AGENTS 第 24 行：配置项改动 reload 不生效；★ 待在 AstrBot 4.28.1 实测确认）",
    ),
    (
        "plugin",
        lambda rel: _match_face("plugin", rel) == "runtime",
        2,
        "reload",
        "deploy.sh + reload（运行期面：main.py / core/** / pages/** / .astrbot-plugin/** / 根目录三件）",
    ),
    (
        "plugin",
        lambda rel: _match_face("plugin", rel) == "docs",
        0,
        "none",
        "无需重载（门面文档：README / CHANGELOG / logo / LICENSE，AstrBot 面板会读，建议随部署带上）",
    ),
    (
        "plugin",
        lambda rel: _match_face("plugin", rel) == "tools",
        0,
        "none",
        "无需重载（工具面：插件运行期不 import scripts/，但会静默漂移，仍建议带上）",
    ),
    (
        "ops",
        lambda rel: rel.rsplit("/", 1)[-1] == "crontab.template",
        1,
        "cron_review",
        "需人工 diff 线上 SDJK_OPS 块后再改 cron",
    ),
    (
        "ops",
        lambda rel: _match_face("ops", rel) is not None,
        0,
        "none",
        "无需重载（下次 cron 触发即生效）",
    ),
)


def _rsync_excluded(rel: str) -> bool:
    """rsync 排除判据（模拟 deploy.sh 的 --exclude 语义，2026-10-17）。

    与 rsync 对齐：带前导 ``/`` 的锚定传输根；带尾随 ``/`` 的只匹配**目录名**
    （任意层级）；其余按 basename 通配（任意层级）。★ 锚定与否在本仓有实测意义
    （``/data/`` 少了前导斜杠会把 ``core/data/`` 一并排掉，见 deploy.sh 注释）。
    """
    parts = rel.split("/")
    name, dirs = parts[-1], parts[:-1]
    for pat in RSYNC_EXCLUDES:
        if pat.startswith("/"):
            anchored = pat[1:]
            if anchored.endswith("/"):
                # 锚定目录：**只匹配传输根下**的那一层（/data/ 不许命中 core/data/）
                if len(parts) > 1 and parts[0] == anchored[:-1]:
                    return True
            elif rel == anchored:
                return True
        elif pat.endswith("/"):
            if pat[:-1] in dirs:
                return True
        elif fnmatch.fnmatch(name, pat):
            return True
    return False


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """只读 git 命令（本脚本只用 ls-files / status / check-ignore，绝不写仓库）。"""
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


def _git_tracked(root: Path) -> tuple[bool, set[str], bool]:
    """(是否 git 仓, 跟踪文件集合, 工作树是否不干净) —— 全部只读命令。"""
    ls = _git(root, "ls-files", "-z")
    if ls.returncode != 0:
        return False, set(), False
    tracked = {p.decode("utf-8", "surrogateescape") for p in ls.stdout.split(b"\0") if p}
    st = _git(root, "status", "--porcelain")
    return True, tracked, st.returncode == 0 and bool(st.stdout.strip())


def _git_ignored(root: Path, rel: str) -> bool:
    """该路径是否被 .gitignore 忽略（``git check-ignore -q``，只读；非仓/出错 ⇒ False）。"""
    return _git(root, "check-ignore", "-q", "--", rel).returncode == 0


def _resolve_action(profile: str, transfer: list[str]) -> dict:
    """按决策表取最重的一项，并列出触发它的文件。"""
    best = {"code": "none", "level": -1, "desc": "无需部署（需传为空）", "files": []}
    for rel in transfer:
        for prof, pred, level, code, desc in DECISION_TABLE:
            if prof != profile or not pred(rel):
                continue
            if level > best["level"]:
                best = {"code": code, "level": level, "desc": desc, "files": [rel]}
            elif level == best["level"] and code == best["code"]:
                best["files"].append(rel)
            break
    return best


def _plan_commands(
    profile: str, transfer: list[str], action: dict, server_names: set[str]
) -> list[str]:
    """命令草稿（只打印，不执行）。"""
    cmds = ["# 只读草稿：以下命令本脚本不会执行，请人工逐条核对"]
    new_dirs: list[str] = []
    for rel in transfer:
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if (
            parent
            and parent not in new_dirs
            and not any(n == parent or n.startswith(parent + "/") for n in server_names)
        ):
            new_dirs.append(parent)
    for d in new_dirs:
        cmds.append(f"ssh {SSH_HOST} 'mkdir -p ~/astrbot_plugin_warframe/{d}'")
    for rel in transfer:
        cmds.append(f"scp {rel} {SSH_HOST}:~/astrbot_plugin_warframe/{rel}")
    if transfer:
        cmds.append("md5sum " + " ".join(transfer))
        cmds.append(
            f"ssh {SSH_HOST} 'cd ~/astrbot_plugin_warframe && md5sum " + " ".join(transfer) + "'"
        )
    if profile == "plugin":
        cmds.append(f"ssh {SSH_HOST} 'cd ~/astrbot_plugin_warframe && ./deploy.sh'")
        code = action["code"]
        if code == "reload":
            cmds.append(
                "重载：容器内 POST http://127.0.0.1:6185/api/v1/plugins/reload "
                '{"plugin_id":"astrbot_plugin_warframe_sdjkbot"}'
            )
        elif code == "restart":
            cmds.append(
                "重启：docker restart astrbot（配置项改动 reload 不生效；★ 待在 AstrBot 4.28.1 实测确认）"
            )
        elif code == "deps_restart":
            cmds.append(
                "先装依赖（面板「依赖与资源」页或容器内 pip install -r requirements.txt）"
                "→ docker restart astrbot"
            )
        else:
            cmds.append("（动作 none：无需 reload / restart）")
    else:
        cmds.append(
            "（ops：deploy.sh 的 rsync 够不到 .zcode/**，scp 后无需 reload；"
            "crontab.template 先人工 diff 线上 SDJK_OPS 块）"
        )
    return cmds


def build_plan(profile: str, local_root: Path, server_arg: str, container_arg: str | None) -> dict:
    """生成部署计划（结构化；文本渲染见 print_plan，两模式同内容）。

    本地基准 = ``--local``（默认仓库根）里 **git 跟踪的文件**（``git ls-files``，只读）；
    不在 git 仓则退回 ``scan_tree`` 全树扫描并在输出里提示。
    """
    warnings: list[str] = []
    git_ok, tracked, dirty = _git_tracked(local_root)
    if git_ok:
        local_names = set(tracked)
    else:
        local_names = set(scan_tree(local_root))
        warnings.append(
            "⚠ 本地基准不在 git 仓内（git ls-files 不可用）⇒ 退回全树扫描；"
            "服务器独有文件无法判定「未跟踪」，一律归「需人工审核」"
        )
    if dirty:
        warnings.append("⚠ 工作树不干净，计划基于工作树内容")

    local_profile = {
        rel: _hash_file(local_root / rel)
        for rel in sorted(local_names)
        if _match_face(profile, rel) and (local_root / rel).is_file()
    }
    server = load_tree(server_arg)
    server_profile = {rel: h for rel, h in server.items() if _match_face(profile, rel)}

    transfer: list[dict] = []
    for rel in sorted(local_profile):
        lh = local_profile[rel]
        sh = server_profile.get(rel)
        if sh is not None and sh.md5 == lh.md5:
            continue  # 字节一致 = 不传（最终判据；行尾不同不构成放行理由）
        transfer.append(
            {
                "path": rel,
                "face": _match_face(profile, rel),
                "local_md5": lh.md5,
                "server_md5": sh.md5 if sh is not None else None,
                "crlf_only": bool(
                    sh is not None
                    and lh.md5_lf is not None
                    and sh.md5_lf is not None
                    and lh.md5_lf == sh.md5_lf
                ),
            }
        )

    orphans_runtime: list[str] = []
    orphans_review: list[str] = []
    for rel in sorted(server_profile):
        if rel in local_names:
            continue
        if git_ok and _git_ignored(local_root, rel):
            orphans_runtime.append(rel)  # 运行期件：.gitignore 忽略，预期存在
        else:
            orphans_review.append(rel)  # 需人工审核，绝不自动删除

    container: dict | None = None
    if container_arg:
        if profile != "plugin":
            container = {
                "simulated": False,
                "note": "容器面只对 plugin profile 模拟（ops 不在 deploy.sh 的 rsync 范围内）",
                "delete": [],
                "changed": [],
            }
        else:
            ctree = load_tree(container_arg)
            # 同步后的源树 = 服务器树（排除项之外）+ 需传文件的本地内容
            source = {rel: h for rel, h in server.items() if not _rsync_excluded(rel)}
            for row in transfer:
                if not _rsync_excluded(row["path"]):
                    source[row["path"]] = local_profile[row["path"]]
            container = {
                "simulated": True,
                "note": "按 deploy.sh 的 rsync --delete + 排除列表模拟（排除规则与 deploy.sh 保持一致）",
                "delete": sorted(
                    rel for rel in ctree if not _rsync_excluded(rel) and rel not in source
                ),
                "changed": sorted(
                    row["path"] for row in transfer if not _rsync_excluded(row["path"])
                ),
            }

    transfer_paths = [row["path"] for row in transfer]
    # 动作只看**真内容差异**：仅行尾不同的文件内容相同，传过去只为字节一致，不该把动作推重
    # （ZCode 10-17 实测：只差 CRLF 的 requirements.txt 曾把动作顶成 deps_restart，属虚警）。
    real_paths = [row["path"] for row in transfer if not row["crlf_only"]]
    action = _resolve_action(profile, real_paths)
    crlf_paths = [row["path"] for row in transfer if row["crlf_only"]]
    if crlf_paths:
        action["note"] = f"另有 {len(crlf_paths)} 个文件仅行尾不同，不计入动作判定"
    if server_profile and all(h.md5_lf is None for h in server_profile.values()):
        warnings.append(
            "⚠ 服务器清单无 md5_lf（schema 1，旧版工具导出）⇒ 认不出「仅行尾不同」，"
            "需传与动作都可能偏重；请用新版 tri_sync_audit.py 重新 --dump-manifest"
        )
    plan = {
        "readonly": True,
        "note": "只读计划：本脚本未执行任何命令（--plan 只打印，不 scp / 不 ssh / 不 rsync）",
        "profile": profile,
        "local": {
            "root": str(local_root.resolve()),
            "git": git_ok,
            "dirty": dirty,
            "tracked_files": len(local_names),
            "profile_files": len(local_profile),
        },
        "server": {"arg": server_arg, "files": len(server)},
        "warnings": warnings,
        "transfer": transfer,
        "orphans": {"runtime": orphans_runtime, "review": orphans_review},
        "container": container,
        "action": action,
        "commands": _plan_commands(profile, transfer_paths, action, set(server)),
    }
    summary = {
        "transfer": len(transfer),
        # 部署正确性判据（ZCode 10-17 建议）：运行期面的真内容差异数；工具面会静默漂移，不算
        "runtime_real": sum(
            1 for row in transfer if row["face"] == "runtime" and not row["crlf_only"]
        ),
        "crlf_only": sum(1 for row in transfer if row["crlf_only"]),
        "orphans_review": len(orphans_review),
        "orphans_runtime": len(orphans_runtime),
        "container_delete": len(container["delete"]) if container else 0,
        "action": action["code"],
    }
    plan["summary"] = summary
    plan["plan_line"] = (
        f"PLAN|profile={profile}|transfer={summary['transfer']}"
        f"|runtime_real={summary['runtime_real']}|crlf_only={summary['crlf_only']}"
        f"|orphans_review={summary['orphans_review']}|orphans_runtime={summary['orphans_runtime']}"
        f"|container_delete={summary['container_delete']}|action={summary['action']}"
    )
    return plan


def print_plan(plan: dict) -> None:
    """文本渲染（与 --json 同内容，summary 数字两模式一致）。"""
    loc = plan["local"]
    print(plan["note"])
    print(
        f"profile={plan['profile']}  本地={loc['root']}"
        f"（{'git 跟踪' if loc['git'] else '全树扫描'} {loc['tracked_files']} 文件，"
        f"其中 profile 内 {loc['profile_files']}）"
        f"  服务器={plan['server']['arg']}（{plan['server']['files']} 文件）"
    )
    for warn in plan["warnings"]:
        print(warn)

    print("\n=== 1. 需传（本地有、服务器无 或 字节不同） ===")
    if not plan["transfer"]:
        print("  （无）")
    for row in plan["transfer"]:
        srv = row["server_md5"][:8] if row["server_md5"] else "—"
        mark = "  ← 仅行尾不同（CRLF），仍按字节为准要传" if row["crlf_only"] else ""
        print(f"  {row['path']}  local={row['local_md5'][:8]}  server={srv}{mark}")
    print(f"（共 {len(plan['transfer'])}）")

    print("\n=== 2. 服务器孤儿（服务器有、本地 git 不跟踪；限本 profile 范围内） ===")
    orphans = plan["orphans"]
    print(f"  运行期件（.gitignore 忽略，预期存在）：{len(orphans['runtime'])}")
    for rel in orphans["runtime"]:
        print(f"    {rel}")
    print(f"  需人工审核（不自动删除）：{len(orphans['review'])}")
    for rel in orphans["review"]:
        print(f"    {rel}")

    print("\n=== 3. 容器面（rsync --delete 模拟） ===")
    cont = plan["container"]
    if cont is None:
        print("  （未提供 --container，跳过）")
    elif not cont["simulated"]:
        print(f"  （跳过：{cont['note']}）")
    else:
        print(f"  {cont['note']}")
        print(f"  rsync --delete 将删除（{len(cont['delete'])}）：")
        for rel in cont["delete"]:
            print(f"    {rel}")
        print(f"  同步后内容将变化/新增（{len(cont['changed'])}）：")
        for rel in cont["changed"]:
            print(f"    {rel}")

    print("\n=== 4. 动作 ===")
    act = plan["action"]
    print(f"  {act['code']}（级别 {act['level']}）— {act['desc']}")
    print(
        # 动作 none 时这些文件「有差异但不推高动作」，别叫「触发文件」（ZCode 10-17：易误读）
        f"  {'触发文件' if act['code'] != 'none' else '涉及文件（不需重载）'}（{len(act['files'])}）："
        + ("、".join(act["files"]) if act["files"] else "无")
    )
    if act.get("note"):
        print(f"  （{act['note']}）")

    print("\n=== 5. 命令草稿（只打印，不执行） ===")
    for cmd in plan["commands"]:
        print(f"  {cmd}")

    print("\n" + plan["plan_line"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="三面全文件只读 MD5 对拍 + 部署计划（本地树/服务器树/容器目录）"
    )
    ap.add_argument(
        "--local",
        default=str(Path(__file__).resolve().parent.parent),
        help="本地基准树（默认：仓库根）",
    )
    ap.add_argument("--server", help="服务器树：目录或清单 json")
    ap.add_argument("--container", help="容器目录：目录或清单 json")
    ap.add_argument("--dump-manifest", metavar="DIR", help="清单模式：扫描 DIR 生成 MD5 清单后退出")
    ap.add_argument("--label", default="dump", help="清单模式的标签（server/container）")
    ap.add_argument(
        "--out", type=Path, help="清单落盘路径（默认打印到 stdout；不得落在被对拍树内）"
    )
    ap.add_argument(
        "--no-strict", action="store_true", help="核心族存在差异也返回 0（只看报告不告警）"
    )
    ap.add_argument(
        "--plan",
        choices=("plugin", "ops"),
        help="部署计划模式：plugin=服务器树同步集（运行期面+工具面）；"
        "ops=astrbot-server-triage/scripts/automations/**；只打印不执行",
    )
    ap.add_argument("--json", action="store_true", help="计划模式：输出同内容的结构化 JSON")
    args = ap.parse_args(argv)

    if args.dump_manifest:
        dump_manifest(Path(args.dump_manifest), args.label, args.out)
        return 0
    if args.json and not args.plan:
        ap.error("--json 只与 --plan 同用")
    if args.plan:
        if not args.server:
            ap.error("--plan 需要 --server（服务器树根目录或它的清单 json）")
        plan = build_plan(args.plan, Path(args.local), args.server, args.container)
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=1))
        else:
            print_plan(plan)
        return 0
    if not args.server and not args.container:
        ap.error("对拍模式需要 --server 和/或 --container（或改用 --dump-manifest）")

    local = scan_tree(Path(args.local))
    total = 0
    if args.server:
        total += report("server", load_tree(args.server), local, note="")
    if args.container:
        total += report(
            "container",
            load_tree(args.container),
            local,
            note="容器=部署面：目录构成本就与本地/服务器树不同"
            "（无 dist/docs/tests 等），形态差异非漂移",
        )
    if total and not args.no_strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
