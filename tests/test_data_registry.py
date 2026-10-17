# -*- coding: utf-8 -*-
"""``core/data_registry.py``（随包数据登记表与统一健康判据）回归测试，**不联网**。

覆盖：登记覆盖度（``git ls-files core/data`` 每个 .json 都在册）/ 字段与枚举 /
生产器路径真实存在 / 三组（damage·drops_relics·names_zh）来源与生产器非空 /
点路径能取到值 / max_age 有天数与内容时间、rotating 有 check /
``evaluate`` 的 unregistered·missing·unknown·坏 JSON·过期判定（固定 now）/
反向验证（source 改 None ⇒ unknown，永不为 ok）。

风格照 ``tests/test_check_runner.py``：只用 ``check(...)`` 计数，跑法
``python tests/test_data_registry.py``（退出码非零 = 有失败）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import data_registry as DR  # noqa: E402

DATA = ROOT / "core" / "data"
DETAIL_GROUPS = ("damage", "drops_relics", "names_zh")
REQUIRED_KEYS = (
    "group",
    "producer",
    "source",
    "source_rev",
    "observed",
    "policy",
    "max_age_days",
    "readers",
    "override",
    "risk",
    "game_version",
    "tracked",
    "check",
    "note",
)

fails = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global fails
    if cond:
        print(f"[PASS] {name}")
    else:
        fails += 1
        print(f"[FAIL] {name}  {detail}")


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="wf_registry_"))


def _write(d: Path, rel: str, obj) -> None:
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _row(rows, key):
    return next((r for r in rows if r["file"] == key), None)


def _tree_md5(root: Path) -> dict:
    """目录下每个文件的 md5（相对路径 → 摘要）：验证账本**只读**。"""
    return {
        p.relative_to(root).as_posix(): hashlib.md5(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ------------------------------------------------------------------ ① 覆盖度
tracked = subprocess.run(
    ["git", "ls-files", "core/data"], cwd=ROOT, capture_output=True, text=True
).stdout.split()
json_files = sorted(
    p[len("core/data/") :] for p in tracked if p.startswith("core/data/") and p.endswith(".json")
)
missing_reg = [f for f in json_files if f not in DR.REGISTRY]
check(
    f"git ls-files 的 {len(json_files)} 个 core/data/*.json 全部已登记",
    not missing_reg,
    str(missing_reg),
)
check("登记表键数 ≥ 62 JSON + 资产 + 运行期缓存", len(DR.REGISTRY) >= 66, str(len(DR.REGISTRY)))

absent = []
for key, meta in DR.REGISTRY.items():
    if "*" in key or not meta.get("tracked", True):
        continue
    if not (DATA / key).exists():
        absent.append(key)
check("tracked=True 的非通配键文件都在场", not absent, str(absent))

# 运行期缓存（tracked=False）应被 .gitignore 忽略、不随包
check(
    "两个运行期缓存 tracked=False",
    DR.REGISTRY["wm_ranks.json"]["tracked"] is False
    and DR.REGISTRY["riven_weekly.json"]["tracked"] is False,
)
check(
    "字体/图标用通配键且为静态资源",
    DR.REGISTRY["fonts/*"]["policy"] == "static_asset"
    and DR.REGISTRY["icons/**"]["policy"] == "static_asset",
)

# ------------------------------------------------------------------ ② 字段与枚举
bad_fields, bad_enums, bad_producer = [], [], []
for key, meta in DR.REGISTRY.items():
    for k in REQUIRED_KEYS:
        if k not in meta:
            bad_fields.append(f"{key}:{k}")
    if meta.get("group") not in DR.GROUPS:
        bad_enums.append(f"{key}.group={meta.get('group')}")
    if meta.get("policy") not in DR.POLICIES:
        bad_enums.append(f"{key}.policy={meta.get('policy')}")
    if meta.get("override") not in DR.OVERRIDES:
        bad_enums.append(f"{key}.override={meta.get('override')}")
    if meta.get("risk") not in DR.RISKS:
        bad_enums.append(f"{key}.risk={meta.get('risk')}")
    if not isinstance(meta.get("tracked"), bool):
        bad_enums.append(f"{key}.tracked")
    if not isinstance(meta.get("readers"), list):
        bad_enums.append(f"{key}.readers")
    producer = meta.get("producer")
    if producer not in (None, "manual", "runtime") and not (ROOT / str(producer)).exists():
        bad_producer.append(f"{key} -> {producer}")
check("每条字段齐全", not bad_fields, str(bad_fields[:5]))
check("group/policy/override/risk 取值在枚举内", not bad_enums, str(bad_enums[:5]))
check("producer 非 manual/runtime 时脚本路径真实存在", not bad_producer, str(bad_producer))

# ------------------------------------------------------------------ ③ 三组必填 + 点路径
no_src, bad_dig, no_age, no_check = [], [], [], []
for key, meta in DR.REGISTRY.items():
    if meta["group"] in DETAIL_GROUPS and (
        meta.get("source") is None or meta.get("producer") is None
    ):
        no_src.append(key)
    if meta["policy"] == "max_age":
        if meta.get("max_age_days") is None or meta.get("observed") is None:
            no_age.append(key)
    if meta["policy"] == "rotating" and not meta.get("check"):
        no_check.append(key)
    if "*" in key:
        continue
    try:
        obj = json.loads((DATA / key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    for field in ("source_rev", "observed"):
        path = meta.get(field)
        if path is None:
            continue
        val = DR._dig(obj, path)
        if val is None or val == "":
            bad_dig.append(f"{key}.{field}={path}")
check("damage/drops_relics/names_zh 三组 source 与 producer 均非空", not no_src, str(no_src))
check("source_rev / observed 点路径真能取到非空值", not bad_dig, str(bad_dig))
check("max_age 条目都有 max_age_days 与 observed", not no_age, str(no_age))
check("rotating 条目都有 check", not no_check, str(no_check))
check(
    "unused 条目注明了 grep 结论",
    all("grep" in m["note"] for k, m in DR.REGISTRY.items() if m["policy"] == "unused"),
)

# ------------------------------------------------------------------ ④ 工具
check("_dig 点路径取值", DR._dig({"a": {"b": {"c": 1}}}, "a.b.c") == 1)
check("_dig 缺层返回 None", DR._dig({"a": {}}, "a.b.c") is None)
check("_dig 非对象返回 None", DR._dig([1, 2], "0") is None)

# ------------------------------------------------------------------ ⑤ evaluate
NOW = datetime(2026, 2, 1, tzinfo=timezone.utc)
_tmp: list[Path] = []

# 真实包内种子：完整（无未登记 / 缺文件）、状态都在枚举内
real = DR.evaluate(DATA, NOW)
check(
    "真实种子：无未登记 / 缺文件",
    DR.integrity_problems(real) == [],
    str(DR.integrity_problems(real)),
)
check("真实种子：状态都在枚举内", all(r["status"] in DR.STATUSES for r in real))
check("真实种子：行数与登记表一致", len(real) == len(DR.REGISTRY), str(len(real)))
check(
    "已知来源的行不会退化成 unknown",
    all(
        not (
            r["status"] == "unknown"
            and DR.REGISTRY[r["file"]].get("source")
            and DR.REGISTRY[r["file"]].get("producer")
        )
        for r in real
        if r["file"] in DR.REGISTRY
    ),
)

# —— unregistered / missing ——
d1 = _tmpdir()
_tmp.append(d1)
_write(d1, "zzz_new.json", {"x": 1})
rows = DR.evaluate(d1, NOW, tracked_files=["zzz_new.json"])
r = _row(rows, "zzz_new.json")
check("未登记文件 ⇒ unregistered", r is not None and r["status"] == "unregistered", str(r))
check(
    "integrity_problems 列出未登记",
    any("zzz_new.json" in p for p in DR.integrity_problems(rows)),
    str(DR.integrity_problems(rows)),
)
rows = DR.evaluate(d1, NOW, tracked_files=[])
r = _row(rows, "weapons_stats.json")
check("tracked=True 但不在场 ⇒ missing", r is not None and r["status"] == "missing", str(r))
check(
    "integrity_problems 列出缺文件",
    any("weapons_stats.json" in p for p in DR.integrity_problems(rows)),
    str(DR.integrity_problems(rows)),
)

# —— 非 git 目录回退 rglob（含 _cache 排除）——
d2 = _tmpdir()
_tmp.append(d2)
_write(d2, "sub/zzz.json", {})
_write(d2, "_cache/big.json", {})
rows = DR.evaluate(d2, NOW)
check(
    "非 git 目录回退 rglob 也能发现未登记",
    any(r["file"].endswith("zzz.json") and r["status"] == "unregistered" for r in rows),
    str([r["file"] for r in rows if r["status"] == "unregistered"]),
)
check("_cache/ 目录被排除", not any("_cache" in r["file"] for r in rows))

# —— max_age：过期 / 未过期 / 无内容时间 / 坏 JSON ——
d3 = _tmpdir()
_tmp.append(d3)
_write(d3, "arb_ratings.json", {"_updated": "2026-01-01"})
r = _row(DR.evaluate(d3, NOW, tracked_files=["arb_ratings.json"]), "arb_ratings.json")
check("max_age 过期 ⇒ stale", r is not None and r["status"] == "stale", str(r))
_write(d3, "arb_ratings.json", {"_updated": "2026-01-25"})
r = _row(DR.evaluate(d3, NOW, tracked_files=["arb_ratings.json"]), "arb_ratings.json")
check("max_age 未过期 ⇒ ok", r is not None and r["status"] == "ok", str(r))
_write(d3, "arb_ratings.json", {})
r = _row(DR.evaluate(d3, NOW, tracked_files=["arb_ratings.json"]), "arb_ratings.json")
check(
    "max_age 取不到内容时间 ⇒ unknown（不是 ok）",
    r is not None and r["status"] == "unknown" and "没有内容时间" in r["detail"],
    str(r),
)
(d3 / "arb_ratings.json").write_text("{坏", encoding="utf-8")
try:
    r = _row(DR.evaluate(d3, NOW, tracked_files=["arb_ratings.json"]), "arb_ratings.json")
    ok = r is not None and r["status"] == "unknown"
except Exception as exc:  # noqa: BLE001
    ok, r = False, repr(exc)
check("坏 JSON 不抛，只让该行 unknown", ok, str(r))
(d3 / "rotations.json").write_text("{坏", encoding="utf-8")
try:
    r = _row(DR.evaluate(d3, NOW, tracked_files=["rotations.json"]), "rotations.json")
    ok = r is not None and r["status"] == "stale"
except Exception as exc:  # noqa: BLE001
    ok, r = False, repr(exc)
check("坏 JSON 的 rotating ⇒ stale（不抛）", ok, str(r))

# —— rotating：expiry / valence ——
_write(d3, "de/acrichis_week.json", {"expiry": "2026-01-01T00:00:00+00:00"})
r = _row(DR.evaluate(d3, NOW, tracked_files=["de/acrichis_week.json"]), "de/acrichis_week.json")
check("rotating expiry 已过 ⇒ stale", r is not None and r["status"] == "stale", str(r))
_write(d3, "de/acrichis_week.json", {"expiry": "2026-03-01T00:00:00+00:00"})
r = _row(DR.evaluate(d3, NOW, tracked_files=["de/acrichis_week.json"]), "de/acrichis_week.json")
check("rotating expiry 未到 ⇒ ok", r is not None and r["status"] == "ok", str(r))

_rot = {
    "tenet": {
        "epoch": "2026-01-01T00:00:00+00:00",
        "period_hours": 96,
        "valence_snapshot": "2026-01-31T00:00:00+00:00",
    },
    "coda": {
        "epoch": "2026-01-01T00:00:00+00:00",
        "period_hours": 96,
        "valence_snapshot": "2026-01-31T00:00:00+00:00",
    },
}
_write(d3, "rotations.json", _rot)
r = _row(DR.evaluate(d3, NOW, tracked_files=["rotations.json"]), "rotations.json")
check("rotating valence 在本轮窗口 ⇒ ok", r is not None and r["status"] == "ok", str(r))
_rot["tenet"]["valence_snapshot"] = "2026-01-02T00:00:00+00:00"
_write(d3, "rotations.json", _rot)
r = _row(DR.evaluate(d3, NOW, tracked_files=["rotations.json"]), "rotations.json")
check("rotating valence 早于窗口起点 ⇒ stale", r is not None and r["status"] == "stale", str(r))

# ------------------------------------------------------------------ ⑥ 反向验证
_orig_ws = DR.REGISTRY["weapons_stats.json"]["source"]
DR.REGISTRY["weapons_stats.json"]["source"] = None
try:
    _write(d3, "weapons_stats.json", {})
    r = _row(DR.evaluate(d3, NOW, tracked_files=["weapons_stats.json"]), "weapons_stats.json")
    check("反向：source=None ⇒ unknown", r is not None and r["status"] == "unknown", str(r))
finally:
    DR.REGISTRY["weapons_stats.json"]["source"] = _orig_ws
check(
    "反向：恢复后不再 unknown",
    _row(DR.evaluate(DATA, NOW), "weapons_stats.json")["status"] != "unknown",
)

_orig_rot = DR.REGISTRY["rotations.json"]["source"]
DR.REGISTRY["rotations.json"]["source"] = None
try:
    _rot["tenet"]["valence_snapshot"] = "2026-01-31T00:00:00+00:00"
    _write(d3, "rotations.json", _rot)
    r = _row(DR.evaluate(d3, NOW, tracked_files=["rotations.json"]), "rotations.json")
    check(
        "反向：轮次通过但来源未确认 ⇒ unknown（永不为 ok）",
        r is not None and r["status"] == "unknown",
        str(r),
    )
finally:
    DR.REGISTRY["rotations.json"]["source"] = _orig_rot

# ------------------------------------------------------------------ ⑦ resolve：面板看实际生效的那份
d4, d5 = _tmpdir(), _tmpdir()  # d5 充当运行目录
_tmp += [d4, d5]
_write(d4, "de/wiki_disp.json", {"snapshot": "2026-01-01T00:00:00+00:00"})  # 种子：旧
_write(d5, "de/wiki_disp.json", {"snapshot": NOW.isoformat()})  # 运行期刷新件：新
_write(d4, "weapons_stats.json", {})
_res = lambda n: d5 / n  # noqa: E731 - weapons_stats 在 d5 不存在：seed_only 若误改看 d5 会报缺文件
_names = ["de/wiki_disp.json", "weapons_stats.json"]
r = _row(DR.evaluate(d4, NOW, tracked_files=_names), "de/wiki_disp.json")
check("resolve 缺省：读种子 ⇒ wiki_disp 过期", r is not None and r["status"] == "stale", str(r))
r = _row(DR.evaluate(d4, NOW, tracked_files=_names, resolve=_res), "de/wiki_disp.json")
check(
    "resolve=运行目录：runtime_refresh 改看生效副本 ⇒ ok",
    r is not None and r["status"] == "ok",
    str(r),
)
r = _row(DR.evaluate(d4, NOW, tracked_files=_names, resolve=_res), "weapons_stats.json")
check("resolve 不影响 seed_only 文件", r is not None and r["status"] == "no_ttl", str(r))
check(
    "tracked_files=() ⇒ 不报未登记（面板不起 git 子进程）",
    not [x for x in DR.evaluate(d4, NOW, tracked_files=()) if x["status"] == "unregistered"],
)

# 字段缓存：内容变了（size/mtime 变）必须重新取值
_write(d4, "mods_stats.json", {"_meta": {"retrieved": "2026-01-01"}})
r1 = _row(DR.evaluate(d4, NOW, tracked_files=()), "mods_stats.json")
_write(d4, "mods_stats.json", {"_meta": {"retrieved": "2026-01-20T00:00:00"}})
r2 = _row(DR.evaluate(d4, NOW, tracked_files=()), "mods_stats.json")
check(
    "字段缓存随文件变化失效",
    r1["observed"] == "2026-01-01" and r2["observed"] == "2026-01-20T00:00:00",
    f"{r1['observed']} / {r2['observed']}",
)

# ------------------------------------------------------------------ ⑧ 有效数据账本（ledger，只读）
d6, d7 = _tmpdir(), _tmpdir()  # d6 = 随包种子目录，d7 = 运行目录
_tmp += [d6, d7]
_keys = [k for k in DR.REGISTRY if "*" not in k]
_disp_seed = {"snapshot": "2026-02-01T00:00:00+00:00", "disp": {f"k{i}": i for i in range(450)}}
_write(d6, "de/wiki_disp.json", _disp_seed)
_write(d6, "weapons_stats.json", {"a": 1})

rows = DR.ledger(d7, d6)
check(
    "ledger：行数 = REGISTRY 非通配键数（含 tracked=False 的运行期缓存）",
    len(rows) == len(_keys),
    f"{len(rows)} vs {len(_keys)}",
)
r = _row(rows, "de/wiki_disp.json")
check(
    "ledger：运行目录没有副本 ⇒ seed_in_use / active=seed",
    r is not None and r["verdict"] == "seed_in_use" and r["active"] == "seed",
    str(r),
)
check(
    "ledger：用随包种子不算问题",
    not any(x["file"] == "de/wiki_disp.json" for x in DR.ledger_problems(rows)),
)
r = _row(rows, "mods_stats.json")
check(
    "ledger：两边都没有 ⇒ none（随包种子缺失）",
    r is not None
    and r["verdict"] == "none"
    and r["active"] == "none"
    and r["seed"]["exists"] is False,
    str(r),
)
check(
    "ledger：缺随包种子进 ledger_problems",
    any(x["file"] == "mods_stats.json" for x in DR.ledger_problems(rows)),
)

# —— 运行副本坏 JSON ⇒ run_bad（插件正在读它，不会回落种子）——
_write(d7, "de/wiki_disp.json", {"snapshot": "x"})
(d7 / "de/wiki_disp.json").write_text("{坏", encoding="utf-8")
rows = DR.ledger(d7, d6)
r = _row(rows, "de/wiki_disp.json")
check(
    "ledger：运行副本坏 JSON ⇒ run_bad / active=run",
    r is not None and r["verdict"] == "run_bad" and r["active"] == "run",
    str(r),
)
check(
    "ledger：run_bad 进 ledger_problems 且记了原因",
    any(x["file"] == "de/wiki_disp.json" for x in DR.ledger_problems(rows))
    and r is not None
    and "JSON 解析失败" in r["run"]["why"],
    str(r),
)

# —— 社区静态快照校验（复用 community_static_ok）——
_write(d7, "de/wiki_disp.json", {"snapshot": NOW.isoformat(), "disp": {"a": 1}})
r = _row(DR.ledger(d7, d6), "de/wiki_disp.json")
check(
    "ledger：社区快照校验生效（disp < 400）⇒ run_bad",
    r is not None and r["verdict"] == "run_bad" and "倾向条目太少" in r["run"]["why"],
    str(r),
)

# —— 新旧对照：比种子旧 ⇒ run_older；比种子新 ⇒ run_ok ——
_disp_run = {"snapshot": "2026-01-01T00:00:00+00:00", "disp": {f"k{i}": i for i in range(450)}}
_write(d7, "de/wiki_disp.json", _disp_run)
r = _row(DR.ledger(d7, d6), "de/wiki_disp.json")
check(
    "ledger：运行副本比随包种子旧 ⇒ run_older",
    r is not None and r["verdict"] == "run_older" and "比随包种子" in r["advice"],
    str(r),
)
_disp_run["snapshot"] = "2026-03-01T00:00:00+00:00"
_write(d7, "de/wiki_disp.json", _disp_run)
rows = DR.ledger(d7, d6)
r = _row(rows, "de/wiki_disp.json")
check(
    "ledger：运行副本比种子新 ⇒ run_ok / active=run",
    r is not None and r["verdict"] == "run_ok" and r["active"] == "run",
    str(r),
)
check(
    "ledger：run_ok 不算问题",
    not any(x["file"] == "de/wiki_disp.json" for x in DR.ledger_problems(rows)),
)

# —— 副本与种子「不是同一种东西」⇒ run_bad；seed_only 有副本 ⇒ run_unexpected ——
_write(d7, "weapons_stats.json", [1])
r = _row(DR.ledger(d7, d6), "weapons_stats.json")
check(
    "ledger：副本顶层类型与种子不同 ⇒ run_bad",
    r is not None and r["verdict"] == "run_bad" and "类型" in r["run"]["why"],
    str(r),
)
_write(d7, "weapons_stats.json", {"a": 2})
r = _row(DR.ledger(d7, d6), "weapons_stats.json")
check(
    "ledger：seed_only 却有运行副本 ⇒ run_unexpected",
    r is not None and r["verdict"] == "run_unexpected",
    str(r),
)

# —— 运行期缓存（override=runtime_only / tracked=False）——
_write(d7, "wm_ranks.json", {"x": 1})
rows = DR.ledger(d7, d6)
r = _row(rows, "wm_ranks.json")
check(
    "ledger：运行期缓存有副本 ⇒ run_only",
    r is not None and r["verdict"] == "run_only" and r["active"] == "run",
    str(r),
)
check(
    "ledger：run_only 不算问题",
    not any(x["file"] == "wm_ranks.json" for x in DR.ledger_problems(rows)),
)
r = _row(rows, "riven_weekly.json")
check(
    "ledger：运行期缓存还没生成 ⇒ not_yet / active=none",
    r is not None and r["verdict"] == "not_yet" and r["active"] == "none",
    str(r),
)
check(
    "ledger：not_yet 不算问题",
    not any(x["file"] == "riven_weekly.json" for x in DR.ledger_problems(rows)),
)

# —— 用户存档保护：不在 REGISTRY 的文件一个都不许出现在账本里 ——
_write(d7, "subs.json", {"subs": []})
check(
    "ledger：不在 REGISTRY 的用户存档不出现在账本里",
    not any(r["file"] == "subs.json" for r in DR.ledger(d7, d6)),
)
check("ledger：run_dir == pkg_dir ⇒ []（没有覆盖层可言）", DR.ledger(d6, d6) == [])

# —— 只读：调用前后运行目录的文件清单与内容 md5 必须一模一样 ——
_before = _tree_md5(d7)
DR.ledger(d7, d6)
check("ledger 只读：运行目录清单与内容 md5 前后一致", _tree_md5(d7) == _before)

# —— CLI（ZCode 在服务器上只读跑真实运行目录用）——
_p = subprocess.run(
    [sys.executable, "-m", "core.data_registry", "--ledger", str(d7)],
    cwd=ROOT,
    capture_output=True,
    text=True,
)
_out = _p.stdout.strip().splitlines()
_clrows = DR.ledger(d7, DATA)  # CLI 的 pkg 缺省 = 真实包内种子（paths.PKG_DATA）
check(
    "CLI：有问题 ⇒ rc=1 且末行以 LEDGER| 开头",
    _p.returncode == 1 and bool(_out) and _out[-1].startswith("LEDGER|"),
    f"rc={_p.returncode} {_p.stdout[-160:]!r}",
)
check(
    "CLI：每个文件一行 + 末行汇总",
    len(_out) == len(_clrows) + 1,
    f"{len(_out)} vs {len(_clrows) + 1}",
)
check(
    "CLI：汇总数字与账本一致",
    _out[-1] == f"LEDGER|rows={len(_clrows)}|problems={len(DR.ledger_problems(_clrows))}",
    _out[-1] if _out else "",
)

for _p in _tmp:
    shutil.rmtree(_p, ignore_errors=True)

print()
if fails:
    print(f"✗ {fails} 项失败")
    sys.exit(1)
print("✓ 全部通过")
