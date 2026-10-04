# -*- coding: utf-8 -*-
"""价格榜单“游标续跑 + 每轮限量”离线测试：python3 tests/test_rank_crawl.py

2026-09-14：全量抓取一轮要好几个小时且占用 WM 全局限速（拖慢用户 wm/wr），
改成：每轮最多 limit 项、落盘 cursor 续跑、跑满一轮才刷新 ts。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import api_client as AC  # noqa: E402
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
        "跑满一轮：cursor 归零 + ts 已刷新",
        store.get("cursor") == 0 and bool(store.get("ts")),
        str(store.get("ts")),
    )


asyncio.run(main2())

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("全部通过 ✔")
