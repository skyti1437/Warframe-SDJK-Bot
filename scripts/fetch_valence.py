# -*- coding: utf-8 -*-
"""刷新 Coda（终幕）/ Tenet（信条）的效价加成 —— wiki Reset 页 → rotations.json。

为什么需要这个脚本
------------------
DE 不提供效价加成（Valence Bonus）的 API，唯一来源是 wiki 的 *Current Valence
Bonuses* 玩家上报表。而插件侧直连 wiki.warframe.com 会被 Cloudflare 拦
（index.php / api.php / rest.php 全 403），所以走同机 FlareSolverr。

★ 时效性关键：wiki 页面**只展示当前生效批次**的 Coda 表（表头写作
  ``Weapon (Batch A/B)``），而 A/B 两批每 4 天（00:00 UTC）交替。
  因此**每次换批后都要跑一次**，否则另一半批次的数据会一直停在旧快照 ——
  这正是「终幕卡片里元素与加成整段丢失」的根因（2026-09-17：B 批生效时
  batches[1] 的 element/bonus 全为 null，卡片只能退化成一段说明文字）。

用法
----
    # 在装有 FlareSolverr 的机器上（127.0.0.1:8191，FlareSolverr 部署机本地）
    python3 scripts/fetch_valence.py                 # 抓取并写回 rotations.json
    python3 scripts/fetch_valence.py --dry-run       # 只打印解析结果，不写文件
    python3 scripts/fetch_valence.py --html /tmp/reset.json   # 从已存响应离线解析
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROTATION_FILE = ROOT / "core" / "data" / "rotations.json"
DEFAULT_FLARE = "http://127.0.0.1:8191/v1"
WIKI_URL = "https://wiki.warframe.com/w/Reset"

BATCH_IDX = {"A": 1, "B": 2}


def fetch_html(flare: str, url: str = WIKI_URL, timeout: int = 120) -> str:
    """经 FlareSolverr 取回渲染后的 HTML（直连会被 CF 拦）。"""
    payload = json.dumps({"cmd": "request.get", "url": url,
                          "maxTimeout": timeout * 1000}).encode("utf-8")
    req = urllib.request.Request(
        flare, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout + 60) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("status") != "ok":
        raise SystemExit(f"FlareSolverr 返回失败：{data.get('message')}")
    body = data.get("solution", {}).get("response") or ""
    if len(body) < 50_000:
        raise SystemExit(f"页面疑似未渲染完全（{len(body)} 字节）")
    return body


def _cells(row_html: str) -> list[str]:
    """取 <tr> 里每个 <td>/<th> 的纯文本。"""
    out = []
    for td in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S):
        txt = re.sub(r"<[^>]+>", " ", td)
        out.append(re.sub(r"\s+", " ", _html.unescape(txt)).strip())
    return out


def _weapon_row(cells: list[str]) -> dict:
    """``['Coda Tysis', 'Heat', '48.0%']`` → ``{en, element, bonus}``。"""
    m = re.search(r"([\d.]+)", cells[2]) if len(cells) > 2 else None
    return {"en": cells[0].strip(),
            "element": cells[1].strip() if len(cells) > 1 else "",
            "bonus": float(m.group(1)) if m else None}


def parse(src: str) -> dict:
    """解析页面上的效价表。

    Returns:
        ``{"coda": {1: [...], 2: [...]}, "tenet": [...]}``
        —— coda 的键是**批次号**（1=A、2=B），只含页面当前展示的那一批。
    """
    res: dict = {"coda": {}, "tenet": []}
    for tm in re.finditer(r"<table[^>]*>", src):
        start = tm.end()
        end = src.find("</table>", start)
        if end < 0:
            continue
        rows = [_cells(r) for r in
                re.findall(r"<tr[^>]*>(.*?)</tr>", src[start:end], re.S)]
        rows = [r for r in rows if r and any(r)]
        if not rows:
            continue
        head = rows[0][0].strip()
        mb = re.match(r"Weapon\s*\(Batch\s*([AB])\)", head, re.I)
        if mb:
            idx = BATCH_IDX[mb.group(1).upper()]
            res["coda"][idx] = [_weapon_row(r) for r in rows[1:] if len(r) >= 3]
            continue
        if head.lower() == "weapon":       # 无批次后缀的表 = Tenet（Ergo Glast）
            items = [_weapon_row(r) for r in rows[1:]
                     if len(r) >= 3 and r[0].lower().startswith("tenet")]
            if items:
                res["tenet"] = items
    return res


def apply(data: dict, parsed: dict, dry: bool = False) -> list[str]:
    """把解析结果写进 rotations.json 结构，返回变更描述。"""
    changes: list[str] = []
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _merge(container: list[dict], src_items: list[dict], tag: str) -> int:
        """按英文名对齐，更新 element/bonus；返回更新条数。"""
        by_en = {(s.get("en") or "").lower(): s for s in src_items}
        n = 0
        for it in container:
            src = by_en.get((it.get("en") or "").lower())
            if not src:
                if not it.get("bonus"):
                    changes.append(f"  [缺口] {tag} {it.get('en')} 在 wiki 表中没有")
                continue
            if it.get("bonus") != src["bonus"] or it.get("element") != src["element"]:
                changes.append(
                    f"  {it.get('cn') or it.get('en')}: "
                    f"{it.get('element')} {it.get('bonus')} → "
                    f"{src['element']} {src['bonus']}")
                if not dry:
                    it["element"], it["bonus"] = src["element"], src["bonus"]
                n += 1
            elif not it.get("cn"):
                pass
        return n

    coda = data.setdefault("coda", {})
    batches = coda.get("batches") or []
    for idx, items in sorted(parsed["coda"].items()):
        if not (1 <= idx <= len(batches)):
            changes.append(f"  [跳过] wiki 的 Batch {idx} 超出本地批次表")
            continue
        label = (coda.get("batch_label") or [])[idx - 1] if \
            isinstance(coda.get("batch_label"), list) else str(idx)
        n = _merge(batches[idx - 1], items, f"终幕批{label}")
        changes.append(f"  · Batch {label}：{len(items)} 条来自 wiki，"
                       f"其中 {n} 条数值有变化")
        if not dry:
            coda["valence_snapshot"] = now

    tenet = data.setdefault("tenet", {})
    titems = tenet.get("items") or []
    if parsed["tenet"]:
        n = _merge(titems, parsed["tenet"], "信条")
        changes.append(f"  · Tenet：{len(parsed['tenet'])} 条来自 wiki，"
                       f"其中 {n} 条数值有变化")
        if not dry:
            tenet["valence_snapshot"] = now

    return changes


def detect_indent(text: str, default: int = 1) -> int:
    """探测原文件的缩进宽度，写回时沿用 —— 否则整文件行都会变，diff 没法看。"""
    m = re.search(r"\n( +)\"", text)
    return len(m.group(1)) if m else default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flare", default=DEFAULT_FLARE)
    ap.add_argument("--url", default=WIKI_URL)
    ap.add_argument("--html", help="从已保存的 FlareSolverr 响应 JSON 解析")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.html:
        raw = json.loads(Path(args.html).read_text(encoding="utf-8"))
        src = raw.get("solution", {}).get("response") or raw.get("html") or ""
    else:
        print(f"→ FlareSolverr 抓取 {args.url}")
        src = fetch_html(args.flare, args.url)

    parsed = parse(src)
    print(f"解析到：Coda 批次 {sorted(parsed['coda'])}（各自 "
          f"{ {k: len(v) for k, v in parsed['coda'].items()} } 把）、"
          f"Tenet {len(parsed['tenet'])} 把")
    if not parsed["coda"] and not parsed["tenet"]:
        raise SystemExit("没解析到任何效价表 —— 页面结构可能变了，请人工核对")

    original = ROTATION_FILE.read_text(encoding="utf-8")
    data = json.loads(original)
    changes = apply(data, parsed, dry=args.dry_run)
    print("\n".join(changes))

    if args.dry_run:
        print("\n[dry-run] 未写入文件")
        return
    indent = detect_indent(original)
    ROTATION_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=indent) + "\n",
        encoding="utf-8")
    print(f"\n已写入 {ROTATION_FILE}（缩进 {indent} 空格，与原文件一致）")


if __name__ == "__main__":
    main()
