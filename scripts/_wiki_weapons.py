"""英文 wiki 武器数据（wiki.warframe.com，CC BY-NC-SA 3.0）的本地读取：P2 四个生成器共用。

输入目录（``--wiki``）里放从 wiki 原样取下的页面源码（``index.php?title=<页>&action=raw``）：
- ``primary.lua`` ``secondary.lua`` ``melee.lua`` ``archwing.lua`` ``companion.lua``
  ``modular.lua`` ``misc.lua`` = ``Module:Weapons/data/<名>``；
- ``Condition_Overload_(Mechanic).wiki`` = 「Condition Overload (Mechanic)」页（Attack Catalog 表）。
取法见 ``fetch()``（只在本机 / Mac 跑，**不在服务器上抓 wiki**）。
"""

from __future__ import annotations

import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lua_table import loads  # noqa: E402

MODULES = ("primary", "secondary", "melee", "archwing", "companion", "modular", "misc")
CO_PAGE = "Condition_Overload_(Mechanic)"
WIKI = "https://wiki.warframe.com/index.php"

# wiki 伤害键 → 我方键（Finisher 计算器不建模，跳过；Tau 计算器有类型与异常表）
DAMAGE_KEYS = {
    "Impact": "impact",
    "Puncture": "puncture",
    "Slash": "slash",
    "Heat": "heat",
    "Cold": "cold",
    "Electricity": "electricity",
    "Toxin": "toxin",
    "Blast": "blast",
    "Radiation": "radiation",
    "Gas": "gas",
    "Magnetic": "magnetic",
    "Viral": "viral",
    "Corrosive": "corrosive",
    "Void": "void",
    "True": "true",
    "Tau": "tau",
}
_ORDER = list(DAMAGE_KEYS.values())


def fetch(out_dir: Path, pause: float = 3.0) -> None:
    """把七个数据模块与 CO 页原样存到 out_dir（低频：每页间隔 pause 秒）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = [(f"Module:Weapons/data/{m}", f"{m}.lua") for m in MODULES] + [
        (CO_PAGE, f"{CO_PAGE}.wiki")
    ]
    for title, fn in pages:
        url = f"{WIKI}?title={urllib.parse.quote(title)}&action=raw"
        req = urllib.request.Request(url, headers={"User-Agent": "SDJK-Bot data build (low-rate)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            (out_dir / fn).write_bytes(r.read())
        time.sleep(pause)


def load_weapons(wiki_dir: Path) -> dict[str, dict]:
    """wiki 名 → 条目（加 ``_module`` 字段）。"""
    out: dict[str, dict] = {}
    for m in MODULES:
        data = loads((wiki_dir / f"{m}.lua").read_text(encoding="utf-8"))
        for name, v in data.items():
            if isinstance(v, dict):
                v["_module"] = m
                out[str(name)] = v
    return out


def key_of(entry: dict) -> str:
    """条目 → 我方键（小写 uniqueName）。"""
    return str(entry.get("InternalName") or "").lower()


def damage(atk: dict) -> dict:
    """wiki Damage 表 → 我方伤害向量（键序固定，带 total）。"""
    raw = atk.get("Damage") or {}
    out = {}
    for wk, ok in DAMAGE_KEYS.items():
        v = raw.get(wk)
        if v:
            out[ok] = float(v)
    out = {k: out[k] for k in _ORDER if k in out}
    if out:
        out["total"] = round(sum(out.values()), 4)
    return out


def roots_and_children(entry: dict) -> tuple[list[dict], dict[int, list[dict]]]:
    """攻击按「根攻击 / 子攻击（AttackParentIndex）」分组；无 AttackIndex 的按出现顺序编号。"""
    atks = list(entry.get("Attacks") or [])
    for i, a in enumerate(atks, 1):
        a.setdefault("AttackIndex", i)
    roots = [a for a in atks if not a.get("AttackParentIndex")]
    kids: dict[int, list[dict]] = {}
    for a in atks:
        p = a.get("AttackParentIndex")
        if p:
            kids.setdefault(int(p), []).append(a)
    return roots, kids


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


# ---------------------------------------------------------------- CO 页 Attack Catalog
_WEAPON_TPL = re.compile(r"\{\{Weapon\|([^}|]+)(?:\|[^}]*)?\}\}")


def co_catalog(wiki_dir: Path) -> list[dict]:
    """「Condition Overload (Mechanic)」Attack Catalog 各表逐行：
    {section, weapons: [wiki 名…], attack, type, behavior, notes}。
    behavior 取原文（Adding / Multiplying / N/A …）；表头外的行（Stances / Amps 等）也照收，
    由调用方按 section 过滤。"""
    text = (wiki_dir / f"{CO_PAGE}.wiki").read_text(encoding="utf-8")
    start = text.index("==Attack Catalog==")
    end = text.index("==Patch History==")
    rows: list[dict] = []
    section = ""
    cols: list[str] = []
    for block in re.split(r"\n\|-\s*\n", text[start:end]):
        for line in block.splitlines():
            m = re.match(r"^===\s*(.+?)\s*===\s*$", line)
            if m:
                section, cols = m.group(1), []
            if line.startswith("!"):
                cols = [c.strip().lower() for c in line[1:].split("!!")]
        line = next(
            (
                ln
                for ln in block.splitlines()
                if ln.startswith("|") and not ln.startswith(("|}", "|-"))
            ),
            "",
        )
        if not line or "{{Weapon|" not in line or not cols:
            continue
        cells = [c.strip() for c in line[1:].split("||")]
        cells += [""] * (len(cols) - len(cells))
        row = dict(zip(cols, cells))
        rows.append(
            {
                "section": section,
                "weapons": [w.strip() for w in _WEAPON_TPL.findall(row.get("weapon", ""))],
                "attack": row.get("attack name", ""),
                "type": row.get("projectile type") or row.get("attack type", ""),
                "behavior": row.get("math/behavior type", ""),
                "notes": row.get("notes", ""),
            }
        )
    return rows


def catalog_by_weapon(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        for w in r["weapons"]:
            out.setdefault(w, []).append(r)
    return out


def attack_category(name: str) -> str:
    """攻击名 → 粗类别（CO 页与数据模块的攻击名写法不同，按类别对）：
    incarnon / base（未蓄力）/ charged / alt（副开火类）/ semi / auto / main（主射，兜底）。"""
    n = str(name).lower()
    if "incarnon" in n:
        return "incarnon"
    if "uncharged" in n or "quick" in n:
        return "base"
    if "charge" in n:
        return "charged"
    if re.search(r"\balt\b|alt-fire|alternate|secondary|active|lock-on|grenade|throw|harpoon", n):
        return "alt"
    if re.search(r"\bsemi\b|semi-", n):
        return "semi"
    if re.search(r"full[- ]auto|\bauto\b", n) and "burst" not in n:
        return "auto"
    return "main"


def resolve_catalog_row(row_attack: str, roots: list[dict], main: dict, ok=lambda r: True):
    """CO 页一行的攻击名 → 武器的根攻击（对不上返回 None）。
    主射类（Normal Attack / Main-fire / Hitscan AoE Direct…）= 默认主段；其余按类别找同类根攻击；
    副开火类找不到同类时，若只有一个带段（ok）的非主段、非灵化根攻击，就是它。"""
    cat = attack_category(row_attack)
    if cat == "main" or (
        cat in ("semi", "auto") and attack_category(main.get("AttackName") or "") == cat
    ):
        return main
    same = [r for r in roots if attack_category(r.get("AttackName") or "") == cat and ok(r)]
    if same:
        return same[0]
    if cat == "alt":
        others = [
            r
            for r in roots
            if r is not main and attack_category(r.get("AttackName") or "") != "incarnon" and ok(r)
        ]
        if len(others) == 1:
            return others[0]
    return None


# CO 页没列、2026-10-26 维护者核查补上的行（待查清单执行报告 §3）。依据两类：
#   「页」= 该武器 wiki 页正文直接写了 CO / Galvanized 的叠法；「同构」= 游戏数据导出 behaviours 与已列出的
#   同族武器逐攻击结构相同，按同族 CO 页行。格式同 co_catalog 行，多一个 ``source``。
_U = "维护者核查 2026-10-26"
USER_VERIFIED_CO_ROWS: list[dict] = [
    {
        "weapons": ["Epitaph Prime"],
        "attack": "Charged Shot",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：Epitaph Prime 页「Galvanized Shot … multiplicative … in both firing modes」",
    },
    {
        "weapons": ["Epitaph Prime"],
        "attack": "Uncharged Direct Hit",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：同上",
    },
    {
        "weapons": ["Latron Wraith"],
        "attack": "Incarnon Mode",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：同构 Latron / Latron Prime（CO 页 Multiplying）",
    },
    {
        "weapons": ["Mk1-Paris"],
        "attack": "Incarnon Mode",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：同构 Paris / Paris Prime（CO 页 Multiplying）",
    },
    {
        "weapons": ["Ballistica", "Rakta Ballistica"],
        "attack": "Incarnon Mode",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：灵化形态与 Ballistica Prime 同构（CO 页 Multiplying；普通形态结构不同，不推）",
    },
    {
        "weapons": ["Akarius"],
        "attack": "Rocket Impact",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：同构 Akarius Prime（CO 页 Multiplying）",
    },
    {
        "weapons": ["Trumna Prime"],
        "attack": "Main-fire Hitscan Radial Attack",
        "type": "AoE",
        "behavior": "Adding",
        "notes": "AoE does not scale off multishot.",
        "source": f"{_U}：同构 Trumna（CO 页）",
    },
    {
        "weapons": ["Trumna Prime"],
        "attack": "Alt-fire",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：Trumna Prime 页 Galvanized Aptitude 乘算 + 同构 Trumna",
    },
    {
        "weapons": ["Fulmin Prime"],
        "attack": "Semi-Mode",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：Fulmin / Fulmin Prime 页均写半自动乘算",
    },
    {
        "weapons": ["Kuva Grattler"],
        "attack": "Normal attack",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：同构 Grattler（CO 页 Multiplying）",
    },
    {
        "weapons": ["Larkspur"],
        "attack": "Alt-fire",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：与 Larkspur Prime 完全同构（CO 页 Multiplying）",
    },
    {
        "weapons": ["Coda Catabolyst"],
        "attack": "Partial Reload Impact",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：同构 Catabolyst（CO 页 Multiplying）",
    },
    {
        "weapons": ["Coda Catabolyst"],
        "attack": "Reload From Empty Impact",
        "type": "Projectile",
        "behavior": "Multiplying",
        "notes": "",
        "source": f"{_U}：同上",
    },
    {
        "weapons": ["Coda Pox"],
        "attack": "DoT Cloud",
        "type": "AoE",
        "behavior": "Adding",
        "notes": "",
        "source": f"{_U}：同构 Pox（CO 页毒云 Adding）",
    },
]


def catalog_rows(wiki_dir: Path) -> list[dict]:
    """CO 页 Attack Catalog 各行 + 维护者核查补充行（``USER_VERIFIED_CO_ROWS``）。"""
    return co_catalog(wiki_dir) + [{"section": "维护者核查", **r} for r in USER_VERIFIED_CO_ROWS]
