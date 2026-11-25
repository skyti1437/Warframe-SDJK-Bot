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
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any

if __package__:
    from .logging_compat import logger
else:
    from logging_compat import logger

PKG_DATA = Path(__file__).resolve().parent / "data"  # 随包分发的只读资源目录

_RUN: Path | None = None


def set_run_dir(path) -> None:
    """注入运行期数据目录（由插件入口调用；传 None 表示回到包内目录）。"""
    global _RUN
    _RUN = Path(path) if path else None


def run_dir() -> Path:
    """当前运行期数据目录；未注入时退化为包内数据目录（仅离线场景）。"""
    return _RUN if _RUN is not None else PKG_DATA


_Stamp = tuple[int, int, int, int]
_RUN_JSON_OK: dict[str, tuple[_Stamp, bool]] = {}
_JSON_CACHE: dict[str, tuple[_Stamp, Any]] = {}
_JSON_LOCKS = tuple(threading.Lock() for _ in range(32))


def _file_stamp(st) -> _Stamp:
    return st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size


@lru_cache(maxsize=256)
def _real_json_path(path: str, device: int, inode: int) -> str:
    """文件身份改变时重新解析符号链接，热查只需一次 stat。"""
    return os.path.realpath(path)


def _run_json_ok(p: Path, *, shared: bool = False) -> bool:
    try:
        return _cached_json(p, validate=True, share_data=shared)
    except OSError:
        return True  # stat 失败交给读者处理。


def read_path(name: str, *, root: Path | None = None, shared: bool = False) -> Path:
    """读路径：运行目录优先，缺则回退包内种子文件。

    ★ GPT 评估 P07（2026-10-21）：运行副本是**坏 JSON** 且包内有种子时也回落种子 ——
    原先坏副本会一直遮蔽种子（各处读法读坏返回空），表现为「部署成功但线上数据是空的」。
    只认 JSON 解析失败；结构校验不过（条数太少等）仍按原样读，由数据账本 / 面板提示。
    没有包内种子的文件（用户存档等）不受影响。
    """
    root = run_dir() if root is None else root
    p = root / name
    if root != PKG_DATA:
        seed = PKG_DATA / name
        if seed.exists() and (
            not p.exists() or (name.endswith(".json") and not _run_json_ok(p, shared=shared))
        ):
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


def _cached_json(path, *, validate=False, share_data=True):
    absolute = os.path.abspath(path)
    cache = _RUN_JSON_OK if validate else _JSON_CACHE
    while True:
        stamp = _file_stamp(os.stat(absolute))
        key = _real_json_path(absolute, *stamp[:2])
        hit = cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        with _JSON_LOCKS[hash(key) % len(_JSON_LOCKS)]:
            if _file_stamp(os.stat(absolute)) != stamp:
                continue
            hit = cache.get(key)
            if hit is not None and hit[0] == stamp:
                return hit[1]
            cached = _JSON_CACHE.get(key)
            if validate and cached is not None and cached[0] == stamp:
                _RUN_JSON_OK[key] = (stamp, True)
                return True
            try:
                data = json.loads(Path(key).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                if _file_stamp(os.stat(absolute)) != stamp:
                    continue
                if not validate:
                    raise
                logger.warning("[sdjk] 运行目录副本 JSON 损坏，已回落随包种子：%s（%s）", path, exc)
                _RUN_JSON_OK[key] = (stamp, False)
                return False
            if _file_stamp(os.stat(absolute)) != stamp:
                continue
            if share_data:
                _JSON_CACHE[key] = (stamp, data)
            if validate:
                _RUN_JSON_OK[key] = (stamp, True)
                return True
            return data


def load_json(path, *, required: bool = False) -> Any:
    """按文件身份与修改戳缓存只读 JSON，同一版本的读者共享一个对象。"""
    try:
        return _cached_json(path)
    except (OSError, ValueError):
        if required:
            raise
        return {}


def load_table(name: str, *, root: Path | None = None, required: bool = False) -> dict:
    """运行目录优先的共享只读字典。验证运行副本时复用同一次解码。"""
    data = load_json(read_path(name, root=root, shared=True), required=required)
    if isinstance(data, dict):
        return data
    if required:
        raise ValueError(f"{name} 顶层应为字典")
    return {}


def table_section(table: dict, key: str) -> dict:
    """取字典字段，缺项或格式不符时让读者走缺表降级。"""
    value = table.get(key)
    return value if isinstance(value, dict) else {}


def table_index(*names: str):
    """只保留当前这代共享表的派生索引，并发首查只构建一次。"""

    def decorate(build):
        sources = result = None
        lock = threading.Lock()

        @wraps(build)
        def get():
            nonlocal sources, result
            with lock:
                root = run_dir()
                current = tuple(load_table(name, root=root) for name in names)
                if sources is None or any(a is not b for a, b in zip(current, sources)):
                    result = build(*current)
                    sources = current
                return result

        return get

    return decorate


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
    "load_table",
    "table_section",
    "table_index",
]
