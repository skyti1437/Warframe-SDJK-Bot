# -*- coding: utf-8 -*-
"""预检 GitHub / 市场自动导入归档，或生成兜底 flat ZIP。

★ **2026-10-04 起用途变更**：市场已与仓库绑定，**公开仓版号一变即自动推送** ——
本脚本产出的 zip **不再是发版的必经上传件**，保留用途改为：
使用 --github-archive 按 GitHub 的 git archive 路径预检自动导入包。
默认位置参数用法继续生成 flat ZIP，供市场绑定故障时兜底上传。
发版链见 `.zcode/commands/wf/pack.md` 与 `09-open-market.md §18.0`。

为什么要单独一个脚本：市场与「本地安装」要的 zip 结构**不一样**。

* `dist/package_release.py` 产出的 `astrbot_plugin_warframe_sdjkbot.zip` 是
  **嵌套一层** `astrbot_plugin_warframe_sdjkbot/...` —— 适合 AstrBot 从 zip 安装插件。
* 市场（cloud.astrbot.app 上传压缩包通道）要的是 **flat**：`main.py` / `README.md`
  直接在最外层。2026-09-19 下载线上 v1.0.2 产物核实：159 条目、2.41MB、
  顶层元素就是 `.gitattributes / .github / README.md / _conf_schema.json …`，
  **没有** 插件名目录。该历史上传通道与当前仓库自动导入的归档结构不同。
* 仓库自动导入使用带 repo-commit/ 前缀的 GitHub ZIP，遵循 .gitattributes
  的 export-ignore。预检从完整开源 stage 建隔离 Git 提交，按独立运行文件清单
  对拍归档路径与内容。公开仓仍同步完整 stage，CI 与 README 图片保留在 Git 树中。

用法（先跑 `python dist/package_release.py --opensource` 准备好开源目录）：

    python scripts/make_market_zip.py [输出路径]
    python scripts/make_market_zip.py --github-archive [输出路径]

对应默认后缀为 -market.zip / -github.zip。正式发版使用 --github-archive。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
import sys
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OSS_DIR = ROOT / "dist" / "opensource" / "astrbot_plugin_warframe_sdjkbot"
# 官方文档写 16MB，按十进制保守检查；旧 flat 通道保留原来的 16 MiB 判据。
MAX_MARKET_BYTES = 16_000_000

# 排除清单优先复用打包脚本的（避免两处走散）；但 `dist/` 不进分发包，
# 所以别人拿到的是开源包时这个 import 会失败 —— 那时退回下面这份等价清单。
try:
    sys.path.insert(0, str(ROOT / "dist"))
    import package_release as pr  # noqa: E402

    SKIP_DIRS = set(pr.EXCLUDE_DIRS) | set(pr.OSS_EXCLUDE_DIRS)
    SKIP_FILES = set(pr.EXCLUDE_FILES) | set(pr.OSS_EXCLUDE_FILES)
    SKIP_SUFFIX = set(pr.EXCLUDE_SUFFIX)
    SKIP_GLOBS = tuple(pr.EXCLUDE_GLOBS)
    SKIP_SOURCE = "dist/package_release.py"
except Exception:  # noqa: BLE001
    # 2026-09-21 阶段 3：与主清单对齐 —— 移除本机宿主平台的工作区目录条目
    # （stage 内本就不存在该目录，字面量写入公开仓属私有痕迹残留），补
    # ".zcode"/".archive"（阶段 1 起主清单新增的排除项）。
    SKIP_DIRS = {
        ".git",
        "__pycache__",
        ".pytest_cache",
        "runtime",
        ".venv",
        "venv",
        "node_modules",
        ".idea",
        ".vscode",
        "dist",
        ".audit",
        "kb_src",
        "_cache",
        "docs",
        "outputs",
        "output",
        ".zcode",
        ".archive",
    }
    # 注：fonts/ 不再整体排除（2026-09-25）——随包放行子集字体
    # NotoSansCJKsc-Subset-{Regular,Bold}.otf（约 6.6MB，市场版开箱出图）；
    # 完整 ttc 由 SKIP_FILES 兜底排除。
    SKIP_FILES = {
        "deploy.sh",
        ".DS_Store",
        ".gitattributes",
        ".gitignore",
        "SDJKwfbot_README.md",
        "wm_ranks.json",
        "riven_weekly.json",
        "wiki_disp.json",
        "package_reverse_searcher_sdjk.py",
        "NotoSansCJK-Regular.ttc",
        "NotoSansCJK-Bold.ttc",
    }
    SKIP_SUFFIX = {".pyc", ".pyo"}
    SKIP_GLOBS = (
        "*报告*.md",
        "*调研*.md",
        "*诊断*.md",
        "*对照*.md",
        "*核验*.md",
        "*体检*.md",
        "*复评*.md",
        "*选型*.md",
        "*实测*.md",
        "*结案*.md",
        "*澄清*.md",
        "*方案*.md",
        "*.diff",
        "*.patch",
        "*_before_*.json",
    )
    SKIP_SOURCE = "内置回退清单"
    pr = None  # 独立运行（无 dist/）时用下方镜像判据


# ---------------------------------------------------------------------------
# 种子新鲜度闸门（2026-09-29 v1.1.2 追加批；与 dist/package_release.py 同判据）
# ---------------------------------------------------------------------------
# 优先调用 package_release；独立市场打包时复用随包 core.data_registry 的骨架校验。
# 效价动态字段残留 / 静态骨架损坏 / 言录使动态货单残留均拒绝，不提供 bypass。
def _seed_problems_fallback(now=None, rot_path=None, acr_path=None) -> list[str]:
    problems: list[str] = []
    rot_p = rot_path or (ROOT / "core" / "data" / "rotations.json")
    acr_p = acr_path or (ROOT / "core" / "data" / "de" / "acrichis_week.json")
    try:
        rot = json.loads(Path(rot_p).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # noqa: BLE001
        rot, problems = {}, [f"rotations.json 不可读/损坏：{exc}"]
    import sys

    sys.path.insert(0, str(ROOT))
    from core.data_registry import acrithis_seed_problems, valence_seed_problems

    problems.extend(valence_seed_problems(rot))
    try:
        acr = json.loads(Path(acr_p).read_text(encoding="utf-8"))
        problems.extend(acrithis_seed_problems(acr))
    except (OSError, ValueError) as exc:  # noqa: BLE001
        problems.append(f"言录使静态目录不可读/损坏：{exc}")
    return problems


def _seed_problems(now=None, rot_path=None, acr_path=None) -> list[str]:
    if pr is not None and hasattr(pr, "seed_freshness_problems"):
        return pr.seed_freshness_problems(now, rot_path, acr_path)
    return _seed_problems_fallback(now, rot_path, acr_path)


# ---------------------------------------------------------------------------
# 市场件专属排除（2026-09-22 瘦身：条目 177 → 80）
# ---------------------------------------------------------------------------
# 开源**仓库**保留 tests / scripts / .github 等开发与验证物料（透明、可复现）；
# 但市场安装用户只需要运行时必需件——这些物料进安装包是死代码，白占体积还
# 扩大审读面。目标形态：core / main.py / metadata.yaml / _conf_schema.json /
# requirements.txt / README.md / CHANGELOG.md / LICENSE / kb/。
# （.gitignore / .gitattributes 已被上方 SKIP_FILES 排除，不重复列。）
# assets：README 参考图，插件运行用不到
MARKET_SKIP_DIRS = {"tests", "scripts", ".github", "assets"}
MARKET_SKIP_FILES = {
    "README.en.md",  # 英文文档由中文安装包 README 链接到公开仓
    ".gitleaks.toml",  # 仓库门面（防泄漏 CI 配置），非运行件
    # ruff 配置（2026-09-25）：开发物料，市场安装用户不需要
    "pyproject.toml",
    # 根目录的三个数据构建入口（scripts/ 里的同族已随目录整体排除）
    "build_damage_data.py",
    "build_de_data.py",
    "build_stances.py",
}
# 条目基线：177（v1.0.5 前）→ 80（v1.0.5 瘦身）→ 82（v1.0.6：+core/matching.py、
# +core/data/dispositions_rivenmirror.json，变体解析倾向数据随市场件分发）。
# 与 package_release.EXPECTED_OSS_STAGE_FILES 同理——有意变更须同步
# 此常量并在 commit 正文列文件名与理由。
# → 98（金星小帐篷：+core/data/de/venus_job_manifest.json —— 随包的点位清单，
#   否则市场版小帐篷无数据源；构建脚本在 scripts/ 不进市场件）。
# → 97（删「对话助手」指令：-core/data/kim.json；实测改前 98 → 改后 97）。
# → 98（2026-09-27 §五：+core/data/de/zh_ext.json —— 市场版也要能查到官方简中，
#   否则卡面照样落英文；构建脚本 scripts/build_zh_ext.py 不进市场件）
# → 111（2026-09-29 结构优化 D1-D11：+core/commands/ 13 个 .py（__init__/
#   base/daily/progress/arbitration/rotations/relic/market/riven/wiki_misc/
#   vision/scan/dun）—— main.py 拆解为 Mixin 子包，运行期 import 必随包。
#   ⚠ D1-D11 各笔只同步了 stage 常量漏本常量（优化批不打市场件未暴露），
#   由合并链 make_market_zip 预检抓出，本笔补正：98 + 13 = 111。）
# → 108（2026-10-02 紫卡家族判定改用 DE 官方 parentName 谱系：
#   +core/data/de/riven_families.json —— 家族判定运行期必读，市场版同样需要）
# ⚠ 2026-10-03 复核：批 E 曾把本常量抬到 109，理由写「+scripts/build_riven_families.py」
#   —— **该理由不成立**（scripts/ 不进市场件；用 5717837 干净 worktree 重建实测=108），
#   属「幻觉性抬基线」。真实演进：批 E 实测 108（常量当时误为 109，两值不符未被发现）
#   → 本笔 +core/data/de/palladino_shop.json（碎银兑换表，运行期必读）⇒ 实测 109，
#   与既有常量恰好相符；本笔只补注释不改数，**别再照着 109 那个中间值反推历史**。
# → 110（2026-10-03 B1 翻译批：+core/data/de/name_bilingual.json 双语名称表，运行期必读）
# → 111（2026-10-04 警报奖励批：+core/data/de/storeitems_zh.json，scripts/ 不进市场件）
# → 112（2026-10-09 插件 Logo：+logo.png —— 顶层文件，随市场卡片展示）
# → 113（2026-10-09 wfsim 对照 B 批：+core/data/co_behavior.json —— CO 桶别表，运行期必读）
# 2026-10-16 插件页面批 +3：core/webpanel.py、pages/sdjk/index.html、.astrbot-plugin/i18n/zh-CN.json
# 2026-10-17 数据登记表批 +1：core/data_registry.py（随包数据登记表与统一健康判据，
# 面板 / health / 打包闸门读同一份；tests/ 不进市场件）
# 2026-10-17 清理批 −2：core/data/de/fissureModifiers.json、core/data/de/syndicatesData.json（插件零读取）
EXPECTED_MARKET_ENTRIES = 153
# 2026-10-23 紫卡词条池补抽样批：118 → 119（+core/data/de/riven_pools_survey.json；
#   scripts/ 不进市场件）
# 2026-10-23 紫卡词条范围批：116 → 118（+core/riven_pools.py、+core/data/de/riven_pools.json；
#   scripts/ tests/ 不进市场件）

# ★ 可复现打包（2026-09-26 用户侧建议）：统一 zip 条目时间戳 = 2026-01-01T00:00:00Z。
#   之前取文件 mtime，导致「内容没变、重建却换 sha」（上传期两次被迫冻结重建：
#   727ba7a7 → a5159a32 这类）。可用 SOURCE_DATE_EPOCH 覆盖。
ZIP_EPOCH_DEFAULT = 1767225600  # 2026-01-01T00:00:00Z


def _zip_datetime() -> tuple:
    """zip 条目统一时间戳 ``(Y,M,D,h,m,s)`` —— 可复现打包（2026-09-26）。

    背景：zip 条目时间戳默认取**文件 mtime**，所以只要文件被重写（哪怕内容一字
    未变），重建出来的 sha 就不同 —— 我们已经两次因此在上传期被迫「冻结重建」
    （`727ba7a7 → a5159a32` 这类换号）。这里把所有条目钉到**固定时刻**：
    **同内容 ⇒ 同 sha**。可被 ``SOURCE_DATE_EPOCH`` 覆盖（reproducible-builds 惯例）；
    早于 zip 格式下限（1980-01-01）的值抬到下限，避免打包报错。
    """
    raw = os.environ.get("SOURCE_DATE_EPOCH", "")
    try:
        epoch = int(raw) if raw.strip() else ZIP_EPOCH_DEFAULT
    except ValueError:
        epoch = ZIP_EPOCH_DEFAULT
    t = time.gmtime(max(epoch, 315532800))  # 315532800 = 1980-01-01T00:00:00Z
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


# 开源 stage 的期望文件数（★ 必须与 dist/package_release.py::EXPECTED_OSS_STAGE_FILES **同步**；
# 两处基线任一变动都要同笔改）。
# 2026-10-08 审核实证：本脚本原先只查「stage 存在」+「种子新鲜」，**不查 stage 是否追平 HEAD**
# ⇒ 若绕开重建直接打包，会把旧 stage（当时 239 文件、不含本批任何修复）打出去。这里加数量闸门兜住；
# 数量相同但内容不同的情形，仍靠流程约束（打包前必重跑 dist/package_release.py --opensource）。
EXPECTED_STAGE_FILES = 340  # 与 dist/package_release.py::EXPECTED_OSS_STAGE_FILES 同步
# 2026-10-23 紫卡词条池补抽样批：278 → 280（+scripts/survey_riven_pools.py、+core/data/de/riven_pools_survey.json）
# 2026-10-23 紫卡词条范围批：274 → 278（+core/riven_pools.py、+core/data/de/riven_pools.json、
#   +scripts/build_riven_pools.py、+tests/test_riven_ranges.py）
# 2026-10-23 趋势折线图批：273 → 274（+tests/test_trend_chart.py；tests/ 不进市场件）
# 2026-10-23 紫卡实时排行批：272 → 273（+tests/test_riven_realtime_rank.py；tests/ 不进市场件）
# 2026-10-17 清理批：272 → 270（−fissureModifiers.json、−syndicatesData.json）
# 2026-10-17 数据登记表批：267 → 269（+core/data_registry.py、+tests/test_data_registry.py）
# 2026-10-17 部署计划批：269 → 270（+tests/test_deploy_plan.py；tests/ 不进市场件）
# 2026-10-17 发版计划器批：270 → 272（+scripts/release_plan.py、+tests/test_release_plan.py；
# 两者都不进市场件）


def market_files(files: dict[str, bytes]) -> dict[str, bytes]:
    """运行文件清单，与 Git 属性规则独立对拍，防止误排或漏排。"""
    out = {}
    for name, content in files.items():
        rel = Path(name)
        if set(rel.parts) & (SKIP_DIRS | MARKET_SKIP_DIRS):
            continue
        if rel.name in SKIP_FILES | MARKET_SKIP_FILES or rel.suffix in SKIP_SUFFIX:
            continue
        if any(rel.match(g) for g in SKIP_GLOBS):
            continue
        out[name] = content
    return out


def make_github_archive(files: dict[str, bytes], out: Path) -> None:
    """由完整开源投影构建 Git 归档，对拍通过后原子写出，不访问远端。"""
    expected = market_files(files)
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="archive-preview",
        GIT_AUTHOR_EMAIL="archive@example.invalid",
        GIT_COMMITTER_NAME="archive-preview",
        GIT_COMMITTER_EMAIL="archive@example.invalid",
        GIT_AUTHOR_DATE="2026-01-01T00:00:00Z",
        GIT_COMMITTER_DATE="2026-01-01T00:00:00Z",
    )
    with tempfile.TemporaryDirectory(prefix=".archive-", dir=out.parent) as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()

        def git(*args: str) -> bytes:
            return subprocess.run(
                ["git", *args], cwd=repo, env=env, check=True, capture_output=True
            ).stdout

        git("init", "--quiet", "--template=", "--object-format=sha1", "-b", "archive-preview")
        for name, content in files.items():
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts or ".git" in rel.parts:
                raise ValueError(f"非法归档路径：{name}")
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o644)
        git("add", "--force", "--all")
        staged = {}
        for entry in git("ls-files", "--stage", "-z").split(b"\0"):
            if entry:
                meta, name = entry.split(b"\t", 1)
                staged[name.decode()] = meta.split()[1].decode()
        original = {
            name: hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            for name, data in files.items()
        }
        if staged != original:
            raise ValueError("临时 Git 提交与开源投影字节不一致（检查换行或过滤器）")
        git("commit", "--quiet", "-m", "归档预检")
        commit = git("rev-parse", "HEAD").decode().strip()
        prefix = f"Warframe-SDJK-Bot-{commit}/"
        archive = Path(tmp) / "archive.zip"
        git("archive", "--format=zip", f"--prefix={prefix}", f"--output={archive}", "HEAD")
        with zipfile.ZipFile(archive) as z:
            entries = [i for i in z.infolist() if not i.is_dir()]
            actual = {i.filename.removeprefix(prefix): z.read(i) for i in entries}
        if len(entries) != len(actual) or actual != expected:
            missing = sorted(expected.keys() - actual.keys())
            extra = sorted(actual.keys() - expected.keys())
            changed = sorted(k for k in actual.keys() & expected.keys() if actual[k] != expected[k])
            raise ValueError(
                f"Git 归档与运行文件清单不一致：缺失={missing}，多出={extra}，内容变化={changed}"
            )
        if archive.stat().st_size > MAX_MARKET_BYTES:
            raise ValueError(f"Git 归档超过 {MAX_MARKET_BYTES} 字节上限")
        archive.replace(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("--github-archive", action="store_true", help="预检市场自动导入归档")
    args = parser.parse_args()
    if not OSS_DIR.is_dir():
        print(f"✗ 找不到开源目录 {OSS_DIR}\n  先跑：python dist/package_release.py --opensource")
        return 2

    if args.github_archive:
        if pr is None:
            print("✗ GitHub 归档预检需要私有构建树的 dist/ 同步闸门")
            return 2
        from sync_opensource import expected_stage, local_gates, stage_files

        ok, evidence = local_gates(OSS_DIR, ROOT)
        print("\n".join(evidence))
        if not ok:
            return 1
        # 后续版本解析与归档共用这一份字节快照，并重新对拍 HEAD，拒绝闸门期间的改写。
        files = stage_files(OSS_DIR)
        if files != expected_stage(ROOT):
            print("✗ stage 在闸门后发生变化，与 HEAD 开源投影不一致")
            return 1

    # 种子新鲜度硬闸门（2026-09-29 v1.1.2 追加批）：静态结构损坏或动态值残留即拒绝打包，无 bypass。
    problems = _seed_problems()
    if problems:
        print("✗ [种子新鲜度闸门] 拒绝打包 ——")
        for p in problems:
            print("  - " + p)
        print(
            "  效价种子只保留轮换骨架；言录使只保留静态候选目录，修复 "
            "core/data/de/acrichis_week.json 后重跑（见 dist/package_release.py::_SEED_FIX_GUIDE）"
        )
        return 1

    _stage_files = [
        p
        for p in OSS_DIR.rglob("*")
        if p.is_file()
        and not {".git", "__pycache__"}.intersection(p.relative_to(OSS_DIR).parts)
        and p.suffix != ".pyc"
    ]
    stage_count = len(files) if args.github_archive else len(_stage_files)
    if stage_count != EXPECTED_STAGE_FILES:
        print(
            f"✗ [stage 新鲜度闸门] stage 文件数 {stage_count} != 基线 {EXPECTED_STAGE_FILES}"
            " —— stage 未追平 HEAD，先重跑：python dist/package_release.py --opensource"
        )
        return 1

    meta = (
        files["metadata.yaml"].decode("utf-8")
        if args.github_archive
        else (OSS_DIR / "metadata.yaml").read_text(encoding="utf-8")
    )
    ver = re.search(r"^version:\s*v?([\d.]+)\s*$", meta, re.M)
    ver = ver.group(1) if ver else "0.0.0"
    suffix = "github" if args.github_archive else "market"
    out = args.output or ROOT / "dist" / f"astrbot_plugin_warframe_sdjkbot-{ver}-{suffix}.zip"
    if args.github_archive:
        if out.resolve().is_relative_to(OSS_DIR.resolve()):
            print("✗ 归档输出须位于开源 stage 之外")
            return 1
        if len(market_files(files)) != EXPECTED_MARKET_ENTRIES:
            print(f"✗ 运行文件数与基线 {EXPECTED_MARKET_ENTRIES} 不符")
            return 1
        try:
            make_github_archive(files, out)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"✗ GitHub 归档预检失败：{exc}")
            return 1
        with zipfile.ZipFile(out) as z:
            n = sum(not i.is_dir() for i in z.infolist())
        size = out.stat().st_size
        print(f"[GitHub archive] {out}（{n} 文件，{size} 字节，{size / 1e6:.2f} MB）")
        print("  ✓ 完整 stage 保留；归档文件集合与内容逐字节通过，export-ignore 生效")
        return 0

    n = 0
    dt = _zip_datetime()  # ★ 固定时间戳（同内容 ⇒ 同 sha）
    with zipfile.ZipFile(out, "w") as z:
        for f in sorted(OSS_DIR.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(OSS_DIR)
            if set(rel.parts) & SKIP_DIRS:
                continue
            if set(rel.parts) & MARKET_SKIP_DIRS or rel.name in MARKET_SKIP_FILES:
                continue
            if rel.name in SKIP_FILES or rel.suffix in SKIP_SUFFIX:
                continue
            if any(rel.match(g) for g in SKIP_GLOBS):
                continue
            zi = zipfile.ZipInfo(rel.as_posix(), date_time=dt)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o100644 << 16  # 稳定权限位（不受 umask 影响）
            z.writestr(zi, f.read_bytes())  # ★ 不套顶层目录
            n += 1

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"[flat zip] {out}  ({n} 条目, {size_mb:.2f} MB)")
    print(f"  排除清单来源：{SKIP_SOURCE}")
    if n != EXPECTED_MARKET_ENTRIES:
        print(
            f"✗ 条目数 {n} != 基线 {EXPECTED_MARKET_ENTRIES}"
            "（有意变更请同步常量并在 commit 正文列文件名与理由）"
        )
        return 1
    if size_mb > 16:
        print("✗ 超过市场 16MB 上限")
        return 1
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        top = sorted({x.split("/")[0] for x in names if "/" in x})
        print("  顶层目录（应为 core + 插件页面 pages、.astrbot-plugin）：", top or "无")
        for must in ("metadata.yaml", "main.py", "README.md", "CHANGELOG.md"):
            mark = "✓" if must in names else "✗ 缺失"
            print(f"  {mark} {must}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
