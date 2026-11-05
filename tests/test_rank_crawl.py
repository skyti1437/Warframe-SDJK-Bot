# -*- coding: utf-8 -*-
"""价格榜单“游标续跑 + 每轮限量”离线测试：python3 tests/test_rank_crawl.py

2026-09-14：全量抓取一轮要好几个小时且占用 WM 全局限速（拖慢用户 wm/wr），
改成：每轮最多 limit 项、落盘 cursor 续跑、跑满一轮才刷新 ts。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import api_client as AC  # noqa: E402
from core import paths  # noqa: E402
from core.api_client import WarframeClient  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


class FakeClient(WarframeClient):
    """20 个候选物品，每次 statistics 计数 +1（模拟网络）。"""

    def __init__(self, store: dict):
        self.store = store
        self.calls = 0

    async def wm_items(self):
        return [
            {
                "url_name": f"item_{i}",
                "id": str(i),
                "zh": f"物品{i}",
                "en": f"Item {i}",
                "tags": ["prime"],
                "tradable": True,
                "ducats": 10,
            }
            for i in range(20)
        ]

    async def wm_statistics(self, slug, platform="pc"):
        self.calls += 1
        return {"payload": {"statistics_closed": {"48h": []}}}

    def _load_json_file(self, path):
        return self.store

    def _save_json_file(self, path, data):
        self.store.clear()
        self.store.update(data)


def _rank_record_stub(it, stats):
    return {"name": it["zh"], "plat": 1}


# ★ 2026-10-03：真实现句柄（下面 main2 要还原 —— stub 会掩盖 rec=None 的语义）
_REAL_RANK_RECORD = WarframeClient.__dict__.get("_rank_record")
_REAL_RANK_CANDIDATE = AC._rank_candidate


async def main():
    AC._rank_candidate = lambda it: True
    WarframeClient._rank_record = staticmethod(_rank_record_stub)

    store: dict = {}
    c = FakeClient(store)
    await c.crawl_wm_ranks(limit=6)
    check("每轮限量：只抓 6 项", c.calls == 6, str(c.calls))
    check("落盘游标 = 6", store.get("cursor") == 6, str(store.get("cursor")))
    check("rows 数 = 6", len(store.get("rows") or {}) == 6, str(len(store)))
    check("未跑满不刷新 ts", not store.get("ts"), str(store.get("ts")))

    c2 = FakeClient(store)  # 同一份存档 -> 续跑
    await c2.crawl_wm_ranks(limit=6)
    check("第二轮从第 7 项继续（累计 12）", store.get("cursor") == 12, str(store.get("cursor")))
    check("rows 累计 12", len(store.get("rows") or {}) == 12)

    c3 = FakeClient(store)
    await c3.crawl_wm_ranks(limit=20)
    check("跑到末尾：cursor 归零", store.get("cursor") == 0, str(store.get("cursor")))
    check("跑满一轮后 rows=20", len(store.get("rows") or {}) == 20)
    check("跑满一轮后刷新 ts", bool(store.get("ts")), str(store.get("ts")))

    c4 = FakeClient(store)  # 新一轮从头开始
    await c4.crawl_wm_ranks(limit=3)
    check("新一轮从头抓（cursor=3）", store.get("cursor") == 3, str(store.get("cursor")))

    c5 = FakeClient(store)
    await c5.crawl_wm_ranks()  # 不限量 -> 一次跑满
    check(
        "不限量一次跑满：cursor 归零且 ts 存在", store.get("cursor") == 0 and bool(store.get("ts"))
    )
    check("总项数记录正确", store.get("total") == 20, str(store.get("total")))


asyncio.run(main())


# ---------------------------------------------------------------------------
# 2026-10-03 追加：僵尸行修复 + 「请求失败 vs 无成交」严格区分（价格排行交办）
#   旧实现 `if rec: rows[slug] = rec` —— rec 为 None（无 48h 成交）时什么都不做
#   ⇒ 曾入榜的行永远留着最后一次有成交时的价格（注脚「无 48h 成交不入榜」失真）。
#   新语义：请求失败 ⇒ 保留旧行；_rank_record → None ⇒ 该行出榜（pop）。
# ---------------------------------------------------------------------------
class ZombieClient(WarframeClient):
    """3 个物品：①有 48h 成交 ②无 48h 成交 ③请求异常。"""

    def __init__(self, store: dict):
        self.store = store

    async def wm_items(self):
        return [
            {
                "url_name": f"z_{i}",
                "id": str(i),
                "zh": f"僵尸{i}",
                "en": f"Zombie {i}",
                "tags": ["mod"],
                "tradable": True,
            }
            for i in range(3)
        ]

    async def wm_statistics(self, slug, platform="pc"):
        if slug == "z_2":
            raise RuntimeError("net down")
        if slug == "z_1":  # h48 里 median 全为 0 ⇒ _rank_record 返回 None
            return {"h48": [{"median": 0, "min": 0, "max": 0, "volume": 0}], "d90": []}
        return {
            "h48": [{"median": 12.5, "min": 10, "max": 15, "volume": 3}],
            "d90": [{"median": 9.0, "volume": 1}, {"median": 9.5, "volume": 2}],
        }

    def _load_json_file(self, path):
        return self.store

    def _save_json_file(self, path, data):
        self.store.clear()
        self.store.update(data)


async def main2():
    WarframeClient._rank_record = _REAL_RANK_RECORD  # 还原真实现
    store = {
        "ts": "",
        "cursor": 0,
        "total": 3,
        "rows": {
            "z_0": {"zh": "旧0", "median48": 1.0},
            "z_1": {"zh": "旧1", "median48": 2.0},
            "z_2": {"zh": "旧2", "median48": 3.0},
        },
    }
    c = ZombieClient(store)
    await c.crawl_wm_ranks()
    rows = store.get("rows") or {}
    check("★ 僵尸行：本轮无 48h 成交（z_1）⇒ 旧行被 pop 出榜", "z_1" not in rows, str(sorted(rows)))
    check(
        "★ 请求异常（z_2）⇒ 旧行保留（不得把网络抖动当「无成交」误删）",
        rows.get("z_2", {}).get("median48") == 3.0,
        str(rows.get("z_2")),
    )
    check(
        "有 48h 成交（z_0）⇒ 旧行被新值覆盖（median48=12.5）",
        rows.get("z_0", {}).get("median48") == 12.5,
        str(rows.get("z_0")),
    )
    check(
        "跑满一轮但 1/3 失败（67% < 80%）⇒ cursor 归零、ts 不刷新、记 low_coverage、z_2 进待补",
        store.get("cursor") == 0
        and not store.get("ts")
        and store.get("low_coverage") is True
        and store.get("retry") == ["z_2"],
        str({k: store.get(k) for k in ("ts", "low_coverage", "retry", "coverage")}),
    )


asyncio.run(main2())


# ---------------------------------------------------------------------------
# 2026-10-17 覆盖率闸门（维护者定 80%）：游标跑完 ≠ 成功跑完
# ---------------------------------------------------------------------------
class FlakyClient(WarframeClient):
    """10 个物品；fail 集合里的请求抛异常。"""

    def __init__(self, store: dict, fail: set, n: int = 10):
        self.store, self.fail, self.n, self.calls = store, fail, n, []

    async def wm_items(self):
        return [
            {"url_name": f"f_{i}", "id": str(i), "zh": f"物{i}", "en": f"I{i}", "tags": ["prime"]}
            for i in range(self.n)
        ]

    async def wm_statistics(self, slug, platform="pc"):
        self.calls.append(slug)
        if slug in self.fail:
            raise RuntimeError("429")
        return {"h48": [{"median": 5, "min": 4, "max": 6, "volume": 1}], "d90": []}

    def _load_json_file(self, path):
        return self.store

    def _save_json_file(self, path, data):
        self.store.clear()
        self.store.update(data)


async def main3():
    AC._rank_candidate = lambda it: True
    WarframeClient._rank_record = staticmethod(_rank_record_stub)

    # ① 2/10 失败 = 80% ⇒ 刚好达标：刷新 ts，失败项进待补
    st: dict = {}
    c = FlakyClient(st, {"f_3", "f_7"})
    await c.crawl_wm_ranks()
    check("80% 达标 ⇒ ts 刷新", bool(st.get("ts")) and st.get("low_coverage") is False, str(st))
    check("80% 达标 ⇒ 失败项进待补", st.get("retry") == ["f_3", "f_7"], str(st.get("retry")))
    check("每行带成功时间 at", all(r.get("at") for r in st["rows"].values()), str(st["rows"]))

    # ② 下一轮先补待补项：f_3 恢复 ⇒ 出待补；f_7 仍失败 ⇒ 留着
    c.fail, c.calls = {"f_7"}, []
    st["cursor"] = 0
    await c.crawl_wm_ranks(limit=2)
    check(
        "下一轮先补待补项（前两次请求就是 f_3、f_7）", c.calls[:2] == ["f_3", "f_7"], str(c.calls)
    )
    check("补成功的出待补、失败的留着", st.get("retry") == ["f_7"], str(st.get("retry")))

    # ③ 3/10 失败 = 70% ⇒ ts 不动 + low_coverage
    st3: dict = {"ts": "2026-10-01T00:00:00+00:00"}
    c3 = FlakyClient(st3, {"f_1", "f_2", "f_3"})
    await c3.crawl_wm_ranks()
    check(
        "70% ⇒ ts 不刷新、low_coverage、覆盖率 0.7",
        st3.get("ts") == "2026-10-01T00:00:00+00:00"
        and st3.get("low_coverage") is True
        and st3.get("coverage") == 0.7,
        str({k: st3.get(k) for k in ("ts", "low_coverage", "coverage")}),
    )

    # ④ 上一轮不足，下次补抓后达标 ⇒ 刷新 ts、解除 low_coverage
    c3.fail = set()
    await c3.crawl_wm_ranks(limit=3)
    check(
        "补抓后达标 ⇒ ts 刷新、解除 low_coverage",
        st3.get("ts") != "2026-10-01T00:00:00+00:00" and st3.get("low_coverage") is False,
        str({k: st3.get(k) for k in ("ts", "low_coverage", "retry")}),
    )

    # ⑤ 全部失败 ⇒ ts 不动（整榜全失败不得获得新鲜时间）
    st5: dict = {"ts": "2026-10-01T00:00:00+00:00"}
    await FlakyClient(st5, {f"f_{i}" for i in range(10)}).crawl_wm_ranks()
    check(
        "整榜全失败 ⇒ ts 不刷新", st5.get("ts") == "2026-10-01T00:00:00+00:00", str(st5.get("ts"))
    )

    # ⑥ 物品表为空（取不到）⇒ 不刷新 ts、不写盘
    st6: dict = {"ts": "2026-10-01T00:00:00+00:00", "rows": {"x": {}}}
    c6 = FlakyClient(st6, set(), n=0)
    c6._rank_save_failed = True
    n6 = await c6.crawl_wm_ranks()
    check(
        "物品表为空 ⇒ 跳过、不刷新 ts、行数照旧",
        st6.get("ts") == "2026-10-01T00:00:00+00:00" and n6 == 1,
        str(st6),
    )
    check("空表向面板留痕，旧落盘失败不沿用", c6._rank_source_empty and not c6._rank_save_failed)
    c6.n = 10
    await c6.crawl_wm_ranks()
    check("恢复可用物品表后清除空表状态", not c6._rank_source_empty)


asyncio.run(main3())


# 单飞（2026-10-24）：自动循环与手动「排行 刷新」/ 面板按钮同时触发 ⇒ 只抓一份，手动入口报「在抓」
class SlowClient(FakeClient):
    async def wm_statistics(self, slug, platform="pc"):
        await asyncio.sleep(0.001)
        return await super().wm_statistics(slug, platform)


async def main4():
    c = SlowClient({})
    first = asyncio.ensure_future(c.crawl_wm_ranks())
    await asyncio.sleep(0)
    started, _, _ = c.start_rank_crawl()
    await asyncio.gather(first, c.crawl_wm_ranks())
    check("★ 单飞：并发两轮只抓一份（20 次而非 40 次）", c.calls == 20, str(c.calls))
    check("★ 单飞：自动循环在抓时手动入口不另起一份", started is False)


asyncio.run(main4())


async def main5():
    """真实入口及落盘：目标序列迁移/变化、补抓去重、失败或取消后安全续跑。"""

    class DiskClient(WarframeClient):
        def __init__(self):
            super().__init__(flare_enabled=False)
            self.order, self.calls, self.fail = list("abcd"), [], set()
            self.block = None
            self.entered = asyncio.Event()

        async def wm_items(self):
            return [{"url_name": s, "en": s, "tags": ["mod"]} for s in self.order]

        async def wm_statistics(self, slug, platform="pc"):
            self.calls.append(slug)
            if slug == self.block:
                self.entered.set()
                await asyncio.Event().wait()
            if slug in self.fail:
                raise RuntimeError("fixture network failure")
            return {"h48": [{"median": 100, "volume": 1}]}

    previous = paths._RUN
    with (
        tempfile.TemporaryDirectory(prefix="wf_rank_sequence_") as temp,
        patch.object(AC, "_rank_candidate", _REAL_RANK_CANDIDATE),
        patch.object(WarframeClient, "_rank_record", _REAL_RANK_RECORD),
    ):
        paths.set_run_dir(temp)
        target = Path(temp) / AC.RANKS_NAME
        c = DiskClient()
        try:
            await c.crawl_wm_ranks(limit=2)
            data = c._load_json_file(target)
            check(
                "普通榜保存目标序列及部分游标",
                data["cursor_targets"] == list("abcd") and data["cursor"] == 2,
            )
            c.calls.clear()
            await c.crawl_wm_ranks(limit=1)
            check("目标序列不变：从c续跑，不无故回0", c.calls == ["c"])
            c.order.reverse()
            c.calls.clear()
            await c.crawl_wm_ranks()
            data = c._load_json_file(target)
            check(
                "目标重排：按新序列重扫，不漏d且整轮后才标100%",
                c.calls == list("dcba")
                and set(data["rows"]) == set("abcd")
                and data["coverage"] == 1.0,
            )

            legacy = {"cursor": 2, "rows": {"a": {}}, "coverage": 1.0, "low_coverage": True}
            target.write_text(json.dumps(legacy))
            c.order, c.calls = list("abcd"), []
            await c.crawl_wm_ranks(limit=1)
            data = c._load_json_file(target)
            check(
                "旧数字断点首次保守迁移，不沿用覆盖率认证",
                c.calls == ["a"]
                and data["cursor"] == 1
                and data["coverage"] is None
                and not data["ts"],
            )
            c.calls.clear()
            await c.crawl_wm_ranks(limit=1)
            check("旧断点迁移后按新序列续跑", c.calls == ["b"])

            data = c._load_json_file(target)
            data.update(
                round_failed=["gone"],
                retry=["gone", "gone"],
                rows={**data["rows"], "gone": {}},
                low_coverage=True,
            )
            target.write_text(json.dumps(data))
            c.order, c.calls = list("abce"), []
            await c.crawl_wm_ranks(limit=1)
            data = c._load_json_file(target)
            check(
                "目标增删：清理退出行/failed/retry，重扫且不提前认证",
                c.calls == ["a"]
                and "gone" not in data["rows"]
                and "gone" not in data["round_failed"]
                and "gone" not in data["retry"]
                and data["cursor_targets"] == list("abce")
                and data["coverage"] is None,
            )

            target.write_text(
                json.dumps({"cursor_targets": list("abcd"), "cursor": 0, "retry": ["b", "b"]})
            )
            c.order, c.calls, c.fail = list("abcd"), [], {"b"}
            await c.crawl_wm_ranks()
            data = c._load_json_file(target)
            check(
                "补抓重叠：同项只请求一次，失败不因二次尝试误计成功",
                c.calls == list("bacd")
                and data["coverage"] == 0.75
                and data["retry"] == ["b"]
                and not data["ts"],
            )

            # 落盘失败必须保留旧检查点，下一次按原断点恢复，不跳过未保存的新行。
            c.fail.clear()
            c.calls.clear()
            before = target.read_bytes()
            with patch.object(c, "_save_json_file", return_value=False):
                await c.crawl_wm_ranks(limit=1)
            check(
                "落盘失败保留磁盘检查点并向健康面板留痕",
                target.read_bytes() == before and c._rank_save_failed,
            )
            await c.crawl_wm_ranks()
            check(
                "落盘失败后重试可完整恢复",
                set(c._load_json_file(target)["rows"]) == set("abcd") and not c._rank_save_failed,
            )

            target.write_text(
                json.dumps({"cursor_targets": list("abcd"), "cursor": 1, "rows": {"a": {}}})
            )
            c.calls, c.block = [], "c"
            task = asyncio.create_task(c.crawl_wm_ranks())
            await asyncio.wait_for(c.entered.wait(), 2)
            task.cancel()
            try:
                await task
                check("取消保留CancelledError", False)
            except asyncio.CancelledError:
                check("取消保留CancelledError", True)
            check(
                "取消不超前写游标，专用锁释放",
                c._load_json_file(target)["cursor"] == 1
                and not c._crawl_lock("_rank_crawl_lock").locked(),
            )
            c.calls, c.block = [], None
            await c.crawl_wm_ranks()
            check(
                "取消后按最后落盘断点恢复，允许补抓但不漏项",
                c.calls == list("bcd") and set(c._load_json_file(target)["rows"]) == set("abcd"),
            )
        finally:
            paths.set_run_dir(previous)
            await c.close()


asyncio.run(main5())


async def handler_protection():
    """真实 parser → handler：权限拒绝发生在启动调用之前，读取不启动后台。"""
    from core.commands.market import MarketCommands
    from core.parser import parse

    class Client(FakeClient):
        def __init__(self):
            super().__init__({"done": 3, "total": 20})
            self.starts = 0
            self.result = (True, 3, 20)
            self.error = None
            self.riven_state = "cooldown"
            self.weekly_forces = []

        def start_rank_crawl(self):
            self.starts += 1
            if self.error:
                raise self.error
            return self.result

        def rank_rows(self):
            return []

        async def de_weekly_rivens(self, platform, force=False):
            self.weekly_forces.append(force)
            return {"platform": "PC", "rows": []}

        async def wm_riven_weapons(self):
            return []

        def riven_realtime_rows(self):
            return {}

        def start_riven_rt_crawl(self, *, force=False):
            check("紫卡群入口不绕过冷却", not force)
            self.starts += 1
            return self.riven_state, 3, 20

    class Host(MarketCommands):
        def _is_admin(self, event):
            return event == "admin"

    client = Client()
    host = Host()
    host.client = client
    reply = await host._h_rank(parse("排行 刷新"), "member", "pc")
    check("普通用户刷新在启动前拒绝", client.starts == 0 and "只有管理员" in "".join(reply.lines))
    reply = await host._h_rank(parse("排行"), "member", "pc")
    check(
        "普通用户可查看排行且引导由管理员启动",
        client.starts == 0 and "管理员" in "".join(reply.lines),
    )
    reply = await host._h_rank(parse("排行 进度"), "member", "pc")
    check("普通用户可只读进度", client.starts == 0 and "3/20" in "".join(reply.lines))
    reply = await host._h_rank(parse("排行 刷新"), "admin", "pc")
    check(
        "管理员刷新启动并展示进度",
        client.starts == 1 and "已启动" in "".join(reply.lines) and "3/20" in "".join(reply.lines),
    )
    client.result = (False, 3, 20)
    reply = await host._h_rank(parse("排行 刷新"), "admin", "pc")
    check(
        "管理员已有任务显示进行中进度",
        "已在进行中" in "".join(reply.lines) and "3/20" in "".join(reply.lines),
    )
    client.error = AC.WarframeAPIError("排行冷却中，请在 30 分钟后再试")
    reply = await host._h_rank(parse("排行 刷新"), "admin", "pc")
    text = "".join(reply.lines)
    check(
        "排行冷却明确反馈不伪称运行", "未启动" in text and "冷却" in text and "进行中" not in text
    )
    client.starts = 0
    reply = await host._h_rank(parse("紫卡排行 刷新"), "member", "pc")
    check("紫卡普通用户保留周报刷新不启动WM", client.starts == 0 and client.weekly_forces[-1])
    reply = await host._h_rank(parse("紫卡排行 刷新"), "admin", "pc")
    text = "".join(reply.lines)
    check(
        "紫卡管理员冷却中文提示",
        client.starts == 1 and "冷却" in text and "10 分钟" in text and "cooldown" not in text,
    )


asyncio.run(handler_protection())


async def rank_cooldown_lifecycle():
    """真实启动器与后台任务：虚拟时钟覆盖开始/结束窗口和异常取消。"""

    class Controlled(FakeClient):
        def __init__(self):
            super().__init__({"done": 2, "total": 20})
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.runs = 0
            self.fail = False

        async def crawl_wm_ranks(self):
            self.runs += 1
            self.entered.set()
            await self.release.wait()
            if self.fail:
                raise RuntimeError("offline failure")
            return 1

    async def blocked(c, name):
        try:
            c.start_rank_crawl()
        except AC.WarframeAPIError as exc:
            check(name, "30 分钟" in str(exc))
        else:
            check(name, False)

    now = [10000.0]
    with patch.object(AC.time, "time", side_effect=lambda: now[0]):
        for outcome in ("成功", "失败", "取消", "调度前取消"):
            c = Controlled()
            check(f"{outcome}首次可启动", c.start_rank_crawl() == (True, 2, 20))
            task = c._rank_crawl_task
            check(f"{outcome}并发启动复用进度", c.start_rank_crawl() == (False, 2, 20))
            if outcome == "调度前取消":
                task.cancel()
            else:
                await c.entered.wait()
                # 持续超过启动窗口，结束后仍应重新保护完整30分钟。
                now[0] += 2000
                c.fail = outcome == "失败"
                if outcome == "取消":
                    task.cancel()
                else:
                    c.release.set()
            try:
                await task
            except (RuntimeError, asyncio.CancelledError):
                pass
            await asyncio.sleep(0)
            finished = now[0]
            await blocked(c, f"{outcome}结束立即冷却")
            now[0] = finished + 1799
            await blocked(c, f"{outcome}结束未满30分钟拒绝")
            now[0] = finished + 1800
            check(f"{outcome}结束满30分钟可重试", c.start_rank_crawl()[0])
            c._rank_crawl_task.cancel()
            try:
                await c._rank_crawl_task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0)
            check(f"{outcome}无重叠后台轮次", c.runs == (0 if outcome == "调度前取消" else 1))


asyncio.run(rank_cooldown_lifecycle())


async def automatic_batch_manual_guard():
    """真实自动crawl退出后保护手动入口，但不阻塞下个游标续批。"""

    class BatchClient(FakeClient):
        def __init__(self):
            super().__init__({})
            self.block = False
            self.entered = asyncio.Event()

        async def wm_statistics(self, slug, platform="pc"):
            if self.block:
                self.entered.set()
                await asyncio.Event().wait()
            return await super().wm_statistics(slug, platform)

    now = [20000.0]
    with (
        patch.object(AC.time, "time", side_effect=lambda: now[0]),
        patch.object(AC, "_rank_candidate", lambda item: True),
        patch.object(WarframeClient, "_rank_record", staticmethod(_rank_record_stub)),
    ):
        for outcome in ("成功", "取消"):
            c = BatchClient()
            await c.crawl_wm_ranks(limit=2)
            if outcome == "取消":
                now[0] += 2000  # 先让前一成功批的保护过期，避免掩盖取消未记结束时间。
                c.block = True
                task = asyncio.create_task(c.crawl_wm_ranks(limit=2))
                await c.entered.wait()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                c.block = False
            for delta in (0, 1799):
                now[0] += delta
                try:
                    c.start_rank_crawl()
                except AC.WarframeAPIError as exc:
                    check(f"自动批次{outcome}退出后{delta}秒拒绝手动", "30 分钟" in str(exc))
                else:
                    check(f"自动批次{outcome}退出后{delta}秒拒绝手动", False)
            before = c.calls
            await c.crawl_wm_ranks(limit=2)
            check(
                f"自动批次{outcome}退出不阻塞直接续批且游标续进",
                c.calls == before + 2 and c.store["cursor"] == 4,
            )
            now[0] += 1800
            check(f"自动续批{outcome}退出满30分钟允许手动", c.start_rank_crawl()[0])
            c._rank_crawl_task.cancel()
            try:
                await c._rank_crawl_task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0)


asyncio.run(automatic_batch_manual_guard())

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("全部通过 ✔")
