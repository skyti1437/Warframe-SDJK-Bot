# -*- coding: utf-8 -*-
"""构建期：仲裁节点「社区实测中位数 → 档位」表 ``core/data/arb_ratings.json``。

数据源（arbi.wf.wiki，社区维护、非官方）：
  · ``arbys.schedule.v2.json``   排期（取其中出现过的节点 ID 全集 = 88）
  · ``arbys.nodes.zh.json``      节点中文名 / 星球 / 任务类型（仅供人工核对）
  · ``/api/leaderboard?board=browse&nodeId=<ID>&page=N``   逐条实测记录（每页 50）

口径（2026-10-04 定稿，不可搞错）：
  · 按记录 ``id`` **去重**；剔除 ``perHour < 300``；剩余 **``n >= 3``** 才出档；
  · 档位 = 该节点 ``perHour`` **中位数** 套**绝对阈值**（与记录自带 ``grade`` 同规则，
    2026-10-04 拟合 9315/9317 = 99.98%）：S ≥ 800 / A+ ≥ 700 / A ≥ 600 / A- ≥ 500 / F < 500；
  · ★ 对**每个节点分别**取中位数 —— 不是全站混池、不是取最高值、不是跨节点平均。

★ 口径提醒（维护者 2026-10-04 补充）：**实测值（尤其「生存 / 中断」）吃队伍熟练度** ——
  记录来自上传的高水平队伍，普通队伍未必打得到该数值 ⇒ 卡片脚注与本表 `_note` 都要写明。

★ 失败要吵：某节点拉取失败会**重试**，仍失败则**整体报错退出、绝不写盘**
  （原型踩过坑：``SolNode167`` 一次 URLError 就被静默写成 0 条）。
★ 与 arbi 官方 tierlist 的关系：本表**只存实测档**；运行期由 ``core/arbi.py`` 做
  **三级回落**（arbi tierlist 优先 → 实测档 → 未评级），本生成器不碰 tierlist，
  也不重排它的 16 个官方评级。

用法：
    python scripts/build_arb_ratings.py            # 拉全量并写盘
    python scripts/build_arb_ratings.py --check    # 只对拍现有文件（不写盘）
    python scripts/build_arb_ratings.py --cache <原始记录.json>   # 用已落盘的原始记录离线复算
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core" / "data" / "arb_ratings.json"
DATA = "https://arbi.wf.wiki/data/"
API = "https://arbi.wf.wiki/api/leaderboard"
UA = {"User-Agent": "sdjk-build-arb/1.0"}
MIN_PER_HOUR = 300  # 剔除明显过低值（全站 <300 仅 0.5%）
MIN_N = 3  # 样本不足不出档
# ★ 2026-10-16 用户口径：F 放宽为中位数 < 450（原 < 500，与数据源站点分档一致）⇒ A- 下限 500 → 450。
#   这一档从此与站点分档不再一一对应（450–500 的节点站点记 F、我们记 A-）。
TIERS = ((800, "S"), (700, "A+"), (600, "A"), (450, "A-"))
RETRY = 5


def get(url: str, timeout: int = 45):
    return json.load(
        urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
    )


def get_retry(url: str):
    """重试拉取（指数退避）。★ arbi 站偶发 SSL EOF（SolNode167 第 3 页实测），
    退避后通常能过；5 次仍失败才抛（调用方保证不写盘）。"""
    last = None
    for i in range(RETRY):
        try:
            return get(url)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2.0 * (i + 1))
    raise RuntimeError("拉取失败（重试 %d 次）：%s -> %s" % (RETRY, url, last))


def tier_of(median: float) -> str:
    for lo, t in TIERS:
        if median >= lo:
            return t
    return "F"


def fetch_all_nodes(resume_from: "str | None" = None, flush_to: "str | None" = None) -> dict:
    """拉全 88 个排期节点的原始记录（按 id 去重）。

    ★ 断点续抓：``resume_from`` 已存在的原始记录文件会被复用（已抓节点直接跳过），
    每抓完一个节点就 ``flush_to`` 落盘一次 —— 站点偶发抖动时不必从头再来。
    """
    prev = {}
    if resume_from:
        try:
            prev = json.loads(Path(resume_from).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            prev = {}
    sched = get_retry(DATA + "arbys.schedule.v2.json")
    nodes_meta = get_retry(DATA + "arbys.nodes.zh.json").get("nodes") or {}
    node_ids = sorted(set(sched["nodes"]))
    print("排期节点：%d 个" % len(node_ids), flush=True)
    out: dict[str, list] = {}
    for i, nid in enumerate(node_ids, 1):
        if nid in prev:
            out[nid] = prev[nid]
            n = nodes_meta.get(nid) or {}
            print(
                "[%2d/%d] %-12s %-12s %-8s 记录=%d（续用已抓）"
                % (
                    i,
                    len(node_ids),
                    nid,
                    n.get("nameZh", "?"),
                    n.get("missionNameZh", "?"),
                    len(prev[nid]),
                ),
                flush=True,
            )
            continue
        rows, seen, page = [], set(), 1
        while True:
            d = get_retry(
                "%s?%s"
                % (
                    API,
                    urllib.parse.urlencode({"board": "browse", "nodeId": nid, "page": str(page)}),
                )
            )
            rs = d.get("rows") or []
            for r in rs:
                rid = r.get("id")
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                rows.append(r)
            total = d.get("total") or 0
            if len(rs) < 50 or len(rows) >= total:
                break
            page += 1
            time.sleep(0.1)
        out[nid] = rows
        if flush_to:
            Path(flush_to).write_text(
                json.dumps(out, ensure_ascii=False), encoding="utf-8", newline="\n"
            )
        n = nodes_meta.get(nid) or {}
        print(
            "[%2d/%d] %-12s %-12s %-8s 记录=%d"
            % (i, len(node_ids), nid, n.get("nameZh", "?"), n.get("missionNameZh", "?"), len(rows)),
            flush=True,
        )
        time.sleep(0.15)
    return out


def build(raw: dict, nodes_meta: dict) -> dict:
    """原始记录 → 新表（逐节点中位数 + 档位）。"""
    out = {}
    for nid, rows in raw.items():
        ph = [r.get("perHour") for r in rows if isinstance(r.get("perHour"), (int, float))]
        used = [v for v in ph if v >= MIN_PER_HOUR]
        n = nodes_meta.get(nid) or {}
        rec = {
            "name": n.get("nameZh") or "?",
            "system": n.get("systemNameZh") or "",
            "mission": n.get("missionNameZh") or "",
            "n": len(used),
        }
        if len(used) >= MIN_N:
            med = round(float(statistics.median(used)), 1)
            rec["median"] = med
            rec["tier"] = tier_of(med)
        out[nid] = rec
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只对拍不写盘")
    ap.add_argument("--cache", help="用已落盘的原始记录 JSON（{nodeId: [rows]}）离线复算")
    ap.add_argument("--raw-out", help="把原始记录落盘（便于日后离线复算）")
    args = ap.parse_args()

    nodes_meta = get_retry(DATA + "arbys.nodes.zh.json").get("nodes") or {}
    if args.cache:
        raw = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    else:
        raw = fetch_all_nodes()
    if args.raw_out:
        Path(args.raw_out).write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8", newline="\n"
        )

    nodes = build(raw, nodes_meta)
    rated = {k: v for k, v in nodes.items() if v.get("tier")}
    dist = Counter(v["tier"] for v in rated.values())
    print("\n有档位 %d / %d（剔除 <%d 且 n>=%d）" % (len(rated), len(nodes), MIN_PER_HOUR, MIN_N))
    print("档位分布：", dict(sorted(dist.items())))
    print(
        "未出档 %d 个：" % (len(nodes) - len(rated)),
        [nodes[k]["name"] for k in nodes if not nodes[k].get("tier")],
    )
    print("\n逐节点（回报用）：")
    for k in sorted(nodes, key=lambda x: nodes[x]["name"] or ""):
        v = nodes[k]
        print(
            "  %-12s %-10s %-8s n=%-4d median=%-7s tier=%s"
            % (k, v["name"], v["mission"], v["n"], v.get("median", "-"), v.get("tier", "-"))
        )

    payload = {
        "_note": (
            "社区实测（arbi.wf.wiki 排行榜），非官方数据，仅供参考；"
            "★ 实测值（尤其「生存 / 中断」）吃队伍熟练度——来自上传记录的高水平队伍，"
            "普通队伍未必打得到"
        ),
        "_source": "https://arbi.wf.wiki/api/leaderboard?board=browse&nodeId=<key>",
        "_rule": (
            "每节点 perHour 中位数（按记录 id 去重，剔除 <%d，n>=%d）；"
            "S>=800 / A+>=700 / A>=600 / A->=450 / F<450" % (MIN_PER_HOUR, MIN_N)
        ),
        "_updated": date.today().isoformat(),
        "nodes": dict(sorted(nodes.items())),
    }
    if args.check:
        cur = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
        same = cur.get("nodes") == payload["nodes"]
        print("\n--check：与现表逐节点一致 =", same)
        if not same:
            cn = cur.get("nodes") or {}
            for k in sorted(set(cn) | set(nodes)):
                if cn.get(k) != nodes.get(k):
                    print("   差异", k, cn.get(k), "->", nodes.get(k))
        return 0 if same else 1
    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    print(
        "\n写出 %s（%d KB，%d 节点）"
        % (OUT.relative_to(ROOT), OUT.stat().st_size // 1024, len(nodes))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
