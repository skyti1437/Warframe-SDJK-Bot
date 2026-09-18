# -*- coding: utf-8 -*-
"""构建 Baro Ki'Teer 历史库存（用于「预测」）。

数据源：WARFRAME Wiki 的 ``Module:Baro/data``（每件物品的历次上架日期）。
该页需经 FlareSolverr 抓取（Cloudflare），抓取命令见文件末尾。

产出：core/data/baro_history.json
    { source, fetched, visits: [...], items: {name: {t, d, c, v}} }
    · visits  = 所有访问日（升序，来自全物品日期并集）
    · v       = 该物品出现的**访问索引**（而非日期串）→ 体积小一个量级

用法：
    python scripts/build_baro_history.py --raw <抓下来的源码.txt>
    python scripts/build_baro_history.py --raw ... --check   # 只看统计
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "core" / "data"
OUT = DATA / "baro_history.json"

DATE_RE = re.compile(r'"(\d{4}-\d{2}-\d{2})"')


def split_items(text: str) -> dict[str, str]:
    """把 ``["Items"]`` 里的每个 ``["名字"] = { ... }`` 块切出来。

    用**括号配平**而不是正则 —— 块内有嵌套表（OfferingDates），
    非贪婪正则会在第一个 `}` 就截断。"""
    start = text.find('["Items"]')
    if start < 0:
        raise SystemExit("找不到 Items 段（wiki 结构变了？）")
    i = text.find("{", start)
    depth, begin = 0, None
    blocks: dict[str, str] = {}
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
            if depth == 2:                        # 进入某个物品块
                begin = i
        elif ch == "}":
            if depth == 2 and begin is not None:
                # 回溯找该块的名字：形如 ["xxx"] = {
                head = text[max(0, begin - 400):begin]
                m = None
                for m in re.finditer(r'\["((?:[^"\\]|\\.)+)"\]\s*=\s*$', head):
                    pass
                if m:
                    blocks[m.group(1)] = text[begin:i + 1]
            depth -= 1
            if depth == 0:                        # Items 表结束
                break
        i += 1
    return blocks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="Module:Baro/data 的源码文本")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    text = Path(args.raw).read_text(encoding="utf-8", errors="replace")
    blocks = split_items(text)
    print(f"解析出物品块：{len(blocks)}")

    # ① 所有访问日（全物品日期并集，升序）—— 这就是 Baro 的到访日历
    all_dates: set[str] = set()
    raw_items: dict[str, dict] = {}
    for name, blk in blocks.items():
        dates = sorted(set(DATE_RE.findall(
            re.search(r"OfferingDates\s*=\s*\{(.*?)\}", blk, re.S).group(1)
            if re.search(r"OfferingDates\s*=\s*\{", blk) else "")))
        if not dates:
            dates = sorted(set(DATE_RE.findall(blk)))
        ducat = re.search(r"DucatCost\s*=\s*(\d+)", blk)
        credit = re.search(r"CreditCost\s*=\s*(\d+)", blk)
        typ = re.search(r'Type\s*=\s*"([^"]*)"', blk)
        if not dates:
            continue
        all_dates |= set(dates)
        raw_items[name] = {
            "t": typ.group(1) if typ else "",
            "d": int(ducat.group(1)) if ducat else 0,
            "c": int(credit.group(1)) if credit else 0,
            "dates": dates,
        }

    visits = sorted(all_dates)
    idx = {d: i for i, d in enumerate(visits)}
    print(f"访问日：{len(visits)} 次（{visits[0]} → {visits[-1]}）")
    print(f"有库存记录的物品：{len(raw_items)}")

    items = {name: {"t": r["t"], "d": r["d"], "c": r["c"],
                    "v": [idx[d] for d in r["dates"] if d in idx]}
             for name, r in raw_items.items()}
    # 用最后 20 次访问做「近月」统计参考
    out = {
        "source": "wiki.warframe.com · Module:Baro/data",
        "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "period_days": 14,
        "visits": visits,
        "items": items,
    }
    blob = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    print(f"产出体积：{len(blob) / 1024:.0f} KB")
    if args.check:
        return 0
    OUT.write_text(json.dumps(out, ensure_ascii=False,
                              separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"已写 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# ---------------------------------------------------------------------------
# 刷新数据（需要服务器上的 FlareSolverr，wiki 有 Cloudflare）：
#
#   cat > /tmp/fb.py <<'PY'
#   import json, re, html as H, urllib.request
#   FL="http://172.17.0.1:8191/v1"
#   U="https://wiki.warframe.com/w/Module:Baro/data?action=raw"
#   req=urllib.request.Request(FL, data=json.dumps(
#       {"cmd":"request.get","url":U,"maxTimeout":120000}).encode(),
#       headers={"Content-Type":"application/json"})
#   h=json.loads(urllib.request.urlopen(req,timeout=160).read())
#   html=(h.get("solution") or {}).get("response") or ""
#   m=re.search(r"<pre[^>]*>(.*?)</pre>", html, re.S)
#   open("/tmp/baro_raw.txt","w",encoding="utf-8").write(
#       H.unescape(m.group(1)) if m else html)
#   PY
#   ssh <你的服务器别名> "docker cp /tmp/fb.py astrbot:/tmp/fb.py &&
#                        docker exec astrbot python3 /tmp/fb.py &&
#                        docker cp astrbot:/tmp/baro_raw.txt /tmp/baro_raw.txt"
#   scp <你的服务器别名>:/tmp/baro_raw.txt /tmp/ && \
#   python scripts/build_baro_history.py --raw /tmp/baro_raw.txt
# ---------------------------------------------------------------------------
