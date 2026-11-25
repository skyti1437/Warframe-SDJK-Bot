# -*- coding: utf-8 -*-
"""时效总览不许静默吞行 + 仲裁锚点死代码清理守卫（2026-09-25）。

事故：`fmt_timers` 遇到取不到数据的项直接 ``continue``，而 `_h_timers` 更早一步
用 ``isinstance(r, dict)`` 把源恒抛的条目（仲裁 / 钢铁侵蚀——DE 源没有这两个键）
过滤掉了 ⇒ 两行自 10o.io 停摆后从「时效总览」卡里静默消失，用户以为看全了。

铁律：**合法空值≠故障，但不允许静默错槽** —— 取不到就显式打
「暂无时效数据（源未下发）」，行位不丢、顺序不乱。

第二个守卫：`锚点` 指令相关的死代码（fmt_arbitration / fetch_arbseq /
arb_from_anchor / load_arb_slots / core/data/arb_slots.json）已清理，防复发。
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core import formatters as fmt  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ★ 2026-10-11 第二步改版：行内只写「暂无数据」，「= 数据源未下发该项计时」的解释挪到卡尾注脚
MISSING = "暂无数据"


# ---------------------------------------------------------------- 1. 行位不丢
def t_no_silent_drop():
    timers = [
        ("夜灵平野", {"expiry": "2030-01-01T00:00:00+00:00"}),
        ("仲裁", None),  # 源恒抛（事故现场）
        ("钢铁侵蚀", {}),  # 空 dict
        ("每日突击", {"foo": 1, "bar": 2}),  # 有字段但两个键都没有
        ("虚空奸商", {"activation": "2030-01-01T00:00:00+00:00"}),
    ]
    _title, lines = fmt.fmt_timers(timers)
    # ★ 2026-10-11 第二步改版：时效卡分「开放世界 / 每日 / 每周 / 其它周期」四段，行为
    #   「· 名称　内容　计时」；◆ 分段标题与 ※ 注脚不是数据行，数据行 = 以「· 」开头的列表行。
    body = [ln for ln in lines if ln.startswith("· ")]
    check("5 项输入 → 5 行输出（一行都不许吞）", len(body) == 5, str(lines))
    check(
        "仲裁 行显式提示源未下发",
        any(ln.startswith("· 仲裁　每小时轮换") and MISSING in ln for ln in body),
        str(body),
    )
    check(
        "钢铁侵蚀 行显式提示源未下发（显示名=钢铁之路）",
        any(ln.startswith("· 钢铁之路　") and MISSING in ln for ln in body),
        str(body),
    )
    check(
        "空 dict / 无关键 dict 也显式提示", sum(1 for ln in body if MISSING in ln) == 3, str(body)
    )
    check(
        "有缺失行时卡尾注脚解释「暂无数据」的含义",
        any(ln.startswith("※") and "暂无数据" in ln and "未下发" in ln for ln in lines),
        str(lines[-1:]),
    )
    # ★ 2026-10-11 改断言：虚空商人的 activation 行措辞为「X 后抵达」（其余仍「后开始」）
    check(
        "正常 expiry/activation 行仍出倒计时",
        any("剩余" in ln for ln in body) and any("后抵达" in ln for ln in body),
        str(body),
    )
    # 改版后行序 = 「分段顺序 + 同段内输入序」；本用例输入恰好按段排好，所以行序仍应等于输入序
    check(
        "顺序不乱（同段内行序=输入序）",
        [ln[2:].split("　")[0] for ln in body]
        == ["夜灵平野", "仲裁", "钢铁之路", "每日突击", "虚空商人"],
        str(body),
    )
    check(
        "名称列 ≤10 字（名称 / 内容 / 计时三列各管各，不会把说明塞进名称）",
        all(1 <= len(ln[2:].split("　")[0]) <= 10 for ln in body),
        str(body),
    )


# ---------------------------------------------------------------- 2. 全空也不崩
def t_deal_expiries():
    now = datetime(2026, 11, 24, tzinfo=timezone.utc)
    with patch.object(fmt, "_now", return_value=now):
        _, lines = fmt.fmt_daily_deals(
            [
                {"item": "无偏移", "expiry": "2026-11-24T01:00:00"},
                {"item": "坏数据", "expiry": {"invalid": 1}},
                {"item": "正常商品", "expiry": "2026-11-24T03:00:00+01:00"},
            ]
        )
    rows = [line for line in lines if line.startswith("· ")]
    check(
        "特惠无偏移时间按 UTC，坏值仅影响本行倒计时",
        len(rows) == 3
        and rows[0].endswith("剩1小时0分")
        and rows[1].endswith("剩?")
        and rows[2].endswith("剩2小时0分"),
        str(rows),
    )
    check(
        "无效到期值显式返回未知",
        all(
            fmt.parse_iso(value) is None and fmt.countdown(value) == "?"
            for value in (None, 1, [], {"invalid": 1}, "not a date")
        ),
    )


def t_all_missing():
    _title, lines = fmt.fmt_timers([("仲裁", None), ("钢铁侵蚀", None)])
    check(
        "全缺失时不抛且每项都有行",
        len([ln for ln in lines if ln.startswith("· ")]) == 2,
        str(lines),
    )


# ---------------------------------------------------------------- 3. 源码级守卫
def t_source_guards():
    src_fmt = (ROOT / "core/formatters.py").read_text(encoding="utf-8")
    src_main = (ROOT / "main.py").read_text(encoding="utf-8")
    check(
        "fmt_timers 不再有 `if not data: continue` 静默分支",
        "if not data:\n            continue" not in src_fmt,
    )
    # 只在 _h_timers 函数体内断言（别处 `isinstance(r, dict)` 过滤仍是合法用法）
    # 结构优化 D1：_h_timers 已迁 core/commands/daily.py（Mixin，方法体逐字未改）
    src_daily = (ROOT / "core" / "commands" / "daily.py").read_text(encoding="utf-8")
    _i = src_daily.index("async def _h_timers")
    _body = src_daily[_i : _i + 2600]
    check(
        "_h_timers 把非 dict 结果透传为 None（不再 isinstance 过滤丢行）",
        "r if isinstance(r, dict) else None" in _body and "if isinstance(r, dict)]" not in _body,
    )
    # 死代码清理守卫
    check("formatters.fmt_arbitration 已清理", "def fmt_arbitration" not in src_fmt)
    check(
        "api_client.fetch_arbseq 已清理",
        "def fetch_arbseq" not in (ROOT / "core/api_client.py").read_text(encoding="utf-8"),
    )
    src_dw = (ROOT / "core/de_worldstate.py").read_text(encoding="utf-8")
    check(
        "de_worldstate 锚点推算器已清理",
        "arb_from_anchor" not in src_dw and "load_arb_slots" not in src_dw,
    )
    check("core/data/arb_slots.json 已删除", not (ROOT / "core/data/arb_slots.json").exists())
    check(
        "「.锚点」废弃提示仍保留（按铁律给替代指令）", "已废弃" in src_main and "仲裁表" in src_main
    )


# ---------------------------------------------------------------- 4. B11（2026-10-11）
def t_b11_arbi_and_sources():
    from core import arbi

    # ★ 2026-10-22：仲裁时间表的评级列带**社区实测每小时生息中位数**（取整）。
    #   档位+中位数、只档位、未评级三种形态都要对；中位数取整（528.9 → 529）。
    _tier_of = {"N1": "A", "N2": "A+", "N3": "S"}
    _med = {
        "N1": {"tier": "A", "median": 612.4, "n": 9},
        "N2": {"tier": "A+", "median": 705.0, "n": 9},
    }
    _saved = arbi._MEASURED
    arbi._MEASURED = dict(_med, N3={"tier": "S"})  # N3：官方有档、社区无样本
    try:
        check(
            "★ 评级后带每小时中位数（取整）：A 612 / A+ 705",
            arbi.tier_label_with_median("N1", _tier_of) == "A 612"
            and arbi.tier_label_with_median("N2", _tier_of) == "A+ 705",
            f"{arbi.tier_label_with_median('N1', _tier_of)!r} {arbi.tier_label_with_median('N2', _tier_of)!r}",
        )
        check(
            "★ 有档无样本 ⇒ 只给档位；未评级 ⇒ 空串",
            arbi.tier_label_with_median("N3", _tier_of) == "S"
            and arbi.tier_label_with_median("NX", _tier_of) == "",
        )
        check(
            "  图例句在场（只在带中位数的表上出现）",
            "每小时生息中位数" in arbi.TIER_MEDIAN_NOTE,
            arbi.TIER_MEDIAN_NOTE,
        )
    finally:
        arbi._MEASURED = _saved

    # ★ 2026-10-22：「下次高效」—— 从下一场起找第一个 S/A+/A（与「仲裁 高效」同口径）。
    _sched = {"startTs": 0, "stepSec": 3600, "seq": [0, 1, 2, 3], "nodes": ["kA", "kB", "kC", "kD"]}
    _tier2 = {"kA": "B", "kB": "A-", "kC": "A", "kD": "S"}
    check(
        "★ next_efficient：跳过 B/A-，命中第一个 A",
        arbi.next_efficient(_sched, _tier2, 0) == (2, "kC"),
        str(arbi.next_efficient(_sched, _tier2, 0)),
    )
    check(
        "★ next_efficient：从下一场起（含），全不高效 ⇒ None",
        arbi.next_efficient({"seq": [0, 1], "nodes": ["kA", "kB"]}, _tier2, 0) is None,
    )
    check(
        "★ next_efficient：下一场就是高效 ⇒ 就是它",
        arbi.next_efficient(_sched, _tier2, 3) == (3, "kD"),
        str(arbi.next_efficient(_sched, _tier2, 3)),
    )

    # 仲裁换场 = 下一个整点（排期表 startTs 对齐整点、步长 3600）
    ts = 1791680400 + 1234  # 2026-10-11T01:20:34Z
    check("arbi.next_switch → 下一个整点", arbi.next_switch(ts) == 1791680400 + 3600)
    sched = {"startTs": 1791680400 - 7200, "stepSec": 3600, "seq": [0, 1, 0], "nodes": ["A", "B"]}
    check(
        "arbi.next_switch 按排期表起点/步长",
        arbi.next_switch(ts, sched) == 1791680400 + 3600,
    )
    check("arbi.key_at 表内正常取值", arbi.key_at(sched, ts)[0] == "A")
    try:
        arbi.key_at(sched, arbi.schedule_end(sched) + 1)
        expired = False
    except arbi.ScheduleExpired:
        expired = True
    check("arbi.key_at 排期表到期后抛 ScheduleExpired（不回绕到表头）", expired)
    from core.display_time import use_zone

    with use_zone("UTC-03:30"):
        check("到期提示带实际 UTC 偏移", "UTC-03:30" in arbi.expired_hint(sched))

    src_daily = (ROOT / "core" / "commands" / "daily.py").read_text(encoding="utf-8")
    _i = src_daily.index("async def _h_timers")
    _body = src_daily[_i : _i + 4000]
    check(
        "时效：仲裁走 arbi.next_switch、钢铁侵袭走 steel_path_incursions（旧接口恒抛）",
        "self._arb_timer()" in _body
        and "steel_path_incursions(platform)" in _body
        and "self.client.arbitration(platform)" not in _body
        and "self.client.steel_path(platform)" not in _body,
    )


async def descendia_failure():
    import asyncio
    from unittest.mock import AsyncMock
    from core.commands.daily import DailyCommands

    handler = DailyCommands()
    handler.client = AsyncMock()
    handler.client.descendia.side_effect = TimeoutError("fixture")
    handler.client.sortie.return_value = {"expiry": "2099-01-01T00:00:00Z"}
    handler._gather = lambda calls: asyncio.gather(*calls, return_exceptions=True)
    handler._worldstate_source = lambda row: ""
    reply = await handler._h_timers(None, None, "pc")
    check(
        "沉沦超时只影响本行，突击与周常仍出卡",
        any("沉沦之地" in ln and MISSING in ln for ln in reply.lines)
        and all(
            any(name in ln and "剩余" in ln for ln in reply.lines)
            for name in ("每日突击", "本周重置")
        ),
    )


async def t_utc_incursion_cache():
    import asyncio
    import httpx
    from core import api_client

    start = 20000 * 86400
    now = start + 86399
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        calls.append(request.url.path)
        entered.set()
        await release.wait()
        if len(calls) == 2:
            return httpx.Response(200, text="无当日节点")
        return httpx.Response(200, text=f"{start};One,Two\n{start + 86400};Three")

    client = api_client.WarframeClient()
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with patch.object(api_client.time, "time", side_effect=lambda: now):
            requests = [asyncio.create_task(client.steel_path_incursions("pc")) for _ in range(8)]
            await asyncio.wait_for(entered.wait(), 2)
            release.set()
            rows = await asyncio.gather(*requests)
            shared = await client.steel_path_incursions("ps4")
            check(
                "钢铁入侵同日八并发与跨平台热查只发一次 HTTP",
                len(calls) == 1 and all(row["nodes"] == ["One", "Two"] for row in [*rows, shared]),
            )
            now += 1
            missing = await client.steel_path_incursions("pc")
            new = await client.steel_path_incursions("pc")
            check(
                "UTC 午夜换键，未解析出节点可立即重试",
                missing == {} and len(calls) == 3 and new["nodes"] == ["Three"],
            )
    finally:
        release.set()
        await client.close()


async def t_arbi_schedule_cache_validation():
    import time
    import httpx
    from core import api_client, arbi
    from core.commands.arbitration import ArbitrationCommands
    from core.parser import parse

    good = {"startTs": int(time.time()) - 10, "stepSec": 3600, "nodes": ["A", "B"], "seq": [0, 1]}
    responses = []
    schedule_calls = 0

    def respond(request):
        nonlocal schedule_calls
        if request.url.path.endswith("schedule.v2.json"):
            schedule_calls += 1
            return httpx.Response(200, json=responses.pop(0))
        if request.url.path.endswith("nodes.zh.json"):
            return httpx.Response(
                200, json={"nodes": {"A": {"nameZh": "节点甲"}, "B": {"nameZh": "节点乙"}}}
            )
        return httpx.Response(200, json={"tierBuckets": {}})

    client = api_client.WarframeClient()
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    handler = ArbitrationCommands()
    handler.client = client
    parsed = parse("仲裁表")
    try:
        legacy = {k: v for k, v in good.items() if k != "stepSec"}
        responses.append(legacy)
        await handler._h_arbtable(parsed, None, "pc")
        check(
            "旧排期省略 stepSec 读取并按 3600 秒换场",
            arbi.key_at(legacy, good["startTs"] + 3600)[0] == "B"
            and schedule_calls == 1
            and not responses,
        )
        for invalid in (float("nan"), float("inf"), float("-inf")):
            try:
                arbi._validate_schedule({**good, "stepSec": invalid})
            except api_client.WarframeAPIError:
                check(f"stepSec 拒绝显式非有限值 {invalid}", True)
            else:
                check(f"stepSec 拒绝显式非有限值 {invalid}", False)
        for field, bad in (
            ("seq", [-1]),
            ("seq", [2]),
            ("seq", [True]),
            ("seq", [0.0]),
            ("seq", []),
            ("stepSec", None),
            ("stepSec", 0),
            ("stepSec", -1),
            ("stepSec", True),
            ("startTs", None),
            ("startTs", True),
            ("nodes", []),
            ("nodes", [None]),
        ):
            client.cache.drop_prefix(arbi.BASE)
            before = schedule_calls
            responses.extend([{**good, field: bad}, good])
            try:
                await handler._h_arbtable(parsed, None, "pc")
            except api_client.WarframeAPIError as exc:
                message = api_client.public_error_text(exc)
            else:
                message = ""
            check(
                f"仲裁坏表 {field}={bad} 给出数据源格式异常",
                "数据源格式异常" in message and field in message,
            )
            reply = await handler._h_arbtable(parsed, None, "pc")
            slot = await arbi.current(client)
            check(
                f"仲裁 {field}={bad} 修正后立即出表、推送共用有效缓存",
                "节点乙" in "".join(reply.lines)
                and slot["key"] == "A"
                and schedule_calls == before + 2
                and not responses,
            )
    finally:
        await client.close()


def main():
    import asyncio

    asyncio.run(descendia_failure())
    asyncio.run(t_utc_incursion_cache())
    asyncio.run(t_arbi_schedule_cache_validation())
    t_no_silent_drop()
    t_deal_expiries()
    t_all_missing()
    t_source_guards()
    t_b11_arbi_and_sources()
    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} -> {FAILED}")
        sys.exit(1)
    print("全部通过（时效总览不静默吞行 + 死代码清理守卫）")


if __name__ == "__main__":
    main()
