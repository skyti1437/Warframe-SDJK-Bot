# -*- coding: utf-8 -*-
"""刷新 Coda（终幕）/ Tenet（信条）的效价加成 —— wiki Reset 页 → rotations.json。

为什么需要这个脚本
------------------
DE 不提供效价加成（Valence Bonus）的 API，唯一来源是 wiki 的 *Current Valence
Bonuses* 玩家上报表。而插件侧直连 wiki.warframe.com 会被 Cloudflare 拦
（index.php / api.php / rest.php 全 403），所以走同机 FlareSolverr。

★ 时效性关键：Coda 的 A/B 两批每 4 天（00:00 UTC）交替（Tenet 同周期、锚点差 24h），
  加成**每次换批重掷**。因此**每次换批后都要跑一次**，否则数据停在旧快照 ——
  这正是「终幕卡片里元素与加成整段丢失」的根因（2026-09-17：B 批生效时
  batches[1] 的 element/bonus 全为 null，卡片只能退化成一段说明文字）。

用法
----
    # 在装有 FlareSolverr 的机器上（172.17.0.1:8191，走 docker0 网桥地址；本地开发需 SSH 隧道且目标同样写 172.17.0.1）
    python3 scripts/fetch_valence.py                 # ★ 默认：抓源页 wikitext（推荐，见下）
    python3 scripts/fetch_valence.py --reset         # 旧路径：抓渲染后的 Reset 页
    python3 scripts/fetch_valence.py --dry-run       # 只打印解析结果，不写文件
    python3 scripts/fetch_valence.py --html /tmp/reset.json   # 离线：已存渲染页响应
    python3 scripts/fetch_valence.py --wikitext coda.txt tenet.txt   # 离线：已存源页 wikitext

★ 2026-10-03 为什么改抓源页 wikitext（Reset 路径保留仅作兼容）：
  Reset 页对这两段是**跨页嵌入**（``{{#lst:Coda Weapons|eleanor_coda_timer}}`` /
  ``{{#lst:Tenet Weapons|glast_tenet_melee_timer}}``），而渲染页走 MediaWiki
  **解析缓存** ⇒ 换批后源页已更新、Reset 仍吐旧内容（实测 2026-10-03：Coda
  换批到 B，源页 A/B 两表俱全，Reset 渲染页却只有 A 表 ⇒ 旧路径抓不到 B）。
  源页 wikitext 是**真源、无解析缓存**，且 A/B 两批同页全量公布 ⇒ 一次抓取两批
  齐全（旧文案「页面只展示当前批」仅对 Reset 渲染页成立，对源页不成立）。
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROTATION_FILE = ROOT / "core" / "data" / "rotations.json"
DEFAULT_FLARE = "http://172.17.0.1:8191/v1"
WIKI_URL = "https://wiki.warframe.com/w/Reset"
# ★ 2026-10-03：真源页（Reset 页的嵌入来源；wikitext 无解析缓存、两批全量）
#   Coda/Tenet 主标题页都是 #REDIRECT ⇒ 直接抓带 "Weapons" 的目标页。
WIKI_RAW_CODA = "https://wiki.warframe.com/w/Coda_Weapons?action=raw"
WIKI_RAW_TENET = "https://wiki.warframe.com/w/Tenet_Weapons?action=raw"

BATCH_IDX = {"A": 1, "B": 2}


def fetch_html(flare: str, url: str = WIKI_URL, timeout: int = 120,
               min_len: int = 50_000) -> str:
    """经 FlareSolverr 取回页面（直连会被 CF 拦）。

    ``min_len``：渲染页要求整页（>50KB）；``?action=raw`` 只有十几 KB，
    调用方传小阈值。
    """
    payload = json.dumps({"cmd": "request.get", "url": url,
                          "maxTimeout": timeout * 1000}).encode("utf-8")
    req = urllib.request.Request(
        flare, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout + 60) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("status") != "ok":
        raise SystemExit(f"FlareSolverr 返回失败：{data.get('message')}")
    body = data.get("solution", {}).get("response") or ""
    if len(body) < min_len:
        raise SystemExit(f"页面疑似未渲染完全（{len(body)} 字节 < {min_len}）")
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


def _wt_plain(cell: str) -> str:
    """wikitext 单元格 → 纯文本（去模板壳不在这里，见 _weapon_row_wt）。"""
    txt = re.sub(r"<[^>]+>", " ", cell)
    return re.sub(r"\s+", " ", _html.unescape(txt)).strip()


def _weapon_row_wt(row: str) -> dict:
    """wikitext 数据行 → ``{en, element, bonus}``。

    行形如 ``<tr><td>Coda Hema</td><td>{{D|Magnetic}}</td>
    <td>{{ValenceBonusPercentageColor|34.2}}</td></tr>``；
    Tenet 页名字带模板壳 ``{{Weapon|Tenet Ferrox}}``。
    """
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
    out = {"en": "", "element": "", "bonus": None}
    if cells:
        m = re.search(r"\{\{\s*Weapon\s*\|\s*([^}|]+?)\s*\}\}", cells[0])
        out["en"] = m.group(1) if m else _wt_plain(cells[0])
    if len(cells) > 1:
        m = re.search(r"\{\{\s*D\s*\|\s*([^}|]+?)\s*\}\}", cells[1])
        out["element"] = m.group(1) if m else _wt_plain(cells[1])
    if len(cells) > 2:
        m = (re.search(r"ValenceBonusPercentageColor\s*\|\s*([\d.]+)", cells[2])
             or re.search(r"([\d.]+)", _wt_plain(cells[2])))
        out["bonus"] = float(m.group(1)) if m else None
    return out


def parse_wikitext(src: str) -> dict:
    """解析 Coda Weapons / Tenet Weapons 源页的 wikitext（真源，无解析缓存）。

    ★ 2026-10-03：Reset 页的这两段是 ``{{#lst:…}}`` 跨页嵌入，渲染页受解析
    缓存影响会落后于源页（换批当天实测：源页 A/B 两表俱全，Reset 渲染页只有
    旧表）⇒ 改从源页 wikitext 解析。A/B 两批**同页全量**，一次抓取即两批齐全。

    ``?action=raw`` 的响应是 ``<pre>`` 包裹、实体转义过的 wikitext ⇒ 先
    ``html.unescape``。返回与 `parse()` 同构：``{"coda": {1:…, 2:…}, "tenet": […]}``。
    """
    src = _html.unescape(src)
    res: dict = {"coda": {}, "tenet": []}
    for tm in re.finditer(r"<table[^>]*>", src):
        start = tm.end()
        end = src.find("</table>", start)
        if end < 0:
            continue
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", src[start:end], re.S)
        heads = [r for r in rows if "<th" in r]          # 表头行（<th>）
        data = [r for r in rows if len(re.findall(r"<td", r)) >= 3]
        if not heads or not data:
            continue
        head = re.sub(r"<[^>]+>", " ", heads[0])
        head = re.sub(r"\s+", " ", _html.unescape(head)).strip()
        mb = re.match(r"Weapon\s*\(Batch\s*([AB])\)", head, re.I)
        if mb:
            idx = BATCH_IDX[mb.group(1).upper()]
            res["coda"][idx] = [_weapon_row_wt(r) for r in data]
            continue
        if head.lower().startswith("weapon"):     # 无批次后缀 = Tenet
            items = [_weapon_row_wt(r) for r in data]
            items = [it for it in items
                     if (it["en"] or "").lower().startswith("tenet")]
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
    ap.add_argument("--reset", action="store_true",
                    help="旧路径：抓渲染后的 Reset 页（受解析缓存影响，可能落后一批）")
    ap.add_argument("--html", help="离线：从已保存的 FlareSolverr 渲染页响应 JSON 解析")
    ap.add_argument("--wikitext", nargs=2, metavar=("CODA_RAW", "TENET_RAW"),
                    help="离线：从已保存的源页 wikitext 解析（Coda Weapons / Tenet Weapons）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.wikitext:
        p_coda = parse_wikitext(Path(args.wikitext[0]).read_text(encoding="utf-8"))
        p_tenet = parse_wikitext(Path(args.wikitext[1]).read_text(encoding="utf-8"))
        parsed = {"coda": dict(p_coda["coda"]), "tenet": p_tenet["tenet"]}
        if not parsed["tenet"]:            # 兜底：源文件顺序与参数相反也能解析
            parsed["tenet"] = p_coda["tenet"]
        if not parsed["coda"]:
            parsed["coda"] = dict(p_tenet["coda"])
    elif args.html:
        raw = json.loads(Path(args.html).read_text(encoding="utf-8"))
        src = raw.get("solution", {}).get("response") or raw.get("html") or ""
        parsed = parse(src)
    elif args.reset:                       # 旧路径（保留兼容；见文件头 ★ 2026-10-03）
        print(f"→ FlareSolverr 抓取（渲染页）{args.url}")
        parsed = parse(fetch_html(args.flare, args.url))
    else:                                  # ★ 默认：源页 wikitext（真源、两批全量）
        print(f"→ FlareSolverr 抓取源页 wikitext：\n   {WIKI_RAW_CODA}\n   {WIKI_RAW_TENET}")
        p_coda = parse_wikitext(fetch_html(args.flare, WIKI_RAW_CODA, min_len=2_000))
        p_tenet = parse_wikitext(fetch_html(args.flare, WIKI_RAW_TENET, min_len=2_000))
        parsed = {"coda": dict(p_coda["coda"]), "tenet": p_tenet["tenet"]}
        if not parsed["coda"]:
            parsed["coda"] = dict(p_tenet["coda"])
        if not parsed["tenet"]:
            parsed["tenet"] = p_coda["tenet"]

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
