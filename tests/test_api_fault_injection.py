# -*- coding: utf-8 -*-
"""api_client 上游故障注入测试：python3 tests/test_api_fault_injection.py

模拟上游数据源各类故障（超时 / 断网 / 非标准 JSON / 空数据 / 限速），
验证插件的容错重试、单飞缓存与降级提示是否符合预期。
所有 Mock 只打在系统边界（client._http.get），不碰内部实现。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from core.api_client import (
    WarframeAPIError,
    WarframeClient,  # noqa: E402
    parse_url_list,
    public_error_text,
)

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class FakeResponse:
    """模拟 httpx.Response 的最小替身。"""

    def __init__(self, status: int = 200, payload=None, bad_json: bool = False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"{self.status_code}", request=None, response=None)

    def json(self):
        if self._bad_json:
            raise json.JSONDecodeError("Expecting value", "<html>503</html>", 0)
        return self._payload


def make_client(**kw) -> WarframeClient:
    return WarframeClient(flare_enabled=False, **kw)


# ---------------------------------------------------------------- 1. 超时
async def t_timeout():
    calls = {"n": 0}

    async def boom(*a, **k):
        calls["n"] += 1
        raise httpx.ReadTimeout("read timed out")

    c = make_client()
    c._http.get = boom
    try:
        await c._fetch_json("https://x.test/ws", ttl=30, retries=2)
        check("超时最终抛 WarframeAPIError", False, "未抛出")
    except WarframeAPIError as e:
        check("超时最终抛 WarframeAPIError", True)
        check("超时错误信息可读（含 URL）", "x.test" in str(e), str(e))
    except Exception as e:
        check("超时最终抛 WarframeAPIError", False, f"抛出其他异常: {type(e).__name__}")
    check("超时按 retries=2 共尝试 3 次", calls["n"] == 3, f"实际 {calls['n']}")
    await c.close()


# ---------------------------------------------------------------- 2. 断网
async def t_connect_error():
    async def boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    c = make_client()
    c._http.get = boom
    try:
        await c._fetch_json("https://x.test/ws", ttl=30, retries=1)
        check("断网最终抛 WarframeAPIError", False, "未抛出")
    except WarframeAPIError as e:
        check("断网最终抛 WarframeAPIError", True)
        check("断网不泄露内部栈（仅外层包装信息）", "Traceback" not in str(e), str(e))
    await c.close()


# ---------------------------------------------------------------- 3. 非标准 JSON
async def t_bad_json():
    calls = {"n": 0}

    async def bad(*a, **k):
        calls["n"] += 1
        return FakeResponse(200, bad_json=True)

    c = make_client()
    c._http.get = bad
    try:
        await c._fetch_json("https://x.test/ws", ttl=30, retries=2)
        check("非标准 JSON 最终抛 WarframeAPIError", False, "未抛出")
    except WarframeAPIError:
        check("非标准 JSON 最终抛 WarframeAPIError", True)
    check("非标准 JSON 也走重试（3 次）", calls["n"] == 3, f"实际 {calls['n']}")
    await c.close()


# ---------------------------------------------------------------- 4. 404 不重试
async def t_404_no_retry():
    calls = {"n": 0}

    async def nf(*a, **k):
        calls["n"] += 1
        return FakeResponse(404)

    c = make_client()
    c._http.get = nf
    try:
        await c._fetch_json("https://x.test/gone", ttl=30, retries=2)
        check("404 抛 WarframeAPIError", False, "未抛出")
    except WarframeAPIError as e:
        check("404 抛 WarframeAPIError", True)
        check("404 提示「接口不存在」", "不存在" in str(e), str(e))
    check("404 不重试（1 次即失败）", calls["n"] == 1, f"实际 {calls['n']}")
    await c.close()


# ---------------------------------------------------------------- 5. 429 退避后成功
async def t_429_then_ok():
    calls = {"n": 0}

    async def limited(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            return FakeResponse(429)
        return FakeResponse(200, payload={"ok": True})

    c = make_client()
    c._http.get = limited
    data = await c._fetch_json("https://x.test/wm", ttl=30, retries=2)
    check("429×2 后成功返回数据", data == {"ok": True}, str(data))
    check("429 路径共尝试 3 次", calls["n"] == 3, f"实际 {calls['n']}")
    await c.close()


# ---------------------------------------------------------------- 6. 空数据降级
async def t_empty_data():
    async def empty_dict(*a, **k):
        return FakeResponse(200, payload={})

    c = make_client()
    c._http.get = empty_dict
    try:
        await c._de_bundle()
        check("DE 源返回空 dict → 抛降级提示", False, "未抛出")
    except WarframeAPIError as e:
        check("DE 源返回空 dict → 抛降级提示", "稍后重试" in str(e), str(e))

    async def empty_list(*a, **k):
        return FakeResponse(200, payload=[])

    c2 = make_client(worldsource="warframestat")
    c2._http.get = empty_list
    data = await c2.worldstate("pc", "fissures")
    check("warframestat 空数组 → 原样返回不崩", data == [], str(data))
    await c.close()
    await c2.close()


# ---------------------------------------------------------------- 7. 并发单飞：失败也共享
async def t_singleflight_failure():
    calls = {"n": 0}

    async def boom(*a, **k):
        # 必须真正 yield：真实网络 I/O 会让出事件循环，单飞窗口由此产生；
        # 同步抛异常的 mock 会串行执行，测不出单飞语义（2026-09-18 实测）
        await asyncio.sleep(0.01)
        calls["n"] += 1
        raise httpx.ConnectError("down")

    c = make_client()
    c._http.get = boom
    results = await asyncio.gather(
        *[c._fetch_json("https://x.test/ws", ttl=30, retries=0) for _ in range(10)],
        return_exceptions=True,
    )
    check(
        "10 并发失败全部拿到 WarframeAPIError",
        all(isinstance(r, WarframeAPIError) for r in results),
        str([type(r).__name__ for r in results]),
    )
    check("10 并发只回源 1 次（单飞）", calls["n"] == 1, f"实际 {calls['n']}")
    # 错误不缓存：稍后再调应重新回源
    try:
        await c._fetch_json("https://x.test/ws", ttl=30, retries=0)
    except WarframeAPIError:
        pass
    check("失败不缓存（再次调用重新回源）", calls["n"] == 2, f"实际 {calls['n']}")
    await c.close()


# ---------------------------------------------------------------- 8. 缓存命中：成功后不重复回源
async def t_cache_hit():
    calls = {"n": 0}

    async def ok(*a, **k):
        calls["n"] += 1
        return FakeResponse(200, payload={"v": 1})

    c = make_client()
    c._http.get = ok
    a = await c._fetch_json("https://x.test/cache", ttl=30)
    b = await c._fetch_json("https://x.test/cache", ttl=30)
    check("TTL 内第二次走缓存", calls["n"] == 1 and a == b, f"calls={calls['n']}")
    await c.close()


# ---------------------------------------------------------------- 9. 会话生命周期
async def t_close():
    c = make_client()
    check("初始会话未关闭", not c._http.is_closed)
    await c.close()
    check("close() 后会话关闭", c._http.is_closed)
    await c.close()  # 二次 close 不应抛
    check("close() 幂等（二次调用不抛）", True)


# ---------------------------------------------------------------- 9.5 单飞失败无 asyncio 警告（BUG-1 回归）
async def t_future_exception_consumed():
    """BUG-1：单飞失败后 GC 不应打印 "Future exception was never retrieved"。"""
    import gc

    captured: list[dict] = []
    loop = asyncio.get_running_loop()
    old = loop.get_exception_handler()
    loop.set_exception_handler(lambda l, ctx: captured.append(ctx))
    try:
        c = make_client()

        async def boom(*a, **k):
            await asyncio.sleep(0)
            raise httpx.ConnectError("down")

        c._http.get = boom
        try:
            await c._fetch_json("https://x.test/fut", ttl=30, retries=0)
        except WarframeAPIError:
            pass
        await c.close()
        for _ in range(3):  # 让 GC 有机会回收 future 并触发 handler
            gc.collect()
            await asyncio.sleep(0.05)
        hits = [x for x in captured if "never retrieved" in str(x.get("message", ""))]
        check("单飞失败无 never-retrieved 警告（BUG-1 回归）", not hits, str(hits[:1]))
    finally:
        loop.set_exception_handler(old)


# ---------------------------------------------------------------- 10. 配置项 parse_url_list 边界
def t_parse_url_list():
    check("空字符串 → 空列表", parse_url_list("") == [])
    check("None → 空列表", parse_url_list(None) == [])
    check(
        "逗号分隔",
        parse_url_list("http://a:1/v1, http://b:2/v1") == ["http://a:1/v1", "http://b:2/v1"],
    )
    check(
        "换行分隔",
        parse_url_list("http://a:1/v1\nhttp://b:2/v1") == ["http://a:1/v1", "http://b:2/v1"],
    )
    check("缺 /v1 自动补", parse_url_list("http://a:8191") == ["http://a:8191/v1"])
    check(
        "非 http 内容被丢弃",
        parse_url_list("not-a-url, ftp://x") == [],
        str(parse_url_list("not-a-url, ftp://x")),
    )
    check("去重", parse_url_list("http://a/v1, http://a/v1") == ["http://a/v1"])
    check("尾部斜杠归一", parse_url_list("http://a:8191/v1/") == ["http://a:8191/v1"])
    check("超长输入不崩", parse_url_list("http://a/" + "x" * 100000)[0].endswith("/v1"))


# ------------------------------------------- WM 上游抖动重试节奏（2026-10-26）
async def t_wm_retry_pacing():
    """WM 与别的数据源**不能**共用一套重试节奏（线上实测 2026-10-26）：

    * WM 的 429 是「约 90 s 窗口约 8 次」的**整窗**封禁 ⇒ 1.2 s 级重试必然再吃一次 429，
      只会把窗口拖长。⇒ WM 侧 429 **不重试**，直接抛可执行提示。
    * WM 超时/5xx 实测会持续 45–120 s ⇒ 退避必须逐次拉长（否则三次尝试全落在同一个坏窗口）。
    """
    # ① WM 429：一次就抛，且给的是可执行提示（不是「HTTP 429」这种术语）
    calls = {"n": 0}

    async def always_429(*a, **k):
        calls["n"] += 1
        return FakeResponse(429)

    c = make_client()
    c._http.get = always_429
    try:
        await c._fetch_json("https://x.test/wm429", ttl=30, retries=2, wm_rate_limit=True)
        check("WM 429 抛出", False, "未抛出")
    except WarframeAPIError as e:
        check("WM 429 抛出", True)
        check("WM 429 用可执行提示", str(e) == WarframeClient.WM_RATE_LIMIT_HINT, str(e))
        check("WM 429 不重试（只打 1 次）", calls["n"] == 1, f"实际 {calls['n']}")
    await c.close()

    # ② 非 WM 源（如社区快照镜像）的 429 仍保留原重试：它们恢复比 WM 快
    calls2 = {"n": 0}

    async def raw_429(*a, **k):
        calls2["n"] += 1
        return FakeResponse(429)

    c2 = make_client()
    c2._http.get = raw_429
    try:
        await c2._fetch_json("https://x.test/raw429", ttl=30, retries=2)
        check("非 WM 429 抛出", False, "未抛出")
    except WarframeAPIError:
        check("非 WM 429 仍重试（共 3 次）", calls2["n"] == 3, f"实际 {calls2['n']}")
    await c2.close()

    # ③ WM 超时：退避逐次拉长（测试里把常量压到 0.01 s，只验次数与口径）
    calls3 = {"n": 0}

    async def boom(*a, **k):
        calls3["n"] += 1
        raise httpx.ReadTimeout("read timed out")

    c3 = make_client()
    c3.WM_RETRY_BACKOFF_S = 0.01
    c3._http.get = boom
    try:
        await c3._fetch_json("https://x.test/wmto", ttl=30, retries=2, wm_rate_limit=True)
        check("WM 超时抛出", False, "未抛出")
    except WarframeAPIError as e:
        check("WM 超时抛出", True)
        check("WM 超时重试 3 次", calls3["n"] == 3, f"实际 {calls3['n']}")
        check("WM 超时提示可执行", "重发一次" in public_error_text(e), public_error_text(e))
    await c3.close()


# ---------------------------------------------------------------- 报错脱敏（2026-10-10 L2）
def t_public_error_text():
    """发到群里的报错不得带上游 URL / 内网地址 / httpx 原文，但要说清是哪个数据源。"""
    cases = [
        (
            "请求失败（https://api.warframe.market/v2/orders/item/x?a=1）："
            "All connection attempts failed",
            "warframe.market 暂时不可用（上游超时或无响应），请稍后重发一次",
        ),
        (
            "接口不存在或暂未开放：https://api.warframestat.us/pc/kuva",
            "warframestat.us 接口不存在或暂未开放，请稍后再试",
        ),
        (
            "FlareSolverr 不可用/求解失败（[Errno 111] Connect call failed "
            "('172.17.0.1', 8191)；http://172.17.0.1:8191/v1）",
            "FlareSolverr（CF 绕过代理） 暂时不可用（上游超时或无响应），请稍后重发一次",
        ),
        (
            "DE 紫卡周报拉取失败：Client error '403 Forbidden' for url "
            "'https://www-static.warframe.com/repos/weeklyRivensPC.json'",
            "DE 官方紫卡周报 暂时不可用（上游超时或无响应），请稍后重发一次",
        ),
        (
            "WM v2 接口错误：{'detail': 'x'}",
            "warframe.market 暂时不可用（上游超时或无响应），请稍后重发一次",
        ),
        # 插件自己写的降级提示：原样保留（不该被糊成通用句）
        ("warframe.market 限速（HTTP 429）", "warframe.market 限速（HTTP 429）"),
        (
            "FlareSolverr 已在配置里关闭（「启用 CF 绕过代理」= 否）",
            "FlareSolverr 已在配置里关闭（「启用 CF 绕过代理」= 否）",
        ),
        # 兜底：未知文本里混进的 URL / IP:端口 也要去掉
        ("奇怪错误 http://10.0.0.5:9000/x 与 192.168.1.2:80", "奇怪错误 上游接口 与 内部地址"),
    ]
    for raw, want in cases:
        got = public_error_text(WarframeAPIError(raw))
        check(f"脱敏：{raw[:24]}…", got == want, got)
        check(
            f"脱敏无 URL/IP：{raw[:24]}…",
            "http" not in got and "172.17" not in got and "8191" not in got,
            got,
        )


async def main():
    await t_timeout()
    await t_connect_error()
    await t_bad_json()
    await t_404_no_retry()
    await t_429_then_ok()
    await t_empty_data()
    await t_singleflight_failure()
    await t_cache_hit()
    await t_close()
    await t_future_exception_consumed()
    t_parse_url_list()
    await t_wm_retry_pacing()
    t_public_error_text()
    await t_wm_schema_drift()

    print()
    if FAILED:
        print(f"共 {len(FAILED)} 项失败：{FAILED}")
        sys.exit(1)
    print("api_client 故障注入：全部断言通过")


async def t_wm_schema_drift():
    """★ 2026-10-25（Astra 故障注入）：WM 结构缺字段不能被说成「没有在售单」。"""
    from core.api_client import WarframeClient as _WC

    async def _run(payload):
        c = _WC.__new__(_WC)
        c.wm_v2_base, c.language = "", "zh-hans"

        async def _fake(url, **kw):
            return payload

        c._fetch_json = _fake
        try:
            return await c.wm_orders("serration", "pc")
        except WarframeAPIError as exc:
            return str(exc)

    r1 = await _run({"apiVersion": "x"})
    check(
        "★ WM 缺 data 字段 ⇒ 报结构异常（不是零挂单）", isinstance(r1, str) and "结构异常" in r1, r1
    )
    r2 = await _run({"data": [{"id": 1, "user": {}}]})
    check("★ 订单全缺价格字段 ⇒ 报结构异常", isinstance(r2, str) and "结构异常" in r2, r2)
    r3 = await _run({"data": []})
    check("真实零挂单 ⇒ 空列表（不误报）", r3 == ([], {}), str(r3))
    r4 = await _run({"data": [{"platinum": 10, "type": "sell", "user": {}}]})
    check("正常订单照常解析", isinstance(r4, tuple) and r4[0][0]["platinum"] == 10, str(r4))


if __name__ == "__main__":
    asyncio.run(main())
