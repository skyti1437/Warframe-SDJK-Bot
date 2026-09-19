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

from pathlib import Path

PKG_DATA = Path(__file__).resolve().parent / "data"   # 随包分发的只读资源目录

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


__all__ = ["PKG_DATA", "set_run_dir", "run_dir", "read_path", "write_path"]
