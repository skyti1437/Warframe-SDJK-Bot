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

from core.api_client import (WarframeAPIError, WarframeClient,  # noqa: E402
                             parse_url_list)

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
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=None)

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
        check("断网不泄露内部栈（仅外层包装信息）",
              "Traceback" not in str(e), str(e))
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
        *[c._fetch_json("https://x.test/ws", ttl=30, retries=0)
          for _ in range(10)],
        return_exceptions=True)
    check("10 并发失败全部拿到 WarframeAPIError",
          all(isinstance(r, WarframeAPIError) for r in results),
          str([type(r).__name__ for r in results]))
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
        hits = [x for x in captured
                if "never retrieved" in str(x.get("message", ""))]
        check("单飞失败无 never-retrieved 警告（BUG-1 回归）",
              not hits, str(hits[:1]))
    finally:
        loop.set_exception_handler(old)


# ---------------------------------------------------------------- 10. 配置项 parse_url_list 边界
def t_parse_url_list():
    check("空字符串 → 空列表", parse_url_list("") == [])
    check("None → 空列表", parse_url_list(None) == [])
    check("逗号分隔", parse_url_list("http://a:1/v1, http://b:2/v1")
          == ["http://a:1/v1", "http://b:2/v1"])
    check("换行分隔", parse_url_list("http://a:1/v1\nhttp://b:2/v1")
          == ["http://a:1/v1", "http://b:2/v1"])
    check("缺 /v1 自动补", parse_url_list("http://a:8191") == ["http://a:8191/v1"])
    check("非 http 内容被丢弃", parse_url_list("not-a-url, ftp://x")
          == [], str(parse_url_list("not-a-url, ftp://x")))
    check("去重", parse_url_list("http://a/v1, http://a/v1") == ["http://a/v1"])
    check("尾部斜杠归一", parse_url_list("http://a:8191/v1/") == ["http://a:8191/v1"])
    check("超长输入不崩", parse_url_list("http://a/" + "x" * 100000)[0].endswith("/v1"))


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

    print()
    if FAILED:
        print(f"共 {len(FAILED)} 项失败：{FAILED}")
        sys.exit(1)
    print("api_client 故障注入：全部断言通过")


if __name__ == "__main__":
    asyncio.run(main())
