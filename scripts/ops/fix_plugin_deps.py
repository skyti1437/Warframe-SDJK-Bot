#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性补齐所有「启用插件」的 Python 依赖，绕过 AstrBot pip_installer 挂起。
只读扫描 + 缺失检测；--apply 才安装。

在 AstrBot 宿主机上运行（需要 sudo -n 与 docker 权限）：
    sudo -n python3 fix_plugin_deps.py            # 只读体检（DRY-RUN）
    sudo -n python3 fix_plugin_deps.py --all --apply   # 容器重建/升级后必跑

环境变量（均为可选，缺省即 AstrBot docker 常规部署值）：
    ASTRBOT_DATA        宿主机侧 data 目录     （缺省 /opt/astrbot/data）
    ASTRBOT_CONTAINER   AstrBot 容器名          （缺省 astrbot）
"""
import io
import json
import os
import re
import subprocess
import sys

ASTRBOT_DATA = os.environ.get("ASTRBOT_DATA", "/opt/astrbot/data")
ASTRBOT_CONTAINER = os.environ.get("ASTRBOT_CONTAINER", "astrbot")
PLUG = ASTRBOT_DATA + "/plugins"
APPLY = "--apply" in sys.argv
# ★ 2026-09-20 实测：AstrBot 的 pip_installer 协程在下载大包时会**挂起不返回**，
#   而它连「已禁用」插件的依赖也会尝试安装（逐个 import 失败即触发），
#   所以要让启动顺畅必须把**全部**插件目录的依赖装齐 —— 加 --all 即不排除禁用插件。
INCLUDE_DISABLED = "--all" in sys.argv


def run(*a, timeout=900):
    r = subprocess.run(["sudo", "-n"] + list(a), capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def norm(name):
    name = name.strip()
    name = re.split(r"[<>=!;\[]", name)[0].strip()
    return name.replace("_", "-").lower()


# 1) 已禁用的插件（DB 里存的是 data.plugins.<name>.main 形式，取中间段）
disabled = set()
_DB_URI = "file:%s/data_v4.db?mode=ro" % ASTRBOT_DATA
out, _, _ = run("python3", "-c", (
    "import sqlite3,json;c=sqlite3.connect('" + _DB_URI + "',uri=True);"
    "r=c.execute(\"select value from preferences where scope='global' and key='inactivated_plugins'\").fetchone();"
    "print(json.dumps(json.loads(r[0])['val']) if r else '[]')"))
try:
    raw = json.loads(out)
    for x in raw:
        parts = str(x).split(".")
        if len(parts) >= 3 and parts[0] == "data":
            disabled.add(parts[2])
        else:
            disabled.add(str(x))
    print("已禁用插件(%d): %s" % (len(disabled), ", ".join(sorted(disabled))))
except Exception as e:
    print("读取禁用清单失败:", e, out[:200])

if INCLUDE_DISABLED:
    print("★ --all：不排除已禁用插件（避免启动时触发 pip_installer 挂起）")
    disabled = set()

# 2) 收集所有启用插件的 requirements
need = {}
for name in sorted(os.listdir(PLUG)):
    d = os.path.join(PLUG, name)
    if not os.path.isdir(d) or name in disabled:
        continue
    rf = os.path.join(d, "requirements.txt")
    if not os.path.exists(rf):
        continue
    pkgs = [norm(l) for l in io.open(rf, encoding="utf-8-sig", errors="replace")
            if l.strip() and not l.strip().startswith("#")]
    pkgs = [p for p in pkgs if p]
    if pkgs:
        need[name] = pkgs

print()
print("启用插件的依赖声明：")
for k, v in need.items():
    print("  %-34s %s" % (k, ", ".join(v)))

allpkgs = sorted({p for v in need.values() for p in v})

# 3) 容器内检测缺失（用 importlib.metadata，比 pip list 快）
code = (
    "import importlib.metadata as m, json\n"
    "names = %r\n"
    "miss = []\n"
    "for n in names:\n"
    "    try:\n"
    "        m.distribution(n)\n"
    "    except Exception:\n"
    "        miss.append(n)\n"
    "print(json.dumps(miss))\n" % allpkgs)
out, err, rc = run("docker", "exec", ASTRBOT_CONTAINER, "python", "-c", code)
missing = json.loads(out) if out.startswith("[") else []
print()
print("缺失的包（共 %d / 声明 %d）：%s" % (len(missing), len(allpkgs), ", ".join(missing) or "无"))

if not missing:
    print("\n依赖已齐，无需安装。")
    sys.exit(0)

if not APPLY:
    print("\n(DRY-RUN) 将安装以上包。加 --apply 执行。")
    sys.exit(0)

# 4) 用清华源安装（AstrBot 的安装协程会挂起，这里手动绕开）
cmd = ("pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --no-cache-dir "
       + " ".join(missing) + " > /tmp/fixdeps.log 2>&1")
print("\n开始安装（后台）...")
run("docker", "exec", "-d", ASTRBOT_CONTAINER, "sh", "-c", cmd)
print("已后台启动：docker exec %s sh -c '%s'" % (ASTRBOT_CONTAINER, cmd[:120]))
print("日志：/tmp/fixdeps.log")
