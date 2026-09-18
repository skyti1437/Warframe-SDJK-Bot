# -*- coding: utf-8 -*-
"""把 wiki `Module:Stances/data` 的 Lua 表解析成结构化数据。

产出 core/data/stances_stats.json：
  stances[归一化名] = {
      name, zh, weapon_type, unique_to_weapon,
      combos: { "Neutral": {name, avg_mult, hits, total, procs:[], ips:{}}, ... }
  }

为什么需要它：架势 MOD 在 warframe-items 里**完全没有数值**（只有极性），
但架势真正决定了近战每一段的伤害倍率与强制异常 —— 不接这块，近战配卡的
「单发伤害」就是裸基础值，与实际差 1.5~3 倍。
"""
import json
import os
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent / "core" / "data"
# wiki Lua 源码缓存（抓法见 SKILL：fandom API + 本地代理；?action=raw 会被 CF 拦）
CACHE = Path(os.environ.get("WF_STANCE_CACHE")
            or r"REDACTED_TMP_DIR/dmgsrc/stances_api.json")

# 强制异常的 wiki 写法 → 我们的伤害类型键
PROC_MAP = {
    "bleed": "slash", "slash": "slash",
    "impact": "impact", "puncture": "puncture",
    "heat": "heat", "cold": "cold", "electricity": "electricity",
    "toxin": "toxin", "blast": "blast", "corrosive": "corrosive",
    "gas": "gas", "magnetic": "magnetic", "radiation": "radiation",
    "viral": "viral", "void": "void",
}


# ---------------------------------------------------------------------------
# 极简 Lua 表解析（只覆盖这个模块用到的子集）
# ---------------------------------------------------------------------------
class LuaParser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def _ws(self):
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif self.s.startswith("--", self.i) and not self.s.startswith("--[", self.i):
                j = self.s.find("\n", self.i)
                self.i = len(self.s) if j < 0 else j + 1
            elif self.s.startswith("--[[", self.i):
                j = self.s.find("]]", self.i)
                self.i = len(self.s) if j < 0 else j + 2
            else:
                return

    def value(self):
        self._ws()
        c = self.s[self.i]
        if c == "{":
            return self.table()
        if c == '"':
            j = self.i + 1
            out = []
            while j < len(self.s) and self.s[j] != '"':
                out.append(self.s[j])
                j += 1
            self.i = j + 1
            return "".join(out)
        if c == "'":
            j = self.i + 1
            out = []
            while j < len(self.s) and self.s[j] != "'":
                out.append(self.s[j])
                j += 1
            self.i = j + 1
            return "".join(out)
        # 数字 / true / false / nil
        m = re.match(r"-?[\d.]+|true|false|nil", self.s[self.i:])
        if m:
            tok = m.group(0)
            self.i += len(tok)
            if tok == "true":
                return True
            if tok == "false":
                return False
            if tok == "nil":
                return None
            return float(tok) if "." in tok else int(tok)
        raise ValueError(f"无法解析 @{self.i}: {self.s[self.i:self.i+40]!r}")

    def table(self):
        assert self.s[self.i] == "{"
        self.i += 1
        out: dict = {}
        arr: list = []
        idx = 0
        while True:
            self._ws()
            if self.i >= len(self.s):
                break
            if self.s[self.i] == "}":
                self.i += 1
                break
            if self.s[self.i] == ",":
                self.i += 1
                continue
            # key = value 形式
            m = re.match(r'(\w+)\s*=\s*', self.s[self.i:])
            m2 = re.match(r'\["([^"]+)"\]\s*=\s*', self.s[self.i:])
            if m2:
                key = m2.group(1)
                self.i += m2.end()
                out[key] = self.value()
            elif m:
                key = m.group(1)
                self.i += m.end()
                out[key] = self.value()
            else:
                arr.append(self.value())
                idx += 1
        return out if out else arr


def extract_stance_data(text: str) -> dict:
    """从模块源码里抠出 `local StanceData = { ... }` 这一段。"""
    start = text.find("local StanceData")
    if start < 0:
        raise RuntimeError("找不到 StanceData")
    i = text.find("{", start)
    depth, j = 0, i
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return LuaParser(text[i:j + 1]).value()


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def combo_stats(combo: dict) -> dict | None:
    """把一段连招折算成「平均每段倍率 / 命中数 / 强制异常 / IPS 修饰」。"""
    attacks = combo.get("Attacks")
    if not isinstance(attacks, list) or not attacks:
        return None
    total, hits_n = 0.0, 0
    procs: list[str] = []
    ips: dict[str, float] = {}
    for atk in attacks:
        if not isinstance(atk, dict):
            continue
        dmg = atk.get("Dmg") or []
        hits = atk.get("Hits") or []
        for k in range(len(dmg)):
            d = float(dmg[k])
            h = float(hits[k]) if k < len(hits) else 1.0
            total += d * h
            hits_n += h
        for p in (atk.get("Procs") or []):
            key = PROC_MAP.get(norm(str(p)).replace("proc", ""))
            if key:
                procs.append(key)
        for k, field in (("impact", "ImpactMultiplier"),
                         ("puncture", "PunctureMultiplier"),
                         ("slash", "SlashMultiplier")):
            v = atk.get(field)
            # 数据源里既有 `SlashMultiplier = 1.25` 也有 `= { 1.25 }` 两种写法
            if isinstance(v, (int, float)):
                ips[k] = max(ips.get(k, 0.0), float(v))
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, (int, float)):
                        ips[k] = max(ips.get(k, 0.0), float(x))
    if hits_n <= 0:
        return None
    return {
        "name": combo.get("Name") or "",
        "avg_mult": round(total / hits_n, 2),   # 每段平均倍率（%）
        "total": round(total, 2),
        "hits": int(hits_n),
        "procs": sorted(set(procs)),
        "ips": ips or None,
    }


def build_stances() -> dict:
    text = json.loads(CACHE.read_text(encoding="utf-8"))["parse"]["wikitext"]
    raw = extract_stance_data(text)

    # 中文名：架势在 mods_stats.json 的 names 表里（compat='Stance'）
    zh_map: dict[str, str] = {}
    try:
        md = json.loads((DATA / "mods_stats.json").read_text(encoding="utf-8"))
        for rec in list((md.get("names") or {}).values()) + list((md.get("mods") or {}).values()):
            if (rec.get("compat") or "") == "Stance" and rec.get("name"):
                zh_map[norm(rec["name"])] = rec.get("zh") or ""
    except Exception:  # noqa: BLE001
        pass

    out: dict[str, dict] = {}
    for sname, stance in raw.items():
        if not isinstance(stance, dict) or sname == "__Legend":
            continue
        combos: dict[str, dict] = {}
        for cname, combo in stance.items():
            if not isinstance(combo, dict) or "Attacks" not in combo:
                continue
            st = combo_stats(combo)
            if st:
                combos[str(cname)] = st
        if not combos:
            continue
        out[norm(sname)] = {
            "name": sname,
            "zh": zh_map.get(norm(sname), ""),
            "weapon_type": stance.get("WeaponType") or "",
            "unique_to_weapon": bool(stance.get("UniqueToWeapon")),
            "combos": combos,
        }

    payload = {
        "_meta": {
            "source": "warframe.fandom.com Module:Stances/data",
            "retrieved": "2026-09-16",
            "note": "avg_mult=该连招每段平均伤害倍率(%)，procs=强制异常，ips=该段对物理三系的修饰",
        },
        "stances": out,
    }
    (DATA / "stances_stats.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"stances_stats.json：{len(out)} 个架势"
          f"（含中文名 {sum(1 for v in out.values() if v['zh'])}）")
    return out


if __name__ == "__main__":
    st = build_stances()
    for k in ("ironphoenix", "crimsondesert", "tempo royale"):
        v = st.get(k.replace(" ", ""))
        if not v:
            continue
        print(f"\n### {v['name']} ({v['zh']}) / {v['weapon_type']}")
        for c, s in v["combos"].items():
            print(f"   {c:16s} {s['name'][:28]:30s} 平均 {s['avg_mult']:7.1f}% "
                  f"命中{s['hits']:3d}  强制异常 {s['procs'] or '-'}"
                  + (f"  IPS{s['ips']}" if s["ips"] else ""))
