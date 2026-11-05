"""按实际渠道和任务保存视觉提示词；仅提供原版与自定义两种状态。

最大长度按 Python Unicode 字符数计，为 16000。草稿必须包含非空白字符；
保存保留原文，启用复制草稿，停用保留草稿。每次读写重新加载文件，
读取不落盘；写入以同目录跨进程文件锁和原子替换协调多实例及热重载。
Unix 使用 flock，Windows 使用 msvcrt 字节锁；不涉及模型调用。
坏文件拒绝管理读写，识别回落内置提示词并以每实例至多一次的日志告警。
"""

from __future__ import annotations

import json
import os
import tempfile
import sys
from contextlib import contextmanager
from pathlib import Path

from .logging_compat import logger

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

MAX_PROMPT_LENGTH = 16000


TASKS = {
    "riven_json": {
        "label": "紫卡主读",
        "contract": "只输出JSON对象：weapon为卡面武器名，stat_lines为逐条完整原文；保留正负号、小数、单位、倍率和未知位置，不做分析或换算。",
    },
    "riven_lines": {
        "label": "紫卡逐行读取",
        "contract": "逐行照抄词条，保留正负号、数值与单位；不要编号、列表或解释。",
    },
    "scan_main": {
        "label": "识卡主读",
        "contract": "只输出JSON对象，包含weapon、capacity、mods、panel、arcanes；每张mods含name、drain、color、polarity。panel保留单位、千分位、箭头两侧数值，并以damage_rows逐行记录伤害。",
    },
    "scan_title": {
        "label": "识卡标题",
        "contract": "只回答左上标题栏斜杠后的文字，单行、非空、不超过40字符；不要引号或解释。",
    },
    "scan_panel": {
        "label": "识卡面板",
        "contract": "逐行输出面板标签与数值，或输出含panel/rows的JSON；保留同一行所有数字、箭头两侧数值及单位，不推测补全。",
    },
    "scan_mods": {
        "label": "识卡模组",
        "contract": "每张已装备卡一行：名称 | 容量；包含姿态，忽略空槽与仓库。赋能名称可单独逐行附在末尾。",
    },
    "scan_tiles": {
        "label": "识卡槽位复读",
        "contract": "输出mods数组，每项包含tile区域编号、name卡面原名、drain容量数字、color数字颜色；空槽名称为null，不输出等级或推算值。",
    },
    "scan_arcanes": {
        "label": "识卡赋能",
        "contract": "只逐行照抄已装备的赋能名称，保留主要/次要/近战等前缀及间隔点；不要JSON、编号或解释。",
    },
}


class PromptConflict(ValueError):
    """提交的修订号已过期；调用方应重新读取后再操作。"""


class VisionPromptStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._warned = False

    @staticmethod
    def _key(provider_id, task):
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("实际渠道不能为空")
        if not isinstance(task, str) or task not in TASKS:
            raise ValueError("未知视觉任务")

    @staticmethod
    def _text(text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("提示词不能为空")
        if len(text) > MAX_PROMPT_LENGTH:
            raise ValueError("提示词超过16000字符")

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + ".lock").open("a+b") as lock:
            if sys.platform == "win32":
                # 字节锁覆盖偏移0；append模式初始化一个字节后必须重新定位。
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if sys.platform == "win32":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "providers": {}}
        except UnicodeError:
            raise ValueError("视觉提示词配置损坏，未改写文件") from None
        try:
            data = json.loads(raw)
            if data == {}:
                return {"version": 1, "providers": {}}
            if not isinstance(data, dict) or set(data) != {"version", "providers"}:
                raise ValueError("配置结构错误")
            if type(data["version"]) is not int or data["version"] != 1:
                raise ValueError("配置版本错误")
            if not isinstance(data["providers"], dict):
                raise ValueError("渠道结构错误")
            for pid, tasks in data["providers"].items():
                if not isinstance(tasks, dict):
                    raise ValueError("任务结构错误")
                for task, state in tasks.items():
                    self._key(pid, task)
                    if not isinstance(state, dict) or set(state) != {"revision", "draft", "active"}:
                        raise ValueError("方案状态错误")
                    if type(state["revision"]) is not int or state["revision"] < 1:
                        raise ValueError("修订号错误")
                    for field in ("draft", "active"):
                        if state[field] is not None:
                            self._text(state[field])
                    if state["active"] is not None and state["draft"] is None:
                        raise ValueError("启用方案缺少已保存草稿")
            return data
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("视觉提示词配置损坏，未改写文件") from exc

    @staticmethod
    def _view(data, provider_id, task):
        return dict(
            data["providers"]
            .get(provider_id, {})
            .get(task, {"revision": 0, "draft": None, "active": None})
        )

    def view(self, provider_id, task):
        self._key(provider_id, task)
        # 写入只用原子替换，因此无锁读取得到完整的旧快照或新快照。
        return self._view(self._read(), provider_id, task)

    def overview(self) -> list[dict]:
        """从单次磁盘快照列出已保存方案的状态，不暴露提示词正文。"""
        data = self._read()
        return [
            {
                "provider_id": provider_id,
                "task": task,
                "revision": state["revision"],
                "has_draft": state["draft"] is not None,
                "active": state["active"] is not None,
                "draft_matches_active": (
                    state["draft"] is not None
                    and state["active"] is not None
                    and state["draft"] == state["active"]
                ),
            }
            for provider_id, tasks in sorted(data["providers"].items())
            for task, state in sorted(tasks.items())
            if state["draft"] is not None or state["active"] is not None
        ]

    def effective(self, provider_id, task, original):
        self._key(provider_id, task)
        try:
            active = self.view(provider_id, task)["active"]
        except (OSError, ValueError, UnicodeError):
            if not self._warned:
                logger.warning("视觉提示词配置读取失败，回落内置提示词；请检查方案配置文件")
                self._warned = True
            return original
        return original if active is None else active

    def _write(self, data):
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=self.path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def apply(self, provider_id, task, action, revision, text=None):
        self._key(provider_id, task)
        if action not in ("save", "activate", "reset"):
            raise ValueError("未知操作")
        if type(revision) is not int or revision < 0:
            raise ValueError("修订号必须为非负整数")
        if action == "save":
            self._text(text)
        elif text is not None:
            raise ValueError("启用与停用不接收文本，请先保存草稿")
        with self._locked():
            data = self._read()
            state = self._view(data, provider_id, task)
            if revision != state["revision"]:
                raise PromptConflict("修订号冲突，请重新读取")
            if action == "save":
                state["draft"] = text
            elif action == "activate":
                self._text(state["draft"])
                state["active"] = state["draft"]
            else:
                state["active"] = None
            state["revision"] += 1
            data["providers"].setdefault(provider_id, {})[task] = state
            self._write(data)
            return dict(state)
