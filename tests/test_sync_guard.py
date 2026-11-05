# -*- coding: utf-8 -*-
"""开源同步脚本的闸门守卫（python3 tests/test_sync_guard.py）

背景（两起真实事故）：
1. **2026-09-19**：`dist/package_release.py` 重建开源目录时是「先把目录清空（保留
   .git）再逐个复制」，清空到复制完成之间有十几秒窗口。有一次
   `dist/sync_opensource.py` 正好落在这个窗口里，于是「本地暂时缺了的文件」被当成
   用户意图 —— GitHub 上收到一份**删掉 8 个文件**的提交（含 `.gitignore`、CI
   workflow、`CHANGELOG.md`），16 秒后才被下一次同步补回。
2. **2026-09-25 18:23**：发版链**顺手**跑了一次同步（无说明、无授权），推上去的是
   **「v1.0.8 代码 + v1.0.7 标签」的半成品态** —— 与本地 stage 有 10 个文件不符
   （四个版本落点、CHANGELOG 缺条目、子集字体少 `≡ 犽` 两字、`.gitattributes`、
   `make_market_zip.py`）。当时四个版本落点**互相一致**（都还是 v1.0.7），所以
   「只比版本标签」的校验拦不住它；能拦住它的是「stage == HEAD 的开源投影」这条
   逐文件对拍。

这里钉住：
1. `guard_mass_delete()`：删除项可疑（命中关键文件 / 超过阈值）时拒绝提交；
2. 零变化不建提交（以前每次调用都会留一条历史，纯噪音）；
3. 防护必须发生在**任何写操作之前**；
4. **默认 dry-run**：不加 `--yes` 一个字节都不写远端；
5. **stage == HEAD 开源投影**（逐文件字节对拍）——半成品态必须被拒；
6. 版本落点一致 + stage 版本不得低于远端。

注：`dist/` 不进分发包，开源包里本测试自动跳过。
"""

from __future__ import annotations

import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNC = ROOT / "dist" / "sync_opensource.py"
PKG = ROOT / "dist" / "package_release.py"

if not SYNC.is_file():
    # 开源包（分发包）里没有 dist/ —— 这些脚本本来就不对外发布
    print("[SKIP] dist/sync_opensource.py 不在当前副本内（开源包），跳过")
    sys.exit(0)

sys.path.insert(0, str(ROOT / "dist"))
import sync_opensource as S  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 1. 事故场景复现：删除 8 个文件里含关键文件 → 必须拒绝
# ---------------------------------------------------------------------------
ACCIDENT_GONE = [
    ".gitattributes",
    ".github/workflows/gitleaks.yml",
    ".gitignore",
    ".gitleaks.toml",
    "CHANGELOG.md",
    "build_damage_data.py",
    "build_de_data.py",
    "build_stances.py",
]
ok, why = S.guard_mass_delete(ACCIDENT_GONE, 155, allow=False)
check("★ 事故复现：8 个删除项（含关键文件）被拒绝", ok is False, why)
check("  拒绝原因指出关键文件", "关键文件" in why, why)

# 逐个关键文件都要拦得住（单删一个也不许）
for name in (
    ".gitignore",
    ".gitattributes",
    "README.md",
    "main.py",
    "metadata.yaml",
    "CHANGELOG.md",
    "LICENSE",
    "_conf_schema.json",
    ".github/workflows/gitleaks.yml",
    ".github/workflows/ci.yml",
):
    ok, why = S.guard_mass_delete([name], 155, allow=False)
    check(f"  单删关键文件也拒绝：{name}", ok is False, why)

# ---------------------------------------------------------------------------
# 2. 阈值：普通文件少量删除放行，成批删除拦住
# ---------------------------------------------------------------------------
ok, why = S.guard_mass_delete([], 155, allow=False)
check("无删除项 → 放行", ok is True, why)

ok, why = S.guard_mass_delete(["old_script.py"], 155, allow=False)
check("删 1 个普通文件 → 放行（阈值内重命名/清理是正常的）", ok is True, why)

ok, why = S.guard_mass_delete(["a.py", "b.py"], 155, allow=False)
check("删 2 个普通文件 → 放行（刚触绝对下限）", ok is True, why)

ok, why = S.guard_mass_delete(["a.py", "b.py", "c.py"], 155, allow=False)
check("删 3 个普通文件 → 拒绝（超绝对下限 2）", ok is False, why)

# 远端很大时按 1% 算：remote=1000 → limit = max(2, 10) = 10
ok, _ = S.guard_mass_delete([f"f{i}.py" for i in range(10)], 1000, allow=False)
check("远端 1000 个时删 10 个 → 放行（10 ≤ 1% 阈值）", ok is True)
ok, why = S.guard_mass_delete([f"f{i}.py" for i in range(11)], 1000, allow=False)
check("远端 1000 个时删 11 个 → 拒绝（超 1% 阈值）", ok is False, why)

# ---------------------------------------------------------------------------
# 3. 逃生门：--allow-mass-delete 才放行
# ---------------------------------------------------------------------------
ok, why = S.guard_mass_delete(ACCIDENT_GONE, 155, allow=True)
check("带 allow=True（--allow-mass-delete）→ 放行", ok is True, why)
check("  放行原因标注了显式确认", "显式确认" in why, why)
check(
    "阈值常量存在且合理",
    S.GUARD_ABS == 2 and 0 < S.GUARD_RATIO < 0.05,
    f"ABS={S.GUARD_ABS} RATIO={S.GUARD_RATIO}",
)
check(
    "命令行开关可被识别（不混进提交信息）",
    "--allow-mass-delete" not in S._resolve_msg(),
    S._resolve_msg(),
)

# ---------------------------------------------------------------------------
# 4. 源码级守卫：防护必须在写操作之前 + 零变化短路 + 默认 dry-run
# ---------------------------------------------------------------------------
src = SYNC.read_text(encoding="utf-8")
i_guard = src.index("guard_mass_delete(gone")
# ★ 锚点要用**写操作本身**：`/git/blobs` 也出现在只读的 read_remote_versions() 里
i_blob = src.index('api("POST", f"/repos/{OWNER}/{REPO}/git/blobs"')
i_tree = src.index('api("POST", f"/repos/{OWNER}/{REPO}/git/trees"')
i_nomsg = src.index("无需提交")
i_srcguard = src.index("本地开源目录不存在")
check("★ 删除闸门在创建任何 blob 之前执行", i_guard < i_blob, f"{i_guard} vs {i_blob}")
check("★ 零变化短路在建树之前执行", i_nomsg < i_tree, f"{i_nomsg} vs {i_tree}")
check("入口先校验本地目录存在", i_srcguard < i_blob, f"{i_srcguard} vs {i_blob}")
check(
    "零变化分支 return 0（不建提交）",
    bool(re.search(r"if not diff and not gone:\n(?:.*\n)*?\s+return 0", src)),
)
check(
    "import 本模块不再读凭据（离线可单测）",
    S._AUTH._cached is None and not re.search(r"^TOKEN = token\(\)", src, re.M),
)
check("模块级可见 --allow-mass-delete 开关", S.ALLOW_MASS_DELETE is False)

# ★ 2026-09-25 新增：默认 dry-run + 本地/远端闸门都在写操作之前
i_lgate = src.index("local_gates(SRC")
i_rgate = src.index("remote_gate(stage_v")
i_dry = src.index("DRY-RUN")
check("★ 本地闸门在写操作之前执行", i_lgate < i_blob, f"{i_lgate} vs {i_blob}")
check("★ 远端版本闸门在写操作之前执行", i_rgate < i_blob, f"{i_rgate} vs {i_blob}")
check("★ dry-run 短路在写操作之前执行", i_dry < i_blob, f"{i_dry} vs {i_blob}")
check(
    "★ 默认 dry-run：只有显式 --yes 才推",
    re.search(r'^PUSH = "--yes" in FLAGS$', src, re.M) is not None,
)
check("本进程未带 --yes → PUSH 为 False（默认不推）", S.PUSH is False)

# ★★ 2026-09-26：零变化短路必须排在闸门**之后**（stage 过期时会静默空转）
#   实测事故：二次修复提交后跑同步，远端 push.py blob ≠ 本地，脚本却说「需更新 0」——
#   因为「零变化」是拿**磁盘 stage 目录**（构建产物，可能过期）跟远端比的。
# 锚点用**代码行**（注释里也出现了「无需提交」字样，别被注释骗到）
i_zero = src.index("if not diff and not gone:")
check(
    "★ 零变化短路在 local_gates 之后（stage 过期不得静默空转）",
    i_lgate < i_zero,
    f"local_gates@{i_lgate} vs 短路@{i_zero}",
)
check(
    "★ 零变化短路在闸门判定 `if not ok` 之后",
    src.index("if not ok:") < i_nomsg,
    f"{src.index('if not ok:')} vs {i_nomsg}",
)
check("★ 零变化文案点明「stage == HEAD 开源投影」前提", "stage == HEAD 开源投影 ⇒ 无需提交" in src)

# ---------------------------------------------------------------------------
# 5. 根因侧：构建脚本不得再「先清空再复制」+ .gitattributes 行尾要归一
# ---------------------------------------------------------------------------
if PKG.is_file():
    psrc = PKG.read_text(encoding="utf-8")
    i_cc = psrc.index("def copy_clean")
    i_next = psrc.index("\ndef ", i_cc + 10)
    body = psrc[i_cc:i_next]
    check(
        "★ copy_clean 先构建到 .new（不再先清空原目录）",
        ".new" in body and body.index("fresh.mkdir") < body.index("STAGE.iterdir()"),
    )
    check("★ copy_clean 仍保留 .git（开源目录是 git 仓库）", 'child.name == ".git"' in body)
    check("copy_clean 复制目标写成 fresh（不是 STAGE）", "dst = fresh / rel" in body)
    check("替换完成后清理 .new 残留", "shutil.rmtree(fresh" in body)
    # ★ 2026-09-25：与「只比版本标签拦不住半成品态」同源的坑——stage 的
    #   .gitattributes 曾是 CRLF（write_text 的 Windows 行为 + normalize_eol
    #   把它漏了），于是本地 git 看它是 LF、公开仓里是 CRLF，blob 永远对不上。
    i_ne = psrc.index("def normalize_eol")
    i_ne_end = psrc.index("\ndef ", i_ne + 10)
    ne_body = psrc[i_ne:i_ne_end]
    check(
        "★ normalize_eol 覆盖 dotfile（.gitattributes 不再漏）",
        'f.name.startswith(".")' in ne_body,
        ne_body[:120],
    )
    check(
        "★ normalize_eol 不碰 stage 自己的 .git/", '".git" in f.relative_to(STAGE).parts' in ne_body
    )
    check("normalize_eol 有二进制兜底（按内容判 NUL）", 'b"\\x00" in raw' in ne_body)
else:
    print("[SKIP] dist/package_release.py 不在当前副本内")

# ---------------------------------------------------------------------------
# 6. 离线端到端：把网络换成记账本，验证 main() 的几条路径
# ---------------------------------------------------------------------------
FAKE_REF = {"object": {"sha": "a" * 40}}


class _FakeAPI:
    """把 S.api 换成记账本：读请求返回构造数据，写请求被记录。

    ★ 用 400（4xx）而不是 500 表示「不该被调到这里」—— api() 对 5xx 会自动
    退避重试（sleep 3+6 秒），会把测试拖慢。
    """

    def __init__(self, blobs: list[dict] | None = None):
        self.calls: list[tuple[str, str]] = []
        self._blobs = blobs or []

    def __call__(self, method, path, payload=None, tries=3):
        self.calls.append((method, path))
        if method == "GET" and "/git/ref/heads/" in path:
            return 200, FAKE_REF
        # ★ 2026-09-26：闸门改为「先取 commit sha，再按 sha 取 tree」——
        #   桩要对上这个调用式（按分支名读 tree 已废弃，见 §12）
        if method == "GET" and "git/trees/" in path and "recursive=1" in path:
            return 200, {"tree": self._blobs}
        if method == "GET" and "/git/commits/" in path:
            return 200, {"tree": {"sha": "e" * 40}}
        if method == "POST" and path.endswith("/git/blobs"):
            return 201, {"sha": "b" * 40}
        if method == "POST" and path.endswith("/git/trees"):
            return 201, {"sha": "c" * 40}
        if method == "POST" and path.endswith("/git/commits"):
            return 201, {"sha": "d" * 40}
        if method == "PATCH" and "/git/refs/heads/" in path:
            return 200, {"object": {"sha": "d" * 40}}
        return 400, {"message": "不该被调用的接口"}

    @property
    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PATCH", "PUT", "DELETE")]


def _run_main(fake, src: Path, *, push: bool = True, gates: bool = True) -> int:
    """用假 API + 临时 SRC 跑一次 main()（全程无网络、无真实仓库）。

    ★ 2026-09-25：main() 多了本地/远端闸门（仓库干净 / stage==HEAD 投影 /
    版本落点）。临时目录不是真仓库，必须**只打桩这两个缝**，否则测的就变成
    「桩目录不是真仓库」而不是被测行为。闸门本身在 §7~§9 单独测。
    """
    orig = (S.api, S.SRC, S.PUSH, S.local_gates, S.remote_gate)
    S.api, S.SRC, S.PUSH = fake, src, push
    if gates:
        S.local_gates = lambda *a, **k: (True, ["（测试打桩：本地闸门跳过）"])
        S.remote_gate = lambda *a, **k: (True, ["（测试打桩：远端闸门跳过）"])
    try:
        return S.main()
    finally:
        (S.api, S.SRC, S.PUSH, S.local_gates, S.remote_gate) = orig


def _mk(d: Path, names, content: bytes = b"x"):
    for n in names:
        p = d / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


# 6a. 残缺目录（本地 2 个 / 远端 155 个）→ 拒绝，且**一个写请求都没有**
REMOTE_155 = sorted(set(ACCIDENT_GONE) | {f"core/f{i}.py" for i in range(147)})
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    _mk(d, ["core/f1.py", "core/f2.py"])
    fake = _FakeAPI([{"path": p, "type": "blob", "sha": "deadbeef"} for p in REMOTE_155])
    rc = _run_main(fake, d)
    check("★ 端到端：残缺目录 → main() 返回 1（拒绝同步）", rc == 1, f"rc={rc}")
    check("★ 端到端：拒绝时没有任何写请求（远端毫发无损）", not fake.writes, str(fake.writes))
    check(
        "  但仍读了远端 ref/tree（先比较再决定）", any("git/ref/heads" in p for _, p in fake.calls)
    )

# 6b. 零变化 → 返回 0 且不建提交
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    names = ["core/f1.py", "core/f2.py"]
    _mk(d, names)
    fake = _FakeAPI([{"path": n, "type": "blob", "sha": S.blob_sha(b"x")} for n in names])
    rc = _run_main(fake, d)
    check("★ 端到端：内容一致 → main() 返回 0", rc == 0, f"rc={rc}")
    check("★ 端到端：零变化不产生任何写请求（不再留空提交）", not fake.writes, str(fake.writes))

# 6c. 正常更新（1 个新文件、0 删除）+ 显式 --yes → 放行并真的推送
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    _mk(d, ["core/f1.py", "core/f2.py", "core/new.py"])
    fake = _FakeAPI(
        [{"path": n, "type": "blob", "sha": S.blob_sha(b"x")} for n in ("core/f1.py", "core/f2.py")]
    )
    rc = _run_main(fake, d, push=True)
    check("★ 端到端：正常新增文件 + --yes → 放行（闸门不误伤正常同步）", rc == 0, f"rc={rc}")
    check(
        "  确实发起了写请求（上传 blob + 更新 ref）",
        any(p.endswith("/git/blobs") for _, p in fake.writes)
        and any("/git/refs/heads/" in p for _, p in fake.writes),
        str(fake.writes),
    )

# 6d. 同样的更新，但**不带 --yes**（默认 dry-run）→ 0 写请求
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    _mk(d, ["core/f1.py", "core/f2.py", "core/new.py"])
    fake = _FakeAPI(
        [{"path": n, "type": "blob", "sha": S.blob_sha(b"x")} for n in ("core/f1.py", "core/f2.py")]
    )
    rc = _run_main(fake, d, push=False)
    check("★ 端到端：默认 dry-run → 返回 0 且零写请求", rc == 0, f"rc={rc}")
    check(
        "★ 端到端：dry-run 不发任何 POST/PATCH（远端毫发无损）", not fake.writes, str(fake.writes)
    )

# 6e. 本地目录不存在 → 立即拒绝，连读请求都不发
fake = _FakeAPI([])
rc = _run_main(fake, Path(tempfile.gettempdir()) / "definitely_not_here_9527")
check(
    "★ 端到端：目录不存在 → 返回 1 且不发任何请求",
    rc == 1 and not fake.calls,
    f"rc={rc} calls={fake.calls}",
)

# ---------------------------------------------------------------------------
# 7. 版本落点：按落点语义解析（不是无脑取第一个数字）
# ---------------------------------------------------------------------------
check(
    "README 首行取版本",
    S.version_of_file("README.md", "# AstrBot 插件 · Warframe SDJKBOT v1.0.8（`x`）\n")
    == (1, 0, 8),
)
check(
    "metadata.yaml 取 version 行",
    S.version_of_file("metadata.yaml", "name: x\nversion: v1.0.8\nauthor: y\n") == (1, 0, 8),
)
check(
    "core/__init__.py 取 __version__",
    S.version_of_file("core/__init__.py", '__version__ = "1.0.8"\n') == (1, 0, 8),
)
MAIN_TXT = (
    '@register("astrbot_plugin_warframe_sdjkbot", "skyti1437",\n'
    '          f"{BRAND}：世界状态 / 市场查价 / 蹲点推送",\n'
    '          "1.0.8")\n'
)
check(
    "main.py @register 第 4 参（跨行）取版本", S.version_of_file("main.py", MAIN_TXT) == (1, 0, 8)
)
check(
    "CHANGELOG 首条小节取版本",
    S.version_of_file("CHANGELOG.md", "# 更新日志\n\n说明…\n\n## v1.0.8\n- x\n\n## v1.0.7\n- y\n")
    == (1, 0, 8),
)
check("解析不到时返回 None", S.version_of_file("metadata.yaml", "name: x\n") is None)

V_OK = {k: (1, 0, 8) for k in S.VERSION_SOURCES}
ok, why = S.versions_agree(V_OK)
check("四个落点同号 → 通过", ok is True, why)
ok, why = S.versions_agree({**V_OK, "metadata.yaml": (1, 0, 7)})
check("★ 落点不一致 → 拒绝", ok is False and "不一致" in why, why)
ok, why = S.versions_agree({**V_OK, "README.md": None})
check("★ 落点缺失/读不出 → 拒绝", ok is False and "缺失" in why, why)

# ---------------------------------------------------------------------------
# 8. ★ 半成品态必须被拒：stage ≠ HEAD 的开源投影
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    _mk(d, ["main.py"], b"V1.0.8\n")
    check("投影一致 → 无问题", S.stage_projection_diff(d, {"main.py": b"V1.0.8\n"}) == [])

    probs = S.stage_projection_diff(d, {"main.py": b"V1.0.8\n", "core/new_feature.py": b"new\n"})
    check(
        "★ 半成品态①：HEAD 有而 stage 没有（没重跑构建）→ 报缺失",
        any("缺少应有文件" in p for p in probs),
        str(probs),
    )

    probs = S.stage_projection_diff(d, {"main.py": b"V1.0.7\n"})
    check(
        "★ 半成品态②：stage 里是 HEAD 没有的内容（含未提交改动）→ 报不符",
        any("与 HEAD 不符" in p for p in probs),
        str(probs),
    )

    _mk(d, ["core/uncommitted.py"], b"half\n")
    probs = S.stage_projection_diff(d, {"main.py": b"V1.0.8\n"})
    check("★ 半成品态③：stage 多出未提交文件 → 报多余", any("多出" in p for p in probs), str(probs))

# 真实的 09-25 事故形态：stage 的版本落点落后于 HEAD 的投影
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    _mk(d, ["main.py", "metadata.yaml"], b'@register("x", "y", "1.0.7")\n')
    expected = {"main.py": b'@register("x", "y", "1.0.8")\n', "metadata.yaml": b"version: v1.0.7\n"}
    probs = S.stage_projection_diff(d, expected)
    check(
        "★ 事故形态复现：stage 落后于 HEAD 投影 → 报不符",
        any("main.py" in p and "不符" in p for p in probs),
        str(probs),
    )
    ok, why = S.versions_agree(
        S.read_versions(
            {
                "README.md": b"v1.0.7\n",
                "metadata.yaml": b"version: v1.0.7\n",
                "core/__init__.py": b'__version__ = "1.0.7"\n',
                "CHANGELOG.md": b"## v1.0.7\n",
                "main.py": MAIN_TXT.replace('"1.0.8"', '"1.0.7"').encode(),
            }
        )
    )
    check(
        "（注：事故当时四个落点**互相一致**（都 v1.0.7），所以只比版本标签"
        "拦不住 → 必须靠投影对拍）",
        ok is True,
        why,
    )

# ---------------------------------------------------------------------------
# 9. 远端版本闸门：不得往回推
# ---------------------------------------------------------------------------
R_OLD = {k: (1, 0, 7) for k in S.VERSION_SOURCES}
R_NEW = {k: (1, 0, 8) for k in S.VERSION_SOURCES}
ok, lines = S.remote_version_gate((1, 0, 8), R_OLD)
check("stage 版本高于远端 → 放行", ok is True, str(lines))
ok, lines = S.remote_version_gate((1, 0, 7), R_OLD)
check("同版本重推（标签/内容对齐修复）→ 放行", ok is True, str(lines))
ok, lines = S.remote_version_gate((1, 0, 7), R_NEW)
check("★ stage 版本低于远端 → 拒绝", ok is False and "低于远端" in lines[0], str(lines))
ok, lines = S.remote_version_gate(None, R_NEW)
check("★ 读不出 stage 版本 → 拒绝", ok is False, str(lines))
ok, lines = S.remote_version_gate((1, 0, 8), {k: None for k in S.VERSION_SOURCES})
check("远端没有版本落点（首次同步）→ 放行且注明跳过", ok is True and "跳过" in lines[0], str(lines))

# ---------------------------------------------------------------------------
# 10. 真实仓库侧写：HEAD 的开源投影必须还原出基线文件数
# ---------------------------------------------------------------------------
if PKG.is_file() and (ROOT / ".git").exists():
    import package_release as P  # noqa: E402

    try:
        exp = S.expected_stage(ROOT)
        check(
            f"★ 真实仓库：HEAD 的开源投影 == 基线 {P.EXPECTED_OSS_STAGE_FILES} 个文件",
            len(exp) == P.EXPECTED_OSS_STAGE_FILES,
            f"投影 {len(exp)} vs 基线 {P.EXPECTED_OSS_STAGE_FILES}",
        )
        check(
            "投影里含 6 个变换产出（main.py/_conf_schema.json/README/metadata/"
            ".gitignore/.gitattributes）",
            S.TRANSFORM_FILES <= set(exp),
            str(sorted(S.TRANSFORM_FILES - set(exp))),
        )
        stage = S.SRC
        if stage.is_dir():
            probs = S.stage_projection_diff(stage, exp)
            extra = [p for p in probs if "多出" in p]
            check(
                "★ 真实仓库：stage 里没有未提交/未预期文件（危险方向）", not extra, str(extra[:3])
            )
            if probs:
                print(
                    f"[INFO] 当前 stage 相对 HEAD 有 {len(probs)} 处差异"
                    "（多为「提交后没重跑构建」的正常落后；"
                    "推送前 sync 闸门会强制要求一致）"
                )
    except RuntimeError as e:
        print(f"[SKIP] 无法重算投影：{e}")
else:
    print("[SKIP] 不是私有仓库副本（缺 dist/ 或 .git）")

# ---------------------------------------------------------------------------
# 11. 市场打包可复现（2026-09-26）：zip 条目时间戳固定 → 同内容 ⇒ 同 sha
#     （此前取文件 mtime，内容没变、重建却换 sha，上传期两次被迫冻结重建）
# ---------------------------------------------------------------------------
MK = ROOT / "scripts" / "make_market_zip.py"
if MK.is_file():
    import os  # noqa: E402

    sys.path.insert(0, str(ROOT / "scripts"))
    import make_market_zip as M  # noqa: E402

    base = M._zip_datetime()
    check(
        "★ 市场打包：条目时间戳固定且合法（≥1980）",
        base == M._zip_datetime() and base[0] >= 1980,
        str(base),
    )
    check(
        "  固定值 = 常量默认（不是当前时间）",
        base == time.gmtime(M.ZIP_EPOCH_DEFAULT)[:6],
        f"{base} vs {time.gmtime(M.ZIP_EPOCH_DEFAULT)[:6]}",
    )
    os.environ["SOURCE_DATE_EPOCH"] = "1700000000"
    check(
        "  SOURCE_DATE_EPOCH 可覆盖（reproducible-builds 惯例）",
        M._zip_datetime()[0] == 2023,
        str(M._zip_datetime()),
    )
    os.environ["SOURCE_DATE_EPOCH"] = "100"
    check(
        "  早于 zip 下限的值抬到 1980（避免打包报错）",
        M._zip_datetime()[0] == 1980,
        str(M._zip_datetime()),
    )
    os.environ["SOURCE_DATE_EPOCH"] = "not-a-number"
    check("  非法值回落默认（不抛异常）", M._zip_datetime() == base, str(M._zip_datetime()))
    del os.environ["SOURCE_DATE_EPOCH"]
    msrc = MK.read_text(encoding="utf-8")
    check(
        "★ 写 zip 走 ZipInfo + date_time（不再 z.write 取 mtime）",
        "zipfile.ZipInfo(" in msrc and "date_time=dt" in msrc and "z.write(f" not in msrc,
    )
    check("  权限位固定（不受 umask 影响）", "external_attr" in msrc)
else:
    print("[SKIP] 没有 scripts/make_market_zip.py")

# ---------------------------------------------------------------------------
# 12. 硬规矩：远端树必须**按 commit sha 取**（分支引用有缓存，2026-09-26 实测）
# ---------------------------------------------------------------------------
ssrc = SYNC.read_text(encoding="utf-8")
check(
    "★ 闸门：不再按分支名读 tree（/trees/{BRANCH}）", "/git/trees/{BRANCH}?recursive=1" not in ssrc
)
check(
    "★ 闸门：先取 ref→commit sha→tree sha 再读 tree",
    "/git/commits/{head}" in ssrc
    and 'commit["tree"]["sha"]' in ssrc
    and "/git/trees/{tree_sha}?recursive=1" in ssrc,
)
check(
    "★ 闸门：检查 truncated（截断的树会让差异清单失真，宁拒不假）",
    '"truncated"' in ssrc and "截断" in ssrc,
)
psrc = (
    ROOT
    / ".zcode"
    / "skills"
    / "astrbot-server-triage"
    / "scripts"
    / "automations"
    / "publish_community_snapshot.py"
)
if psrc.is_file():
    ptxt = psrc.read_text(encoding="utf-8")
    check(
        "★ 发布器：同样按 sha 读 tree + truncated 检查",
        "/git/trees/{BRANCH}?recursive=1" not in ptxt
        and 'commit["tree"]["sha"]' in ptxt
        and "truncated" in ptxt,
    )

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 开源同步闸门守卫全部通过")

# ---------------------------------------------------------------------------
# 种子新鲜度硬闸门（2026-09-29 v1.1.2 追加批）：双向覆盖 + 镜像判据一致性
# ---------------------------------------------------------------------------
# 判据与运行期同源：效价 = epoch+floor((now-epoch)/period)*period 窗口；
# 言录使 = expiry <= now。过期必须拦（无 bypass），新鲜必须放行。
import json as _json  # noqa: E402
from datetime import datetime as _dt, timezone as _tz  # noqa: E402
import importlib.util as _ilu  # noqa: E402

_SPEC = _ilu.spec_from_file_location("package_release", ROOT / "dist" / "package_release.py")
_PR = _ilu.module_from_spec(_SPEC) if _SPEC and _SPEC.loader else None
if _PR is not None:
    _SPEC.loader.exec_module(_PR)  # 只加载模块，不跑 main()
    _NOW = _dt(2026, 9, 29, 12, 0, tzinfo=_tz.utc)
    _SEED = {
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
    _FRESH_ROT = _json.dumps(_SEED)
    _STALE_ROT = _json.dumps(
        {
            **_SEED,
            "tenet": {
                **_SEED["tenet"],
                "valence_snapshot": "2026-09-20T01:00:00+00:00",
                "valence_source": "fixture",
            },
        }
    )
    _ACR_SEED = {
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
    _FRESH_ACR = _json.dumps(_ACR_SEED)
    _STALE_ACR = _json.dumps({**_ACR_SEED, "expiry": None})

    with tempfile.TemporaryDirectory() as _td:
        _T = Path(_td)

        def _w(name, text):
            p = _T / name
            p.write_text(text, encoding="utf-8")
            return p

        _ok = _PR.seed_freshness_problems(
            now=_NOW,
            rot_path=_w("r_fresh.json", _FRESH_ROT),
            acr_path=_w("a_fresh.json", _FRESH_ACR),
        )
        _bad = _PR.seed_freshness_problems(
            now=_NOW,
            rot_path=_w("r_stale.json", _STALE_ROT),
            acr_path=_w("a_stale.json", _STALE_ACR),
        )
        check("★ 种子闸门：新鲜放行（零问题）", _ok == [], str(_ok))
        check(
            "★ 种子闸门：动态残留与言录使过期均拦截",
            len(_bad) >= 3 and any("效价" in b for b in _bad) and any("言录使" in b for b in _bad),
            str(_bad),
        )
        # 边界：快照恰落窗口起 → 不算过期（< 严格小于）
        _edge = _PR.seed_freshness_problems(
            now=_NOW,
            rot_path=_w(
                "r_edge.json",
                _FRESH_ROT.replace("2026-09-29T00:06:05+00:00", "2026-09-29T00:00:00+00:00"),
            ),
            acr_path=_w("a_fresh2.json", _FRESH_ACR),
        )
        check("★ 种子闸门：骨架不依赖快照时刻", _edge == [], str(_edge))

    # 镜像判据（scripts/make_market_zip 的回退实现）与实际一致
    _SPEC2 = _ilu.spec_from_file_location(
        "make_market_zip", ROOT / "scripts" / "make_market_zip.py"
    )
    _MMZ = _ilu.module_from_spec(_SPEC2) if _SPEC2 and _SPEC2.loader else None
    if _MMZ is not None:
        _SPEC2.loader.exec_module(_MMZ)
        with tempfile.TemporaryDirectory() as _td2:
            _T2 = Path(_td2)
            _r1 = _T2 / "r.json"
            _r1.write_text(_FRESH_ROT, encoding="utf-8")
            _a1 = _T2 / "a.json"
            _a1.write_text(_FRESH_ACR, encoding="utf-8")
            _r2 = _T2 / "r2.json"
            _r2.write_text(_STALE_ROT, encoding="utf-8")
            _a2 = _T2 / "a2.json"
            _a2.write_text(_STALE_ACR, encoding="utf-8")
            _mf = _MMZ._seed_problems_fallback(now=_NOW, rot_path=_r1, acr_path=_a1)
            _mb = _MMZ._seed_problems_fallback(now=_NOW, rot_path=_r2, acr_path=_a2)
            check(
                "★ 种子闸门：镜像判据与真源一致（fresh/stale 两向）",
                _mf == _ok and len(_mb) == len(_bad),
                f"{_mf!r} / {_mb!r}",
            )
        # 每个拒绝原因分别走包装与独立市场入口，避免只测组合故障。
        for _case in (
            "bonus",
            "snapshot",
            "missing_items",
            "bad_epoch",
            "bad_json",
            "acrichis_expired",
        ):
            _value = _json.loads(_FRESH_ROT)
            if _case == "bonus":
                _value["tenet"]["items"][0]["bonus"] = 40
            elif _case == "snapshot":
                _value["coda"]["valence_snapshot"] = "2026-09-29T00:00:00+00:00"
            elif _case == "missing_items":
                del _value["tenet"]["items"]
            elif _case == "bad_epoch":
                _value["coda"]["epoch"] = "broken"
            with tempfile.TemporaryDirectory() as _case_dir:
                _cr = Path(_case_dir) / "rot.json"
                _ca = Path(_case_dir) / "acr.json"
                _cr.write_text(
                    "{bad" if _case == "bad_json" else _json.dumps(_value), encoding="utf-8"
                )
                _ca.write_text(
                    _STALE_ACR if _case == "acrichis_expired" else _FRESH_ACR, encoding="utf-8"
                )
                _pp = _PR.seed_freshness_problems(_NOW, _cr, _ca)
                _mp = _MMZ._seed_problems_fallback(_NOW, _cr, _ca)
                check(f"包装与独立市场拒绝{_case}", bool(_pp) and bool(_mp), f"{_pp} / {_mp}")
        for _acr_case in (
            "items",
            "expiry",
            "observed",
            "source",
            "catalog",
            "price",
            "name",
            "rule",
            "notes",
        ):
            _acr_bad = _json.loads(_FRESH_ACR)
            _first = next(iter(_acr_bad["_en_catalog"]))
            if _acr_case in ("items", "expiry", "observed", "source"):
                _acr_bad[_acr_case] = None
            elif _acr_case == "catalog":
                del _acr_bad["_en_catalog"][_first]
            elif _acr_case == "price":
                _acr_bad["_en_catalog"][_first]["price"] = 0
            elif _acr_case == "name":
                _acr_bad["_en_catalog"][_first]["name"] = ""
            elif _acr_case == "rule":
                _acr_bad["_reset_rule"] = "每天"
            else:
                _acr_bad["_zh_official_notes"] = {}
            with tempfile.TemporaryDirectory() as _td:
                _rp, _ap = Path(_td) / "rot.json", Path(_td) / "acr.json"
                _rp.write_text(_FRESH_ROT, encoding="utf-8")
                _ap.write_text(_json.dumps(_acr_bad), encoding="utf-8")
                _pp = _PR.seed_freshness_problems(_NOW, _rp, _ap)
                _mp = _MMZ._seed_problems_fallback(_NOW, _rp, _ap)
                check(
                    f"言录使静态{_acr_case}两打包入口同源拒绝", bool(_pp) and _pp == _mp, str(_pp)
                )
else:
    check("种子闸门：dist/ 不在场时跳过（开源包环境）", True)

# ★ 2026-10-15：剥离块接缝空行（开源件 main.py 曾因此过不了 ruff format --check）—— 双向验证
if _PR is not None:
    _cls = "class A:\n    x = 1\n\n    # 私有起\n    y = 2\n    # 私有止\n\n    z = 3\n"
    _got = _PR._drop_block(_cls, "# 私有起", "# 私有止", "t")
    check(
        "剥离接缝（缩进体内）：两空行合成一个",
        _got == "class A:\n    x = 1\n\n    z = 3\n",
        repr(_got),
    )
    _mod = "import a\n\n# 私有起\nimport b\n# 私有止\n\n\ndef f():\n    pass\n"
    _got = _PR._drop_block(_mod, "# 私有起", "# 私有止", "t")
    check(
        "剥离接缝（模块级）：顶格定义前的空行不动",
        _got == "import a\n\n\n\ndef f():\n    pass\n",
        repr(_got),
    )
    _one = "class A:\n    x = 1\n\n    import_private = 1\n\n    z = 3\n"
    _got = _PR._drop_block(_one, "import_private", "import_private", "t")
    check("剥离接缝（单行块）：同样合并", _got == "class A:\n    x = 1\n\n    z = 3\n", repr(_got))


# 限仓App认证真实入口：假helper子进程 + 内存HTTPS协议处理器，不调用真实凭据/网络。
def _app_auth_checks():
    import contextlib
    import importlib.util
    import io
    import json
    import os
    import subprocess
    from email.message import Message
    from unittest.mock import patch
    from urllib.response import addinfourl

    import github_app_auth as A

    public = "skyti1437/Warframe-SDJK-Bot"
    private = "skyti1437/bot"
    secret = "ghs_TEST_ONLY_" + "opaque-JWT_" * 65 + ".segment.signature"
    now = [2_000_000_000]
    check(
        "认证固定两仓与专用helper映射",
        A.HELPERS
        == {
            private: "~/.local/bin/git-credential-wf-github-app",
            public: "~/.local/bin/git-credential-wf-release-app",
        },
    )
    real_builder = A.urllib.request.build_opener

    with tempfile.TemporaryDirectory(prefix="wf-api-auth-") as folder:
        base = Path(folder)
        log = base / "helper-calls.jsonl"
        output = base / "helper-output.txt"
        error_output = base / "helper-stderr.txt"
        exit_code = base / "helper-exit.txt"
        output.write_text(
            f"username=x-access-token\npassword={secret}\npassword_expiry_utc={now[0] + 3600}\n\n"
        )
        error_output.write_text("")
        exit_code.write_text("0")
        helper = base / "fixture-helper"
        helper.write_text(
            "#!"
            + sys.executable
            + "\nimport sys,json\nfrom pathlib import Path\n"
            + "context=sys.stdin.read(20000)\n"
            + "with Path("
            + repr(str(log))
            + ").open('a') as file: file.write(json.dumps({'argv':sys.argv[1:],'context':context})+'\\n')\n"
            + "sys.stdout.write(Path("
            + repr(str(output))
            + ").read_text())\n"
            + "sys.stderr.write(Path("
            + repr(str(error_output))
            + ").read_text())\n"
            + "raise SystemExit(int(Path("
            + repr(str(exit_code))
            + ").read_text()))\n"
        )
        helper.chmod(0o700)
        http_calls = []
        response_mode = ["ok"]

        class FixtureHTTPS(A.urllib.request.HTTPSHandler):
            def https_open(self, req):
                http_calls.append(
                    (
                        req.get_method(),
                        req.full_url,
                        req.get_header("Authorization") == "Bearer " + secret,
                    )
                )
                if response_mode[0] == "exception":
                    raise RuntimeError(secret)
                headers = Message()
                code, body = 200, {"ok": True}
                if response_mode[0] == "redirect":
                    headers["Location"] = "https://elsewhere.invalid/" + secret
                    code = 302
                elif response_mode[0] == "error":
                    code, body = 403, {"message": secret, "authorization": secret}
                elif response_mode[0] == "echo":
                    body = {"echo": secret, secret: {"token": secret}}
                response = addinfourl(
                    io.BytesIO(json.dumps(body).encode()), headers, req.full_url, code
                )
                response.msg = "fixture"
                return response

        def build(*handlers):
            return real_builder(
                *[
                    FixtureHTTPS()
                    if isinstance(handler, A.urllib.request.HTTPSHandler)
                    else handler
                    for handler in handlers
                ]
            )

        def helper_calls():
            return (
                [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            )

        with (
            patch.dict(A.HELPERS, {public: str(helper), private: str(helper)}, clear=True),
            patch.object(A.time, "time", side_effect=lambda: now[0]),
            patch.object(A.urllib.request, "build_opener", side_effect=build),
            patch.object(S, "_AUTH", A.RepositoryAPI(public)),
        ):
            result = S.api("GET", f"/repos/{public}/git/ref/heads/main", tries=1)
            check(
                "公开同步真实API入口使用专用helper且成功",
                result == (200, {"ok": True})
                and helper_calls()[-1]
                == {
                    "argv": ["get"],
                    "context": f"protocol=https\nhost=github.com\npath={public}.git\n\n",
                }
                and http_calls[-1][2],
            )
            S.api("GET", f"/repos/{public}/git/refs/heads/bot-data", tries=1)
            check(
                "公开仓bot-data分支兼容且未过期令牌复用",
                len(helper_calls()) == 1 and http_calls[-1][1].endswith("/heads/bot-data"),
            )
            now[0] += 3550
            output.write_text(
                f"username=x-access-token\npassword={secret}\npassword_expiry_utc={now[0] + 3600}\n\n"
            )
            S.api("GET", f"/repos/{public}/git/ref/heads/main", tries=1)
            check("带expiry的令牌提前60秒重新签发", len(helper_calls()) == 2)

            count = len(helper_calls())
            for bad in (
                f"/repos/{private}/git/ref/heads/main",
                f"/repos/{public}-other",
                "https://api.github.com/repos/skyti1437/Warframe-SDJK-Bot",
                "https://elsewhere.invalid/x",
                f"/repos/{public}/../bot",
                f"/repos/{public}/%2e%2e/bot",
                f"/repos/{public}/\\..\\bot",
                f"/repos/{public}#x",
            ):
                result = S.api("GET", bad, tries=1)
                check(
                    "跨仓/绝对URL/路径穿越在取凭据前拒绝",
                    result[0] == 0 and len(helper_calls()) == count,
                )
            try:
                A.RepositoryAPI("skyti1437/other")
                check("未知仓库拒绝", False)
            except A.AppAuthError:
                check("未知仓库拒绝", True)

            # 私有API备用通道导入无副作用；实际API调用才取bot helper。
            spec = importlib.util.spec_from_file_location(
                "isolated_private_api", ROOT / "dist/api_push_private.py"
            )
            module = importlib.util.module_from_spec(spec)
            with (
                patch.object(A.subprocess, "run", side_effect=AssertionError("导入不得调用helper")),
                patch.object(sys, "argv", ["isolated-private"]),
            ):
                spec.loader.exec_module(module)
            check(
                "私有API导入离线无副作用且ROOT按文件定位",
                module.ROOT == ROOT and module.DRY is True and module._AUTH._cached is None,
            )
            output.write_text(f"username=x-access-token\npassword={secret}\n\n")
            count = len(helper_calls())
            first = module.api("GET", f"/repos/{private}/git/ref/heads/main")
            second = module.api("GET", f"/repos/{private}/git/ref/heads/main")
            check(
                "旧bot helper没有expiry时每次API均获取新令牌",
                first == second == {"ok": True}
                and len(helper_calls()) == count + 2
                and all(f"path={private}.git\n" in row["context"] for row in helper_calls()[-2:]),
            )
            try:
                module.api("GET", f"/repos/{public}/git/ref/heads/main")
                check("私有API备用入口拒绝公开仓作用域", False)
            except SystemExit as exc:
                check("私有API备用入口拒绝公开仓作用域且不泄密", secret not in str(exc))

            bad_outputs = [
                "quit=true\n\n",
                f"username=x-access-token\npassword={secret}\npassword=duplicate\n\n",
                "username=x-access-token\npassword=\n\n",
                "username=x-access-token\npassword=with space\n\n",
                "username=x-access-token\npassword=with\x00control\n\n",
                "username=x-access-token\npassword=with\rcontrol\n\n",
                f"username=other\npassword={secret}\n\n",
                f"username=x-access-token\npassword={secret}\npassword_expiry_utc=0\n\n",
                f"username=x-access-token\npassword={secret}\npassword_expiry_utc={now[0] + 30}\n\n",
                f"username=x-access-token\npassword={secret}\n\nextra=value\n",
                "username=x-access-token\npassword=" + "x" * (A.TOKEN_LIMIT + 1) + "\n\n",
                "x" * (A.HELPER_OUTPUT_LIMIT + 1),
            ]
            for raw in bad_outputs:
                output.write_text(raw)
                capture_out, capture_err = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(capture_out),
                    contextlib.redirect_stderr(capture_err),
                ):
                    result = A.RepositoryAPI(public).api("GET", f"/repos/{public}", tries=1)
                check(
                    "坏helper协议失败关闭且不泄露原文",
                    result[0] == 0
                    and secret not in repr(result)
                    and not capture_out.getvalue()
                    and not capture_err.getvalue(),
                )
            output.write_text(f"username=x-access-token\npassword={secret}\n\n")
            error_output.write_text(secret)
            exit_code.write_text("1")
            result = A.RepositoryAPI(public).api("GET", f"/repos/{public}", tries=1)
            check("helper异常stdout/stderr不得泄露", result[0] == 0 and secret not in repr(result))
            error_output.write_text("")
            exit_code.write_text("0")
            with patch.dict(A.HELPERS, {public: str(base / "absent-helper")}):
                result = A.RepositoryAPI(public).api("GET", f"/repos/{public}", tries=1)
                check("缺helper不借用其它认证", result[0] == 0 and secret not in repr(result))
            for mode in ("redirect", "exception", "error", "echo"):
                response_mode[0] = mode
                http_calls.clear()
                result = A.RepositoryAPI(public).api("GET", f"/repos/{public}", tries=1)
                check(
                    "HTTP重定向/异常/回显均不泄露认证",
                    secret not in repr(result)
                    and all(url.startswith("https://api.github.com/") for _, url, _ in http_calls)
                    and len(http_calls) == 1
                    and (result[0] == 200 if mode == "echo" else result[0] in {0, 403}),
                )

    # 旧askpass无论提示内容均不能提供认证。
    for prompt in ("Username", "Password", "arbitrary"):
        result = subprocess.run(
            ["/bin/sh", str(ROOT / "dist/git_askpass.sh"), prompt],
            capture_output=True,
            text=True,
            check=False,
        )
        check(
            "旧askpass恒失败且stdout为空",
            result.returncode != 0 and result.stdout == "" and "helper" in result.stderr,
        )

    # wrapper只在临时树跑假构建/同步脚本，不执行真实发布器。
    with tempfile.TemporaryDirectory(prefix="wf-publish-wrapper-") as folder:
        base = Path(folder)
        (base / "dist").mkdir()
        wrapper = base / "dist/publish_opensource.sh"
        wrapper.write_text((ROOT / "dist/publish_opensource.sh").read_text())
        log = base / "calls.jsonl"
        fake = (
            "import json,sys,os\nfrom pathlib import Path\n"
            + "with Path("
            + repr(str(log))
            + ").open('a') as file: file.write(json.dumps({'script':Path(__file__).name,'args':sys.argv[1:],'py':sys.executable})+'\\n')\n"
            + "raise SystemExit(int(os.environ.get('FIXTURE_BUILD_EXIT','0')) if Path(__file__).name=='package_release.py' else 0)\n"
        )
        for name in ("package_release.py", "sync_opensource.py"):
            (base / "dist" / name).write_text(fake)
        bin_dir = base / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3.12").symlink_to(sys.executable)
        invalid = base / "invalid-python"
        invalid.write_text("#!/bin/sh\nexit 1\n")
        invalid.chmod(0o700)
        env = {
            "HOME": str(base),
            "PATH": str(bin_dir) + ":/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        def wrapper_run(args=(), override=None, build_exit=0):
            if log.exists():
                log.unlink()
            current = {**env, "FIXTURE_BUILD_EXIT": str(build_exit)}
            if override is not None:
                current["WARFRAME_TEST_PY"] = override
            result = subprocess.run(
                ["/bin/sh", str(wrapper), *args],
                env=current,
                capture_output=True,
                text=True,
                check=False,
            )
            calls = (
                [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            )
            return result, calls

        result, calls = wrapper_run(override=sys.executable)
        check(
            "发行wrapper显式合格解释器且默认仅构建不发布",
            result.returncode == 0 and [row["script"] for row in calls] == ["package_release.py"],
        )
        result, calls = wrapper_run(("--dry-run", "说明"), override=sys.executable)
        check(
            "发行wrapper --dry-run不添加发布授权",
            result.returncode == 0
            and [row["script"] for row in calls] == ["package_release.py", "sync_opensource.py"]
            and all("--yes" not in row["args"] for row in calls),
        )
        result, calls = wrapper_run(("--push", "说明"), override=sys.executable)
        check(
            "发行wrapper只有显式--push传--yes",
            result.returncode == 0 and "--yes" in calls[-1]["args"],
        )
        result, calls = wrapper_run(("--push",), override=str(invalid))
        check(
            "发行wrapper拒绝显式错误解释器且不回落",
            result.returncode != 0 and not calls and "3.10" in result.stderr,
        )
        result, calls = wrapper_run(("--push",), override=sys.executable, build_exit=2)
        check(
            "构建失败不被管道掩盖且不运行同步",
            result.returncode == 2 and [row["script"] for row in calls] == ["package_release.py"],
        )
        result, calls = wrapper_run()
        check(
            "无显式解释器且无venv时选择合格PATH解释器", result.returncode == 0 and len(calls) == 1
        )
        (base / ".venv/bin").mkdir(parents=True)
        (base / ".venv/bin/python").symlink_to(sys.executable)
        result, calls = wrapper_run()
        check(
            "无显式解释器优先项目venv",
            result.returncode == 0 and "/.venv/bin/python" in calls[0]["py"],
        )
        result, calls = wrapper_run(override=str(invalid))
        check("即使有venv也不绕过错误的显式解释器", result.returncode != 0 and not calls)

    baseline = ROOT / ".zcode/skills/astrbot-warframe-plugin-dev/scripts/refresh_baseline.py"
    if baseline.is_file():
        spec = importlib.util.spec_from_file_location("isolated_baseline", baseline)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {}, clear=True):
            spec.loader.exec_module(module)
        import socket
        import urllib.request

        original = {
            name: (ROOT / "core/data" / name).read_bytes()
            for name in ("rotations.json", "de/acrichis_week.json")
        }
        source_before = {
            name: (
                (ROOT / "core/data" / name).read_bytes(),
                (ROOT / "core/data" / name).stat().st_mtime_ns,
            )
            for name in original
        }
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "core/data/de").mkdir(parents=True)
            cases = (
                ("合法无值种子", None, 0),
                ("效价动态残留", ("rotations.json", lambda d: d["tenet"].update(bonus=1)), 1),
                ("效价缺骨架", ("rotations.json", lambda d: d.pop("tenet")), 1),
                ("言录使动态残留", ("de/acrichis_week.json", lambda d: d.update(items=[])), 1),
                ("言录使缺目录", ("de/acrichis_week.json", lambda d: d.pop("_en_catalog")), 1),
                ("损坏JSON", "broken", 1),
                ("缺失文件", "missing", 1),
            )
            for label, mutation, expected in cases:
                for name, content in original.items():
                    (base / "core/data" / name).write_bytes(content)
                if isinstance(mutation, tuple):
                    name, mutate = mutation
                    path = base / "core/data" / name
                    value = json.loads(path.read_text())
                    mutate(value)
                    path.write_text(json.dumps(value), encoding="utf-8")
                elif mutation == "broken":
                    (base / "core/data/rotations.json").write_bytes(b"{")
                elif mutation == "missing":
                    (base / "core/data/rotations.json").unlink()
                before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in base.rglob("*.json")}
                with (
                    patch.object(module, "REPO", base),
                    patch("subprocess.run", side_effect=AssertionError("禁止子进程")) as child,
                    patch.object(
                        urllib.request, "urlopen", side_effect=AssertionError("禁止网络")
                    ) as net,
                    patch.object(
                        socket, "socket", side_effect=AssertionError("禁止socket")
                    ) as sock,
                ):
                    result = module.main()
                check("静态种子入口：" + label, result == expected)
                check(
                    "静态种子入口不联网或启动子进程：" + label,
                    child.call_count == net.call_count == sock.call_count == 0,
                )
                check(
                    "静态种子入口字节和mtime不变：" + label,
                    before
                    == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in base.rglob("*.json")},
                )
            check(
                "静态种子入口不改真实种子",
                source_before
                == {
                    name: (
                        (ROOT / "core/data" / name).read_bytes(),
                        (ROOT / "core/data" / name).stat().st_mtime_ns,
                    )
                    for name in original
                },
            )
    else:
        print("[SKIP] 私有基线刷新脚本不在当前分发副本")


_app_auth_checks()

# 开发口吻漏网：日期在前/后带冒号，以及定向需求短语；正常玩家说明不误伤。
if _PR is not None:
    for text in (
        "2026-10-25 \u7528\u6237：内部约定",
        "2026-10-25\u7528\u6237: 内部约定",
        "26-10  \u7528\u6237：内部约定",
        "\u7528\u6237要「内部视图」",
    ):
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder) / "sample.py"
            fixture.write_text("# " + text, encoding="utf-8")
            check("开发口吻扫描拦截：" + text, bool(_PR.scan_sensitive(Path(folder))))
    for text in (
        "2026-10-25 维护者定：内部约定",
        "\u7528\u6237可以上传字体并选择正文",
        "该\u7528\u6237要先打开推送开关",
        "2026-10-25\n\u7528\u6237：玩家可见对话",
    ):
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder) / "sample.py"
            fixture.write_text("# " + text, encoding="utf-8")
            check("开发口吻扫描保留：" + text, not _PR.scan_sensitive(Path(folder)))

# 追加段自己的收尾（本文件原汇总在上面，须为追加检查再守一次退出码）
print()
if FAILED:
    print(f"✗ 种子闸门段 {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 种子新鲜度闸门守卫段全部通过")

# Official media references do not exempt a private host alias elsewhere in the same file.
if _PR is not None:
    for relative in (
        "core/messaging.py",
        "core/qq_media.py",
        "tests/test_messaging.py",
        "tests/test_qq_media.py",
    ):
        with tempfile.TemporaryDirectory() as folder:
            fixture = Path(folder) / relative
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text("ref = 'qq" + "bot-media://image/v1/safe'", encoding="utf-8")
            check(
                "official media protocol allowed: " + relative, not _PR.scan_sensitive(Path(folder))
            )
            with fixture.open("a", encoding="utf-8") as out:
                out.write("\nhost = 'qq" + "bot'")
            check(
                "private host still rejected: " + relative, bool(_PR.scan_sensitive(Path(folder)))
            )
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "sample.py").write_text(
            "ref = 'qq" + "bot-media://image/v1/safe'", encoding="utf-8"
        )
        check("media exemption is file-scoped", bool(_PR.scan_sensitive(Path(folder))))
    if FAILED:
        sys.exit(1)
