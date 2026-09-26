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
    ".gitattributes", ".github/workflows/gitleaks.yml", ".gitignore",
    ".gitleaks.toml", "CHANGELOG.md", "build_damage_data.py",
    "build_de_data.py", "build_stances.py",
]
ok, why = S.guard_mass_delete(ACCIDENT_GONE, 155, allow=False)
check("★ 事故复现：8 个删除项（含关键文件）被拒绝", ok is False, why)
check("  拒绝原因指出关键文件", "关键文件" in why, why)

# 逐个关键文件都要拦得住（单删一个也不许）
for name in (".gitignore", ".gitattributes", "README.md", "main.py",
             "metadata.yaml", "CHANGELOG.md", "LICENSE", "_conf_schema.json",
             ".github/workflows/gitleaks.yml", ".github/workflows/ci.yml"):
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
check("阈值常量存在且合理",
      S.GUARD_ABS == 2 and 0 < S.GUARD_RATIO < 0.05,
      f"ABS={S.GUARD_ABS} RATIO={S.GUARD_RATIO}")
check("命令行开关可被识别（不混进提交信息）",
      "--allow-mass-delete" not in S._resolve_msg(), S._resolve_msg())

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
check("零变化分支 return 0（不建提交）",
      bool(re.search(r"if not diff and not gone:\n(?:.*\n)*?\s+return 0", src)))
check("import 本模块不再读凭据（离线可单测）",
      S._TOKEN is None and not re.search(r"^TOKEN = token\(\)", src, re.M))
check("模块级可见 --allow-mass-delete 开关", S.ALLOW_MASS_DELETE is False)

# ★ 2026-09-25 新增：默认 dry-run + 本地/远端闸门都在写操作之前
i_lgate = src.index("local_gates(SRC")
i_rgate = src.index("remote_gate(stage_v")
i_dry = src.index("DRY-RUN")
check("★ 本地闸门在写操作之前执行", i_lgate < i_blob, f"{i_lgate} vs {i_blob}")
check("★ 远端版本闸门在写操作之前执行", i_rgate < i_blob, f"{i_rgate} vs {i_blob}")
check("★ dry-run 短路在写操作之前执行", i_dry < i_blob, f"{i_dry} vs {i_blob}")
check("★ 默认 dry-run：只有显式 --yes 才推",
      re.search(r'^PUSH = "--yes" in FLAGS$', src, re.M) is not None)
check("本进程未带 --yes → PUSH 为 False（默认不推）", S.PUSH is False)

# ★★ 2026-09-26：零变化短路必须排在闸门**之后**（stage 过期时会静默空转）
#   实测事故：二次修复提交后跑同步，远端 push.py blob ≠ 本地，脚本却说「需更新 0」——
#   因为「零变化」是拿**磁盘 stage 目录**（构建产物，可能过期）跟远端比的。
# 锚点用**代码行**（注释里也出现了「无需提交」字样，别被注释骗到）
i_zero = src.index("if not diff and not gone:")
check("★ 零变化短路在 local_gates 之后（stage 过期不得静默空转）",
      i_lgate < i_zero, f"local_gates@{i_lgate} vs 短路@{i_zero}")
check("★ 零变化短路在闸门判定 `if not ok` 之后",
      src.index("if not ok:") < i_nomsg,
      f"{src.index('if not ok:')} vs {i_nomsg}")
check("★ 零变化文案点明「stage == HEAD 开源投影」前提",
      "stage == HEAD 开源投影 ⇒ 无需提交" in src)

# ---------------------------------------------------------------------------
# 5. 根因侧：构建脚本不得再「先清空再复制」+ .gitattributes 行尾要归一
# ---------------------------------------------------------------------------
if PKG.is_file():
    psrc = PKG.read_text(encoding="utf-8")
    i_cc = psrc.index("def copy_clean")
    i_next = psrc.index("\ndef ", i_cc + 10)
    body = psrc[i_cc:i_next]
    check("★ copy_clean 先构建到 .new（不再先清空原目录）",
          ".new" in body and body.index("fresh.mkdir") < body.index("STAGE.iterdir()"))
    check("★ copy_clean 仍保留 .git（开源目录是 git 仓库）",
          'child.name == ".git"' in body)
    check("copy_clean 复制目标写成 fresh（不是 STAGE）",
          "dst = fresh / rel" in body)
    check("替换完成后清理 .new 残留", "shutil.rmtree(fresh" in body)
    # ★ 2026-09-25：与「只比版本标签拦不住半成品态」同源的坑——stage 的
    #   .gitattributes 曾是 CRLF（write_text 的 Windows 行为 + normalize_eol
    #   把它漏了），于是本地 git 看它是 LF、公开仓里是 CRLF，blob 永远对不上。
    i_ne = psrc.index("def normalize_eol")
    i_ne_end = psrc.index("\ndef ", i_ne + 10)
    ne_body = psrc[i_ne:i_ne_end]
    check("★ normalize_eol 覆盖 dotfile（.gitattributes 不再漏）",
          'f.name.startswith(".")' in ne_body, ne_body[:120])
    check("★ normalize_eol 不碰 stage 自己的 .git/",
          '".git" in f.relative_to(STAGE).parts' in ne_body)
    check("normalize_eol 有二进制兜底（按内容判 NUL）",
          'b"\\x00" in raw' in ne_body)
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
        return [c for c in self.calls
                if c[0] in ("POST", "PATCH", "PUT", "DELETE")]


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
    fake = _FakeAPI([{"path": p, "type": "blob", "sha": "deadbeef"}
                     for p in REMOTE_155])
    rc = _run_main(fake, d)
    check("★ 端到端：残缺目录 → main() 返回 1（拒绝同步）", rc == 1, f"rc={rc}")
    check("★ 端到端：拒绝时没有任何写请求（远端毫发无损）",
          not fake.writes, str(fake.writes))
    check("  但仍读了远端 ref/tree（先比较再决定）",
          any("git/ref/heads" in p for _, p in fake.calls))

# 6b. 零变化 → 返回 0 且不建提交
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    names = ["core/f1.py", "core/f2.py"]
    _mk(d, names)
    fake = _FakeAPI([{"path": n, "type": "blob", "sha": S.blob_sha(b"x")}
                     for n in names])
    rc = _run_main(fake, d)
    check("★ 端到端：内容一致 → main() 返回 0", rc == 0, f"rc={rc}")
    check("★ 端到端：零变化不产生任何写请求（不再留空提交）",
          not fake.writes, str(fake.writes))

# 6c. 正常更新（1 个新文件、0 删除）+ 显式 --yes → 放行并真的推送
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    _mk(d, ["core/f1.py", "core/f2.py", "core/new.py"])
    fake = _FakeAPI([{"path": n, "type": "blob", "sha": S.blob_sha(b"x")}
                     for n in ("core/f1.py", "core/f2.py")])
    rc = _run_main(fake, d, push=True)
    check("★ 端到端：正常新增文件 + --yes → 放行（闸门不误伤正常同步）",
          rc == 0, f"rc={rc}")
    check("  确实发起了写请求（上传 blob + 更新 ref）",
          any(p.endswith("/git/blobs") for _, p in fake.writes)
          and any("/git/refs/heads/" in p for _, p in fake.writes),
          str(fake.writes))

# 6d. 同样的更新，但**不带 --yes**（默认 dry-run）→ 0 写请求
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    _mk(d, ["core/f1.py", "core/f2.py", "core/new.py"])
    fake = _FakeAPI([{"path": n, "type": "blob", "sha": S.blob_sha(b"x")}
                     for n in ("core/f1.py", "core/f2.py")])
    rc = _run_main(fake, d, push=False)
    check("★ 端到端：默认 dry-run → 返回 0 且零写请求", rc == 0, f"rc={rc}")
    check("★ 端到端：dry-run 不发任何 POST/PATCH（远端毫发无损）",
          not fake.writes, str(fake.writes))

# 6e. 本地目录不存在 → 立即拒绝，连读请求都不发
fake = _FakeAPI([])
rc = _run_main(fake, Path(tempfile.gettempdir()) / "definitely_not_here_9527")
check("★ 端到端：目录不存在 → 返回 1 且不发任何请求",
      rc == 1 and not fake.calls, f"rc={rc} calls={fake.calls}")

# ---------------------------------------------------------------------------
# 7. 版本落点：按落点语义解析（不是无脑取第一个数字）
# ---------------------------------------------------------------------------
check("README 首行取版本",
      S.version_of_file("README.md",
                        "# AstrBot 插件 · Warframe SDJKBOT v1.0.8（`x`）\n")
      == (1, 0, 8))
check("metadata.yaml 取 version 行",
      S.version_of_file("metadata.yaml", "name: x\nversion: v1.0.8\nauthor: y\n")
      == (1, 0, 8))
check("core/__init__.py 取 __version__",
      S.version_of_file("core/__init__.py", '__version__ = "1.0.8"\n') == (1, 0, 8))
MAIN_TXT = ('@register("astrbot_plugin_warframe", "skyti1437",\n'
            '          f"{BRAND}：世界状态 / 市场查价 / 蹲点推送",\n'
            '          "1.0.8")\n')
check("main.py @register 第 4 参（跨行）取版本",
      S.version_of_file("main.py", MAIN_TXT) == (1, 0, 8))
check("CHANGELOG 首条小节取版本",
      S.version_of_file("CHANGELOG.md",
                        "# 更新日志\n\n说明…\n\n## v1.0.8\n- x\n\n## v1.0.7\n- y\n")
      == (1, 0, 8))
check("解析不到时返回 None",
      S.version_of_file("metadata.yaml", "name: x\n") is None)

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
    check("投影一致 → 无问题",
          S.stage_projection_diff(d, {"main.py": b"V1.0.8\n"}) == [])

    probs = S.stage_projection_diff(
        d, {"main.py": b"V1.0.8\n", "core/new_feature.py": b"new\n"})
    check("★ 半成品态①：HEAD 有而 stage 没有（没重跑构建）→ 报缺失",
          any("缺少应有文件" in p for p in probs), str(probs))

    probs = S.stage_projection_diff(d, {"main.py": b"V1.0.7\n"})
    check("★ 半成品态②：stage 里是 HEAD 没有的内容（含未提交改动）→ 报不符",
          any("与 HEAD 不符" in p for p in probs), str(probs))

    _mk(d, ["core/uncommitted.py"], b"half\n")
    probs = S.stage_projection_diff(d, {"main.py": b"V1.0.8\n"})
    check("★ 半成品态③：stage 多出未提交文件 → 报多余",
          any("多出" in p for p in probs), str(probs))

# 真实的 09-25 事故形态：stage 的版本落点落后于 HEAD 的投影
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    _mk(d, ["main.py", "metadata.yaml"], b'@register("x", "y", "1.0.7")\n')
    expected = {"main.py": b'@register("x", "y", "1.0.8")\n',
                "metadata.yaml": b"version: v1.0.7\n"}
    probs = S.stage_projection_diff(d, expected)
    check("★ 事故形态复现：stage 落后于 HEAD 投影 → 报不符",
          any("main.py" in p and "不符" in p for p in probs), str(probs))
    ok, why = S.versions_agree(S.read_versions({
        "README.md": b"v1.0.7\n", "metadata.yaml": b"version: v1.0.7\n",
        "core/__init__.py": b'__version__ = "1.0.7"\n',
        "CHANGELOG.md": b"## v1.0.7\n",
        "main.py": MAIN_TXT.replace('"1.0.8"', '"1.0.7"').encode()}))
    check("（注：事故当时四个落点**互相一致**（都 v1.0.7），所以只比版本标签"
          "拦不住 → 必须靠投影对拍）", ok is True, why)

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
check("★ stage 版本低于远端 → 拒绝", ok is False and "低于远端" in lines[0],
      str(lines))
ok, lines = S.remote_version_gate(None, R_NEW)
check("★ 读不出 stage 版本 → 拒绝", ok is False, str(lines))
ok, lines = S.remote_version_gate((1, 0, 8), {k: None for k in S.VERSION_SOURCES})
check("远端没有版本落点（首次同步）→ 放行且注明跳过",
      ok is True and "跳过" in lines[0], str(lines))

# ---------------------------------------------------------------------------
# 10. 真实仓库侧写：HEAD 的开源投影必须还原出基线文件数
# ---------------------------------------------------------------------------
if PKG.is_file() and (ROOT / ".git").exists():
    import package_release as P  # noqa: E402

    try:
        exp = S.expected_stage(ROOT)
        check("★ 真实仓库：HEAD 的开源投影 == 基线 "
              f"{P.EXPECTED_OSS_STAGE_FILES} 个文件",
              len(exp) == P.EXPECTED_OSS_STAGE_FILES,
              f"投影 {len(exp)} vs 基线 {P.EXPECTED_OSS_STAGE_FILES}")
        check("投影里含 6 个变换产出（main.py/_conf_schema.json/README/metadata/"
              ".gitignore/.gitattributes）",
              S.TRANSFORM_FILES <= set(exp),
              str(sorted(S.TRANSFORM_FILES - set(exp))))
        stage = S.SRC
        if stage.is_dir():
            probs = S.stage_projection_diff(stage, exp)
            extra = [p for p in probs if "多出" in p]
            check("★ 真实仓库：stage 里没有未提交/未预期文件（危险方向）",
                  not extra, str(extra[:3]))
            if probs:
                print(f"[INFO] 当前 stage 相对 HEAD 有 {len(probs)} 处差异"
                      "（多为「提交后没重跑构建」的正常落后；"
                      "推送前 sync 闸门会强制要求一致）")
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
    check("★ 市场打包：条目时间戳固定且合法（≥1980）",
          base == M._zip_datetime() and base[0] >= 1980, str(base))
    check("  固定值 = 常量默认（不是当前时间）", base == time.gmtime(M.ZIP_EPOCH_DEFAULT)[:6],
          f"{base} vs {time.gmtime(M.ZIP_EPOCH_DEFAULT)[:6]}")
    os.environ["SOURCE_DATE_EPOCH"] = "1700000000"
    check("  SOURCE_DATE_EPOCH 可覆盖（reproducible-builds 惯例）",
          M._zip_datetime()[0] == 2023, str(M._zip_datetime()))
    os.environ["SOURCE_DATE_EPOCH"] = "100"
    check("  早于 zip 下限的值抬到 1980（避免打包报错）",
          M._zip_datetime()[0] == 1980, str(M._zip_datetime()))
    os.environ["SOURCE_DATE_EPOCH"] = "not-a-number"
    check("  非法值回落默认（不抛异常）", M._zip_datetime() == base, str(M._zip_datetime()))
    del os.environ["SOURCE_DATE_EPOCH"]
    msrc = MK.read_text(encoding="utf-8")
    check("★ 写 zip 走 ZipInfo + date_time（不再 z.write 取 mtime）",
          "zipfile.ZipInfo(" in msrc and "date_time=dt" in msrc and "z.write(f" not in msrc)
    check("  权限位固定（不受 umask 影响）", "external_attr" in msrc)
else:
    print("[SKIP] 没有 scripts/make_market_zip.py")

# ---------------------------------------------------------------------------
# 12. 硬规矩：远端树必须**按 commit sha 取**（分支引用有缓存，2026-09-26 实测）
# ---------------------------------------------------------------------------
ssrc = SYNC.read_text(encoding="utf-8")
check("★ 闸门：不再按分支名读 tree（/trees/{BRANCH}）",
      '/git/trees/{BRANCH}?recursive=1' not in ssrc)
check("★ 闸门：先取 ref→commit sha→tree sha 再读 tree",
      '/git/commits/{head}' in ssrc and 'commit["tree"]["sha"]' in ssrc
      and '/git/trees/{tree_sha}?recursive=1' in ssrc)
check("★ 闸门：检查 truncated（截断的树会让差异清单失真，宁拒不假）",
      '"truncated"' in ssrc and "截断" in ssrc)
psrc = (ROOT / ".zcode" / "skills" / "astrbot-server-triage" / "scripts"
        / "automations" / "publish_community_snapshot.py")
if psrc.is_file():
    ptxt = psrc.read_text(encoding="utf-8")
    check("★ 发布器：同样按 sha 读 tree + truncated 检查",
          '/git/trees/{BRANCH}?recursive=1' not in ptxt
          and 'commit["tree"]["sha"]' in ptxt and "truncated" in ptxt)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 开源同步闸门守卫全部通过")
