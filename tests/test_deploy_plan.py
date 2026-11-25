# -*- coding: utf-8 -*-
"""部署计划（``scripts/tri_sync_audit.py --plan``）与 ``deploy.sh`` 先验后写的回归。

只读、不联网、不 ssh：本地/服务器/容器三棵树都在**临时目录**里现造（本地那棵在
临时目录里 git init/add/commit —— 属测试夹具，绝不动本仓）。风格照
``tests/test_check_runner.py`` 的 check() 计数；跑法 ``python tests/test_deploy_plan.py``。
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "tri_sync_audit.py"
DEPLOY = ROOT / "deploy.sh"
sys.path.insert(0, str(ROOT / "scripts"))

import tri_sync_audit as TS  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 夹具：临时"本地仓"（git 跟踪）/ "服务器树" / "容器树"
# ---------------------------------------------------------------------------

# 值可以是 str 或 bytes（bytes 用于模拟二进制文件）
LOCAL_FILES: dict[str, str | bytes] = {
    "main.py": "print('main')\n",
    "core/__init__.py": '__version__ = "1.2.3"\n',
    "core/a.py": "A = 1\n",
    "core/b.py": "line1\nline2\n",
    "core/data/x.json": '{"k": 1}\n',
    "_conf_schema.json": '{"a": {}}\n',
    "metadata.yaml": "name: t\nversion: 1.2.3\n",
    "requirements.txt": "requests\n",
    "pages/sdjk/index.html": "<html></html>\n",
    ".astrbot-plugin/i18n/zh-CN.json": '{"a": "b"}\n',
    "scripts/x.py": "print('tool')\n",
    "logo.png": b"\x89PNG\r\n\x1a\n\xff\xfe",  # 非 UTF-8 ⇒ md5_lf 应退化为 md5
    ".gitignore": "core/data/wm_ranks.json\n",
    ".zcode/skills/astrbot-server-triage/scripts/automations/crontab.template": "# cron\n",
    ".zcode/skills/astrbot-server-triage/scripts/automations/server_patrol.py": "print('patrol')\n",
    "deploy.sh": "#!/bin/sh\n",  # 2026-10-19 起归 plugin 工具面（服务器树运行，不进容器）
    # 以下均**不在** plugin profile 内：用于验证它们不进计划
    "AGENTS.md": "agents\n",
    "docs/notes.md": "notes\n",
    "tests/test_x.py": "pass\n",
    "dist/package_release.py": "# pkg\n",
    "kb_src/raw.json": "{}\n",
    "assets/readme/a.png": "PNG\n",
}
CRON = ".zcode/skills/astrbot-server-triage/scripts/automations/crontab.template"
PATROL = ".zcode/skills/astrbot-server-triage/scripts/automations/server_patrol.py"


def _write(root: Path, rel: str, body: str | bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        p.write_bytes(body)
    else:
        p.write_text(body, encoding="utf-8", newline="")


def _repo() -> Path:
    """临时"本地仓"：git init/add/commit（只在临时目录里做，属测试夹具）。"""
    root = Path(tempfile.mkdtemp(prefix="wf_plan_repo_"))
    for rel, body in LOCAL_FILES.items():
        _write(root, rel, body)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    for cmd in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-q", "-m", "init")):
        r = subprocess.run(["git", "-C", str(root), *cmd], capture_output=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"git {cmd} 失败：{r.stderr.decode('utf-8', 'replace')}")
    return root


def _server(
    overrides: dict | None = None,
    extras: dict | None = None,
    drop: set[str] | None = None,
) -> Path:
    """临时"服务器树"：默认与本地仓逐字节相同（LF），可覆盖 / 追加 / 删除若干文件。"""
    root = Path(tempfile.mkdtemp(prefix="wf_plan_srv_"))
    body = dict(LOCAL_FILES)
    for rel in drop or ():
        body.pop(rel, None)
    body.update(overrides or {})
    body.update(extras or {})
    for rel, text in body.items():
        _write(root, rel, text)
    return root


def _tree(files: dict[str, str | bytes]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="wf_plan_tree_"))
    for rel, body in files.items():
        _write(root, rel, body)
    return root


def _run(*args: str) -> tuple[int, str, str]:
    p = subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, encoding="utf-8"
    )
    return p.returncode, p.stdout, p.stderr


def _plan_json(profile: str, local: Path, server, container=None) -> tuple[int, dict | None, str]:
    args = ["--plan", profile, "--local", str(local), "--server", str(server), "--json"]
    if container is not None:
        args += ["--container", str(container)]
    rc, out, err = _run(*args)
    try:
        return rc, json.loads(out), err
    except json.JSONDecodeError:
        return rc, None, err + "\n[stdout 非 JSON]\n" + out[:400]


def _plan_text(profile: str, local: Path, server, container=None) -> tuple[int, str, str]:
    args = ["--plan", profile, "--local", str(local), "--server", str(server)]
    if container is not None:
        args += ["--container", str(container)]
    return _run(*args)


def _snapshot(root: Path) -> dict[str, str]:
    """树快照（文件清单 + md5；跳过 .git —— git 只读命令可能刷新 .git/index）。"""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            p = Path(dirpath) / name
            out[p.relative_to(root).as_posix()] = hashlib.md5(p.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# 1. 夹具自检 + plugin 主场景（需传 / CRLF 标记 / 孤儿 / 动作 / 文本与 JSON 一致）
# ---------------------------------------------------------------------------
LOCAL = _repo()
SRV = _server(
    overrides={"core/a.py": "A = 2\n", "core/b.py": "line1\r\nline2\r\n"},
    extras={
        "core/data/wm_ranks.json": "{}\n",  # .gitignore 忽略 ⇒ 运行期件
        "core/zzz_server_only.py": "pass\n",  # 未忽略 ⇒ 需人工审核
        "scripts/zzz_new_tool.py": "pass\n",  # 工具面孤儿 ⇒ 同样归审核
        "docs/server_only.md": "x\n",  # 不在 profile 内 ⇒ 不该出现
    },
)
_tracked = subprocess.run(
    ["git", "-C", str(LOCAL), "ls-files"], capture_output=True, text=True
).stdout.split()
check(
    "夹具：临时仓已跟踪 main.py / core/a.py",
    "main.py" in _tracked and "core/a.py" in _tracked,
    str(_tracked[:5]),
)
check(
    "夹具：临时仓工作树干净（无未提交改动）",
    "??"
    not in subprocess.run(
        ["git", "-C", str(LOCAL), "status", "--porcelain"], capture_output=True, text=True
    ).stdout,
)

rc, plan, err = _plan_json("plugin", LOCAL, SRV)
check("计划：plugin 模式退出 0 且 --json 可解析", rc == 0 and plan is not None, err)
plan = plan or {}
_transfer = plan.get("transfer", [])
check(
    "需传：内容不同（含 CRLF）才进清单，同内容不进",
    [r.get("path") for r in _transfer] == ["core/a.py", "core/b.py"],
    str([r.get("path") for r in _transfer]),
)
check(
    "需传：仅行尾不同 ⇒ 带 crlf_only 标记（不从需传里去掉）",
    [r.get("path") for r in _transfer if r.get("crlf_only")] == ["core/b.py"],
    str(_transfer),
)
check(
    "需传：本地 md5 与服务器 md5 前 8 位都给（有差异）",
    _transfer
    and all(r.get("local_md5") and r.get("server_md5") for r in _transfer)
    and _transfer[0]["local_md5"] != _transfer[0]["server_md5"],
    str(_transfer),
)
check(
    "孤儿：.gitignore 忽略的服务器独有文件归「运行期件」",
    plan.get("orphans", {}).get("runtime") == ["core/data/wm_ranks.json"],
    str(plan.get("orphans")),
)
check(
    "孤儿：未忽略的服务器独有文件归「需人工审核」（不自动删除）",
    plan.get("orphans", {}).get("review") == ["core/zzz_server_only.py", "scripts/zzz_new_tool.py"],
    str(plan.get("orphans")),
)
check(
    "孤儿：不在 profile 内的服务器独有文件不出现",
    "docs/server_only.md" not in json.dumps(plan.get("orphans"), ensure_ascii=False),
    str(plan.get("orphans")),
)
check(
    "动作：改 core/a.py + core/b.py ⇒ reload",
    plan.get("action", {}).get("code") == "reload",
    str(plan.get("action")),
)
check(
    "动作：触发文件只列真内容差异（仅行尾不同的 core/b.py 不算）",
    plan.get("action", {}).get("files") == ["core/a.py"],
    str(plan.get("action")),
)
check(
    "计划：readonly 标记与只读说明在 JSON 里", plan.get("readonly") is True, str(plan.get("note"))
)

_rc, _out, _err = _plan_text("plugin", LOCAL, SRV)
_lines = _out.strip().splitlines() if _out.strip() else []
check("文本：顶部注明「本脚本未执行任何命令」", "本脚本未执行任何命令" in _out, _out[:200])
check(
    "文本：CRLF 行标出「仅行尾不同」",
    "仅行尾不同（CRLF），仍按字节为准要传" in _out and "core/b.py" in _out,
    _out[-600:],
)
check("文本：工作树干净 ⇒ 无「不干净」告警", "工作树不干净" not in _out, _out[:400])
check(
    "文本：末行是 PLAN| 摘要",
    bool(_lines) and _lines[-1].startswith("PLAN|profile=plugin|"),
    _lines[-1] if _lines else "",
)
_sum = plan.get("summary", {})
_expect = (
    f"PLAN|profile=plugin|transfer={_sum.get('transfer')}|runtime_real={_sum.get('runtime_real')}"
    f"|crlf_only={_sum.get('crlf_only')}"
    f"|orphans_review={_sum.get('orphans_review')}|orphans_runtime={_sum.get('orphans_runtime')}"
    f"|container_delete={_sum.get('container_delete')}|action={_sum.get('action')}"
)
check(
    "文本与 --json 的摘要数字一致",
    bool(_lines) and _lines[-1] == _expect,
    f"text={_lines[-1] if _lines else ''} json={_expect}",
)

# 服务器没有的新文件（本地有）⇒ 需传且 server_md5 为 None
SRV_NEW = _server(drop={".astrbot-plugin/i18n/zh-CN.json"})
_rc, _p, _err = _plan_json("plugin", LOCAL, SRV_NEW)
_new_rows = [
    r for r in (_p or {}).get("transfer", []) if r.get("path") == ".astrbot-plugin/i18n/zh-CN.json"
]
check(
    "需传：服务器没有的新文件 ⇒ 需传且 server_md5 为 None",
    _rc == 0 and len(_new_rows) == 1 and _new_rows[0].get("server_md5") is None,
    f"rc={_rc} {(_p or {}).get('transfer')}",
)
shutil.rmtree(SRV_NEW, ignore_errors=True)

# 二进制文件：CRLF→LF 也不该被判成「仅行尾不同」（md5_lf 退化为 md5）
SRV_BIN = _server(overrides={"logo.png": b"\x89PNG\n\x1a\n\xff\xfe"})
_rc, _p, _err = _plan_json("plugin", LOCAL, SRV_BIN)
_bin_rows = [r for r in (_p or {}).get("transfer", []) if r.get("path") == "logo.png"]
_h = TS._hash_file(LOCAL / "logo.png")
check(
    "二进制：不可 UTF-8 解码 ⇒ md5_lf 退化为 md5",
    _h.md5_lf == _h.md5,
    str(_h),
)
check(
    "二进制：行尾变了也不算「仅行尾不同」",
    _rc == 0 and len(_bin_rows) == 1 and _bin_rows[0].get("crlf_only") is False,
    f"rc={_rc} {_bin_rows}",
)
shutil.rmtree(SRV_BIN, ignore_errors=True)

# ---------------------------------------------------------------------------
# 2. 动作决策表（改动类型 → 动作）
# ---------------------------------------------------------------------------
for _name, _ov, _expect_code in (
    # AstrBot 4.28.1 重载插件时会重读配置 schema。
    ("只改 _conf_schema.json ⇒ reload", {"_conf_schema.json": '{"a": {"b": 1}}\n'}, "reload"),
    (
        "只改 requirements.txt ⇒ deps_restart",
        {"requirements.txt": "requests\nnumpy\n"},
        "deps_restart",
    ),
    (
        "只改 scripts/x.py ⇒ none（工具面有需传但无需重载）",
        {"scripts/x.py": "print('t2')\n"},
        "none",
    ),
    (
        "只改 deploy.sh ⇒ none（工具面：服务器树里要更新，但不进容器、无需重载）",
        {"deploy.sh": "#!/bin/sh\necho old\n"},
        "none",
    ),
    ("只改 pages/… ⇒ reload", {"pages/sdjk/index.html": "<html>2</html>\n"}, "reload"),
    (
        "只改 .astrbot-plugin/… ⇒ reload",
        {".astrbot-plugin/i18n/zh-CN.json": '{"a": "c"}\n'},
        "reload",
    ),
    ("只改 metadata.yaml ⇒ reload", {"metadata.yaml": "name: t\nversion: 1.2.4\n"}, "reload"),
    ("只改 logo.png（门面文档）⇒ none", {"logo.png": b"\x89PNG-other"}, "none"),
):
    _srv = _server(overrides=_ov)
    _rc, _p, _err = _plan_json("plugin", LOCAL, _srv)
    check(
        f"动作：{_name}",
        _rc == 0 and (_p or {}).get("action", {}).get("code") == _expect_code,
        f"rc={_rc} {(_p or {}).get('action')} {_err[-200:]}",
    )
    shutil.rmtree(_srv, ignore_errors=True)

# requirements.txt 仅有行尾差异时无需重启依赖。
_srv = _server(overrides={"requirements.txt": "requests\r\n", "core/a.py": "A = 2\n"})
_rc, _p, _err = _plan_json("plugin", LOCAL, _srv)
_p = _p or {}
check(
    "动作：requirements.txt 仅行尾不同 ⇒ 不触发 deps_restart（只由 core/a.py 定为 reload）",
    _p.get("action", {}).get("code") == "reload" and "仅行尾不同" in _p["action"].get("note", ""),
    str(_p.get("action")),
)
check(
    "摘要：runtime_real 只数运行期面真内容差异（CRLF 不算）",
    _p.get("summary", {}).get("runtime_real") == 1 and _p["summary"].get("crlf_only") == 1,
    str(_p.get("summary")),
)
shutil.rmtree(_srv, ignore_errors=True)

# 门面文档计入 docs，运行期内容差异不计这部分。
_srv = _server(overrides={"logo.png": b"\x89PNG-other"})
_rc, _p, _err = _plan_json("plugin", LOCAL, _srv)
_p = _p or {}
check(
    "门面文档：face=docs、runtime_real=0",
    [r.get("face") for r in _p.get("transfer", [])] == ["docs"]
    and _p.get("summary", {}).get("runtime_real") == 0,
    str(_p.get("transfer")),
)
shutil.rmtree(_srv, ignore_errors=True)

# 工具面：有需传、动作 none（两面都要显式验证）
# 2026-10-19：deploy.sh 改了 ⇒ 进需传清单（以前两个 profile 都不覆盖，服务器上静默留旧版）
_srv_d = _server(overrides={"deploy.sh": "#!/bin/sh\necho old\n"})
_rc_d, _p_d, _err_d = _plan_json("plugin", LOCAL, _srv_d)
check(
    "需传：deploy.sh 有差异 ⇒ 出现在 plugin 计划的需传清单（工具面）",
    _rc_d == 0 and [r.get("path") for r in (_p_d or {}).get("transfer", [])] == ["deploy.sh"],
    f"rc={_rc_d} {(_p_d or {}).get('transfer')} {_err_d[-200:]}",
)
_srv = _server(overrides={"scripts/x.py": "print('t2')\n"})
_rc, _p, _err = _plan_json("plugin", LOCAL, _srv)
check(
    "动作：工具面需传仍列出（只是无需重载）",
    [r.get("path") for r in (_p or {}).get("transfer", [])] == ["scripts/x.py"]
    and (_p or {}).get("action", {}).get("code") == "none",
    f"rc={_rc} {(_p or {}).get('transfer')} {(_p or {}).get('action')}",
)
shutil.rmtree(_srv, ignore_errors=True)

# ---------------------------------------------------------------------------
# 3. ops profile
# ---------------------------------------------------------------------------
_srv = _server(overrides={CRON: "# cron v2\n"})
_rc, _p, _err = _plan_json("ops", LOCAL, _srv)
check(
    "ops：改 crontab.template ⇒ cron_review",
    _rc == 0 and (_p or {}).get("action", {}).get("code") == "cron_review",
    f"rc={_rc} {(_p or {}).get('action')} {_err[-200:]}",
)
check(
    "ops：需传只含该文件（其余同内容不进）",
    [r.get("path") for r in (_p or {}).get("transfer", [])] == [CRON],
    str((_p or {}).get("transfer")),
)
_rc, _p, _err = _plan_json("ops", LOCAL, _srv, container=_tree({"x.py": "x\n"}))
check(
    "ops：给了 --container 也跳过容器面（不在 rsync 范围）",
    (_p or {}).get("container", {}).get("simulated") is False
    and (_p or {}).get("summary", {}).get("container_delete") == 0,
    str((_p or {}).get("container")),
)
shutil.rmtree(_srv, ignore_errors=True)

_srv = _server(overrides={PATROL: "print('patrol2')\n"})
_rc, _p, _err = _plan_json("ops", LOCAL, _srv)
check(
    "ops：改其它脚本 ⇒ none（下次 cron 触发即生效）",
    _rc == 0 and (_p or {}).get("action", {}).get("code") == "none",
    f"rc={_rc} {(_p or {}).get('action')}",
)
shutil.rmtree(_srv, ignore_errors=True)

# ---------------------------------------------------------------------------
# 4. 容器面（rsync --delete 模拟）
# ---------------------------------------------------------------------------
CONT = _tree(
    {
        "main.py": LOCAL_FILES["main.py"],  # 与服务器一致 ⇒ 不删不改
        "core/a.py": "A = 0\n",  # 同步后内容变化
        "old_gone.py": "x\n",  # 服务器树没有、不在排除里 ⇒ --delete 将删
        "__pycache__/junk.pyc": b"\x00\x01",  # 排除项 ⇒ 不算
        "tests/should_stay.py": "stay\n",  # 排除项（tests/）⇒ 不算
        "assets/readme/a.png": "PNG\n",  # 排除项（assets/）⇒ 不算
    }
)
_rc, _p, _err = _plan_json("plugin", LOCAL, SRV, container=CONT)
_cont = (_p or {}).get("container", {})
check(
    "容器面：服务器树没有且未被排除的容器文件 ⇒ 「rsync --delete 将删除」",
    _rc == 0 and _cont.get("delete") == ["old_gone.py"],
    f"rc={_rc} {_cont.get('delete')} {_err[-200:]}",
)
check(
    "容器面：需传文件列为「同步后内容将变化/新增」",
    set(_cont.get("changed", []))
    == {rel for rel in _snapshot(SRV) if not TS._rsync_excluded(rel)} - {"main.py"},
    str(_cont.get("changed")),
)
check(
    "容器面：摘要 container_delete 与删除清单一致",
    (_p or {}).get("summary", {}).get("container_delete") == 1,
    str((_p or {}).get("summary")),
)
check(
    "容器面：排除项（tests/ assets/ __pycache__）不计入删除",
    "tests/should_stay.py" not in json.dumps(_cont, ensure_ascii=False)
    and "assets/readme/a.png" not in json.dumps(_cont, ensure_ascii=False),
    str(_cont),
)
_rc, _out, _err = _plan_text("plugin", LOCAL, SRV, container=CONT)
check(
    "容器面：文本模式给出「rsync --delete 将删除」字样",
    "rsync --delete 将删除" in _out,
    _out[-600:],
)

# 本地和服务器已对齐，容器单独落后。也校验容器已对齐时没有多余重载。
with tempfile.TemporaryDirectory() as _container, tempfile.TemporaryDirectory() as _server_dir:
    _ct = Path(_container)
    _sd = Path(_server_dir)
    for _rel, _content in LOCAL_FILES.items():
        _write(_sd, _rel, _content)
        _write(_ct, _rel, _content)
    _write(_ct, "main.py", "old container entry")
    _write(_ct, "tmp/server.log", "old log")
    _write(_ct, "data/preserved.log", "runtime")
    (_ct / "old-link").symlink_to("missing-target")
    _rc, _plan, _err = _plan_json("plugin", LOCAL, _sd, _ct)
    check(
        "仅容器入口落后触发重载",
        _rc == 0
        and not _plan["transfer"]
        and _plan["container"]["changed"] == ["main.py"]
        and _plan["action"]["code"] == "reload",
    )
    check(
        "容器独有日志和悬空链接列入删除，顶层 data 保留",
        _plan["container"]["delete"] == ["old-link", "tmp/server.log"],
    )
    _write(_ct, "main.py", LOCAL_FILES["main.py"])
    _write(_sd, "main.py", "server needs update")
    _rc, _plan, _err = _plan_json("plugin", LOCAL, _sd, _ct)
    check(
        "待传服务器的入口已在容器生效，无需重载",
        _plan["transfer"][0]["path"] == "main.py"
        and not _plan["container"]["changed"]
        and _plan["action"]["code"] == "none",
    )

# ★ Gate B 六词零命中：本文件随开源包分发 ⇒ 那两个字面量不能出现在源码文本里
#   （tri_sync_audit.py 里同处置，见其 _WS_DIR 注释）。
_WS = "work" + "buddy"

check(
    "rsync 排除：__pycache__/ 与 *.pyc",
    TS._rsync_excluded("core/__pycache__/x.pyc") and TS._rsync_excluded("core/x.pyc"),
)
check(
    "rsync 排除：tests/ 与 assets/ 任意层级",
    TS._rsync_excluded("tests/a.py")
    and TS._rsync_excluded("sub/assets/a.png")
    and not TS._rsync_excluded("core/data/x.json"),
)
check(
    "rsync 排除：锚定 /data/ 不误伤 core/data/",
    TS._rsync_excluded("data/x.json") and not TS._rsync_excluded("core/data/x.json"),
)
check(
    f"rsync 排除：锚定 /.{_WS}/ 不误伤深层同名目录",
    TS._rsync_excluded(f".{_WS}/memory/a.md") and not TS._rsync_excluded(f"sub/.{_WS}/a.md"),
)
check(
    "rsync 排除：deploy.sh / .gitignore / .git/ 在列",
    TS._rsync_excluded("deploy.sh")
    and TS._rsync_excluded("core/.gitignore")
    and TS._rsync_excluded("sub/.git/a"),
)

# ---------------------------------------------------------------------------
# 5. 清单 schema 1 / 2（向后兼容 + 往返）
# ---------------------------------------------------------------------------
DUMP_DIR = Path(tempfile.mkdtemp(prefix="wf_plan_dump_"))
MAN2 = DUMP_DIR / "server.json"
_rc, _out, _err = _run("--dump-manifest", str(LOCAL), "--label", "server", "--out", str(MAN2))
_payload = json.loads(MAN2.read_text(encoding="utf-8")) if MAN2.is_file() else {}
check(
    "schema 2：--dump-manifest 写出 schema=2 且每行三列",
    _rc == 0 and _payload.get("schema") == 2 and all(len(r) == 3 for r in _payload["files"]),
    f"rc={_rc} schema={_payload.get('schema')}",
)
check(
    "schema 2：file_count 与 files 行数一致",
    _payload.get("file_count") == len(_payload.get("files", [])),
    f"{_payload.get('file_count')} vs {len(_payload.get('files', []))}",
)
_loaded2 = TS.load_tree(str(MAN2))
check(
    "schema 2：load_tree 带回 md5 与 md5_lf",
    _loaded2["main.py"].md5 is not None and _loaded2["main.py"].md5_lf is not None,
    str(_loaded2.get("main.py")),
)
_rc, _p, _err = _plan_json("plugin", LOCAL, MAN2)
check(
    "schema 2 往返：用自己导出的清单当服务器 ⇒ transfer=0 / action=none",
    _rc == 0
    and (_p or {}).get("summary", {}).get("transfer") == 0
    and (_p or {}).get("action", {}).get("code") == "none",
    f"rc={_rc} {(_p or {}).get('summary')} {_err[-200:]}",
)

MAN1 = DUMP_DIR / "server_schema1.json"
MAN1.write_text(
    json.dumps(
        {
            "schema": 1,
            "label": "server",
            "root": str(LOCAL),
            "file_count": len(_payload.get("files", [])),
            "files": [[r[0], r[1]] for r in _payload.get("files", [])],
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
_loaded1 = TS.load_tree(str(MAN1))
check(
    "schema 1：两列清单仍能读，md5_lf 视为未知（None）",
    _loaded1.get("main.py") is not None and _loaded1["main.py"].md5_lf is None,
    str(_loaded1.get("main.py")),
)
_rc, _p, _err = _plan_json("plugin", LOCAL, MAN1)
check(
    "schema 1：用它做服务器 ⇒ transfer=0（内容一致）",
    _rc == 0 and (_p or {}).get("summary", {}).get("transfer") == 0,
    f"rc={_rc} {(_p or {}).get('summary')} {_err[-200:]}",
)

# schema 1 里 core/b.py 用 CRLF 版本的 md5 ⇒ 该行必须进需传，但**不能**给 CRLF 标记
_crlf_md5 = hashlib.md5(b"line1\r\nline2\r\n").hexdigest()
MAN1B = DUMP_DIR / "server_schema1_crlf.json"
MAN1B.write_text(
    json.dumps(
        {
            "schema": 1,
            "files": [
                [r[0], _crlf_md5 if r[0] == "core/b.py" else r[1]]
                for r in _payload.get("files", [])
            ],
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
_rc, _p, _err = _plan_json("plugin", LOCAL, MAN1B)
_crlf_row = next((r for r in (_p or {}).get("transfer", []) if r.get("path") == "core/b.py"), None)
check(
    "schema 1：md5_lf 未知 ⇒ 不给 CRLF 标记，但仍在需传里",
    _crlf_row is not None and _crlf_row.get("crlf_only") is False,
    f"rc={_rc} {_crlf_row}",
)
check(
    "schema 1：计划提示「无 md5_lf，需传与动作可能偏重」",
    any("schema 1" in w for w in (_p or {}).get("warnings", [])),
    str((_p or {}).get("warnings")),
)

# ---------------------------------------------------------------------------
# 6. 只读：计划模式不修改三棵树 / 不干净告警 / 非 git 仓退回 / 输入读不了
# ---------------------------------------------------------------------------
_before = (_snapshot(LOCAL), _snapshot(SRV), _snapshot(CONT))
_plan_json("plugin", LOCAL, SRV, container=CONT)
_plan_text("plugin", LOCAL, SRV, container=CONT)
_after = (_snapshot(LOCAL), _snapshot(SRV), _snapshot(CONT))
check(
    "只读：计划模式不修改本地/服务器/容器三棵树（清单 + md5 一致）",
    _before == _after,
    f"local={_before[0] == _after[0]} srv={_before[1] == _after[1]} cont={_before[2] == _after[2]}",
)

DIRTY = _repo()
(DIRTY / "main.py").write_text("print('dirty')\n", encoding="utf-8")
_rc, _out, _err = _plan_text("plugin", DIRTY, _server())
check(
    "工作树不干净 ⇒ 计划开头打印「⚠ 工作树不干净，计划基于工作树内容」",
    _rc == 0 and "工作树不干净" in _out,
    _out[:400],
)
shutil.rmtree(DIRTY, ignore_errors=True)

NOGIT = _tree(dict(LOCAL_FILES))
SRV_NOGIT = _server(extras={"core/zzz_only.py": "x\n"})
_rc, _out, _err = _plan_text("plugin", NOGIT, SRV_NOGIT)
check(
    "非 git 仓 ⇒ 退回全树扫描并在输出里提示",
    _rc == 0 and "退回全树扫描" in _out,
    _out[:400],
)
check(
    "非 git 仓 ⇒ 服务器独有文件归「需人工审核」",
    "core/zzz_only.py" in _out,
    _out[-800:],
)
shutil.rmtree(NOGIT, ignore_errors=True)
shutil.rmtree(SRV_NOGIT, ignore_errors=True)

_rc, _out, _err = _run(
    "--plan", "plugin", "--local", str(LOCAL), "--server", "/nonexistent/nowhere"
)
check(
    "输入读不了 ⇒ 退出码非 0 且说明原因",
    _rc != 0 and "无法读取树" in _err,
    f"rc={_rc} {_err[-200:]}",
)
_rc, _out, _err = _run("--plan", "plugin", "--local", str(LOCAL))
check("--plan 缺 --server ⇒ 报错退出（非 0）", _rc != 0 and "--server" in _err, f"rc={_rc}")
_rc, _out, _err = _run("--json", "--server", str(SRV))
check("--json 未配 --plan ⇒ 报错退出（非 0）", _rc != 0 and "--plan" in _err, f"rc={_rc}")

# ---------------------------------------------------------------------------
# 7. deploy.sh：语法检查 + 先验后写（P10：校验挪到 rsync 之前、读空也算失败）
# ---------------------------------------------------------------------------
# deploy.sh 不进开源包（dist/package_release.py::EXCLUDE_FILES）⇒ 公开仓 / 开源 stage 里整节跳过
#   （2026-10-17 v1.2.2 公开仓 CI 红：这里直接读 deploy.sh 抛 FileNotFoundError）
if not DEPLOY.is_file():
    print("[SKIP] deploy.sh 不在本树（开源包不含），跳过第 7 节")
else:
    _r = subprocess.run(["bash", "-n", str(DEPLOY)], capture_output=True, text=True)
    check("deploy.sh：bash -n 语法检查通过", _r.returncode == 0, _r.stderr)

    _SRC = DEPLOY.read_text(encoding="utf-8")
    _DST_LINE = 'DST="/opt/astrbot/data/plugins/astrbot_plugin_warframe_sdjkbot"'
    check("deploy.sh：DST 行仍可被测试替换（夹具前提，防误写真实目标）", _DST_LINE in _SRC)

    _lines_src = _SRC.splitlines()
    _start = next((i for i, ln in enumerate(_lines_src) if ln.startswith("check_version()")), None)
    _end = next(
        (i for i in range((_start or 0) + 1, len(_lines_src)) if _lines_src[i] == "}"), None
    )
    check(
        "deploy.sh：含 check_version() 闸门函数（可直接单测）",
        _start is not None and _end is not None,
        f"start={_start} end={_end}",
    )
    _FUNC = (
        "\n".join(_lines_src[_start : _end + 1]) if _start is not None and _end is not None else ""
    )

    def _check_version(dirpath: Path) -> int:
        script = f'{_FUNC}\ncheck_version {shlex.quote(str(dirpath))} "测试"\n'
        # 只看返回码；输出按 utf-8 宽松解码 —— macOS 自带 bash 3.2 下 `local` 会把中文参数压成
        # 单字节（如 0xbc），严格解码会抛 UnicodeDecodeError。
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, encoding="utf-8", errors="replace"
        ).returncode

    check(
        "check_version：两侧一致 ⇒ 0",
        _check_version(
            _tree(
                {
                    "metadata.yaml": "name: t\nversion: 1.2.3\n",
                    "core/__init__.py": '__version__ = "1.2.3"\n',
                }
            )
        )
        == 0,
    )
    check(
        "check_version：metadata 版本读空 ⇒ 非 0（旧判据会放行）",
        _check_version(
            _tree({"metadata.yaml": "name: t\n", "core/__init__.py": '__version__ = "1.2.3"\n'})
        )
        != 0,
    )
    check(
        "check_version：core.__version__ 读空 ⇒ 非 0",
        _check_version(_tree({"metadata.yaml": "version: 1.2.3\n"})) != 0,
    )
    check(
        "check_version：两侧不一致 ⇒ 非 0",
        _check_version(
            _tree(
                {"metadata.yaml": "version: 1.2.2\n", "core/__init__.py": '__version__ = "1.2.3"\n'}
            )
        )
        != 0,
    )

    runtime = {
        rel: (ROOT / rel).read_bytes()
        for rel in (
            "main.py",
            "_conf_schema.json",
            "requirements.txt",
            "metadata.yaml",
            *(p.relative_to(ROOT).as_posix() for p in (ROOT / "core").glob("*.py")),
            *(p.relative_to(ROOT).as_posix() for p in (ROOT / "core/commands").glob("*.py")),
            "core/data/rotations.json",
            "pages/sdjk/index.html",
            ".astrbot-plugin/i18n/zh-CN.json",
        )
    }

    def _deploy_case(args=(), *, missing="", bad_version=False, incomplete_transfer=False):
        with tempfile.TemporaryDirectory(prefix="wf-deploy-") as directory:
            root = Path(directory)
            src, dst, fakebin = root / "src", root / "dst", root / "bin"
            fakebin.mkdir()
            for rel, content in runtime.items():
                _write(dst, rel, b"old original")
                if rel != missing:
                    _write(src, rel, content)
            if bad_version:
                _write(src, "metadata.yaml", "name: fixture")
            for rel in ("._old", "__pycache__/old.pyc", "old.log", "data/._keep"):
                _write(dst, rel, b"keep until deployment")
            script = _SRC.replace(_DST_LINE, f'DST="{dst}"')
            if script == _SRC:
                raise RuntimeError("部署夹具未隔离目标")
            _write(src, "deploy.sh", script)
            before = _snapshot(dst)
            audit = root / "calls"
            env = {
                **os.environ,
                "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
                "AUDIT": str(audit),
                "SRC_TEST": str(src),
                "DST_TEST": str(dst),
            }
            real_rsync = shutil.which("rsync")
            sync = (
                ("exec " + shlex.quote(real_rsync) + ' "$@"')
                if real_rsync
                else (
                    'case " $* " in *" --dry-run "*) echo "dry-run main.py"; exit 0;; esac\n'
                    'cp -R "$SRC_TEST/." "$DST_TEST" && /bin/rm -f "$DST_TEST/old.log"'
                )
            )
            for command, action in {
                "rsync": 'echo "rsync $*" >> "$AUDIT"\n'
                + ('/bin/rm -f "$DST_TEST/main.py"' if incomplete_transfer else sync),
                "rm": 'echo cleanup >> "$AUDIT"\nexec /bin/rm "$@"',
                "docker": 'echo "docker $1" >> "$AUDIT"',
                "sleep": "exit 0",
            }.items():
                _write(fakebin, command, "#!/bin/sh\n" + action + "\n")
                (fakebin / command).chmod(0o755)
            result = subprocess.run(
                ["bash", str(src / "deploy.sh"), *args],
                env=env,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
            return (
                result.returncode,
                result.stdout + result.stderr,
                audit.read_text() if audit.exists() else "",
                before,
                _snapshot(dst),
            )

    for _args in (("--dry-run",), ("--restart", "--dry-run"), ("--dry-run", "--restart")):
        _rc, _out, _calls, _before, _after = _deploy_case(_args)
        check(
            f"部署演练 {_args} 输出差异和拟清项，目标哈希一致，清理和重启零调用",
            _rc == 0
            and _before == _after
            and "main.py" in _out
            and "._old" in _out
            and "__pycache__" in _out
            and "--dry-run" in _calls
            and "--itemize-changes" in _calls
            and "cleanup" not in _calls
            and "docker" not in _calls,
            _out[-500:],
        )
    _rc, _out, _calls, _before, _after = _deploy_case(("--restart", "--wrong"))
    check(
        "未知参数先于任何操作被拒绝",
        _rc != 0 and "--wrong" in _out and not _calls and _before == _after,
    )
    for _rel in (
        "main.py",
        "core/parser.py",
        "core/messaging.py",
        "core/cache.py",
        "pages/sdjk/index.html",
    ):
        _rc, _out, _calls, _before, _after = _deploy_case(("--restart",), missing=_rel)
        check(
            f"缺 {_rel} 时目标原件及全部哈希保留",
            _rc != 0 and _rel in _out and not _calls and _before == _after,
        )
    _rc, _out, _calls, _before, _after = _deploy_case(bad_version=True)
    check(
        "源树版本读空中止，目标哈希一致",
        _rc != 0 and "门面版本校验失败" in _out and not _calls and _before == _after,
    )
    for _args in ((), ("--restart",)):
        _rc, _out, _calls, _before, _after = _deploy_case(_args)
        check(
            f"正常部署 {_args} 同步、清理且按参数重启",
            _rc == 0
            and _after["main.py"] == hashlib.md5(runtime["main.py"]).hexdigest()
            and "门面版本一致（目标" in _out
            and "old.log" not in _after
            and "._old" not in _after
            and "__pycache__/old.pyc" not in _after
            and _after["data/._keep"] == _before["data/._keep"]
            and "cleanup" in _calls
            and _calls.count("docker restart") == int(bool(_args)),
            _out[-500:],
        )
    _rc, _out, _calls, _, _ = _deploy_case(("--restart",), incomplete_transfer=True)
    check(
        "目标漏传入口，写后复验失败并阻止重启",
        _rc != 0 and "main.py" in _out and "docker" not in _calls,
    )


# ---------------------------------------------------------------------------
print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
