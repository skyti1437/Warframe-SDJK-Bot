# -*- coding: utf-8 -*-
"""加载/刷新路径健壮性（python3 tests/test_load_resilience.py）

来源：2026-09-25 服务器面板「加载失败 (1)：No module named 'core'」事故 ——
从市场安装 v1.0.7 时，**新 main.py 已落地、core/render.py 还是旧的**
（新 main 要 `migrate_legacy_user_fonts`，旧 render 没有）→ 第一步 ImportError
→ `except ImportError` 兼容分支再去 `from core import ...` → 抛出的就变成
「No module named 'core'」。**真错误被兜底伪装成"模块结构坏了"**，面板与日志
第一眼全被误导（实际插件随后自己恢复、一直在服务）。

这里钉住三件事：
1. 兼容分支只在**非包上下文**（离线脚本）才回退，包内失败必须原样抛出；
2. 后台检查点对齐到 **XX:05 UTC**（换轮都在 00:00 UTC，旧 sleep(6h) 最坏等 6h）；
3. 社区快照有**独立的 30 分钟轻轮询**（FS 关闭/不可达的用户才走它）。
"""

from __future__ import annotations

import importlib
import ast
import asyncio
import re
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 1. 兼容分支：只在非包上下文回退
# ---------------------------------------------------------------------------
GUARDED = {
    "main.py": ("except ImportError as _imp_err:", "import core 相关"),
    "core/api_client.py": None,
    "core/damage_calc.py": None,
    "core/wiki_intro.py": None,
}
for rel in GUARDED:
    src = (ROOT / rel).read_text(encoding="utf-8")
    # 「except ImportError」之后（允许中间有注释/空行）出现 `if __package__:` + `raise`
    # 注：不要求 except 行以冒号结尾 —— 行尾可能跟着 `# pragma: no cover`，
    #     里面那个冒号会让贪婪匹配回溯失败（实测踩到）。
    ok = bool(
        re.search(r"except ImportError[^\n]*\n(?:[^\n]*\n){0,12}?\s+if __package__:\n\s+raise", src)
    )
    check(
        f"★ {rel}：包内 ImportError 原样抛出（不再被兜底掩盖）",
        ok,
        "未找到 `if __package__: raise` 守卫",
    )

# 行为验证：一个模拟「包内导入失败」的临时包，必须抛出**真错误**
with tempfile.TemporaryDirectory() as tmp:
    pkg = Path(tmp) / "tmppkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "guarded.py").write_text(
        textwrap.dedent("""
        try:
            from .missing_module import NEEDED   # 模拟「新代码要的东西还没落地」
        except ImportError:
            if __package__:
                raise
            NEEDED = "fallback"
    """),
        encoding="utf-8",
    )
    sys.path.insert(0, tmp)
    try:
        importlib.import_module("tmppkg.guarded")
        check("★ 行为：包内导入失败必须抛真错误（提到缺的那个模块）", False, "竟然导入成功了")
    except ImportError as exc:
        check(
            "★ 行为：包内导入失败必须抛真错误（提到缺的那个模块）",
            "missing_module" in str(exc),
            str(exc),
        )
    finally:
        sys.path.remove(tmp)
        for m in [k for k in sys.modules if k.startswith("tmppkg")]:
            del sys.modules[m]

check(
    "★ 行为：非包上下文（__package__ 为空）仍可回退",
    bool(re.search(r"if __package__:\n\s+raise\n\s+(import|from) ", MAIN)),
    "main.py 的兜底导入不见了",
)

# ---------------------------------------------------------------------------
# 2. 检查点对齐 XX:05 UTC（换轮 00:00 UTC → 最坏 6h 变 ~5min）
# ---------------------------------------------------------------------------
_functions = {
    n.name: n
    for n in ast.walk(ast.parse(MAIN))
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
}
_valence = _functions["_valence_autoloop"]
_aligned_call = ast.parse(
    'self._background_sleep("valence", self._secs_to_aligned_tick())', mode="eval"
).body
_aligned = [
    n
    for n in ast.walk(_valence)
    if isinstance(n, ast.Await) and ast.dump(n.value) == ast.dump(_aligned_call)
]
_raw_sleeps = [
    n
    for n in ast.walk(_valence)
    if isinstance(n, ast.Call)
    and ast.dump(n.func) == ast.dump(ast.parse("asyncio.sleep", mode="eval").body)
]
check("★ 主循环用带对齐间隔的状态包装，不裸等6小时", bool(_aligned) and not _raw_sleeps)
check(
    "对齐助手有步长/分钟/抖动三个参数（抖动防扎堆）",
    [a.arg for a in _functions["_secs_to_aligned_tick"].args.args]
    == ["step_hours", "minute", "jitter_min"]
    and [ast.literal_eval(v) for v in _functions["_secs_to_aligned_tick"].args.defaults]
    == [6, 5, 15],
)
check(
    "源刷新主循环统一使用对齐间隔，不以前置FS探活分流",
    len(_aligned) == 1
    and not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_flare_phase"
        for n in ast.walk(_valence)
    ),
    str(len(_aligned)),
)

# ---------------------------------------------------------------------------
# 3. 社区快照独立轻轮询（所有传输配置均可更新）
# ---------------------------------------------------------------------------
_community = _functions["_community_autoloop"]
check(
    "★ 它是被 create_task 起的（否则永不运行）",
    "asyncio.create_task(self._community_autoloop())" in MAIN,
)


async def _poll_behavior():
    logs = []
    ns = {
        "asyncio": asyncio,
        "logger": SimpleNamespace(warning=lambda *a: logs.append(a), info=lambda *a: None),
    }
    exec(compile(ast.Module(body=[_community], type_ignores=[]), str(ROOT / "main.py"), "exec"), ns)

    class Harness:
        def __init__(self, phase, rounds=1, fail=False):
            self.phase, self.rounds, self.fail = phase, rounds, fail
            self.delays, self.refreshes, self.static, self.records = [], 0, 0, []

        async def _background_sleep(self, name, seconds):
            self.delays.append((name, seconds))
            if len(self.delays) > self.rounds:
                raise asyncio.CancelledError

        def _note_background(self, *args):
            self.records.append(args)

        async def _flare_phase(self):
            return self.phase

        async def _refresh_from_community(self):
            self.refreshes += 1
            if self.fail:
                raise RuntimeError("fixture error")
            return {"rotation": "fresh"}

        async def _refresh_static_community(self):
            self.static += 1
            return {"static": "fresh"}

        def _refresh_outcome(self, results):
            return "ok", "fixture"

    async def run(phase, rounds=1, fail=False):
        obj = Harness(phase, rounds, fail)
        try:
            await ns["_community_autoloop"](obj)
        except asyncio.CancelledError:
            pass
        return obj

    off, absent, ready = await run("off"), await run("absent"), await run("ready", 6)
    check(
        "★ 有独立的社区轻轮询，正常间隔保持30分钟",
        all(delay == ("community", 1800) for obj in (off, absent, ready) for delay in obj.delays),
    )
    check(
        "★ FS关闭/不可达/ready均读社区轮换，fresh由业务入口短路",
        off.refreshes == absent.refreshes == 1 and ready.refreshes == 6,
    )
    check("FS ready时仍每6轮检查静态社区数据", ready.static == 1)
    failed = await run("off", fail=True)
    check(
        "轻轮询失败记状态并静默60秒重试，不向群告警",
        failed.delays == [("community", 1800), ("community", 60)]
        and any(row[:2] == ("community", "error") for row in failed.records)
        and not logs,
    )

    # 实际状态包装必须把传入间隔原样交给sleep，不能仅改记录而破坏调度。
    delegated, states = [], []

    async def sleep(seconds):
        delegated.append(seconds)

    wrapper_ns = {"asyncio": SimpleNamespace(sleep=sleep)}
    exec(
        compile(
            ast.Module(body=[_functions["_background_sleep"]], type_ignores=[]),
            str(ROOT / "main.py"),
            "exec",
        ),
        wrapper_ns,
    )
    obj = SimpleNamespace(_note_background=lambda *a, **kw: states.append((a, kw)))
    await wrapper_ns["_background_sleep"](obj, "valence", 731.5)
    check(
        "状态包装保留对齐助手传入的真实间隔",
        delegated == [731.5] and states == [(("valence", "waiting"), {"delay": 731.5})],
    )


asyncio.run(_poll_behavior())

# ---------------------------------------------------------------------------
# 4. 一致性：三个 refresh 出口名对得上（消费侧/发布侧共用词表）
# ---------------------------------------------------------------------------
api = (ROOT / "core" / "api_client.py").read_text(encoding="utf-8")
check("refresh_valence 会返回 no-community（取不到就静默跳过）", 'return "no-community"' in api)
check(
    "_refresh_from_community 只认 refreshed* 为成功（其余静默）",
    'st.startswith("refreshed")' in MAIN,
)

print()
# ---------------------------------------------------------------------------
# ★ 2026-09-26 新增守卫：**函数体内的绝对导入 `core.*` 必须位于 `except ImportError` 分支**
#   事故：服务器以**包成员**加载（`data.plugins.astrbot_plugin_warframe.main`）⇒ `core` 不可解析；
#   `main.py` 里函数内裸 `from core.parser import TIER_CN` 直接 `No module named 'core'`
#   ⇒ **遗物全套指令（出库/入库/列表/单查）全炸**（2026-09-26 17:02:45 线上实测）。
#   用 AST 精确判定（缩进启发式不可靠）：函数体内的 core.* 导入只允许出现在
#   `except ImportError:` 分支里（即 try 里先 `from .core...`、失败再回落绝对导入）。
# ---------------------------------------------------------------------------
import ast as _ast2  # noqa: E402

_bad2: list[str] = []
_FUNCS = (_ast2.FunctionDef, _ast2.AsyncFunctionDef)
for _f in (
    [ROOT / "main.py"]
    + sorted((ROOT / "core").glob("*.py"))
    + sorted((ROOT / "core" / "commands").glob("*.py"))
):
    try:
        _tree = _ast2.parse(_f.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        continue
    for _fn in [n for n in _ast2.walk(_tree) if isinstance(n, _FUNCS)]:
        # 该函数内：相对形式（from .core...）的 (module, names) 集合
        _rel: set = set()
        _abs: list = []
        for _n in _ast2.walk(_fn):
            # 2026-09-29 D7：相对导入（level>0）一律收集——core/commands 子包内的
            # 正确相对形态 `..parser` 的 module 不含 "core" 前缀；绝对侧仍只认 core.*
            if isinstance(_n, _ast2.ImportFrom) and (
                _n.level > 0 or (_n.module or "").split(".")[0] == "core"
            ):
                _key = (_n.module, tuple(sorted(_a.name for _a in _n.names)))
                if _n.level > 0:
                    _rel.add(_key)
                else:
                    _abs.append((_n.lineno, _key))
            elif isinstance(_n, _ast2.Import):
                for _a in _n.names:
                    if _a.name.split(".")[0] != "core":
                        continue
                    _key = (None, (_a.name,))
                    if _a.name.startswith("."):
                        _rel.add(_key)
                    else:
                        _abs.append((_n.lineno, _key))

        # 函数体内的**绝对**导入必须有同形相对导入兜底（try/except ImportError 双分支）
        # 2026-09-29 D7：配对键规范化——去掉 module 的前导 "core." 再比较。
        # 原 main.py 场景相对形态是 `.core.parser`（module 字面即含 core.），
        # core/commands 子包内的正确相对形态是 `..parser`（module 无 core. 前缀）；
        # 规范化后两种形态与绝对 `core.parser` 同键，护栏语义（防裸绝对导入）不变。
        def _norm_key(k):
            mod, names = k
            if mod == "core":
                mod = ""
            elif mod and mod.startswith("core."):
                mod = mod[len("core.") :]
            return (mod or "", names)

        _rel_norm = {_norm_key(k) for k in _rel}
        for _lineno, _key in _abs:
            if _norm_key(_key) not in _rel_norm:
                _bad2.append(
                    f"{_f.relative_to(ROOT)}:{_lineno} {_key[0] or 'import'} {','.join(_key[1])}"
                )

check("★ 函数内绝对导入 core.* 均带相对导入兜底（0 个裸导入）", not _bad2, "; ".join(_bad2[:5]))

# ---------------------------------------------------------------------------
# ★ 2026-09-26：遗物用法串抽常量（RELIC_USAGE）—— 防两处各写一份漂移
# 2026-09-29 D5：RELIC_USAGE 与 _h_relic 随遗物域迁 core/commands/relic.py
# ---------------------------------------------------------------------------
_src_main = (ROOT / "core" / "commands" / "relic.py").read_text(encoding="utf-8")
_tree_main = _ast2.parse(_src_main)
check(
    "★ RELIC_USAGE 是模块级常量",
    any(
        isinstance(n, _ast2.Assign)
        and any(getattr(x, "id", "") == "RELIC_USAGE" for x in n.targets)
        for n in _tree_main.body
    ),
)
_hard = [
    s.value
    for f in _ast2.walk(_tree_main)
    if isinstance(f, _ast2.FunctionDef) and f.name == "_h_relic"
    for s in _ast2.walk(f)
    if isinstance(s, _ast2.Constant) and isinstance(s.value, str) and "用法：遗物" in s.value
]
check("★ _h_relic 内不再硬编码「用法：遗物」", not _hard, str(_hard[:2]))
check(
    "全仓「用法：遗物」只剩常量一处",
    _src_main.count("用法：遗物") == 1,
    str(_src_main.count("用法：遗物")),
)
check("用法串不含非子集符号（alpha 类）", "α" not in _src_main.split("RELIC_USAGE = ")[1][:200])


# 原子写故障注入只碰 tempdir；两种失败均须保留旧件、清理半成品，随后可恢复。
def _atomic_write_recovery():
    from unittest.mock import patch

    sys.path.insert(0, str(ROOT))
    from core.paths import atomic_write_text

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "nested" / "state.json"
        atomic_write_text(target, '{"revision":1}')
        original_write = Path.write_text

        def partial_write(path, text, **kwargs):
            original_write(path, text[:5], **kwargs)
            raise OSError("fixture disk full")

        for mode in ("partial-write", "replace"):
            hook = (
                patch.object(Path, "write_text", partial_write)
                if mode == "partial-write"
                else patch("core.paths.os.replace", side_effect=OSError("fixture replace denied"))
            )
            try:
                with hook:
                    atomic_write_text(target, '{"revision":2}')
                check(f"原子写 {mode} 故障原样抛出", False)
            except OSError as exc:
                check(f"原子写 {mode} 故障原样抛出", "fixture" in str(exc))
            check(
                f"原子写 {mode} 失败保留旧文件且清空临时件",
                target.read_text() == '{"revision":1}'
                and list(target.parent.iterdir()) == [target],
            )
        atomic_write_text(target, '{"revision":3}')
        check(
            "原子写两种失败后仍可完整提交新文件",
            target.read_text() == '{"revision":3}' and list(target.parent.iterdir()) == [target],
        )


_atomic_write_recovery()


async def _json_store_failure_recovery():
    from unittest.mock import patch

    from core.store import JsonStore

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "store.json"
        target.write_text('{"revision":1}')
        store = JsonStore(target)
        opened = []
        original_open = open

        class PartialFile:
            def __enter__(self):
                self.fh = original_open(target.with_suffix(".tmp"), "w", encoding="utf-8")
                opened.append(self.fh)
                return self

            def write(self, text):
                self.fh.write(text[:5])
                raise OSError("fixture disk full")

            def __exit__(self, *args):
                self.fh.close()

        for mode in ("partial-write", "fsync", "replace"):
            store._data = {"revision": 2}
            hooks = {
                "partial-write": patch("core.store.open", return_value=PartialFile(), create=True),
                "fsync": patch("core.store.os.fsync", side_effect=OSError("fixture fsync")),
                "replace": patch.object(Path, "replace", side_effect=OSError("fixture replace")),
            }
            try:
                with hooks[mode]:
                    await store.save()
                check(f"JsonStore {mode} 故障原样抛出", False)
            except OSError as exc:
                check(f"JsonStore {mode} 故障原样抛出", "fixture" in str(exc))
            check(
                f"JsonStore {mode} 失败保留旧文件、释放锁与文件并清理临时件",
                target.read_text() == '{"revision":1}'
                and not store._lock.locked()
                and all(fh.closed for fh in opened)
                and list(target.parent.iterdir()) == [target],
            )
            await asyncio.wait_for(store.save(), 2)
            check(
                f"JsonStore {mode} 失败后下次保存可恢复",
                JsonStore(target)._data == {"revision": 2}
                and list(target.parent.iterdir()) == [target],
            )
            target.write_text('{"revision":1}')


asyncio.run(_json_store_failure_recovery())


def _shared_json_singleflight():
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event, Lock
    from unittest.mock import patch

    from core import paths

    class ObservedLock:
        def __init__(self):
            self.lock = Lock()
            self.waiting = Event()

        def __enter__(self):
            if self.lock.locked():
                self.waiting.set()
            self.lock.acquire()

        def __exit__(self, *args):
            self.lock.release()

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "shared.json"
        target.write_text('{"revision":1}')
        key = str(target)
        original_read = Path.read_text
        lock = ObservedLock()
        locks = list(paths._JSON_LOCKS)
        locks[hash(key) % len(locks)] = lock
        started = Event()
        reads = []

        def gated_read(path, *args, **kwargs):
            if path == target:
                reads.append(path)
                started.set()
                if not lock.waiting.wait(3):
                    raise AssertionError("没有观察到同文件冷读等待")
            return original_read(path, *args, **kwargs)

        try:
            with (
                patch.object(paths, "_JSON_LOCKS", tuple(locks)),
                patch.object(Path, "read_text", gated_read),
            ):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(paths.load_json, target)
                    assert started.wait(3), "首个 JSON 冷读未开始"
                    second = pool.submit(paths.load_json, target)
                    a, b = first.result(timeout=5), second.result(timeout=5)
            check("共享 JSON 并发冷读只解析一次且返回同一对象", len(reads) == 1 and a is b)
            check("共享 JSON 热读保留对象身份", paths.load_json(target) is a)

            # 分别独立触发 mtime 与 size 变化，避免其中一条失效判据被遗漏。
            st = target.stat()
            target.write_text('{"revision":2}')
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
            updated = paths.load_json(target)
            check(
                "共享 JSON 同 size 改 mtime 后失效", updated == {"revision": 2} and updated is not a
            )
            st = target.stat()
            target.write_text('{"revision":300}')
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
            check("共享 JSON 同 mtime 改 size 后失效", paths.load_json(target) == {"revision": 300})

            # 先过期旧缓存，再注入读失败和解析失败；失败后必须跨线程可重试。
            for mode in ("read", "parse"):
                target.write_text('{"revision":4000}' if mode == "read" else '{"revision":bad!}')
                hook = (
                    patch.object(Path, "read_text", side_effect=OSError("fixture read failure"))
                    if mode == "read"
                    else patch.object(Path, "read_text", original_read)
                )
                with hook, ThreadPoolExecutor(max_workers=1) as pool:
                    check(
                        f"共享 JSON {mode} 失败返回空而非过期缓存",
                        pool.submit(paths.load_json, target).result(timeout=5) == {},
                    )
                st = target.stat()
                target.write_text('{"revision":5000}')
                os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    check(
                        f"共享 JSON {mode} 失败后释放锁且同元数据可恢复",
                        pool.submit(paths.load_json, target).result(timeout=5)
                        == {"revision": 5000},
                    )

            # 两个不同条带的文件必须能同时进入实际读取，不受全局耗时锁阻塞。
            other = next(
                Path(tmp) / f"other-{i}.json"
                for i in range(1000)
                if hash(str(Path(tmp) / f"other-{i}.json")) % len(paths._JSON_LOCKS)
                != hash(key) % len(paths._JSON_LOCKS)
            )
            other.write_text('{"other":1}')
            paths._JSON_CACHE.pop(key, None)
            rendezvous = Barrier(2)

            def parallel_read(path, *args, **kwargs):
                if path in (target, other):
                    rendezvous.wait(timeout=3)
                return original_read(path, *args, **kwargs)

            with (
                patch.object(Path, "read_text", parallel_read),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                futures = [pool.submit(paths.load_json, p) for p in (target, other)]
                check("不同条带 JSON 可以并行读取", all(f.result(timeout=5) for f in futures))
            paths._JSON_CACHE.pop(str(other), None)
        finally:
            paths._JSON_CACHE.pop(key, None)


_shared_json_singleflight()

if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 加载/刷新路径健壮性守卫全部通过")
