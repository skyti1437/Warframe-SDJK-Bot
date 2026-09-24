# -*- coding: utf-8 -*-
"""扫「官方名省略形式被指向别的物品」这一类错映射（审计脚本 A 类抓不到的那批）。

审计脚本 audit_wm_aliases.py 的 A 类只做**精确**官方名比对；本扫描补上
**省略形式**：词典键是某官方名（简中/英文）的前缀或后缀片段，却指向了另一个物品。
例：「充沛」是官方简中「赋能·充沛」(Arcane Energize) 的尾段，词典却指向
primed_flow（川流不息 Prime）——2026-09-24 实测报障同类问题。
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WM_ITEMS_URL = "https://api.warframe.market/v2/items"


def norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def main() -> int:
    table = json.loads((ROOT / "core" / "data" / "aliases.json").read_text(
        encoding="utf-8")).get("wm_items", {})
    req = urllib.request.Request(WM_ITEMS_URL, headers={
        "User-Agent": "wfq-alias-audit", "Accept": "application/json",
        "Language": "zh-hans"})
    items = json.load(urllib.request.urlopen(req, timeout=90)).get("data") or []
    by_url, names = {}, []
    for it in items:
        i18n = it.get("i18n") or {}
        zh = (i18n.get("zh-hans") or {}).get("name", "")
        en = (i18n.get("en") or {}).get("name", "")
        rec = {"url_name": it.get("slug", ""), "zh": zh, "en": en}
        by_url[rec["url_name"]] = rec
        for n, kind in ((zh, "简中"), (en, "英文")):
            if n:
                names.append((norm(n), rec, kind))

    hits = []
    for key, url in sorted(table.items()):
        k = norm(key)
        if len(k) < 2:
            continue
        for n, rec, kind in names:
            if n == k or len(k) >= len(n):
                continue
            # 收紧规则：键必须是官方名**最后一个分隔段**（「赋能·充沛」→「充沛」、
            # 「镀层 准确射手」→「准确射手」）。整段包含会产生大量
            # 「基体 vs 镀层/Prime 升级版」的合理选择噪声（96 条 → 个位数）。
            tail = re.split(r"[·・：:\s]+", n)[-1]
            if k == tail and rec["url_name"] != url:
                hits.append((key, url, by_url.get(url, {}), rec, kind, n))
                break

    print(f"候选 {len(hits)} 条（键 = 官方名尾段 却指向别的物品）：")
    for key, url, cur, rec, kind, n in hits:
        print(f"  '{key}' → {url} ({cur.get('zh') or cur.get('en') or '?'})")
        print(f"       官方名同尾段在 {rec['url_name']} = {rec['zh']} / {rec['en']}"
              f"（{kind}名『{n}』）—— 请人工判断哪边是社区本意")
    return 0


if __name__ == "__main__":
    sys.exit(main())
