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
    wm_retry_after_seconds,
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
            "warframe.market 暂时不可用（连接或响应异常），请稍后重发一次",
        ),
        (
            "接口不存在或暂未开放：https://api.warframestat.us/pc/kuva",
            "warframestat.us 接口不存在或暂未开放，请稍后再试",
        ),
        (
            "FlareSolverr 不可用/求解失败（[Errno 111] Connect call failed "
            "('172.17.0.1', 8191)；http://172.17.0.1:8191/v1）",
            "FlareSolverr（CF 绕过代理） 暂时不可用（连接或响应异常），请稍后重发一次",
        ),
        (
            "DE 紫卡周报拉取失败：Client error '403 Forbidden' for url "
            "'https://www-static.warframe.com/repos/weeklyRivensPC.json'",
            "DE 官方紫卡周报 拒绝访问（HTTP 403），请联系管理员检查数据源连接",
        ),
        (
            "WM v2 接口错误：{'detail': 'x'}",
            "warframe.market 暂时不可用（连接或响应异常），请稍后重发一次",
        ),
        (
            "请求失败（https://api.warframe.com/cdn/worldState.php）："
            "Client error '403 Forbidden' for url 'https://api.warframe.com/cdn/worldState.php'",
            "DE 官方接口 拒绝访问（HTTP 403），请联系管理员检查数据源连接",
        ),
        (
            "请求失败（https://api.warframe.com/cdn/worldState.php）：ReadTimeout",
            "DE 官方接口 请求超时，请稍后重发一次",
        ),
        (
            "请求失败（https://api.warframe.com/cdn/worldState.php）：Server error '503 Service Unavailable'",
            "DE 官方接口 服务异常（HTTP 503），请稍后重发一次",
        ),
        (
            "请求失败（https://api.warframe.com/cdn/worldState.php）：HTTP 429",
            "DE 官方接口 请求受限（HTTP 429），请稍后再试",
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


class VirtualWMClock:
    def __init__(self):
        import time

        self.value = time.monotonic()

    def monotonic(self):
        return self.value

    async def sleep(self, seconds):
        self.value += max(0.0, seconds)


async def virtual_wm_test(test):
    """让真实WM出口完整运行，同时把多组拍卖的补额等待推进为虚拟时间。"""
    import time
    from types import SimpleNamespace
    from unittest.mock import patch

    clock = VirtualWMClock()
    real_get = httpx.AsyncClient.get

    async def virtual_http_get(client, *args, **kwargs):
        try:
            return await real_get(client, *args, **kwargs)
        finally:
            # 假HTTP保留1ms往返耗时，避免连续成功拍卖成为零RTT的15秒边界。
            clock.value += 0.001

    with (
        patch("core.api_client.time", SimpleNamespace(monotonic=clock.monotonic, time=time.time)),
        patch("core.api_client.asyncio.sleep", clock.sleep),
        patch.object(httpx.AsyncClient, "get", virtual_http_get),
    ):
        await test()


async def main():
    await t_wm_bounded_gate()
    await t_wm_origin_normalization()
    await t_wm_waiting_auction_and_http_policy()
    await t_wm_slot_cancellation()
    await t_wm_shared_governor()
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
    await t_wm_rank_cache_identity()
    await t_loopback_fault_recovery()
    await t_cache_cancellation_recovery()
    await t_close_during_request_wait()
    await t_wm_bad_envelope_cache_recovery()
    await virtual_wm_test(t_wm_v1_content_recovery)
    await virtual_wm_test(t_wm_v1_shared_failure)
    await virtual_wm_test(t_wm_v1_auction_directions)
    await t_wm_auction_failure_boundaries()
    await t_wm_auction_attribute_fallback()
    await t_wm_auction_cooldown()
    await t_de_community_fallback()
    await t_manual_crawl_lifecycle()
    await t_manual_crawl_round_results()
    await t_terminate_closes_admission_before_wait()

    print()
    if FAILED:
        print(f"共 {len(FAILED)} 项失败：{FAILED}")
        sys.exit(1)
    print("api_client 故障注入：全部断言通过")


async def t_wm_rank_cache_identity():
    """真实 httpx 请求构造：等级/平台/语言隔离，等价请求复用且凭据不进键。"""
    seen = []

    def respond(request):
        rank = request.url.params.get("rank")
        seen.append((request.url.path, dict(request.headers)))
        order = {"type": "sell", "platinum": 100 + int(rank or 0), "rank": int(rank or 0)}
        data = {"sell": [order], "buy": []} if rank is not None else [order]
        return httpx.Response(200, json={"data": data})

    c = make_client()
    await c._http.aclose()
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        low, _ = await c.wm_orders("serration", "pc", rank=0)
        high, _ = await c.wm_orders("serration", "pc", rank=10)
        again, _ = await c.wm_orders("serration", "pc", rank=10)
        check(
            "WM 等级0/10请求分别返回对应挂单", low[0]["mod_rank"] == 0 and high[0]["mod_rank"] == 10
        )
        check("WM 同等级复用缓存", again == high and len(seen) == 2, str(len(seen)))
        await c._wm_v2("/orders/item/serration/top", ttl=60, platform="pc", params={"rank": 10})
        check(
            "WM query 整数/字符串等价值复用且等级不进请求头",
            len(seen) == 2 and all("rank" not in h for _, h in seen),
        )
        await c.wm_orders("serration", "ps4", rank=10)
        c.language = "en"
        await c.wm_orders("serration", "pc", rank=10)
        check("WM 平台与语言分别隔离", len(seen) == 4)
        normal, _ = await c.wm_orders("serration", "pc")
        await c.wm_orders("serration", "pc")
        check(
            "WM 默认订单路径与同请求复用保持",
            normal[0]["mod_rank"] == 0
            and len(seen) == 5
            and seen[-1][0].endswith("/orders/item/serration")
            and "rank" not in seen[-1][1],
        )
        path = "/orders/item/serration/top"
        await c._wm_v2(
            path,
            ttl=60,
            platform="pc",
            params={"rank": 10},
            headers={"Authorization": "Bearer fixture-one", "X-Filter": "a"},
        )
        await c._wm_v2(
            path,
            ttl=60,
            platform="pc",
            params={"rank": "10"},
            headers={"x-filter": "a", "authorization": "Bearer fixture-one"},
        )
        await c._wm_v2(
            path,
            ttl=60,
            platform="pc",
            params={"rank": 10},
            headers={"authorization": "Bearer fixture-two", "X-Filter": "a"},
        )
        check("WM 自定义头顺序/大小写等价复用，值变化隔离", len(seen) == 7)
        check(
            "WM 缓存键不含凭据原文",
            all("fixture-" not in key and "Bearer" not in key for key in c.cache._store),
        )
    finally:
        await c.close()


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


async def t_loopback_fault_recovery():
    """真实 TCP + 单连接池：故障后同一客户端能继续请求且连接全部关闭。"""
    calls, handlers, writers = {}, set(), set()
    entered, disconnected = asyncio.Event(), asyncio.Event()

    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        writers.add(writer)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            path = header.split(b" ")[1].decode()
            calls[path] = calls.get(path, 0) + 1
            if path in ("/timeout", "/cancel") and calls[path] == 1:
                entered.set()
                await reader.read()  # 客户端超时/取消必须关闭 socket，不能占满单连接池。
                disconnected.set()
                return
            status, body = 200, b'{"ok":true}'
            if calls[path] == 1 and path == "/bad-json":
                body = b"<html>broken</html>"
            if calls[path] == 1 and path == "/limited":
                status, body = 429, b"{}"
            writer.write(
                f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            writers.discard(writer)
            handlers.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    c = make_client()
    await c._http.aclose()
    c._http = httpx.AsyncClient(timeout=1, trust_env=False, limits=httpx.Limits(max_connections=1))
    try:
        for path in ("/timeout", "/bad-json", "/limited"):
            try:
                await c._fetch_json(base + path, ttl=30, retries=0, timeout=0.05)
                check(f"真实HTTP {path} 首次故障有明确异常", False)
            except WarframeAPIError:
                check(f"真实HTTP {path} 首次故障有明确异常", True)
            recovered = await asyncio.wait_for(c._fetch_json(base + path, ttl=30, retries=0), 2)
            cached = await c._fetch_json(base + path, ttl=30, retries=0)
            check(
                f"真实HTTP {path} 后同客户端恢复并缓存，未占死连接池",
                recovered == cached == {"ok": True} and calls[path] == 2,
            )
        await asyncio.wait_for(disconnected.wait(), 2)
        check("真实读超时后服务端观察到连接断开", True)
        entered.clear()
        disconnected.clear()
        task = asyncio.create_task(c._fetch_json(base + "/cancel", ttl=30, retries=0))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(disconnected.wait(), 2)
            check(
                "真实HTTP取消传播且释放连接和单飞登记",
                isinstance(result[0], asyncio.CancelledError) and not c.cache._inflight,
            )
            recovered = await asyncio.wait_for(
                c._fetch_json(base + "/cancel", ttl=30, retries=0), 2
            )
            check("真实HTTP取消后下一次同键调用成功", recovered == {"ok": True})
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await c.close()
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if handlers:
            await asyncio.wait_for(asyncio.gather(*tuple(handlers), return_exceptions=True), 2)
    check(
        "loopback探针收尾无连接、处理任务或单飞残留",
        not handlers and not writers and not c.cache._inflight,
    )


async def t_cache_cancellation_recovery():
    """取消等待者不影响生产者；取消生产者唤醒全体且下次可回源。"""
    from core.cache import TTLCache

    for cancel_creator in (False, True):
        cache = TTLCache()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"value": calls}

        creator = asyncio.create_task(cache.get_or_fetch("key", 30, fetch))
        await asyncio.wait_for(entered.wait(), 2)
        waiter = asyncio.create_task(cache.get_or_fetch("key", 30, fetch))
        survivor = asyncio.create_task(cache.get_or_fetch("key", 30, fetch))
        await asyncio.sleep(0)  # 两个等待者均已进入 shield(fut)。
        tasks = (creator, waiter, survivor)
        try:
            tasks[0 if cancel_creator else 1].cancel()
            if not cancel_creator:
                await asyncio.gather(waiter, return_exceptions=True)
                check(
                    "取消单个缓存等待者不取消共享future", not creator.done() and not survivor.done()
                )
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
            if cancel_creator:
                check(
                    "取消缓存生产者唤醒全部等待者",
                    all(isinstance(x, asyncio.CancelledError) for x in results),
                )
            else:
                check(
                    "缓存其余等待者正常收到唯一回源结果",
                    results[0] == results[2] == {"value": 1} and calls == 1,
                )
            check("缓存取消后锁与单飞登记释放", not cache._lock.locked() and not cache._inflight)
            result = await asyncio.wait_for(cache.get_or_fetch("key", 30, fetch), 2)
            check("缓存取消后同键可继续使用", result == {"value": 2 if cancel_creator else 1})
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def t_close_during_request_wait():
    """真实请求入口在重试/限速等待时卸载，后续不能再访问关闭的连接池。"""
    import time
    from unittest.mock import patch

    real_sleep = asyncio.sleep
    for mode in ("timeout", "limited", "wm-throttle"):
        c = make_client(worldsource="warframestat")
        await c._http.aclose()
        entered, release = asyncio.Event(), asyncio.Event()
        sends = 0

        async def transport(request):
            if mode == "timeout":
                raise httpx.ReadTimeout("fixture", request=request)
            return httpx.Response(429, json={}, request=request)

        c._http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        original_get = c._http.get

        async def counted_get(*args, **kwargs):
            nonlocal sends
            sends += 1
            return await original_get(*args, **kwargs)

        c._http.get = counted_get
        if mode == "wm-throttle":
            c._wm_last = time.monotonic()

        async def paused_sleep(_delay):
            entered.set()
            await release.wait()

        def request():
            return c.wm_items() if mode == "wm-throttle" else c.worldstate("pc", "fissures")

        tasks = []
        try:
            with patch("core.api_client.asyncio.sleep", paused_sleep):
                tasks.append(asyncio.create_task(request()))
                await asyncio.wait_for(entered.wait(), 2)
                tasks.append(asyncio.create_task(request()))
                await real_sleep(0)
                await c.close()
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
            check(
                f"{mode} 等待期间卸载，创建者与跟随者均收到关闭提示",
                all(isinstance(r, WarframeAPIError) and "已关闭" in str(r) for r in results),
                str(results),
            )
            check(
                f"{mode} 卸载后不再调用 HTTP 连接池",
                sends == (0 if mode == "wm-throttle" else 1),
                str(sends),
            )
            check(
                f"{mode} 卸载后单飞登记清空且失败不入缓存",
                not c.cache._inflight and not c.cache._store,
            )
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await c.close()


async def t_wm_bad_envelope_cache_recovery():
    """实际 HTTP transport：坏信封不能阻塞上游恢复，旧坏缓存仅失效对应身份。"""
    for label, bad in (("缺data", {}), ("error", {"error": "temporary failure"})):
        calls = []
        entered, release = asyncio.Event(), asyncio.Event()
        valid = {"data": [{"id": "probe", "slug": "probe", "i18n": {}}]}

        async def respond(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                entered.set()
                await release.wait()
                return httpx.Response(200, json=bad)
            return httpx.Response(200, json=valid)

        c = make_client()
        await c._http.aclose()
        c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        tasks = []
        try:
            tasks.append(asyncio.create_task(c.wm_items()))
            await entered.wait()
            tasks.append(asyncio.create_task(c.wm_items()))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            check(
                f"WM{label}：并发调用均报告错误且只回源一次",
                all(isinstance(r, WarframeAPIError) for r in results) and len(calls) == 1,
            )
            check(
                f"WM{label}：失败不入缓存且单飞登记释放",
                not c.cache._store and not c.cache._inflight,
            )
            recovered = await asyncio.gather(c.wm_items(), c.wm_items())
            check(
                f"WM{label}：上游恢复后并发查询立即成功",
                all(r[0]["url_name"] == "probe" for r in recovered) and len(calls) == 2,
            )
            check(
                f"WM{label}：成功响应继续命中缓存",
                (await c.wm_items()) == recovered[0] and len(calls) == 2,
            )
            key = next(iter(c.cache._store))
            # 夹具模拟升级前已写入的坏缓存；相邻身份与非WM键必须保留。
            await c.cache.set(key, bad, 86400)
            await c.cache.set(key + "-other", {"unchanged": True}, 86400)
            await c.cache.set("other-source", {"unchanged": True}, 86400)
            again = await c.wm_items()
            check(
                f"WM{label}：旧坏缓存同次调用完成重新回源",
                again == recovered[0] and len(calls) == 3,
            )
            check(
                f"WM{label}：只清理坏key保留其它缓存",
                (await c.cache.get(key + "-other")) == {"unchanged": True}
                and (await c.cache.get("other-source")) == {"unchanged": True},
            )
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await c.close()
    c = make_client()
    await c._http.aclose()
    requests = []

    def non_wm(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"error": "source-specific field"})

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(non_wm))
    try:
        first = await c._fetch_json("https://source.invalid/test", ttl=60)
        second = await c._fetch_json("https://source.invalid/test", ttl=60)
        check(
            "非WM请求不套用WM信封校验且正常缓存",
            first == second == {"error": "source-specific field"} and len(requests) == 1,
        )
        for data in (None, [], {"sell": [], "buy": []}):
            c._validate_wm_v2({"data": data})
        check("WM合法data形态不额外收紧", True)
    finally:
        await c.close()


async def t_manual_crawl_lifecycle():
    """真实启动器/爬取入口：关闭守卫、任务异常回收与取消传播，零网络。"""
    from unittest.mock import AsyncMock, patch

    from core import api_client as api

    for state in ("closing", "closed"):
        c = make_client()
        await c._http.aclose()
        calls = []

        def transport(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"data": []})

        c._http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        if state == "closing":
            c.begin_close()
        else:
            await c.close()
        try:
            for name in ("_fetch_json", "crawl_wm_ranks", "crawl_riven_realtime"):
                try:
                    await (
                        c._fetch_json("https://probe.invalid/data", ttl=0)
                        if name == "_fetch_json"
                        else getattr(c, name)()
                    )
                except WarframeAPIError:
                    check(f"生命周期{state}/{name}：拒绝新外呼与抓取", True)
                else:
                    check(f"生命周期{state}/{name}：拒绝新外呼与抓取", False)
            for name in ("start_rank_crawl", "start_riven_rt_crawl"):
                try:
                    getattr(c, name)()
                except WarframeAPIError:
                    check(f"生命周期{state}/{name}：拒绝后台启动", True)
                else:
                    check(f"生命周期{state}/{name}：拒绝后台启动", False)
            check(
                f"生命周期{state}：不发请求也不分配手动任务",
                calls == []
                and not hasattr(c, "_rank_crawl_task")
                and not hasattr(c, "_riven_rt_crawl_task"),
            )
        finally:
            await c.close()

    for kind in ("rank", "riven"):
        c = make_client()
        attr = "_rank_crawl_task" if kind == "rank" else "_riven_rt_crawl_task"
        start = c.start_rank_crawl if kind == "rank" else c.start_riven_rt_crawl
        if kind == "rank":
            c.wm_items = AsyncMock(side_effect=WarframeAPIError("synthetic-private-token"))
        else:
            c.de_weekly_rivens = AsyncMock(side_effect=WarframeAPIError("synthetic-private-token"))
        try:
            with patch.object(api.logger, "warning") as warning:
                start()
                task = getattr(c, attr)
                await asyncio.wait({task}, timeout=2)
                await asyncio.sleep(0)
                logged = str(warning.call_args_list)
                check(
                    f"手动{kind}：完成回调及时取回异常并只记录类型",
                    task.done()
                    and not getattr(task, "_log_traceback", True)
                    and "WarframeAPIError" in logged
                    and "synthetic-private-token" not in logged,
                )
                outcome = task._wf_crawl_status
                check(
                    f"手动{kind}：异常结果绑定本任务",
                    outcome["result"] == "error"
                    and outcome["last_error"] == "WarframeAPIError"
                    and outcome["last_finished"] >= outcome["last_attempt"],
                )
                task.exception()
        finally:
            await c.close()

        c = make_client()
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            return [] if kind == "rank" else {"entries": []}

        if kind == "rank":
            c.wm_items = blocked
        else:
            c.de_weekly_rivens = blocked
        start = c.start_rank_crawl if kind == "rank" else c.start_riven_rt_crawl
        task = None
        try:
            with patch.object(api.logger, "warning") as warning:
                start()
                task = getattr(c, attr)
                await asyncio.wait_for(entered.wait(), 2)
                start()
                check(f"手动{kind}：仍运行时重复启动沿用同一任务", getattr(c, attr) is task)
                task.cancel()
                result = (await asyncio.gather(task, return_exceptions=True))[0]
                check(
                    f"手动{kind}：主动取消保持取消且不记异常",
                    isinstance(result, asyncio.CancelledError)
                    and task.cancelled()
                    and warning.call_count == 0,
                )
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await c.close()

    c = make_client()
    c.wm_items = AsyncMock(return_value=[])
    try:
        c.start_rank_crawl()
        task = c._rank_crawl_task
        task.cancel()  # 不让出事件循环，包装协程尚未执行。
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        meta = task._wf_crawl_status
        check(
            "手动任务未开工取消：不调用上游且补齐结束状态和时间",
            isinstance(result, asyncio.CancelledError)
            and c.wm_items.await_count == 0
            and meta["state"] == "cancelled"
            and meta["result"] is None
            and meta["last_finished"] >= meta["last_attempt"],
        )
    finally:
        await c.close()


async def t_terminate_closes_admission_before_wait():
    """提取真实卸载方法，验证等待旧资源前同步封住任务入口及旧适配器兼容。"""
    import ast
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    source = Path(__file__).resolve().parent.parent / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "terminate"
    )
    cls = ast.ClassDef(name="Host", bases=[], keywords=[], body=[method], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    scope = {"asyncio": asyncio, "logger": Mock()}
    exec(compile(module, str(source), "exec"), scope)
    for legacy in (False, True):
        c = SimpleNamespace(close=AsyncMock()) if legacy else make_client()
        host = scope["Host"]()
        entered, release = asyncio.Event(), asyncio.Event()

        async def stop():
            entered.set()
            await release.wait()

        host.client = c
        host.push = SimpleNamespace(stop=stop)
        work = asyncio.create_task(host.terminate())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            check(
                f"卸载入口{'旧适配器' if legacy else '真实client'}：等待资源前标closing",
                c._closing is True and not work.done(),
            )
            if not legacy:
                try:
                    c.start_rank_crawl()
                except WarframeAPIError:
                    check("卸载等待中群刷新不能启动新任务", not hasattr(c, "_rank_crawl_task"))
                else:
                    check("卸载等待中群刷新不能启动新任务", False)
            release.set()
            await asyncio.wait_for(work, 2)
        finally:
            release.set()
            await asyncio.gather(work, return_exceptions=True)
            await c.close()


async def t_manual_crawl_round_results():
    """真实爬取本轮结果进入当前Task：空源、覆盖率、落盘失败与正常整数返回。"""
    import tempfile
    from functools import partial
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from core import api_client as api, paths
    from core.webpanel import WebPanel

    previous = paths._RUN
    try:
        with tempfile.TemporaryDirectory(prefix="wf-manual-round-") as directory:
            paths.set_run_dir(directory)
            for kind in ("rank", "riven"):
                for mode, want in (
                    ("success", "ok"),
                    ("partial", "degraded"),
                    ("fetch_fail", "degraded"),
                    ("save_fail", "error"),
                    ("empty", "degraded"),
                ):
                    c = make_client()
                    c.RIVEN_RT_GAP_S = c.RIVEN_RT_FAIL_BACKOFF_S = 0
                    c.wm_items = AsyncMock(
                        return_value=[]
                        if mode == "empty"
                        else [{"url_name": "probe", "tags": ["mod"], "zh": "probe", "en": "Probe"}]
                    )
                    stats = {
                        "payload": {
                            "statistics_closed": {
                                "48h": [{"median": 10, "volume": 1}],
                                "90days": [],
                            }
                        }
                    }
                    c.wm_statistics = (
                        AsyncMock(side_effect=WarframeAPIError("synthetic failure"))
                        if mode == "fetch_fail"
                        else AsyncMock(return_value=stats)
                    )
                    c.de_weekly_rivens = AsyncMock(return_value={"entries": []})
                    c.wm_riven_weapons = AsyncMock(return_value=[])
                    c.wm_riven_auctions = (
                        AsyncMock(side_effect=WarframeAPIError("synthetic failure"))
                        if mode == "fetch_fail"
                        else AsyncMock(
                            return_value=[{"buyout_price": 10, "owner": {"status": "online"}}]
                        )
                    )
                    name = api.RANKS_NAME if kind == "rank" else api.RIVEN_RT_NAME
                    path = paths.write_path(name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("{}")
                    if mode == "save_fail":
                        c._save_json_file = lambda *args: False
                    if mode == "partial":
                        # 无参适配器限制本轮工作量，手动启动器仍按原签名调用。
                        if kind == "rank":
                            c.crawl_wm_ranks = partial(c.crawl_wm_ranks, limit=0)
                        else:
                            c.crawl_riven_realtime = partial(c.crawl_riven_realtime, limit=0)
                    panel = WebPanel(SimpleNamespace(client=c), "probe")
                    try:
                        with patch.object(
                            api,
                            "riven_realtime_targets",
                            return_value=[] if mode == "empty" else ["probe"],
                        ):
                            if kind == "rank":
                                c.start_rank_crawl()
                                task = c._rank_crawl_task
                            else:
                                c.start_riven_rt_crawl(force=True)
                                task = c._riven_rt_crawl_task
                            value = await asyncio.wait_for(task, 3)
                            outcome = dict(task._wf_crawl_status)
                            check(
                                f"手动本轮{kind}/{mode}：保持整数接口并记录准确结果",
                                type(value) is int and outcome["result"] == want,
                                str(outcome),
                            )
                            row = next(
                                r for r in panel.background_rows() if r["key"] == "manual_" + kind
                            )
                            check(
                                f"手动本轮{kind}/{mode}：面板状态与结果一致",
                                row["result"] == want
                                and row["status"]
                                == {"ok": "ok", "degraded": "warn", "error": "stale"}[want],
                                str(row),
                            )
                            c.wm_statistics = AsyncMock(return_value=stats)
                            c.wm_riven_auctions = AsyncMock(return_value=[])
                            c.wm_items = AsyncMock(return_value=[])
                            # 自动调用公共crawl不会借用或覆盖已完成手动任务的结果字典。
                            await (
                                c.crawl_wm_ranks(limit=0)
                                if kind == "rank"
                                else c.crawl_riven_realtime(limit=0)
                            )
                            check(
                                f"手动本轮{kind}/{mode}：后续直接抓取不污染结果",
                                task._wf_crawl_status == outcome,
                            )
                    finally:
                        await c.close()
    finally:
        paths.set_run_dir(previous)


async def t_wm_v1_content_recovery():
    """v1 真实消费入口：错误信封不是空结果，恢复与旧缓存淘汰只影响当前身份。"""
    good_row = {"datetime": "2026-10-30T00:00:00Z", "median": 12, "volume": 7}
    auction = {"id": "fixture-auction", "buyout_price": 120}
    for kind in ("stats", "riven", "lich"):
        good = (
            {"payload": {"statistics_closed": {"48hours": [good_row], "90days": []}}}
            if kind == "stats"
            else {"payload": {"auctions": [auction]}}
        )
        wrong_rows = (
            {"payload": {"statistics_closed": {"48hours": ["broken-row"]}}}
            if kind == "stats"
            else {"payload": {"auctions": ["broken-row"]}}
        )
        empty = (
            {"payload": {"statistics_closed": {}}}
            if kind == "stats"
            else {"payload": {"auctions": []}}
        )
        for label, bad in (
            ("error", {"error": {"detail": "fixture 400 should not mark unsupported"}}),
            ("缺payload", {}),
            ("列表信封", []),
            ("空信封", None),
            ("坏行", wrong_rows),
        ):
            calls = []
            payload = bad

            def respond(request):
                calls.append(request.url.path)
                return httpx.Response(
                    200, content=json.dumps(payload), headers={"content-type": "application/json"}
                )

            c = make_client()
            await c._http.aclose()
            c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))

            def call():
                if kind == "stats":
                    return c.wm_statistics("fixture")
                if kind == "riven":
                    return c.wm_riven_auctions("fixture", sorts=("price_asc",))
                return c.wm_lich_auctions("fixture")

            try:
                first = (await asyncio.gather(call(), return_exceptions=True))[0]
                check(
                    f"v1 {kind}/{label}：结构错误不当空结果、不重试、不缓存",
                    isinstance(first, WarframeAPIError)
                    and "结构异常" in str(first)
                    and len(calls) == 1
                    and not c.cache._store
                    and not c.cache._inflight,
                    str(first),
                )
                if kind == "lich":
                    check(
                        f"v1 {kind}/{label}：坏内容不误记无拍卖类目",
                        not c.lich_unsupported("fixture"),
                    )
                payload = good
                recovered = await call()
                check(
                    f"v1 {kind}/{label}：上游恢复后立即成功并缓存",
                    bool(recovered.get("h48") if kind == "stats" else recovered)
                    and await call() == recovered
                    and len(calls) == 2,
                )
                key = next(iter(c.cache._store))
                await c.cache.set(key, bad, 86400)
                await c.cache.set(key + "-neighbor", {"keep": True}, 86400)
                await c.cache.set("other-source", {"keep": True}, 86400)
                results = await asyncio.gather(call(), call())
                check(
                    f"v1 {kind}/{label}：旧坏缓存同次恢复、并发仅回源一次",
                    results == [recovered, recovered] and len(calls) == 3,
                )
                check(
                    f"v1 {kind}/{label}：相邻身份和其它源缓存保留",
                    await c.cache.get(key + "-neighbor") == {"keep": True}
                    and await c.cache.get("other-source") == {"keep": True},
                )
                await c.cache.set(key, None, 0)
                payload = empty
                zero = await call()
                check(
                    f"v1 {kind}/{label}：合法空结果仍缓存",
                    zero == ({"h48": [], "d90": []} if kind == "stats" else [])
                    and await call() == zero
                    and len(calls) == 4,
                )
            finally:
                await c.close()


async def t_wm_v1_shared_failure():
    """共享回源的坏信封唤醒所有调用者，下一轮成功继续共享。"""
    for kind in ("stats", "riven", "lich"):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def respond(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                entered.set()
                await release.wait()
                return httpx.Response(200, json={"error": "fixture"})
            data = (
                {"payload": {"statistics_closed": {"48hours": [], "90days": []}}}
                if kind == "stats"
                else {"payload": {"auctions": []}}
            )
            return httpx.Response(200, json=data)

        c = make_client()
        await c._http.aclose()
        c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))

        def call():
            if kind == "stats":
                return c.wm_statistics("fixture")
            if kind == "riven":
                return c.wm_riven_auctions("fixture", sorts=("price_asc",))
            return c.wm_lich_auctions("fixture")

        tasks = []
        try:
            tasks.append(asyncio.create_task(call()))
            await entered.wait()
            tasks.append(asyncio.create_task(call()))
            await asyncio.sleep(0)
            release.set()
            first = await asyncio.gather(*tasks, return_exceptions=True)
            check(
                f"v1 {kind}：坏信封的创建者与等待者共享错误且只请求一次",
                all(isinstance(x, WarframeAPIError) for x in first) and len(calls) == 1,
            )
            check(
                f"v1 {kind}：失败后无缓存与遗留单飞登记",
                not c.cache._store and not c.cache._inflight,
            )
            second = await asyncio.gather(call(), call())
            check(
                f"v1 {kind}：恢复后的并发合法空结果共享一次回源",
                second[0] == second[1] and len(calls) == 2,
            )
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await c.close()


async def t_wm_v1_auction_directions():
    """结构错不通过删除词条重试；desc 失败可降级、恢复后重新获取。"""
    for failed_direction in ("price_asc", "price_desc"):
        calls = []
        recovered = False
        rows = {direction: {"id": direction} for direction in ("price_asc", "price_desc")}

        def respond(request):
            params = dict(request.url.params)
            calls.append(params)
            direction = params["sort_by"]
            if direction == failed_direction and not recovered:
                return httpx.Response(200, json={"error": "fixture"})
            return httpx.Response(200, json={"payload": {"auctions": [rows[direction]]}})

        c = make_client()
        await c._http.aclose()
        c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            first = (
                await asyncio.gather(
                    c.wm_riven_auctions("fixture", positives=("multishot",)),
                    return_exceptions=True,
                )
            )[0]
            check(
                f"v1 紫卡/{failed_direction}：首方向错误直报，次方向错误只降级",
                isinstance(first, WarframeAPIError)
                if failed_direction == "price_asc"
                else first == [rows["price_asc"]],
            )
            check(
                f"v1 紫卡/{failed_direction}：结构错不去词条重查、不增HTTP重试",
                len(calls) == (1 if failed_direction == "price_asc" else 2)
                and all(x.get("positive_stats") == "multishot" for x in calls),
            )
            recovered = True
            result = await c.wm_riven_auctions("fixture", positives=("multishot",))
            check(
                f"v1 紫卡/{failed_direction}：来源恢复后补齐双方向并正常缓存",
                {x["id"] for x in result} == set(rows)
                and await c.wm_riven_auctions("fixture", positives=("multishot",)) == result
                and len(calls) == 3,
            )
        finally:
            await c.close()


async def t_de_community_fallback():
    from datetime import datetime, timezone, timedelta
    import copy
    from core.formatters import fmt_construction, fmt_void_trader
    from core.baro import group_of_store

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fissures": [{"node": "Larunda Relay (Mercury)"}, {"node": "Unknown node"}],
        "sortie": {"variants": [{"node": "Larunda Relay (Mercury)"}]},
        "constructionProgress": {"fomorianProgress": "25.5", "razorbackProgress": "60"},
        "nightwave": {
            "activeChallenges": [
                {
                    "title": "Meltdown",
                    "id": "1793404800000seasondailykillenemieswithcorrosive",
                    "reputation": 1000,
                },
                {
                    "title": "Unknown",
                    "id": "123unknownchallenge",
                    "desc": "Unknown description",
                    "reputation": 4500,
                },
            ]
        },
        "archonHunt": {
            "boss": "Archon Nira",
            "variants": [],
            "missions": [
                {"node": "Larunda Relay (Mercury)", "type": kind}
                for kind in ("Rescue", "Interception", "Assassination")
            ],
        },
        "voidTrader": {
            "character": "EvilBaroWeek3",
            "location": "Larunda Relay (Mercury)",
            "inventory": [
                {
                    "uniqueName": "/Lotus/StoreItems/Upgrades/Skins/Necramech/MechWeapon/EvilBaroMausolonSkin",
                    "item": "Evil Baro Mausolon Skin",
                    "ducats": 134,
                    "credits": 341876,
                },
                {
                    "uniqueName": "/Lotus/StoreItems/Types/Keys/MummyQuestKeyBlueprint",
                    "item": "Sands Of Inaros Blueprint",
                    "ducats": 100,
                    "credits": 25000,
                },
                {
                    "uniqueName": "/Unknown/Unmapped",
                    "item": "Unknown original",
                    "ducats": 1,
                    "credits": 2,
                },
            ],
            "activation": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    }
    for mode in ("403", "timeout"):
        calls = []

        def respond(request):
            calls.append(str(request.url))
            if "worldState.php" in str(request.url):
                if mode == "timeout":
                    raise httpx.ReadTimeout("fixture", request=request)
                return httpx.Response(403)
            return httpx.Response(200, json=payload)

        c = make_client()
        await c._http.aclose()
        c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            rows = await asyncio.gather(*(c.construction("pc") for _ in range(5)))
            check(f"DE {mode} 并发后备整包单飞", len([x for x in calls if "language=" in x]) == 1)
            check(f"DE {mode} 后备舰队百分比", rows[0]["projects"][0]["pct"] == 25.5)
            check(f"DE {mode} 未伪称无袭击", "未知" in "".join(fmt_construction(rows[0])[1]))
            check(f"DE {mode} 奸商按时间窗口", (await c.void_trader("pc"))["active"])
            trader = await c.void_trader("pc")
            check(
                f"DE {mode} 奸商路径中文与分类",
                trader["inventory"][0]["item"] == "终劫惩戒者外观"
                and group_of_store(trader["inventory"][0]["store"]) == "外观"
                and trader["inventory"][1]["item"] == "Inaros 之沙蓝图",
            )
            check(
                f"DE {mode} 奸商角色地点与未知回退",
                trader["character"] == "Baro Ki'Teer"
                and trader["location"] == "Larunda 中继站（水星）"
                and trader["inventory"][2]["item"] == "Unknown original"
                and len(trader["inventory"]) == len(payload["voidTrader"]["inventory"])
                and [(r["ducats"], r["credits"]) for r in trader["inventory"]]
                == [(r["ducats"], r["credits"]) for r in payload["voidTrader"]["inventory"]],
            )
            check(
                f"DE {mode} 奸商格式化内部角色名消失",
                "EvilBaroWeek3" not in str(fmt_void_trader(trader)),
            )
            fissures = await c.fissures("pc")
            check(
                f"DE {mode} 裂隙突击节点中文与未知回退",
                fissures[0]["node"] == "Larunda 中继站（水星）"
                and fissures[1]["node"] == "Unknown node"
                and (await c.sortie("pc"))["variants"][0]["node"] == "Larunda 中继站（水星）",
            )
            nightwave = await c.nightwave("pc")
            check(
                f"DE {mode} 电波任务保留声望", nightwave["activeChallenges"][0]["standing"] == 1000
            )
            check(
                f"DE {mode} 电波精确匹配中文与未知回退",
                nightwave["activeChallenges"][0]["title"] == "熔毁"
                and nightwave["activeChallenges"][0]["required"] == 150
                and "|COUNT|" in nightwave["activeChallenges"][0]["desc"]
                and nightwave["activeChallenges"][1]["title"] == "Unknown"
                and nightwave["activeChallenges"][1]["desc"] == "Unknown description",
            )
            hunt = await c.archon_hunt("pc")
            check(
                f"DE {mode} 执刑官保留三阶段与源力石代码",
                len(hunt["variants"]) == 3
                and hunt["bossCode"] == "Nira"
                and hunt["variants"][0]["node"] == "Larunda 中继站（水星）"
                and hunt["variants"][2]["missionType"] == "Assassination"
                and hunt["variants"][2]["level"] == "145-150级",
            )
            n = len(calls)
            await c.fissures("pc")
            check(f"DE {mode} 冷却不重打", len(calls) == n)
            try:
                await c.goals("pc")
                check("未核实goals明确不可用", False)
            except WarframeAPIError:
                check("未核实goals明确不可用", True)
        finally:
            await c.close()
    # 网络故障注入走真实 HTTP 边界和赏金 handler；不替换适配器/格式化器。
    from types import SimpleNamespace
    from unittest.mock import patch
    from core.commands.rotations import RotationCommands

    bounty_payload = copy.deepcopy(payload)
    bounty_payload["syndicateMissions"] = [
        {
            "syndicate": "Ostrons",
            "jobs": [
                {
                    "id": "ReclamationBountyTheft1793378461440",
                    "uniqueName": "/Lotus/Types/Game/MissionDecks/EidolonJobMissionRewards/TierATableARewards",
                    "type": "Reclaim the Stolen Artifact",
                    "enemyLevels": [5, 15],
                }
            ],
        }
    ]
    c = make_client()
    await c._http.aclose()
    network_calls = []
    outage = False

    def intermittent(request):
        if "worldState.php" in str(request.url):
            return httpx.Response(403)
        if "bounty-cycle" in str(request.url):
            return httpx.Response(200, json={})
        network_calls.append(str(request.url))
        if outage or len(network_calls) == 1:
            raise httpx.ReadTimeout("community fixture outage", request=request)
        return httpx.Response(200, json=bounty_payload)

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(intermittent))
    try:
        handler = RotationCommands()
        handler.client = c
        parsed = SimpleNamespace(preset="", content_str="地球", page=1)
        reply = await handler._h_bounty(parsed, None, "pc")
        check("社区瞬时ReadTimeout重试恢复", len(network_calls) == 2)
        check(
            "真实赏金入口恢复官方任务名与奖励",
            any("取回被偷的器物" in line for line in reply.lines)
            and any("Gara机体蓝图" in line for line in reply.lines),
        )
        check(
            "真实赏金入口保留社区源与原始快照时间",
            "社区 warframestat（DE 后备）" in reply.footer
            and bounty_payload["timestamp"] in reply.footer,
        )
        key = next(iter(c._ws_last_good))
        await c.cache.set(key, None, ttl=0)
        outage = True
        recovered = await c.fissures("pc")
        check(
            "持续超时两次后沿用有效成功快照",
            len(network_calls) == 4 and recovered[0]["node"] == "Larunda 中继站（水星）",
        )
        check(
            "沿用快照不刷新原始时间戳",
            c._ws_last_good[key]["timestamp"] == bounty_payload["timestamp"],
        )
        await c.fissures("pc")
        check("有效快照失败冷却期间不重打", len(network_calls) == 4)
        # 不睡十分钟：推进校验器墙钟，原始快照超过600秒必须拒绝。
        original_epoch = datetime.fromisoformat(bounty_payload["timestamp"]).timestamp()
        with patch("core.api_client.time.time", return_value=original_epoch + 601):
            try:
                await c.fissures("pc")
                check("冷却期间成功快照过期拒绝", False)
            except WarframeAPIError:
                check("冷却期间成功快照过期拒绝", len(network_calls) == 4)
        # 原pc/zh快照仍有效，也不能拿给别的平台、语言或源地址。
        for name, platform, language, base in (
            ("平台", "ps4", c.language, c.worldstate_base),
            ("语言", "pc", "en", c.worldstate_base),
            ("源地址", "pc", "zh", "https://another-community.test"),
        ):
            c.language = language
            c.worldstate_base = base
            before = len(network_calls)
            try:
                await c.fissures(platform)
                check(f"成功快照{name}隔离", False)
            except WarframeAPIError:
                check(f"成功快照{name}隔离", len(network_calls) == before + 2)
            before = len(network_calls)
            try:
                await c.fissures(platform)
            except WarframeAPIError:
                pass
            check(f"无可用快照{name}失败冷却", len(network_calls) == before)
    finally:
        await c.close()

    # 社区 /fissures 中的九重天仍留在裂隙卡，同时投影给独立风暴指令。
    from core.commands.daily import DailyCommands

    storms_payload = copy.deepcopy(payload)
    future = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    storms_payload["fissures"] = [
        {
            "id": "storm",
            "node": "Larunda Relay (Mercury)",
            "isStorm": True,
            "tier": "Lith",
            "missionType": "Skirmish",
            "expiry": future,
        },
        {"id": "ordinary", "node": "Unknown ordinary", "isStorm": False, "expiry": future},
        {"id": "steel", "node": "Unknown steel", "isHard": True, "expiry": future},
        {"id": "expired", "node": "Unknown expired", "isStorm": True, "expiry": past},
    ]
    for invalid_expiry in (False, None, "missing", "invalid", "2026-10-30T00:00:00"):
        current = copy.deepcopy(storms_payload)
        if invalid_expiry is not False:
            current["fissures"].append(
                {"id": "invalid", "node": "Unknown", "isStorm": True, "expiry": invalid_expiry}
            )
            if invalid_expiry == "missing":
                current["fissures"][-1].pop("expiry")
        c = make_client()
        await c._http.aclose()

        def storms_response(request):
            if "worldState.php" in str(request.url):
                return httpx.Response(403)
            return httpx.Response(200, json=current)

        c._http = httpx.AsyncClient(transport=httpx.MockTransport(storms_response))
        try:
            check(
                "风暴投影不改变裂隙整表语义",
                len(await c.fissures("pc")) == len(current["fissures"]),
            )
            if invalid_expiry is False:
                storms = await c.void_storms("pc")
                check(
                    "风暴仅保留未过期isStorm真值条目及中文节点",
                    len(storms) == 1
                    and storms[0]["id"] == "storm"
                    and storms[0]["nodeCn"] == "Larunda 中继站（水星）"
                    and storms[0]["tier"] == "Lith"
                    and storms[0]["missionType"] == "Skirmish",
                )
                handler = DailyCommands()
                handler.client = c
                reply = await handler._h_voidstorms(SimpleNamespace(), None, "pc")
                text = "\n".join(reply.lines)
                check(
                    "真实风暴入口经过格式化保留任务与中文地点",
                    "Larunda 中继站" in text
                    and "前哨战" in text
                    and "Unknown" not in text
                    and "进行中：1 处" in text,
                )
                check(
                    "真实风暴入口保留社区来源与原始时间戳",
                    "社区 warframestat（DE 后备）" in reply.footer
                    and current["timestamp"] in reply.footer,
                )
            else:
                try:
                    await c.void_storms("pc")
                    check("非法风暴expiry明确局部失败", False)
                except WarframeAPIError as exc:
                    check(
                        "非法风暴expiry明确局部失败",
                        "voidStorms" in str(exc) and "社区世界状态" in str(exc),
                    )
                check("非法风暴expiry不污染其他世界状态", bool(await c.sortie("pc")))
        finally:
            await c.close()

    # 两个平台/语言整包不能互串；失败冷却后必须恢复；解析失败不触发转源。
    c = make_client()
    await c._http.aclose()
    calls = []
    healthy = False

    def changing(request):
        calls.append(str(request.url))
        if "worldState.php" in str(request.url):
            return httpx.Response(403)
        return httpx.Response(200, json=payload if healthy else {})

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(changing))
    try:
        for _ in range(2):
            try:
                await c.fissures("pc")
            except WarframeAPIError:
                pass
        check("社区坏响应失败冷却不重复请求", len(calls) == 2)
        healthy = True
        c._ws_failure_until.clear()
        await c.fissures("pc")
        check("社区冷却后恢复且坏响应未缓存", len(calls) == 3)
        await c.fissures("ps4")
        c.language = "en"
        await c.fissures("pc")
        check(
            "平台与语言缓存身份隔离",
            len(calls) == 5 and any("/ps4?" in x for x in calls) and "language=en" in calls[-1],
        )

        async def parsed_failure():
            return {"_failed": ["fissures"], "fissures": []}

        c._de_bundle = parsed_failure
        c._de_retry_at = 0
        try:
            await c.fissures("pc")
            check("DE解析段失败不能转源", False)
        except WarframeAPIError as exc:
            check("DE解析段失败不能转源", "解析失败" in str(exc) and len(calls) == 5)

        async def recovered_de():
            return {"fissures": [{"id": "DE恢复"}]}

        c._de_bundle = recovered_de
        check("DE冷却到期恢复优先官方源", (await c.fissures("pc"))[0]["id"] == "DE恢复")
    finally:
        await c.close()
    for bad in (
        [],
        {},
        dict(payload, timestamp="bad"),
        dict(payload, timestamp="2000-01-01T00:00:00Z"),
        dict(payload, timestamp="2100-01-01T00:00:00Z"),
        dict(payload, fissures={}),
    ):
        try:
            WarframeClient._validate_community_ws(copy.deepcopy(bad))
            check("社区坏结构/过期/未来拒绝", False)
        except WarframeAPIError:
            check("社区坏结构/过期/未来拒绝", True)
    c = make_client(de_worldstate_url="https://custom.test/worldState.php")
    calls = []

    def custom(request):
        calls.append(str(request.url))
        return httpx.Response(403)

    await c._http.aclose()
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(custom))
    try:
        try:
            await c.fissures("pc")
        except WarframeAPIError:
            pass
        check("自定义DE不跨源", all("custom.test" in x for x in calls))
    finally:
        await c.close()


async def t_wm_auction_failure_boundaries():
    """真实 HTTP 边界：非400失败不删除词条，重试次数由调用方决定。"""
    from unittest.mock import patch

    clock = VirtualWMClock()
    no_sleep = clock.sleep

    for status in (429, 401, 403, 404, 500, "timeout"):
        for retries in (0, 1):
            calls = []

            def respond(request):
                calls.append(dict(request.url.params))
                if status == "timeout":
                    raise httpx.ReadTimeout("fixture", request=request)
                return httpx.Response(status)

            c = make_client()
            await c._http.aclose()
            c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            try:
                with (
                    patch("core.api_client.asyncio.sleep", no_sleep),
                    patch("core.api_client.time.monotonic", clock.monotonic),
                ):
                    result = (
                        await asyncio.gather(
                            c.wm_riven_auctions(
                                "fixture",
                                positives=("multishot",),
                                sorts=("price_asc",),
                                retries=retries,
                            ),
                            return_exceptions=True,
                        )
                    )[0]
                expected = 1 if status in (429, 401, 403, 404) else retries + 1
                check(
                    f"拍卖{status}/retries={retries}不去词条且遵循重试次数",
                    isinstance(result, WarframeAPIError)
                    and len(calls) == expected
                    and all(x.get("positive_stats") == "multishot" for x in calls),
                    f"calls={calls}",
                )
                if status == 429:
                    check(
                        "拍卖429保留HTTP状态cause",
                        isinstance(result.__cause__, httpx.HTTPStatusError)
                        and result.__cause__.response.status_code == 429,
                    )
            finally:
                await c.close()


async def t_wm_auction_attribute_fallback():
    from unittest.mock import patch

    clock = VirtualWMClock()
    no_sleep = clock.sleep

    for allowed, require_negative in ((True, False), (False, False), (True, True)):
        calls = []
        timeouts = []

        def respond(request):
            params = dict(request.url.params)
            calls.append(params)
            timeouts.append(request.extensions["timeout"])
            if "positive_stats" in params or "negative_stats" in params:
                return httpx.Response(400)
            return httpx.Response(503)

        c = make_client()
        await c._http.aclose()
        c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with (
                patch("core.api_client.asyncio.sleep", no_sleep),
                patch("core.api_client.time.monotonic", clock.monotonic),
            ):
                result = (
                    await asyncio.gather(
                        c.wm_riven_auctions(
                            "fixture",
                            positives=() if require_negative else ("multishot",),
                            require_negative=require_negative,
                            sorts=("price_asc",),
                            retries=0,
                            allow_attribute_fallback=allowed,
                            timeout=1.5,
                        ),
                        return_exceptions=True,
                    )
                )[0]
            check(
                f"拍卖400回退开关={allowed}/任意负={require_negative}遵循retries=0和timeout",
                isinstance(result, WarframeAPIError)
                and len(calls) == (2 if allowed else 1)
                and all(all(v == 1.5 for v in item.values()) for item in timeouts)
                and (
                    not allowed or not {"positive_stats", "negative_stats"}.intersection(calls[-1])
                ),
                f"calls={calls},timeouts={timeouts}",
            )
        finally:
            await c.close()

    # 常规wr仍兼容400词条拒绝；降序任何错误不得再加一次去条件请求。
    for failure in (400, 429, 500, "timeout", "envelope"):
        calls = []

        def respond(request):
            params = dict(request.url.params)
            calls.append(params)
            if params["sort_by"] == "price_asc":
                if "positive_stats" in params:
                    return httpx.Response(400)
                return httpx.Response(200, json={"payload": {"auctions": [{"id": "asc"}]}})
            if failure == "timeout":
                raise httpx.ReadTimeout("fixture", request=request)
            if failure == "envelope":
                return httpx.Response(200, json={"error": "fixture"})
            return httpx.Response(failure)

        c = make_client()
        await c._http.aclose()
        c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with (
                patch("core.api_client.asyncio.sleep", no_sleep),
                patch("core.api_client.time.monotonic", clock.monotonic),
            ):
                result = await c.wm_riven_auctions(
                    "fixture",
                    positives=("multishot",),
                    retries=0,
                )
            check(
                f"升序400兼容回退且降序{failure}保留升序、不再去条件",
                result == [{"id": "asc"}]
                and len(calls) == 3
                and calls[-1].get("positive_stats") == "multishot",
                f"calls={calls}",
            )
        finally:
            await c.close()


async def t_wm_auction_cooldown():
    from types import SimpleNamespace
    from unittest.mock import patch

    for retry_after, duration in (
        (None, 90),
        ("12", 12),
        ("0000000000012", 12),
        ("999999999999", 999999999999),
        ("nan", 90),
        ("-3", 90),
        ("1.5", 90),
    ):
        clock = [100.0]

        async def no_sleep(seconds):
            clock[0] += seconds

        calls = []

        def respond(request):
            params = dict(request.url.params)
            calls.append((request.url.path, params))
            if params.get("weapon_url_name") == "limited":
                headers = {} if retry_after is None else {"Retry-After": retry_after}
                return httpx.Response(429, headers=headers)
            return httpx.Response(200, json={"payload": {"auctions": [{"id": "cached"}]}})

        c = make_client()
        await c._http.aclose()
        c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with (
                patch("core.api_client.time", SimpleNamespace(monotonic=lambda: clock[0])),
                patch("core.api_client.asyncio.sleep", no_sleep),
            ):
                cached = await c.wm_riven_auctions("cached", sorts=("price_asc",), retries=0)
                limited = (
                    await asyncio.gather(
                        c.wm_riven_auctions(
                            "limited", positives=("multishot",), sorts=("price_asc",), retries=0
                        ),
                        return_exceptions=True,
                    )
                )[0]
                blocked = (
                    await asyncio.gather(
                        c.wm_lich_auctions("different", element="heat"),
                        return_exceptions=True,
                    )
                )[0]
                hit = await c.wm_riven_auctions("cached", sorts=("price_asc",), retries=0)
                other = (
                    await asyncio.gather(
                        c._fetch_json(f"{c.wm_base}/items", ttl=30, wm_rate_limit=True),
                        return_exceptions=True,
                    )
                )[0]
                clock[0] += duration - 0.1
                still_blocked = (
                    await asyncio.gather(
                        c.wm_riven_auctions("different", sorts=("price_asc",), retries=0),
                        return_exceptions=True,
                    )
                )[0]
                count_before_expiry = len(calls)
                clock[0] += 0.2
                recovered = await c.wm_riven_auctions("different", sorts=("price_asc",), retries=0)
            check(
                f"拍卖冷却Retry-After={retry_after}跨条件/玄骸、缓存可用、其他WM也冷却、到期恢复",
                isinstance(limited, WarframeAPIError)
                and isinstance(blocked, WarframeAPIError)
                and isinstance(still_blocked, WarframeAPIError)
                and hit == cached
                and isinstance(other, WarframeAPIError)
                and count_before_expiry == 2
                and len(calls) == 3
                and recovered == cached,
                f"calls={calls},before_expiry={count_before_expiry}",
            )
        finally:
            await c.close()

    # 当前请求已发出后，另一个在途请求建立了更长冷却；迟到429不能缩短它。
    c = make_client()
    await c._http.aclose()

    def late_429(request):
        c._wm_auction_cool_until = 400.0
        return httpx.Response(429, headers={"Retry-After": "12"})

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(late_429))
    try:
        with patch("core.api_client.time", SimpleNamespace(monotonic=lambda: 100.0)):
            result = (
                await asyncio.gather(
                    c.wm_riven_auctions("late", sorts=("price_asc",), retries=0),
                    return_exceptions=True,
                )
            )[0]
        check(
            "迟到拍卖429不缩短已有更长冷却",
            isinstance(result, WarframeAPIError) and c._wm_auction_cool_until == 400.0,
            f"cool_until={c._wm_auction_cool_until}",
        )
    finally:
        await c.close()

    # 第二请求已进入WM节流等待，首请求随后429；等待结束必须再次检查冷却。
    first_entered, release_first, queued, first_finished = (asyncio.Event() for _ in range(4))
    calls = []

    async def respond(request):
        calls.append(str(request.url))
        first_entered.set()
        await release_first.wait()
        return httpx.Response(429)

    async def queued_sleep(_seconds):
        queued.set()
        await first_finished.wait()

    c = make_client()
    await c._http.aclose()
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    tasks = []
    try:
        with (
            patch("core.api_client.time", SimpleNamespace(monotonic=lambda: 100.0)),
            patch("core.api_client.asyncio.sleep", queued_sleep),
        ):
            tasks.append(
                asyncio.create_task(c.wm_riven_auctions("first", sorts=("price_asc",), retries=0))
            )
            await first_entered.wait()
            tasks.append(
                asyncio.create_task(c.wm_riven_auctions("queued", sorts=("price_asc",), retries=0))
            )
            await queued.wait()
            release_first.set()
            first = (await asyncio.gather(tasks[0], return_exceptions=True))[0]
            first_finished.set()
            second = (await asyncio.gather(tasks[1], return_exceptions=True))[0]
        check(
            "WM排队请求在等待结束后再次检查拍卖冷却",
            len(calls) == 1
            and isinstance(first, WarframeAPIError)
            and isinstance(second, WarframeAPIError),
        )
    finally:
        release_first.set()
        first_finished.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await c.close()


async def t_wm_bounded_gate():
    """真实公开入口的短预算：只用假HTTP，额度等待和槽等待均有界。"""
    calls = []
    c = make_client(timeout=0.04)
    await c._http.aclose()

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"payload": {"auctions": []}})

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    c._wm_auction_tokens = 0.0
    try:
        result = (
            await asyncio.gather(
                c.wm_riven_auctions("budget", sorts=("price_asc",), retries=0, timeout=0.01),
                return_exceptions=True,
            )
        )[0]
        check(
            "公开拍卖额度耗尽按请求短预算报繁忙且不GET",
            isinstance(result, WarframeAPIError)
            and "额度繁忙" in str(result)
            and not calls
            and c._wm_slots._value == 3,
        )
        cancelled = asyncio.create_task(
            c.wm_riven_auctions("cancel", sorts=("price_asc",), retries=0)
        )
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        closing = asyncio.create_task(c.wm_riven_auctions("close", sorts=("price_asc",), retries=0))
        await asyncio.sleep(0)
        c.begin_close()
        result = (await asyncio.gather(closing, return_exceptions=True))[0]
        check(
            "额度等待取消及关闭到期不GET不漏槽",
            isinstance(result, WarframeAPIError)
            and "已关闭" in str(result)
            and not calls
            and c._wm_slots._value == 3,
        )
    finally:
        await c.close()

    release, occupied = asyncio.Event(), asyncio.Event()
    sends = []

    async def held(request):
        sends.append(str(request.url))
        if len(sends) == 3:
            occupied.set()
        await release.wait()
        return httpx.Response(200, json={"data": []})

    c = make_client(timeout=0.08)
    # 此用例只隔离槽门禁：缩短间隔，让三条公开普通查询占满真实HTTP槽。
    c.WM_MIN_INTERVAL_S = 0.001
    await c._http.aclose()
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(held))
    tasks = []
    try:
        tasks = [asyncio.create_task(c.wm_orders(str(i), "pc")) for i in range(3)]
        await asyncio.wait_for(occupied.wait(), 1)
        result = (await asyncio.gather(c.wm_orders("busy", "pc"), return_exceptions=True))[0]
        check(
            "三槽全占时公开查询有界报繁忙且无第四GET",
            isinstance(result, WarframeAPIError) and "额度繁忙" in str(result) and len(sends) == 3,
        )
        waiter = asyncio.create_task(c.wm_orders("cancel", "pc"))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        closing = asyncio.create_task(c.wm_orders("close", "pc"))
        await asyncio.sleep(0)
        c.begin_close()
        result = (await asyncio.gather(closing, return_exceptions=True))[0]
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        check(
            "槽等待取消关闭到期不GET且收尾回到3槽",
            isinstance(result, WarframeAPIError)
            and "已关闭" in str(result)
            and len(sends) == 3
            and c._wm_slots._value == 3,
        )
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await c.close()


async def t_wm_origin_normalization():
    import time

    c = make_client(wm_base="https://WM.EXAMPLE/v1", wm_v2_base="https://WM.EXAMPLE/v2")
    calls = []
    await c._http.aclose()

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    c._wm_auction_cool_until = time.monotonic() + 900
    try:
        for url in ("https://wm.example/v1/items", "HTTPS://WM.EXAMPLE:443/v2/items"):
            result = (
                await asyncio.gather(
                    c._fetch_json(url, ttl=30, wm_rate_limit=False), return_exceptions=True
                )
            )[0]
            check(
                "WM大小写与默认443归属同一origin/" + url,
                isinstance(result, WarframeAPIError) and not calls,
            )
        for url in (
            "https://wm.example.evil/v1/items",
            "https://wm.example/v1evil/items",
            "https://wm.example:444/v1/items",
        ):
            result = await c._fetch_json(url, ttl=30, wm_rate_limit=False)
            check("WM伪前缀或不同端口不误判/" + url, result == {"ok": True})
        check("origin兜底只阻断真实WM", len(calls) == 3)
    finally:
        await c.close()


async def t_wm_waiting_auction_and_http_policy():
    from unittest.mock import patch

    clock = VirtualWMClock()
    parked, resume = asyncio.Event(), asyncio.Event()
    sent = []
    policy = [200, 200, 200, 200, 503]

    async def respond(request):
        sent.append(request.url.path)
        status = policy.pop(0)
        return httpx.Response(
            status,
            headers={"Retry-After": "900"} if status == 503 else {},
            json={"payload": {"auctions": []}, "data": []},
        )

    async def sleep(seconds):
        deadline = clock.value + seconds
        if seconds > 1:
            parked.set()
            await resume.wait()
        clock.value = max(clock.value, deadline)

    c = make_client()
    await c._http.aclose()
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    task = None
    try:
        with (
            patch("core.api_client.time.monotonic", clock.monotonic),
            patch("core.api_client.asyncio.sleep", sleep),
        ):
            await c.wm_riven_auctions("a", sorts=("price_asc",), retries=0)
            await c.wm_riven_auctions("b", sorts=("price_asc",), retries=0)
            task = asyncio.create_task(c.wm_riven_auctions("c", sorts=("price_asc",), retries=0))
            await parked.wait()
            await c.wm_items()
            check(
                "拍卖等额度不占锁槽堵普通查询",
                len(sent) == 3 and c._wm_slots._value == 3 and not c._wm_lock.locked(),
            )
            resume.set()
            await task
            before = clock.value
            result = (await asyncio.gather(c.wm_orders("503", "pc"), return_exceptions=True))[0]
            check(
                "503 Retry-After共享长冷却不重试",
                isinstance(result, WarframeAPIError)
                and len(sent) == 5
                and c._wm_auction_cool_until >= before + 900,
            )
    finally:
        resume.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await c.close()

    sends = []
    c = make_client()
    await c._http.aclose()

    def redirect(request):
        sends.append(request.url)
        return httpx.Response(302, headers={"Location": "/uncounted"})

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True)
    try:
        result = (await asyncio.gather(c.wm_items(), return_exceptions=True))[0]
        check("WM重定向不产生未计数HTTP", isinstance(result, WarframeAPIError) and len(sends) == 1)
    finally:
        await c.close()


async def t_wm_slot_cancellation():
    from unittest.mock import patch

    clock = VirtualWMClock()
    real_sleep = asyncio.sleep
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 3:
            entered.set()
        try:
            await release.wait()
            return httpx.Response(200, json={"data": []})
        finally:
            active -= 1

    async def advance(seconds):
        await clock.sleep(seconds)
        await real_sleep(0)

    c = make_client()
    await c._http.aclose()
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    tasks = []
    try:
        with (
            patch("core.api_client.time.monotonic", clock.monotonic),
            patch("core.api_client.asyncio.sleep", advance),
        ):
            tasks = [asyncio.create_task(c.wm_orders(str(i), "pc")) for i in range(5)]
            await asyncio.wait_for(entered.wait(), 2)
            await real_sleep(0)
            check("WM所有入口最多3在途", peak == 3 and active == 3)
            tasks[0].cancel()
            await real_sleep(0)
            c.begin_close()
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            check("取消与关闭释放所有槽", c._wm_slots._value == 3 and active == 0)
            check(
                "等待槽后重查关闭不发新HTTP",
                peak == 3 and any(isinstance(r, WarframeAPIError) for r in results),
            )
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await c.close()


async def t_wm_shared_governor():
    from unittest.mock import patch
    import math

    check("Retry-After长秒数不截断", wm_retry_after_seconds("000999999") == 999999)
    check("Retry-After超长数字安全", math.isinf(wm_retry_after_seconds("9" * 5000)))
    check(
        "Retry-After HTTP-date",
        wm_retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412420) == 60,
    )
    check(
        "Retry-After旧HTTP-date",
        wm_retry_after_seconds("Wed Oct 21 07:28:00 2015", now=1445412420) == 60,
    )
    check("Retry-After无效回落", wm_retry_after_seconds("-1") == 90)
    clock = VirtualWMClock()
    sends = []
    statuses = [200, 200, 200, 200, 503, 200, 429]

    def respond(request):
        sends.append((clock.value, request.url.path))
        return httpx.Response(statuses.pop(0), json={"payload": {"auctions": []}, "data": []})

    c = make_client()
    await c._http.aclose()
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with (
            patch("core.api_client.time.monotonic", clock.monotonic),
            patch("core.api_client.asyncio.sleep", clock.sleep),
        ):
            for slug in ("a", "b", "c"):
                await c.wm_riven_auctions(slug, sorts=("price_asc",), retries=0)
            await c._fetch_json(c.wm_v2_base + "/items", ttl=30, wm_rate_limit=False)
            await c.wm_orders("fixture", "pc")
            try:
                await c.wm_statistics("fixture")
            except WarframeAPIError:
                pass
            times = [t for t, _ in sends]
            check(
                "共享出口普通间隔严格低于3/s",
                all(b - a >= 0.349999 for a, b in zip(times, times[1:])),
                str(times),
            )
            # 浮点单调时钟相减可能留下尾差，与上面的间隔断言共用 1 微秒容差。
            check(
                "拍卖双burst后等待补额",
                times[1] - times[0] < 1 and times[2] - times[0] >= 15 - 1e-6,
                str(times),
            )
            check("网络重试重新计额度", len(sends) == 7, str(sends))
            hit = await c._fetch_json(c.wm_v2_base + "/items", ttl=30, wm_rate_limit=False)
            check("429期间缓存仍可读", hit.get("data") == [] and len(sends) == 7)
            try:
                await c.wm_orders("blocked", "pc")
            except WarframeAPIError:
                pass
            check("普通429阻断其它WM出口", len(sends) == 7)
            check("出口释放所有在途槽", c._wm_slots._value == 3)
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
