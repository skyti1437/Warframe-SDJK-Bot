#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成倾向补全数据 core/data/dispositions_rivenmirror.json（v2，2026-09-23）。

数据源优先级（2026-09-23 用户确认：wiki.warframe.com 为唯一权威）：
1. **wiki「Riven Mods: Weapon Disposition」页**（628 条，最全最新）——
   输入二选一：
     --wiki-html PATH   本地保存的页面 HTML（浏览器另存为/解包 zip 后的文件）
     --fetch-fs         经服务器 FlareSolverr 抓 canonical 页（自动更新通道）
2. riven-mirror（极镜）disposition.json —— 仅补 wiki 缺失（兜底）。
WM v2 /riven/weapons（418 条）不再作值来源，只提供：zh 名、riven_type/group、
权威 slug；运行期 api_client.wm_riven_weapons() 用本文件**覆盖同名条目的
disposition**并追加缺失条目（否则 WM 手工维护的值会滞后于平衡补丁，
实例：Vectis Prime WM/极镜 0.9 → wiki 1.0）。

zh 来源：WM i18n（表内已有）→ warframe-items name→uniqueName →
DE 官方简中表 name_zh.json。riven_type/group：剥变体 token 后的基类
武器从 WM 表继承。

用法：
    python scripts/build_disposition.py --wiki-html /path/to/page.html
    python scripts/build_disposition.py --fetch-fs
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "core" / "data" / "dispositions_rivenmirror.json"
NAME_ZH = ROOT / "core" / "data" / "de" / "name_zh.json"

WIKI_URL = "https://wiki.warframe.com/w/Riven_Mods/Weapon_Dispos"
MIRROR = "https://raw.githubusercontent.com/pa001024/riven-mirror-data/master/dist/disposition.json"
WM_RIVEN = "https://api.warframe.market/v2/riven/weapons"
WF_ITEMS = "https://fastly.jsdelivr.net/npm/warframe-items@latest/data/json/{}.json"

VARIANT_PREFIX_EN = ("Kuva ", "Tenet ", "Coda ", "Mara ", "Prisma ", "Vandal ",
                     "Wraith ", "Sancti ", "Secura ", "Telos ", "Synoid ",
                     "Rakta ", "Vaykor ", "Carmine ", "Dex ", "MK1-")


def get(url: str, proxy: bool = False):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})) if proxy \
        else urllib.request.build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": "sdjk-build/1.0",
                                               "Language": "zh-hans"})
    return opener.open(req, timeout=90).read()


def fetch_wiki_via_fs() -> str:
    """在服务器上经 FlareSolverr 抓 wiki 页并回传 HTML（自动更新通道）。

    ★ maxTimeout 必须 ≥120000：该页面 CF 挑战+1MB 传输实测 60000 会 500
    （2026-09-23 三连失败），120000 一次通过（900KB）。客户端超时同步放宽。
    ssh 主机从环境变量 ``DEPLOY_SSH_HOST`` 读取（开源版不含内网别名）。
    """
    host = os.environ.get("DEPLOY_SSH_HOST")
    if not host:
        raise RuntimeError("需要 DEPLOY_SSH_HOST 环境变量（FlareSolverr 所在"
                           "服务器的 ssh 别名）")
    inner = (
        "import json,urllib.request\n"
        "def fs(cmd,extra=None,tmo=290):\n"
        "    body={'cmd':cmd,'maxTimeout':120000}\n"
        "    if extra: body.update(extra)\n"
        "    req=urllib.request.Request('http://172.17.0.1:8191/v1',"
        "data=json.dumps(body).encode(),"
        "headers={'Content-Type':'application/json'})\n"
        "    return json.loads(urllib.request.urlopen(req,timeout=tmo).read())\n"
        "sid=''\n"
        "try:\n"
        "    sid=((fs('sessions.create').get('solution') or {}).get('session') or '')\n"
        "    r=fs('request.get',{'url':'" + WIKI_URL + "','session':sid},tmo=260)\n"
        "    sys.stdout.write((r.get('solution') or {}).get('response') or '')\n"
        "finally:\n"
        "    try: fs('sessions.destroy',{'session':sid})\n"
        "    except Exception: pass\n"
    )
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host,
         "python3 -", inner],
        capture_output=True, text=True, timeout=600)
    if out.returncode != 0 or len(out.stdout) < 100000:
        raise RuntimeError(f"FS 抓取失败 rc={out.returncode} "
                           f"stdout={len(out.stdout)}B stderr={out.stderr[-200:]}")
    return out.stdout


def parse_wiki_html(page: str) -> dict[str, float]:
    """解析 wiki 页 `<li><a title=\"EN\">…</a> (值)</li>` 结构。

    href 域名无关：浏览器另存会改写成绝对 URL，FS 原始 HTML 是相对
    `/w/...`——只认 `/w/` 路径段 + title 属性（2026-09-23 实测两种形态）。
    """
    pairs = re.findall(
        r"<a href=\"[^\"]*/w/[^\"]*\" title=\"([^\"]+)\">"
        r".*?</a> \(([\d.]+)\)", page)
    if len(pairs) < 300:
        raise RuntimeError(f"wiki 页解析只得 {len(pairs)} 条，结构可能变了")
    import html as _html
    return {_html.unescape(n): float(v) for n, v in pairs}


def slugify(en: str) -> str:
    return en.lower().replace("'", "").replace(".", "").replace(" ", "_")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki-html", help="本地 wiki 页 HTML 文件")
    ap.add_argument("--fetch-fs", action="store_true",
                    help="经服务器 FlareSolverr 抓取 wiki 页")
    args = ap.parse_args()
    if not (args.wiki_html or args.fetch_fs):
        ap.error("需要 --wiki-html 或 --fetch-fs 之一")

    print("[1/5] wiki 倾向表（主源）…")
    if args.wiki_html:
        page = Path(args.wiki_html).read_text(encoding="utf-8", errors="replace")
    else:
        page = fetch_wiki_via_fs()
    wiki = parse_wiki_html(page)
    print(f"  {len(wiki)} 条")

    print("[2/5] riven-mirror（兜底）…")
    try:
        mirror = {en: v for en, _c, v in json.loads(get(MIRROR, proxy=True))
                  if v}
    except Exception as e:  # noqa: BLE001
        print(f"  拉取失败（跳过）：{e}")
        mirror = {}
    print(f"  {len(mirror)} 条")

    print("[3/5] WM riven 表（zh/riven_type/group/slug）…")
    wm_raw = json.loads(get(WM_RIVEN))["data"]
    wm_by_en: dict[str, dict] = {}
    wm_list = []
    for w in wm_raw:
        i18n = w.get("i18n") or {}
        en = (i18n.get("en") or {}).get("name", "")
        zh = (i18n.get("zh-hans") or {}).get("name", "")
        rec = {"url_name": w.get("slug", ""), "zh": zh,
               "riven_type": w.get("rivenType", ""),
               "group": w.get("group", "")}
        wm_list.append({"en": en, **rec})
        if en:
            wm_by_en[en] = rec
    print(f"  {len(wm_raw)} 条")

    print("[4/5] warframe-items + DE 官方 zh 表 …")
    name2uniq: dict[str, str] = {}
    for cat in ("Primary", "Secondary", "Melee"):
        try:
            data = json.loads(get(WF_ITEMS.format(cat)))
        except Exception as e:  # noqa: BLE001
            print(f"  {cat} 拉取失败（跳过）：{e}")
            continue
        for w in data:
            n = w.get("name")
            if n and w.get("uniqueName"):
                name2uniq.setdefault(n, w["uniqueName"])
    name_zh = json.loads(NAME_ZH.read_text(encoding="utf-8"))
    print(f"  名称 {len(name2uniq)}，DE zh 表 {len(name_zh)}")

    print("[5/5] 合并 …")
    values: dict[str, float] = dict(mirror)
    values.update(wiki)                      # wiki 覆盖 mirror
    out: dict[str, dict] = {}
    wm_url_set = {r["url_name"] for r in wm_list}
    no_zh = no_base = 0
    for en, disp in sorted(values.items()):
        if not en or not disp:
            continue
        if en in wm_by_en:
            info = wm_by_en[en]
            zh = info["zh"]
            rtype, group, url = (info["riven_type"], info["group"],
                                 info["url_name"])
        else:
            uniq = (name2uniq.get(en) or "").lower()
            zh = name_zh.get(uniq, "")
            if not zh:
                no_zh += 1
            base_en = en
            for p in VARIANT_PREFIX_EN:
                if base_en.startswith(p):
                    base_en = base_en[len(p):]
                    break
            base_en = re.sub(r"\s*Prime$", "", base_en)
            info = wm_by_en.get(base_en) or {}
            if not info:
                no_base += 1
            rtype, group = info.get("riven_type", ""), info.get("group", "")
            url = slugify(en)
        out[en] = {"zh": zh, "disposition": disp,
                   "riven_type": rtype, "group": group, "url_name": url}
    covered = sum(1 for e in out.values() if e["url_name"] in wm_url_set)

    payload = {"_说明": "倾向补全/覆盖表（细粒度值）。主源 wiki.warframe.com"
                        "/w/Riven_Mods/Weapon_Dispos（用户确认唯一权威），"
                        "riven-mirror 兜底；zh=WM i18n 或 DE 官方简中表；"
                        "riven_type/group 继承 WM 基类。运行期 "
                        "api_client.wm_riven_weapons() 用本表覆盖同名条目 "
                        "disposition 并追加缺失（WM 手工值滞后于平衡补丁）。"
                        "由 scripts/build_disposition.py 生成。",
               "_wiki_url": WIKI_URL,
               "_生成条数": len(out),
               "entries": out}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1,
                              sort_keys=True), encoding="utf-8")
    print(f"  共 {len(out)} 条（其中覆盖 WM 已有条目 {covered}；"
          f"无 zh {no_zh}、无基类继承 {no_base}）")
    print(f"[OK] {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    for k in ("Vectis Prime", "Rubico", "Rubico Prime", "Kuva Zarr"):
        print(f"  {k:16} -> {json.dumps(out.get(k), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
