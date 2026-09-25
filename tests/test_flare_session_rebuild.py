# -*- coding: utf-8 -*-
"""FlareSolverr 失败自愈：会话重建（destroy→create）与错误归因（2026-09-25 事故）。

事故现场：持久会话 ``sdjk`` 的 Chromium 标签页崩了（FS 日志
``Error solving the challenge. Message: tab crashed``），插件随后**每小时拿坏会话
重试、连败一整天**。两处代码缺口：

1. 旧代码重建分支只发 ``sessions.create`` —— 对**已存在**的同名会话，create 是空操作，
   换不掉崩掉的 Chromium；必须先 ``sessions.destroy`` 再 create。
2. ``_solve`` 里连接级失败（``All connection attempts failed``）不含 ``session`` /
   ``solving`` 关键词 → 不触发重建；且候选地址循环里 ``last_err`` 会把 FS 的真实
   报错掩盖成更早候选的连接错误（日志因此指错方向）。

本测试用替身 HTTP 客户端逐条钉住：重建动作序列、120s maxTimeout、错误归因、
回收会话接口。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.api_client import WarframeAPIError, WarframeClient  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class FakeResponse:
    """模拟 httpx.Response 的最小替身（同 test_api_fault_injection 用法）。"""

    def __init__(self, status: int = 200, payload=None, bad_json: bool = False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=None)

    def json(self):
        if self._bad_json:
            raise json.JSONDecodeError("Expecting value", "<html>503</html>", 0)
        return self._payload


class ScriptedFlare:
    """按脚本逐次回答的 FlareSolverr 替身：step 为 payload dict 或待抛异常。"""

    def __init__(self, steps, base="http://fs.test/v1"):
        self.steps = list(steps)
        self.base = base
        self.calls: list[dict] = []

    async def post(self, base, json=None, timeout=None):
        payload = dict(json or {})
        self.calls.append({"base": base, **payload})
        if not self.steps:
            raise AssertionError(f"替身脚本用尽，多余调用：{payload}")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return FakeResponse(200, payload=step)


class RoutingFlare:
    """按候选地址路由的替身：含 bad 的地址一律抛异常，其余按脚本回答。"""

    def __init__(self, bad_exc, good_steps):
        self.bad_exc = bad_exc
        self.good = list(good_steps)
        self.calls: list[dict] = []

    async def post(self, base, json=None, timeout=None):
        payload = dict(json or {})
        self.calls.append({"base": base, **payload})
        if "bad" in base:
            raise self.bad_exc
        step = self.good.pop(0)
        if isinstance(step, BaseException):
            raise step
        return FakeResponse(200, payload=step)


def make_client(flare, urls=("http://fs.test/v1",)) -> WarframeClient:
    """真 client + 替身 post（保留 _http 的其他方法，close() 仍可用）。"""
    c = WarframeClient(flare_enabled=True, flare_urls=list(urls))
    c._http.post = flare.post
    return c


TAB_CRASHED = {"status": "error",
               "message": "Error solving the challenge. Message: tab crashed\n"
                          "  (Session info: chrome=152.0.7977.82)\n"}
CREATE_OK = {"status": "ok", "message": "Session created"}
DESTROY_OK = {"status": "ok", "message": "The session has been removed."}
PAGE_OK = {"status": "ok", "message": "",
           "solution": {"status": 200, "response": "<html>wiki</html>"}}


# ---------------------------------------------------------------- 1. tab crashed → destroy→create
async def t_tab_crashed_rebuilds():
    f = ScriptedFlare([TAB_CRASHED, DESTROY_OK, CREATE_OK, PAGE_OK])
    c = make_client(f)
    html = await c.fetch_via_flaresolver("https://wiki.test/x")
    cmds = [x["cmd"] for x in f.calls]
    check("tab crashed 后成功取到页面", html == "<html>wiki</html>", html)
    check("重建动作是 destroy→create（不是裸 create）",
          cmds == ["request.get", "sessions.destroy", "sessions.create", "request.get"],
          str(cmds))
    check("重建后重试仍用同一会话名 sdjk",
          all(x.get("session") == "sdjk" for x in f.calls), str(f.calls))
    check("maxTimeout 已抬到 120000（CF 解页实测 ~61s）",
          f.calls[0].get("maxTimeout") == 120000, str(f.calls[0]))
    await c.close()


# ---------------------------------------------------------------- 2. 连接级失败也触发重建
async def t_connection_error_rebuilds():
    exc = httpx.ConnectError("All connection attempts failed")
    f = ScriptedFlare([exc, DESTROY_OK, CREATE_OK, PAGE_OK])
    c = make_client(f)
    html = await c.fetch_via_flaresolver("https://wiki.test/y")
    cmds = [x["cmd"] for x in f.calls]
    check("连接级失败后重建并成功", html == "<html>wiki</html>", html)
    check("连接级失败的调用序列", cmds == ["request.get", "sessions.destroy",
                                          "sessions.create", "request.get"], str(cmds))
    await c.close()


# ---------------------------------------------------------------- 3. 非 JSON 响应也触发重建
async def t_bad_json_rebuilds():
    f = ScriptedFlare([httpx.ConnectError("Expecting value"),
                       DESTROY_OK, CREATE_OK, PAGE_OK])
    c = make_client(f)
    html = await c.fetch_via_flaresolver("https://wiki.test/z")
    check("非 JSON 响应路径重建后成功", html == "<html>wiki</html>", html)
    await c.close()


# ---------------------------------------------------------------- 4. 错误归因不被掩盖
async def t_real_error_not_masked():
    exc = httpx.ConnectError("All connection attempts failed")
    f = RoutingFlare(exc, [TAB_CRASHED, DESTROY_OK, CREATE_OK, TAB_CRASHED, TAB_CRASHED])
    c = make_client(f, urls=("http://bad.test/v1", "http://good.test/v1"))
    try:
        await c.fetch_via_flaresolver("https://wiki.test/w")
        check("两个候选都失败时抛 WarframeAPIError", False, "未抛出")
    except WarframeAPIError as e:
        msg = str(e)
        check("失败信息含 FS 的真实原因 tab crashed", "tab crashed" in msg, msg)
        check("失败信息不再被连接错误顶包", "attempts failed" not in msg, msg)
    await c.close()


# ---------------------------------------------------------------- 5. 会话回收接口
async def t_recycle_session():
    f = ScriptedFlare([DESTROY_OK, CREATE_OK])
    c = make_client(f)
    ok = await c.recycle_flare_session()
    cmds = [x["cmd"] for x in f.calls]
    check("回收会话返回 True", ok is True)
    check("回收动作是 destroy→create", cmds == ["sessions.destroy", "sessions.create"], str(cmds))
    check("回收后记住可用地址", c._flare_url == "http://fs.test/v1", c._flare_url)
    await c.close()

    c2 = WarframeClient(flare_enabled=False)
    check("未启用 FlareSolverr 时回收返回 False（不抛）",
          await c2.recycle_flare_session() is False)
    await c2.close()


async def main():
    await t_tab_crashed_rebuilds()
    await t_connection_error_rebuilds()
    await t_bad_json_rebuilds()
    await t_real_error_not_masked()
    await t_recycle_session()
    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} -> {FAILED}")
        sys.exit(1)
    print("全部通过（FlareSolverr 会话重建与错误归因）")


if __name__ == "__main__":
    asyncio.run(main())
