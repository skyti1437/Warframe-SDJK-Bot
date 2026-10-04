# -*- coding: utf-8 -*-
"""三面全文件只读对拍：本地树 / 服务器树 / 容器目录 的非临时文件 MD5 差异表。

背景（2026-09-28 插件 ID 迁移批教训）：当时只对拍「运行期面」（main.py + core/），
服务器树/容器的 README 停在旧版四版未被发现。本脚本对三棵树做**全文件** MD5
清单比对（排除临时/运行期生成物），差异按目录族分组输出。

红线（开工指令 Slice 1 任务 1.2 裁定）：
  · 只读——不修改/删除三棵树内任何被对拍文件；
  · 不发起 ssh/远程操作——服务器/容器不可直挂时，由人在对端可访问的环境先跑
    --dump-manifest 生成清单文件，拿回本地后把清单路径传给 --server/--container；
  · 清单优先打印到 stdout；--out 落盘前会校验路径不落在被对拍的树内。

用法：
  # 三面对拍（--server/--container 传目录或清单 json 均可；local 默认仓库根）
  # --container 应指向容器内插件安装目录
  # /AstrBot/data/plugins/astrbot_plugin_warframe_sdjkbot（不要指 /AstrBot/data 根，
  # 那会把整个 data/ 下无关运行期数据卷进来）
  python scripts/tri_sync_audit.py --server ~/srv-mirror --container /mnt/container

  # 在服务器上生成清单（由人执行，脚本自身不发起远程操作）
  python scripts/tri_sync_audit.py --dump-manifest ~/astrbot_plugin_warframe \
      --label server --out /tmp/server.manifest.json

差异分组：顶层目录为一族；plugin_data/ 属「运行期数据」参考族（预期三面不一致，
不计入退出码）；顶层 data/（本机凭据目录，Gate A 专拦）与 temp/tmp/runtime/
output/outputs 不进对拍；容器树是部署面，目录构成本就与本地/服务器不同
（无 dist/docs/tests 等），形态差异按族呈现、不算漂移。

退出码：核心族存在差异 → 1（巡检告警口径）；仅运行期族差异或无差异 → 0；
--no-strict 时恒为 0。本脚本运行期不 import 项目任何模块（scripts/ 纪律）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
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


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def scan_tree(root: Path) -> dict[str, str]:
    """只读扫描一棵树 → {posix 相对路径: md5}。不修改任何文件、不跟随符号链接。"""
    out: dict[str, str] = {}
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
            out[rel] = md5_file(Path(dirpath) / name)
    return out


def load_tree(arg: str) -> dict[str, str]:
    """对拍输入：目录（现场扫描）或清单 json（对端导出）。"""
    p = Path(arg)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        return {row[0]: row[1] for row in data["files"]}
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
        "files": sorted([rel, md5] for rel, md5 in files.items()),
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


def report(label: str, other: dict[str, str], local: dict[str, str], note: str) -> int:
    """打印一面对拍差异（按目录族分组），返回核心族差异数。"""
    only_other = sorted(set(other) - set(local))
    only_local = sorted(set(local) - set(other))
    changed = sorted(rel for rel in set(other) & set(local) if other[rel] != local[rel])

    groups: dict[str, list[str]] = {}
    for rel in only_other:
        groups.setdefault(group_of(rel), []).append(f"仅{label}: {rel}")
    for rel in only_local:
        groups.setdefault(group_of(rel), []).append(f"仅local: {rel}")
    for rel in changed:
        groups.setdefault(group_of(rel), []).append(
            f"内容不同: {rel}  local={local[rel][:8]}… {label}={other[rel][:8]}…"
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="三面全文件只读 MD5 对拍（本地树/服务器树/容器目录）")
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
    args = ap.parse_args(argv)

    if args.dump_manifest:
        dump_manifest(Path(args.dump_manifest), args.label, args.out)
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
