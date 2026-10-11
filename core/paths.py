# -*- coding: utf-8 -*-
"""运行期数据目录解析（AstrBot 插件规范：持久化数据必须存 ``data/plugin_data/<插件名>``）。

插件包目录对安装用户是**只读**的（且升级时会被整包覆盖），所以任何运行期写盘
都不能落在包内。规则：

* 插件入口 ``main.py`` 启动时用 :func:`set_run_dir` 注入 AstrBot 官方数据目录
  （``StarTools.get_data_dir()`` → ``data/plugin_data/<插件名>``）；
* **写**：一律落在运行目录（:func:`write_path`，自动建父目录）；
* **读**：运行目录优先；运行目录里没有时回退到包内 ``core/data`` 的同名**种子**
  文件（随包分发的静态数据，只读）—— 这样 `rotations.json` 这类「包内种子 +
  运行期刷新」的文件行为不变。

本模块只做路径拼装，不 import astrbot（离线脚本/单测可直接用）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

PKG_DATA = Path(__file__).resolve().parent / "data"  # 随包分发的只读资源目录

_RUN: Path | None = None


def set_run_dir(path) -> None:
    """注入运行期数据目录（由插件入口调用；传 None 表示回到包内目录）。"""
    global _RUN
    _RUN = Path(path) if path else None


def run_dir() -> Path:
    """当前运行期数据目录；未注入时退化为包内数据目录（仅离线场景）。"""
    return _RUN if _RUN is not None else PKG_DATA


def read_path(name: str) -> Path:
    """读路径：运行目录优先，缺则回退包内种子文件。"""
    p = run_dir() / name
    if _RUN is not None and not p.exists():
        seed = PKG_DATA / name
        if seed.exists():
            return seed
    return p


def write_path(name: str) -> Path:
    """写路径：运行目录（必要时创建父目录）。"""
    p = run_dir() / name
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


_JSON_CACHE: dict[str, tuple[int, int, Any]] = {}
_JSON_LOCK = threading.Lock()


def load_json(path) -> Any:
    """读**只读**静态 JSON（带进程级缓存，按 mtime/size 失效）。

    ⚠️ 返回的是**共享对象**：多个模块拿到的是同一个 dict/list，调用方**不得就地修改**
    （要改先 ``copy.deepcopy``）。只给逐个审计过「全部读者只读」的文件用
    （当前：``drops.json`` —— core/drops.py、core/search.py、core/wiki_intro.py）。
    缺文件 / 坏文件返回 ``{}``（与原各处读法的降级口径一致；不缓存失败结果）。
    """
    p = Path(path)
    key = str(p)
    try:
        st = p.stat()
    except OSError:
        return {}
    with _JSON_LOCK:
        hit = _JSON_CACHE.get(key)
        if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    with _JSON_LOCK:
        _JSON_CACHE[key] = (st.st_mtime_ns, st.st_size, data)
    return data


def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    """原子写文本：同目录临时文件写完再 ``os.replace``（写一半崩溃不会留下半截 JSON）。

    临时名带 pid，避免同进程外的并发写撞名；失败时清掉临时文件并原样抛出。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, p)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


__all__ = [
    "PKG_DATA",
    "set_run_dir",
    "run_dir",
    "read_path",
    "write_path",
    "atomic_write_text",
    "load_json",
]
