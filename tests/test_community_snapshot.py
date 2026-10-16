# -*- coding: utf-8 -*-
"""社区快照（bot-data 分支）守卫（python3 tests/test_community_snapshot.py）

方案④（2026-09-25）：元素加成（wiki《Reset》）与言录使货单（wiki《Acrithis/
Current Offerings》）都在 Cloudflare 盾后，没部署 FlareSolverr 的用户只能吃随包
种子。于是把服务器上已抓到的新鲜快照发布到公开仓的 `bot-data` 分支，插件在
「本地快照过期 + FS 不可用」时读它兜底。

两侧的纪律都是**校验不过就不干**：
- 发布侧（`scripts/publish_community_snapshot.py`）：残缺/过期/相位不符一律不发；
- 消费侧（`core/api_client.py`）：解析不过、比本地旧、已过期一律不采用。

这里钉住两侧的判据 —— 全是纯函数，离线可跑（不 ssh、不联网）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# 消费侧：core.api_client 的纯函数
from core.api_client import (  # noqa: E402
    COMMUNITY_ACRITHIS_URL,
    COMMUNITY_VALENCE_URL,
    community_acrithis_ok,
    community_valence_newer,
    parse_community_valence,
)

# 发布侧：scripts/publish_community_snapshot.py（离线载入，不执行 main）
_spec = importlib.util.spec_from_file_location(
    "publish_community_snapshot",
    ROOT
    / ".zcode"
    / "skills"
    / "astrbot-server-triage"
    / "scripts"
    / "automations"
    / "publish_community_snapshot.py",
)
# 发布器是私有运维脚本（.zcode/ 不进开源包）⇒ 缺失时只跳过发布侧段落并说明原因，
# 消费侧（core/api_client 纯函数 + main/api_client 接线）照常回归。
P = None
if Path(_spec.origin).is_file():
    P = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(P)
else:
    print(
        "[SKIP] 发布侧 §3–§6、§8 --direct、§9：本树无 publish_community_snapshot.py"
        "（私有运维脚本，不随开源包分发）；消费侧 §1/§2/§7/§8 照常检查"
    )

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-09-25T02:07:46+00:00"


def _rot(*, filled_idx: int = 1, anchor: int = 0) -> dict:
    """构造一份与线上同形的 rotations.json（coda 只填当前批）。"""
    batches = []
    for i, names in enumerate(
        (("Coda Hema", "Coda Sporothrix"), ("Coda Bassocyst", "Coda Catabolyst"))
    ):
        batches.append(
            [
                {
                    "en": n,
                    "cn": f"终幕·{n[5:]}",
                    **({"element": "Heat", "bonus": 27.4} if i == filled_idx else {}),
                }
                for n in names
            ]
        )
    return {
        "tenet": {
            "valence_snapshot": STAMP,
            "items": [
                {"en": "Tenet Agendus", "cn": "信条·集议", "element": "Electricity", "bonus": 26.2}
            ],
        },
        "coda": {
            "epoch": "2026-09-13T00:00:00+00:00",
            "period_hours": 96,
            "anchor_idx": anchor,
            "batch_label": ["A", "B"],
            "valence_snapshot": STAMP,
            "batches": batches,
        },
    }


# ---------------------------------------------------------------------------
# 1. 消费侧：valence 快照的解析与「不比本地旧」判据
# ---------------------------------------------------------------------------
GOOD_VALENCE = {
    "schema": 1,
    "published_at": "2026-09-25T12:00:00+00:00",
    "tenet": {
        "snapshot": STAMP,
        "items": [
            {"en": "Tenet Agendus", "cn": "信条·集议", "element": "Electricity", "bonus": 26.2}
        ],
    },
    "coda": {
        "snapshot": STAMP,
        "batch": "B",
        "items": [
            {"en": "Coda Bassocyst", "cn": "终幕·低音爆囊", "element": "Magnetic", "bonus": 31.6}
        ],
    },
}
parsed = parse_community_valence(GOOD_VALENCE)
check(
    "★ 消费侧：合格快照解析出 tenet/coda 映射与批次",
    parsed["tenet"]["Tenet Agendus"] == ("Electricity", 26.2) and parsed["coda_batch"] == "B",
    str(parsed),
)

for bad, why in (
    ({}, "空对象"),
    ({"tenet": {"items": []}, "coda": {"items": [{"en": "x"}], "batch": "B"}}, "tenet 无物品"),
    (
        {
            "tenet": {"items": [{"en": "x", "element": "Heat"}]},
            "coda": {"items": [{"en": "y", "element": "Cold", "bonus": 1}], "batch": "B"},
        },
        "tenet 缺 bonus",
    ),
    (
        {
            "tenet": {"items": [{"en": "x", "element": "Heat", "bonus": 1}]},
            "coda": {"items": [{"en": "y", "element": "Cold", "bonus": 1}]},
        },
        "coda 缺 batch",
    ),
):
    try:
        parse_community_valence(bad)
        check(f"★ 消费侧：残缺快照必须被拒（{why}）", False, "没抛异常")
    except ValueError:
        check(f"★ 消费侧：残缺快照必须被拒（{why}）", True)

check(
    "★ 消费侧：比本地新 → 采用", community_valence_newer("2026-09-25T12:00:00+00:00", STAMP) is True
)
check(
    "★ 消费侧：比本地旧 → 不采用",
    community_valence_newer("2026-09-20T00:00:00+00:00", STAMP) is False,
)
check("★ 消费侧：与本地同刻 → 不采用（不无谓覆盖）", community_valence_newer(STAMP, STAMP) is False)
check(
    "★ 消费侧：时间戳缺失/畸形 → 不采用",
    community_valence_newer("", STAMP) is False
    and community_valence_newer("not-a-date", STAMP) is False,
)
check(
    "消费侧：本地无时间戳 → 不构成拒绝理由",
    community_valence_newer("2026-09-25T12:00:00+00:00", "") is True,
)

# ---------------------------------------------------------------------------
# 2. 消费侧：言录使快照判据（未过期 + 有货 + observed 不早于本周一）
# ---------------------------------------------------------------------------
EXPIRY = "2026-09-28T00:00:00+00:00"
GOOD_ACR = {
    "expiry": EXPIRY,
    "observed": "September 21, 2026",
    "items": [{"name": "Orokin Catalyst", "price": 20}],
}
ok, why = community_acrithis_ok(GOOD_ACR, NOW)
check("★ 消费侧：合格言录使快照被接受", ok is True, why)
ok, why = community_acrithis_ok({**GOOD_ACR, "expiry": "2026-09-21T00:00:00+00:00"}, NOW)
check("★ 消费侧：已过期 → 拒绝", ok is False and "过期" in why, why)
ok, why = community_acrithis_ok({**GOOD_ACR, "items": []}, NOW)
check("★ 消费侧：无货 → 拒绝", ok is False, why)
ok, why = community_acrithis_ok({**GOOD_ACR, "observed": "September 14, 2026"}, NOW)
check(
    "★ 消费侧：observed 早于本周一 → 拒绝（防拿上周货单当本周）",
    ok is False and "本周一" in why,
    why,
)
ok, why = community_acrithis_ok({"expiry": "bogus", "items": [{"name": "x"}]}, NOW)
check("★ 消费侧：expiry 畸形 → 拒绝", ok is False, why)
ok, why = community_acrithis_ok({**GOOD_ACR, "observed": ""}, NOW)
check("消费侧：observed 缺失不算问题（expiry 才是硬判据）", ok is True, why)

check(
    "消费侧 URL 指向 bot-data 分支",
    "/bot-data/" in COMMUNITY_VALENCE_URL and "/bot-data/" in COMMUNITY_ACRITHIS_URL,
    COMMUNITY_VALENCE_URL,
)

if P is not None:
    # ---------------------------------------------------------------------------
    # 3. 发布侧：当前批推导必须与卡面同款（锚点+已过轮数，不是 anchor_idx）
    # ---------------------------------------------------------------------------
    coda = _rot(anchor=0)["coda"]
    check(
        "★ 发布侧：当前批 = (anchor + passed) % 批数（照抄卡面公式）",
        P.current_coda_index(coda, NOW) == 1,
        f"得到 {P.current_coda_index(coda, NOW)}，期望 1",
    )
    check(
        "  （若误用 anchor_idx 会拿到没填元素的那批）",
        coda["anchor_idx"] == 0 and P.current_coda_index(coda, NOW) == 1,
    )
    coda2 = _rot(anchor=1)["coda"]
    check("发布侧：相位基准变一档 → 当前批跟着变", P.current_coda_index(coda2, NOW) == 0)

    # ---------------------------------------------------------------------------
    # 4. 发布侧：校验不过就**不发**（残缺 / 相位不符 / 已过期）
    # ---------------------------------------------------------------------------
    payload = P.build_valence(_rot(filled_idx=1), STAMP, NOW)
    check(
        "★ 发布侧：相位与填充吻合 → 构建成功",
        payload["coda"]["batch"] == "B" and len(payload["coda"]["items"]) == 2,
        str(payload)[:120],
    )
    check(
        "  带来源与许可署名",
        "CC BY-NC-SA" in payload["license_note"] and "wiki" in payload["source"],
    )

    # ★ 包内种子形状：两批都填 → 只要**相位推导的当前批**完整就接受（2026-09-26 实测修）
    _both = _rot(filled_idx=1)
    for _b in _both["coda"]["batches"]:
        for _x in _b:
            _x.setdefault("element", "Heat")
            _x.setdefault("bonus", 27.4)
    try:
        _payload_both = P.build_valence(_both, STAMP, NOW)
        check(
            "★ 包内种子形状（两批都填）→ 接受并按相位取当前批",
            _payload_both["coda"]["batch"] == "B",
            str(_payload_both["coda"]["batch"]),
        )
    except ValueError as exc:
        check("★ 包内种子形状（两批都填）→ 接受并按相位取当前批", False, str(exc))

    for rot, why in (
        (_rot(filled_idx=0), "相位说 B 批、元素却填在 A 批"),
        (_rot(filled_idx=1, anchor=1), "相位说 A 批、元素却填在 B 批"),
    ):
        try:
            P.build_valence(rot, STAMP, NOW)
            check(f"★ 发布侧：{why} → 拒绝发布", False, "没抛异常")
        except ValueError as exc:
            check(f"★ 发布侧：{why} → 拒绝发布", "拒绝发布" in str(exc), str(exc))

    rot_missing = _rot(filled_idx=1)
    rot_missing["tenet"]["items"][0].pop("element")
    try:
        P.build_valence(rot_missing, STAMP, NOW)
        check("★ 发布侧：tenet 缺 element/bonus → 拒绝发布", False, "没抛异常")
    except ValueError as exc:
        check("★ 发布侧：tenet 缺 element/bonus → 拒绝发布", True, str(exc))

    rot_empty = _rot(filled_idx=1)
    rot_empty["tenet"]["items"] = []
    try:
        P.build_valence(rot_empty, STAMP, NOW)
        check("★ 发布侧：tenet 空表 → 拒绝发布", False, "没抛异常")
    except ValueError:
        check("★ 发布侧：tenet 空表 → 拒绝发布", True)

    acr = P.build_acrithis(
        {
            "expiry": EXPIRY,
            "observed": "September 21, 2026",
            "items": [{"name": "Orokin Catalyst", "qty": 1, "price": 20}],
        },
        STAMP,
        NOW,
    )
    check(
        "★ 发布侧：合格言录使 → 构建成功",
        acr["expiry"] == EXPIRY and len(acr["items"]) == 1,
        str(acr)[:120],
    )
    for bad, why in (
        ({"expiry": "2026-09-21T00:00:00+00:00", "items": [{"name": "x"}]}, "已过期"),
        ({"expiry": EXPIRY, "items": []}, "无货"),
        ({"items": [{"name": "x"}]}, "缺 expiry"),
    ):
        try:
            P.build_acrithis(bad, STAMP, NOW)
            check(f"★ 发布侧：言录使{why} → 拒绝发布", False, "没抛异常")
        except ValueError:
            check(f"★ 发布侧：言录使{why} → 拒绝发布", True)

    # ---------------------------------------------------------------------------
    # 5. 发布侧：默认 dry-run（不加 --yes 一个字节都不写远端）
    # ---------------------------------------------------------------------------
    _src = (
        ROOT
        / ".zcode"
        / "skills"
        / "astrbot-server-triage"
        / "scripts"
        / "automations"
        / "publish_community_snapshot.py"
    ).read_text(encoding="utf-8")
    check(
        "★ 发布侧：默认 dry-run，--yes 才写远端",
        'ap.add_argument("--yes", action="store_true"' in _src and "DRY-RUN（默认）" in _src,
    )
    check(
        "★ 发布侧：只写 bot-data 分支（不碰公开仓 main）",
        'BRANCH = "bot-data"' in _src and "refs/heads/main" not in _src,
    )
    check("发布侧：读服务器走只读 ssh（BatchMode）", '"BatchMode=yes"' in _src)
    check("发布侧：孤儿分支（首次提交无 parents）", '"parents": [head] if head else []' in _src)
    check("发布侧：零变化不产生空提交", "无需提交（不产生空提交）" in _src)

    # 6. 发布侧：时间戳取「数据自身」的时刻 → 数据没变就不重发（幂等）
    check(
        "★ 发布侧：observed_to_iso 解析 wiki 上报日期",
        P.observed_to_iso("September 21, 2026") == "2026-09-21T00:00:00+00:00",
        P.observed_to_iso("September 21, 2026"),
    )
    check(
        "发布侧：解析不了 → 空串（调用方回落 expiry/now）",
        P.observed_to_iso("") == "" and P.observed_to_iso("上周一") == "",
    )
    check(
        "★ 发布侧：payload 时间戳不用「本次运行时间」（否则每轮都产生新提交）",
        "val_pub" in _src and "acr_pub" in _src and "published_at = now.isoformat()" not in _src,
    )
    _a = json.dumps(
        P.build_valence(_rot(filled_idx=1), STAMP, NOW), ensure_ascii=False, sort_keys=True
    )
    _b = json.dumps(
        P.build_valence(_rot(filled_idx=1), STAMP, NOW), ensure_ascii=False, sort_keys=True
    )
    check("★ 发布侧：同输入两次构建逐字节相同（幂等）", _a == _b)

# ---------------------------------------------------------------------------
# 7. 接线：**没部署 FS 的用户**（面板关闭 / 不可达）也要走社区快照
#    —— 否则最需要它的那批人永远用不上（v1.0.8 三态分流原本整段跳过）
# ---------------------------------------------------------------------------
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")
API_SRC = (ROOT / "core" / "api_client.py").read_text(encoding="utf-8")
_i_phase = MAIN_SRC.index('if phase != "ready":')
_i_call = MAIN_SRC.index("await self._refresh_from_community()", _i_phase)
_i_sleep = MAIN_SRC.index("await asyncio.sleep(self._secs_to_aligned_tick())", _i_phase)
check(
    "★ 接线：非 ready 分支里先走社区快照、再 sleep（否则整段跳过）",
    _i_phase < _i_call < _i_sleep,
    f"phase={_i_phase} call={_i_call} sleep={_i_sleep}",
)
check(
    "★ 接线：两个刷新都支持 community_only（跳过 FS 的直通路径）",
    "async def refresh_valence(self, community_only: bool = False)" in API_SRC
    and "async def refresh_acrichis_week(self, community_only: bool = False)" in API_SRC,
)
check(
    "接线：取不到社区快照 → 返回 no-community 静默跳过（不告警）",
    'return "no-community"' in API_SRC and "no-community" in API_SRC,
)
check(
    "接线：社区快照刷新只在成功时记 INFO（其余沉默）",
    'if isinstance(st, str) and st.startswith("refreshed")' in MAIN_SRC,
)
check(
    "文案：FS 缺失提示已改为「社区快照或随包快照」（不再说「不再自动刷新」）",
    "社区快照或随包快照" in MAIN_SRC and "将沿用随包快照、不再自动刷新" not in MAIN_SRC,
)

# ---------------------------------------------------------------------------
# 8. 可达性：社区快照走**三条通道**（国内实测读不到 raw）
# ---------------------------------------------------------------------------
from core.api_client import (  # noqa: E402
    COMMUNITY_ACRITHIS_URLS,
    COMMUNITY_VALENCE_URLS,
)

check(
    "★ 消费侧：两条数据各有 3 条通道（raw → jsDelivr → statically）",
    len(COMMUNITY_VALENCE_URLS) == 3 and len(COMMUNITY_ACRITHIS_URLS) == 3,
    f"{len(COMMUNITY_VALENCE_URLS)} / {len(COMMUNITY_ACRITHIS_URLS)}",
)
check(
    "  主通道是 raw，三条都指向同一 bot-data 分支",
    COMMUNITY_VALENCE_URLS[0].startswith("https://raw.githubusercontent.com/")
    and all("bot-data" in u or "@bot-data" in u for u in COMMUNITY_VALENCE_URLS),
    str(COMMUNITY_VALENCE_URLS),
)
check(
    "  两条备用通道都是 jsDelivr（cdn + gcore，互为冗余）",
    "cdn.jsdelivr.net" in COMMUNITY_VALENCE_URLS[1]
    and "gcore.jsdelivr.net" in COMMUNITY_VALENCE_URLS[2],
)
check(
    "★ statically 已弃用（服务器侧实测 12s 超时不可达，不再出现在通道列表）",
    all("statically" not in u for u in COMMUNITY_VALENCE_URLS + COMMUNITY_ACRITHIS_URLS),
)
check(
    "★ 接线：_community_json 按序试通道（成功主机优先 + 失败冷却，行为见文末用例）",
    "for i, url in enumerate(live)" in API_SRC
    and "COMMUNITY_COOLDOWN" in API_SRC
    and "_community_json(COMMUNITY_VALENCE_URLS)" in API_SRC
    and "_community_json(COMMUNITY_ACRITHIS_URLS)" in API_SRC,
)
if P is not None:
    check(
        "发布侧：支持 --direct（在服务器上直读本机绝对路径，不走 ssh）",
        '"--direct"' in _src
        and "direct: bool = False" in _src
        and 'return (ROOT / "core/data/rotations.json",' not in _src,
    )

if P is not None:
    # ---------------------------------------------------------------------------
    # 9. 发布器「多候选取最新」（评审侧提：第一个存在的可能更旧 → 白丢新鲜数据）
    # ---------------------------------------------------------------------------
    _OLD = {
        "expiry": "2026-09-28T00:00:00+00:00",
        "observed": "September 14, 2026",
        "items": [{"name": "x"}],
    }
    _NEW = {
        "expiry": "2026-10-05T00:00:00+00:00",
        "observed": "September 21, 2026",
        "items": [{"name": "y"}],
    }
    check(
        "★ 发布器：快照新鲜度取自身时间戳（观测日 → ISO）",
        P.snapshot_freshness(_OLD) == "2026-09-14T00:00:00+00:00"
        and P.snapshot_freshness(_NEW) == "2026-09-21T00:00:00+00:00",
        f"{P.snapshot_freshness(_OLD)} / {P.snapshot_freshness(_NEW)}",
    )
    check(
        "★ 发布器：两份候选取**最新**那份（不是第一个存在的）",
        P.pick_freshest([(_OLD, "包内旧"), (_NEW, "运行期新")])[1] == "运行期新"
        and P.pick_freshest([(_NEW, "运行期新"), (_OLD, "包内旧")])[1] == "运行期新",
    )
    check(
        "  时间戳都取不到 → 保留最先前那份（等价原优先序）",
        P.pick_freshest([({"a": 1}, "第一"), ({"b": 2}, "第二")])[1] == "第一",
    )
    check(
        "  效价快照读 tenet/coda 段的 valence_snapshot",
        P.snapshot_freshness(
            {
                "tenet": {"valence_snapshot": "2026-09-25T02:07:46+00:00"},
                "coda": {"valence_snapshot": "2026-09-25T02:07:46+00:00"},
            }
        )
        == "2026-09-25T02:07:46+00:00",
    )
    _cands = ("/opt/x/de/acrichis_week.json", "/y/z.json")
    check(
        "★ 发布器：来源说明点明「运行期副本」/「包内种子」及原因",
        P._src_label(_cands[0], _cands, 2).startswith("运行期副本")
        and P._src_label(_cands[1], _cands, 1).startswith("包内种子回落（运行期副本不存在）")
        and P._src_label(_cands[1], _cands, 2).startswith("包内种子（时间戳比运行期副本新）"),
    )

# 巡检侧：三通道探针必须存在（否则"名义三通道、实际两通道"长期无人发现）
_PATROL = (
    ROOT
    / ".zcode"
    / "skills"
    / "astrbot-server-triage"
    / "scripts"
    / "automations"
    / "server_patrol.py"
)
if _PATROL.is_file():
    ptxt = _PATROL.read_text(encoding="utf-8")
    check(
        "★ 巡检：有三通道探针 + 只剩 1 条/0 条时升级为待办",
        "def probe_community_channels" in ptxt
        and "COMMUNITY_CHANNELS" in ptxt
        and "只剩 1 条通道可用" in ptxt
        and "全部不可达" in ptxt,
    )
    check(
        "  巡检通道列表与插件侧同源（raw + cdn + gcore，无 statically）",
        "raw.githubusercontent.com" in ptxt
        and "cdn.jsdelivr.net" in ptxt
        and "gcore.jsdelivr.net" in ptxt
        and "statically.io" not in ptxt,
    )
else:
    print(
        "[SKIP] 巡检侧三通道探针：本树无 .zcode/.../server_patrol.py（私有运维脚本，不随开源包分发）"
    )


# ---------------------------------------------------------------------------
# ★ 2026-10-16：静态快照进 bot-data（仲裁评级 / 奸商到访历史 / 紫卡倾向）+ 用户自填社区地址
# ---------------------------------------------------------------------------
import asyncio as _aio  # noqa: E402
import json as _json  # noqa: E402
import shutil as _shutil  # noqa: E402
import tempfile as _tmp  # noqa: E402

from core import api_client as _AC  # noqa: E402
from core import arbi as _arbi  # noqa: E402
from core import baro as _baro  # noqa: E402
from core import paths as _paths  # noqa: E402

_PKG = Path(_paths.PKG_DATA)
# ★ 2026-10-16（v1.2.1 公开仓 CI 红）：de/wiki_disp.json 在开源包排除表里（OSS_EXCLUDE_FILES，
#   「运行时产物」口径），公开仓没有这份种子 —— 只校验本树实际带着的种子，缺的记 SKIP。
#   下面用到倾向表的用例都是自造数据，不依赖本地种子。
_seed = {
    n: _json.loads((_PKG / s_["local"]).read_text(encoding="utf-8"))
    for n, s_ in _AC.COMMUNITY_STATIC.items()
    if (_PKG / s_["local"]).exists()
}
for _n in sorted(set(_AC.COMMUNITY_STATIC) - set(_seed)):
    print(
        f"[SKIP] 随包种子自检（{_n}）：本树不带该种子（开源包排除，运行期由社区快照 / FlareSolverr 补）"
    )
for _n, _d in _seed.items():
    check(
        f"静态快照校验：随包种子本身能过（{_n}）",
        _AC.community_static_ok(_n, _d)[0],
        _AC.community_static_ok(_n, _d)[1],
    )
check(
    "静态快照校验：节点太少被拒",
    not _AC.community_static_ok("arb_ratings.json", {"_updated": "2026-10-16", "nodes": {}})[0],
)
check(
    "静态快照校验：缺时间戳被拒",
    not _AC.community_static_ok("baro_history.json", {**_seed["baro_history.json"], "fetched": ""})[
        0
    ],
)
_bad_tier = _json.loads(_json.dumps(_seed["arb_ratings.json"]))
next(iter(_bad_tier["nodes"].values()))["tier"] = "SSS"
check(
    "静态快照校验：档位取值不认识被拒",
    not _AC.community_static_ok("arb_ratings.json", _bad_tier)[0],
)
check(
    "静态快照校验：倾向条目 < 400 被拒",
    not _AC.community_static_ok(
        "wiki_disp.json", {"snapshot": "2026-10-16T00:00:00+00:00", "disp": {"a": 1}}
    )[0],
)
check(
    "社区地址：只收 http(s)，去尾斜杠",
    _AC.normalize_community_base(" https://x.org/data/ ") == "https://x.org/data"
    and _AC.normalize_community_base("file:///etc") == ""
    and _AC.normalize_community_base("") == "",
)

_rt = Path(_tmp.mkdtemp(prefix="_tmp_static_comm_"))
try:
    _paths.set_run_dir(_rt)
    _c = _AC.WarframeClient()
    _served = {}
    _urls_seen = []

    async def _fake_fetch(url, ttl=0, headers=None, cache_key=None, **kw):
        _urls_seen.append(url)
        name = url.rsplit("/", 1)[-1]
        if name in _served:
            return _served[name]
        raise RuntimeError("404")

    _c._fetch_json = _fake_fetch
    newer_arb = _json.loads(_json.dumps(_seed["arb_ratings.json"]))
    newer_arb["_updated"] = "2099-01-01"
    k0 = next(iter(newer_arb["nodes"]))
    newer_arb["nodes"][k0]["n"] = 777
    older_baro = {**_seed["baro_history.json"], "fetched": "2000-01-01"}
    _served = {
        "arb_ratings.json": newer_arb,
        "baro_history.json": older_baro,
        "wiki_disp.json": {"snapshot": "2099-01-01T00:00:00+00:00", "disp": {"x": 1}},
    }
    _arbi.reset_measured()
    _arbi.load_measured()  # 先把旧表缓存进来，验证刷新后会清缓存
    res = _aio.run(_c.refresh_community_static())
    check(
        "★ 更新的仲裁评级：落到运行目录",
        res["arb_ratings.json"].startswith("refreshed") and (_rt / "arb_ratings.json").exists(),
        str(res),
    )
    check(
        "★ 落盘后清缓存：立刻读到新表",
        _arbi.load_measured()[k0]["n"] == 777 and _arbi.measured_updated() == "2099-01-01",
    )
    check(
        "★ 比本地旧的奸商历史：不采用",
        res["baro_history.json"] == "fresh" and not (_rt / "baro_history.json").exists(),
    )
    check(
        "★ 校验不过的倾向表：不采用",
        res["wiki_disp.json"].startswith("invalid")
        and not (_rt / "de" / "wiki_disp.json").exists(),
    )
    check(
        "默认走本项目 bot-data（raw 打头）",
        any("raw.githubusercontent.com" in u and "bot-data" in u for u in _urls_seen),
    )
    _served["arb_ratings.json"] = newer_arb
    res = _aio.run(_c.refresh_community_static(("arb_ratings.json",)))
    check("同一份再拉一次：fresh、不重复写", res["arb_ratings.json"] == "fresh")

    # 奸商：更新后 baro.load 也要立刻换
    newer_baro = {**_seed["baro_history.json"], "fetched": "2099-02-02"}
    _served["baro_history.json"] = newer_baro
    _baro.load()
    _aio.run(_c.refresh_community_static(("baro_history.json",)))
    check("★ 奸商历史刷新后 baro.load 读到新快照", _baro.snapshot_date() == "2099-02-02")

    # 用户自填地址：只用它
    _urls_seen.clear()
    _c.community_base = _AC.normalize_community_base("https://mirror.example.com/wf/")
    _aio.run(_c.refresh_community_static(("arb_ratings.json",)))
    check(
        "★ 自填社区地址：只请求该地址下的同名文件",
        _urls_seen == ["https://mirror.example.com/wf/arb_ratings.json"],
        str(_urls_seen),
    )
    _aio.run(_c.close())
finally:
    _paths.set_run_dir(None)
    _arbi.reset_measured()
    _baro.reset()
    _shutil.rmtree(_rt, ignore_errors=True)


# 发布端（私有运维脚本）与消费端的静态快照校验同口径：同一批样本结论必须一致
_pub_path = (
    ROOT / ".zcode/skills/astrbot-server-triage/scripts/automations/publish_community_snapshot.py"
)
if _pub_path.exists():
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location("_pub_static", _pub_path)
    _pub = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_pub)
    _samples = []
    for _n, _d in _seed.items():
        _samples.append((_n, _d))
        _samples.append((_n, {**_d, _AC.COMMUNITY_STATIC[_n]["stamp"]: ""}))
    _samples += [
        ("arb_ratings.json", _bad_tier),
        ("arb_ratings.json", {"_updated": "2026-10-16", "nodes": {}}),
        ("wiki_disp.json", {"snapshot": "2026-10-16T00:00:00+00:00", "disp": {"a": 1}}),
        ("baro_history.json", {**_seed["baro_history.json"], "visits": []}),
    ]
    _diff = [
        (n, _AC.community_static_ok(n, d)[0], not _pub.static_ok(n, d))
        for n, d in _samples
        if _AC.community_static_ok(n, d)[0] != (not _pub.static_ok(n, d))
    ]
    check("★ 静态快照校验：发布端与消费端结论一致", not _diff, str(_diff))
    check("发布端静态快照文件集合 == 消费端", set(_pub.STATIC_PATHS) == set(_AC.COMMUNITY_STATIC))
else:
    print("[SKIP] 发布端静态快照对拍：本树无私有运维脚本")


# ---------------------------------------------------------------------------
# ★ 2026-10-16（GPT 评估 P04 / V01）：社区通道记忆 + 冷却 + 总预算；元素加成快照必须属于当前轮次
# ---------------------------------------------------------------------------
import time as _time  # noqa: E402

_cc = _AC.WarframeClient()
_calls = []
_mode = {"raw": "down", "cdn": "ok"}


async def _ff(url, ttl=0, headers=None, cache_key=None, retries=2, **kw):
    _calls.append((url.split("/")[2], retries))
    h = url.split("/")[2]
    if "raw.githubusercontent" in h and _mode["raw"] == "down":
        raise _AC.WarframeAPIError(f"请求失败（{url}）：timeout")
    if "raw.githubusercontent" in h and _mode["raw"] == "404":
        raise _AC.WarframeAPIError(f"接口不存在或暂未开放：{url}")
    return {"ok": h}


_cc._fetch_json = _ff
r = _aio.run(_cc._community_json(_AC.community_static_urls("arb_ratings.json")))
check("raw 不通 → 换 cdn 拿到", r == {"ok": "cdn.jsdelivr.net"}, str(r))
check("每条通道只试一次（retries=0，多通道本身就是重试）", all(rt == 0 for _h, rt in _calls))
_calls.clear()
_aio.run(_cc._community_json(_AC.community_static_urls("baro_history.json")))
check(
    "★ raw 冷却中：别的文件也直接走上次成功的 cdn，不再等 raw 超时",
    [h for h, _ in _calls] == ["cdn.jsdelivr.net"],
    str(_calls),
)
_cc._comm_cool["cdn.jsdelivr.net"] = _time.monotonic() + 999
_cc._comm_cool["gcore.jsdelivr.net"] = _time.monotonic() + 999
_calls.clear()
_aio.run(_cc._community_json(_AC.community_static_urls("wiki_disp.json")))
check("全部冷却时仍重探排第一的那个（网络恢复能自己回来）", len(_calls) == 1, str(_calls))
_cc2 = _AC.WarframeClient()
_cc2._fetch_json = _ff
_mode["raw"] = "404"
_aio.run(_cc2._community_json(_AC.community_static_urls("arb_ratings.json")))
check("404（镜像没这个文件）不冷却主机", "raw.githubusercontent.com" not in _cc2._comm_cool)


async def _slow(url, **kw):
    await _aio.sleep(5)
    return {}


_cc3 = _AC.WarframeClient()
_cc3._fetch_json = _slow
_old_budget = _AC.COMMUNITY_BUDGET
_AC.COMMUNITY_BUDGET = 1.5
try:
    t0 = _time.monotonic()
    r = _aio.run(_cc3._community_json(_AC.community_static_urls("arb_ratings.json")))
    check(
        "★ 总预算截断：三条慢通道不会叠加等待",
        r is None and _time.monotonic() - t0 < 3.5,
        f"{_time.monotonic() - t0:.1f}s",
    )
finally:
    _AC.COMMUNITY_BUDGET = _old_budget


async def _hang_raw(url, **kw):
    if "raw.githubusercontent" in url:
        await _aio.sleep(60)  # 模拟 raw 卡死（不报错、一直不回）
    return {"ok": url.split("/")[2]}


_cc4 = _AC.WarframeClient()
_cc4._fetch_json = _hang_raw
_old_pc = _AC.COMMUNITY_PER_CHANNEL
_AC.COMMUNITY_PER_CHANNEL = 0.3
try:
    r = _aio.run(_cc4._community_json(_AC.community_static_urls("arb_ratings.json")))
    check(
        "★ raw 卡死：单通道上限到点就换镜像，同一次取数拿到数据（ZCode 10-16 线上发现）",
        r == {"ok": "cdn.jsdelivr.net"},
        str(r),
    )
    check("卡死的主机进入冷却", "raw.githubusercontent.com" in _cc4._comm_cool)
finally:
    _AC.COMMUNITY_PER_CHANNEL = _old_pc
check("失败日志带异常类型（超时类异常 str 为空）", "str(exc) or type(exc).__name__" in API_SRC)
for _c in (_cc, _cc2, _cc3, _cc4):
    _aio.run(_c.close())

# V01：社区元素加成快照必须落在当前轮次窗口内（信条 / 终幕两段 epoch 不同，各算各的窗口）
from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402

_rot = _json.loads((_PKG / "rotations.json").read_text(encoding="utf-8"))
_cv = _AC.WarframeClient()
_now = _dt.now(_tz.utc)


def _win(sect):
    ep = _dt.fromisoformat(sect["epoch"])
    per = _td(hours=int(sect.get("period_hours", 96)))
    return ep + (_now - ep) // per * per, per


_starts = [_win(_rot[k]) for k in ("tenet", "coda")]
for _k in ("tenet", "coda"):
    _rot[_k]["valence_snapshot"] = (
        min(st for st, _ in _starts) - _td(days=30)
    ).isoformat()  # 本地很旧


def _payload(ts):
    return {
        "published_at": ts,
        "tenet": {"items": [{"en": "x", "element": "Heat", "bonus": 0.6}]},
        "coda": {"batch": "A", "items": [{"en": "y", "element": "Heat", "bonus": 0.6}]},
    }


async def _serve(ts):
    async def _f(urls):
        return _payload(ts)

    _cv._community_json = _f
    return await _cv._community_valence_parsed(_rot)


_prev = (max(st for st, _ in _starts) - _td(hours=1)).isoformat()
check("★ V01：上一轮的快照（比本地新但早于本轮开始）不采用", _aio.run(_serve(_prev)) is None, _prev)
_cur = (_now - _td(minutes=1)).isoformat()
_res = _aio.run(_serve(_cur))
check(
    "V01：本轮内的快照照常采用（对照组，证明上一条不是被别的校验拦下）",
    _res is not None and _res.get("snapshot") == _cur,
    str(_res)[:120],
)
check("V01：社区路径写回数据源时刻而非 now", 'snap = str(raw["snapshot"])' in API_SRC)
_aio.run(_cv.close())


# ---------------------------------------------------------------------------
# ★ 2026-10-16（GPT 评估 P01 / P02）：告警出口只在确认送达后去重；OneBot 带正文；多通道兜底
# ---------------------------------------------------------------------------
_ap_path = ROOT / ".zcode/skills/astrbot-server-triage/scripts/automations/alert_push.py"
if _ap_path.exists():
    import importlib.util as _ilu2
    import os as _os

    _spec2 = _ilu2.spec_from_file_location("_alert_push_t", _ap_path)
    _ap = _ilu2.module_from_spec(_spec2)
    _spec2.loader.exec_module(_ap)
    _td_ap = Path(_tmp.mkdtemp(prefix="_tmp_alert_"))
    _ap.LOG_DIR, _ap.ALERTS = _td_ap, _td_ap / "ALERTS.md"
    _env_keys = (
        "ASTRBOT_OPENAPI_BASE",
        "ASTRBOT_OPENAPI_KEY",
        "ASTRBOT_ALERT_UMO",
        "ALERT_WEBHOOK_URL",
        "ALERT_ONEBOT_URL",
        "ALERT_TARGET",
        "ALERT_ONEBOT_TOKEN",
    )
    _env_bak = {k: _os.environ.get(k) for k in _env_keys}
    _sent = []
    _behave = {}

    def _fake_post(url, payload, token=""):
        _sent.append((url, payload))
        b = _behave.get(url.split("/")[2], "ok")
        if b == "raise":
            raise OSError("connection refused")
        if b == "biz_fail":
            return {"status": "failed", "retcode": 100, "message": "bot offline"}
        return {"status": "ok", "retcode": 0}

    _ap._post = _fake_post
    try:
        for k in _env_keys:
            _os.environ.pop(k, None)
        _os.environ.update(
            {
                "ASTRBOT_OPENAPI_BASE": "http://api.local",
                "ASTRBOT_OPENAPI_KEY": "k",
                "ASTRBOT_ALERT_UMO": "bot:FriendMessage:1",
                "ALERT_ONEBOT_URL": "http://ob.local",
                "ALERT_TARGET": "10001",
            }
        )
        _behave = {"api.local": "raise"}
        r = _ap.push("T1", "body1")
        check(
            "★ 主通道抛异常 → 用已配置的 OneBot 兜底送达",
            r.startswith("onebot") and len(_sent) == 2,
            r,
        )
        check(
            "★ OneBot 请求带消息正文（原先只有目标）",
            _sent[-1][1].get("message", "").startswith("T1"),
            str(_sent[-1]),
        )
        _sent.clear()
        check("送达后 24h 去重", "去重" in _ap.push("T1", "body1") and not _sent)
        _behave = {"api.local": "raise", "ob.local": "biz_fail"}
        r = _ap.push("T2", "body2")
        check(
            "★ HTTP 200 但业务失败也算失败；全部失败不记送达", r.startswith("全部远程通道失败"), r
        )
        _behave = {}
        _sent.clear()
        r = _ap.push("T2", "body2")
        check(
            "★ 上次没送达的同一告警，下次触发会重试并送达",
            r.startswith("openapi") and len(_sent) == 1,
            r,
        )
        txt = _ap.ALERTS.read_text(encoding="utf-8")
        check("文件落档同一摘要 24h 只一次（重试不刷文件）", txt.count("]** T2") == 1, txt[-300:])
        for k in _env_keys:
            _os.environ.pop(k, None)
        check(
            "没配远程通道：落文件即视为送达",
            _ap.push("T3", "b3") == "文件(ALERTS.md)" and "去重" in _ap.push("T3", "b3"),
        )
    finally:
        for k, v in _env_bak.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        _shutil.rmtree(_td_ap, ignore_errors=True)
else:
    print("[SKIP] 告警出口用例：本树无私有运维脚本")

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 社区快照守卫全部通过")
