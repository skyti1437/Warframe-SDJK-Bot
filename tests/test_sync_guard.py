# -*- coding: utf-8 -*-
"""开源同步脚本的两道闸门守卫（python3 tests/test_sync_guard.py）

背景（2026-09-19 实测事故）：
`dist/package_release.py` 重建开源目录时是「先把目录清空（保留 .git）再逐个复制」，
清空到复制完成之间有十几秒窗口。有一次 `dist/sync_opensource.py` 正好落在这个
窗口里，于是「本地暂时缺了的文件」被当成用户意图 —— GitHub 上收到一份
**删掉 8 个文件**的提交（含 `.gitignore`、CI workflow、`CHANGELOG.md`），
16 秒后才被下一次同步补回。那一瞬间公开仓库是不完整的。

这里钉住三条，防止复发：
1. `guard_mass_delete()`：删除项可疑（命中关键文件 / 超过阈值）时拒绝提交；
2. 零变化不建提交（以前每次调用都会留一条历史，纯噪音）；
3. 防护必须发生在**任何写操作之前**。

注：`dist/` 不进分发包，开源包里本测试自动跳过。
"""
from __future__ import annotations

import re
import sys
import tempfile
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
# 4. 源码级守卫：防护必须在写操作之前 + 零变化短路
# ---------------------------------------------------------------------------
src = SYNC.read_text(encoding="utf-8")
i_guard = src.index("guard_mass_delete(gone")
i_blob = src.index("/git/blobs")
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

# ---------------------------------------------------------------------------
# 5. 根因侧：构建脚本不得再「先清空再复制」
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
else:
    print("[SKIP] dist/package_release.py 不在当前副本内")

# ---------------------------------------------------------------------------
# 6. 离线端到端：把网络换成记账本，验证 main() 的四条路径
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
        if method == "GET" and "git/trees/main?recursive=1" in path:
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


def _run_main(fake, src: Path) -> int:
    """用假 API + 临时 SRC 跑一次 main()（全程无网络）。"""
    orig_api, orig_src = S.api, S.SRC
    S.api, S.SRC = fake, src
    try:
        return S.main()
    finally:
        S.api, S.SRC = orig_api, orig_src


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

# 6c. 正常更新（1 个新文件、0 删除）→ 放行并真的推送
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    _mk(d, ["core/f1.py", "core/f2.py", "core/new.py"])
    fake = _FakeAPI([{"path": n, "type": "blob", "sha": S.blob_sha(b"x")}
                     for n in ("core/f1.py", "core/f2.py")])
    rc = _run_main(fake, d)
    check("★ 端到端：正常新增文件 → 放行（闸门不误伤正常同步）", rc == 0, f"rc={rc}")
    check("  确实发起了写请求（上传 blob + 更新 ref）",
          any(p.endswith("/git/blobs") for _, p in fake.writes)
          and any("/git/refs/heads/" in p for _, p in fake.writes),
          str(fake.writes))

# 6d. 本地目录不存在 → 立即拒绝，连读请求都不发
fake = _FakeAPI([])
rc = _run_main(fake, Path(tempfile.gettempdir()) / "definitely_not_here_9527")
check("★ 端到端：目录不存在 → 返回 1 且不发任何请求",
      rc == 1 and not fake.calls, f"rc={rc} calls={fake.calls}")

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 开源同步闸门守卫全部通过")
