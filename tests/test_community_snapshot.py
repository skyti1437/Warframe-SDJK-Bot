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

    # ★ 2026-10-19 分段时间戳：每段写**自己的**快照时刻；缺时间戳就**不写**该字段（别拿 now 填）
    check(
        "★ 发布侧：分段快照时刻 —— 每段写自己的 valence_snapshot",
        payload["tenet"].get("snapshot") == STAMP and payload["coda"].get("snapshot") == STAMP,
        f"tenet={payload['tenet'].get('snapshot')!r} coda={payload['coda'].get('snapshot')!r}",
    )
    _no_ts = _rot(filled_idx=1)
    _no_ts["tenet"].pop("valence_snapshot")
    _p_no_ts = P.build_valence(_no_ts, STAMP, NOW)
    check(
        "★ 发布侧：某段没有时间戳 ⇒ **不写** snapshot（不是空串、也不是 now）",
        "snapshot" not in _p_no_ts["tenet"] and _p_no_ts["coda"].get("snapshot") == STAMP,
        f"tenet keys={sorted(_p_no_ts['tenet'])}",
    )
    _later = P._latest_stamp("2026-10-18T06:56:52+00:00", "2026-10-19T00:17:00+00:00")
    check(
        "★ 发布侧：published_at 取两段**较晚者**（留给 ≤1.2.4 旧版插件）",
        _later == "2026-10-19T00:17:00+00:00",
        _later,
    )
    check(
        "  较晚者：解析不了的忽略 / 全空返回空串（调用方再退 now）",
        P._latest_stamp("", None, "2026-10-18T06:56:52+00:00") == "2026-10-18T06:56:52+00:00"
        and P._latest_stamp("", None) == "",
        f"{P._latest_stamp('', None, '2026-10-18T06:56:52+00:00')!r} / {P._latest_stamp('', None)!r}",
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
    # 2026-10-18 起两者另带 force（面板「从源头重抓」），只核对 community_only 仍是第一个参数
    "async def refresh_valence(self, community_only: bool = False" in API_SRC
    and "async def refresh_acrichis_week(self, community_only: bool = False" in API_SRC,
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
# ★ 2026-10-16（v1.2.1 公开仓 CI 红）：开源包排除表（OSS_EXCLUDE_FILES）里的种子公开仓没有
#   —— 只校验本树实际带着的种子，缺的记 SKIP。（紫卡倾向表 2026-10 已退出社区快照。）
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
    "静态快照校验：已退役的倾向表不再认（未知快照）",
    _AC.community_static_ok(
        "wiki_disp.json", {"snapshot": "2026-10-16T00:00:00+00:00", "disp": {"a": 1}}
    )
    == (False, "未知快照"),
)
check(
    "V06：时间戳在未来（> 1 天）被拒，消费端与发布端同口径",
    _AC.community_static_ok(
        "arb_ratings.json", {**_seed["arb_ratings.json"], "_updated": "2099-01-01"}
    )[1].startswith("时间戳在未来"),
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
    _now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    newer_arb["_updated"] = _now_iso
    k0 = next(iter(newer_arb["nodes"]))
    newer_arb["nodes"][k0]["n"] = 777
    older_baro = {**_seed["baro_history.json"], "fetched": "2000-01-01"}
    _served = {
        "arb_ratings.json": newer_arb,
        "baro_history.json": older_baro,
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
        _arbi.load_measured()[k0]["n"] == 777 and _arbi.measured_updated() == _now_iso,
    )
    check(
        "★ 比本地旧的奸商历史：不采用",
        res["baro_history.json"] == "fresh" and not (_rt / "baro_history.json").exists(),
    )
    check(
        "★ 倾向表已退出社区快照：不再拉取、不落盘",
        "wiki_disp.json" not in res and not (_rt / "de" / "wiki_disp.json").exists(),
        str(res),
    )
    check(
        "默认走本项目 bot-data（raw 打头）",
        any("raw.githubusercontent.com" in u and "bot-data" in u for u in _urls_seen),
    )
    _served["arb_ratings.json"] = newer_arb
    res = _aio.run(_c.refresh_community_static(("arb_ratings.json",)))
    check("同一份再拉一次：fresh、不重复写", res["arb_ratings.json"] == "fresh")

    # 奸商：更新后 baro.load 也要立刻换
    newer_baro = {**_seed["baro_history.json"], "fetched": _now_iso}
    _served["baro_history.json"] = newer_baro
    _baro.load()
    _aio.run(_c.refresh_community_static(("baro_history.json",)))
    check("★ 奸商历史刷新后 baro.load 读到新快照", _baro.snapshot_date() == _now_iso)

    # 用户自填地址：只用它（离线测试里 mirror.example.com 解析不了 ⇒ 这段只测路由，内网校验单测在下面）
    _orig_reason = _AC.private_url_reason

    async def _public(_u):
        return ""

    _AC.private_url_reason = _public
    try:
        _urls_seen.clear()
        _c.community_base = _AC.normalize_community_base("https://mirror.example.com/wf/")
        _aio.run(_c.refresh_community_static(("arb_ratings.json",)))
        check(
            "★ 自填社区地址：只请求该地址下的同名文件",
            _urls_seen == ["https://mirror.example.com/wf/arb_ratings.json"],
            str(_urls_seen),
        )
    finally:
        _AC.private_url_reason = _orig_reason
    # ★ 2026-10-25（Astra 评估 S4）：自填地址指向内网 ⇒ 不发请求；显式放行后照常请求
    _urls_seen.clear()
    _c.community_base = _AC.normalize_community_base("http://127.0.0.1:18765/private")
    _aio.run(_c.refresh_community_static(("arb_ratings.json",)))
    check("★ 自填社区地址指向本机 ⇒ 拒绝、不发请求", _urls_seen == [], str(_urls_seen))
    _c.allow_private_urls = True
    _aio.run(_c.refresh_community_static(("arb_ratings.json",)))
    check(
        "allow_private_urls 开启 ⇒ 内网地址照常请求",
        _urls_seen == ["http://127.0.0.1:18765/private/arb_ratings.json"],
        str(_urls_seen),
    )
    _c.allow_private_urls = False
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
        ("baro_history.json", {**_seed["baro_history.json"], "visits": []}),
        # ★ 2026-10-22（V06 后半）：schema 版本 —— 两端必须同判。
        #   （`_seed` 自身**没有** schema 字段 ⇒ 上面那批已覆盖「缺省视为 1」。）
        ("arb_ratings.json", {**_seed["arb_ratings.json"], "schema": 1}),
        ("arb_ratings.json", {**_seed["arb_ratings.json"], "schema": 2}),
        ("arb_ratings.json", {**_seed["arb_ratings.json"], "schema": 0}),
        ("arb_ratings.json", {**_seed["arb_ratings.json"], "schema": "1"}),
        ("arb_ratings.json", {**_seed["arb_ratings.json"], "schema": True}),
        ("baro_history.json", {**_seed["baro_history.json"], "schema": 2}),
        ("baro_history.json", {**_seed["baro_history.json"], "schema": 1}),
    ]
    _diff = [
        (n, _AC.community_static_ok(n, d)[0], not _pub.static_ok(n, d))
        for n, d in _samples
        if _AC.community_static_ok(n, d)[0] != (not _pub.static_ok(n, d))
    ]
    check("★ 静态快照校验：发布端与消费端结论一致", not _diff, str(_diff))
    check(
        "★ V06 后半：schema 缺省视为 1 ⇒ 旧快照照收",
        _AC.community_static_ok("arb_ratings.json", _seed["arb_ratings.json"])[0] is True,
    )
    _hi = {**_seed["arb_ratings.json"], "schema": 2}
    check(
        "★ V06 后半：schema 高于本端 ⇒ 拒收并给原因",
        _AC.community_static_ok("arb_ratings.json", _hi)[0] is False
        and "schema" in _AC.community_static_ok("arb_ratings.json", _hi)[1],
        _AC.community_static_ok("arb_ratings.json", _hi)[1],
    )
    check(
        "★ V06 后半：发布端写入 schema（发布出去的静态快照带版本号）",
        _pub.STATIC_SCHEMA == _AC.COMMUNITY_STATIC_SCHEMA == 1,
        f"{getattr(_pub, 'STATIC_SCHEMA', None)} / {_AC.COMMUNITY_STATIC_SCHEMA}",
    )
    check("发布端静态快照文件集合 == 消费端", set(_pub.STATIC_PATHS) == set(_AC.COMMUNITY_STATIC))
    check(
        "★ 倾向表退役：发布端不再发、但远端旧文件保留（不进待删除）",
        "wiki_disp.json" not in _pub.STATIC_PATHS and "wiki_disp.json" in _pub.RETIRED_STATIC,
    )
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
_aio.run(_cc._community_json(_AC.community_static_urls("acrithis_week.json")))
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


# ZCode 10-17 线上：raw 超时后「超出本次时间预算」，镜像没轮到 —— 外层取消不能及时打断
#   httpx（连接清理受保护）。模拟一条「取消不掉」的 raw：只有把单通道上限作为请求自身的
#   timeout 传进去，它才会按时失败；否则要拖满 3 秒（远超 1.5 s 总预算）。
async def _uncancellable_raw(url, **kw):
    if "raw.githubusercontent" in url:
        t = kw.get("timeout")
        if t is None:
            try:
                await _aio.sleep(3.0)
            except _aio.CancelledError:
                await _aio.sleep(3.0)  # 被取消后清理还要拖一阵（仿 httpx 受保护的连接清理）
                raise
            raise _aio.TimeoutError()
        await _aio.sleep(t)
        raise _AC.WarframeAPIError("请求失败（模拟 httpx 自身超时）")
    return {"ok": url.split("/")[2]}


_cc5 = _AC.WarframeClient()
_cc5._fetch_json = _uncancellable_raw
_old_pc, _old_budget = _AC.COMMUNITY_PER_CHANNEL, _AC.COMMUNITY_BUDGET
_AC.COMMUNITY_PER_CHANNEL, _AC.COMMUNITY_BUDGET = 0.3, 1.5
try:
    t0 = _time.monotonic()
    r = _aio.run(_cc5._community_json(_AC.community_static_urls("arb_ratings.json")))
    check(
        "★ raw 取消不掉时：单通道上限作为请求 timeout 传入 ⇒ 按时换镜像拿到数据（ZCode 10-17）",
        r == {"ok": "cdn.jsdelivr.net"} and _time.monotonic() - t0 < 1.5,
        f"{r} {_time.monotonic() - t0:.1f}s",
    )
finally:
    _AC.COMMUNITY_PER_CHANNEL, _AC.COMMUNITY_BUDGET = _old_pc, _old_budget


class _RecHttp:
    def __init__(self):
        self.kw = None

    async def get(self, url, **kw):
        self.kw = kw
        raise _AC.httpx.ConnectTimeout("t")

    async def aclose(self):
        pass


_cc6 = _AC.WarframeClient()
_rec = _RecHttp()
_real_http, _cc6._http = _cc6._http, _rec
try:
    _aio.run(_cc6._fetch_json("https://example.invalid/x.json", ttl=1, retries=0, timeout=4.0))
except _AC.WarframeAPIError:
    pass
check(
    "_fetch_json 把 timeout 透传给 httpx 请求", (_rec.kw or {}).get("timeout") == 4.0, str(_rec.kw)
)


class _EmptyTimeoutHttp(_RecHttp):
    async def get(self, url, **kw):
        raise _AC.httpx.ReadTimeout("")  # 超时类异常 str() 为空


_cc6._http = _EmptyTimeoutHttp()
try:
    _aio.run(_cc6._fetch_json("https://example.invalid/y.json", ttl=1, retries=0))
    _why = ""
except _AC.WarframeAPIError as _e:
    _why = str(_e)
check(
    "超时异常文本为空时，失败原因退回类型名（不再「：」后空白）", _why.endswith("ReadTimeout"), _why
)
_cc6._http = _real_http
for _c in (_cc, _cc2, _cc3, _cc4, _cc5, _cc6):
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

# ★ 2026-10-19 分段时间戳：信条 / 终幕各自换轮。10-19 实况：published_at = 信条时刻（10-18，早于终幕
#   本轮起点 10-19），终幕段自己的快照在本轮 ⇒ 旧逻辑整份拒用，新逻辑按段判应采用。
_t_start, _ = _win(_rot["tenet"])
_c_start, _ = _win(_rot["coda"])
_early, _late = sorted([_t_start, _c_start])
_late_key = "tenet" if _t_start >= _c_start else "coda"
_early_key = "coda" if _late_key == "tenet" else "tenet"


def _payload2(pub, t_snap=None, c_snap=None):
    p = _payload(pub)
    if t_snap is not None:
        p["tenet"]["snapshot"] = t_snap
    if c_snap is not None:
        p["coda"]["snapshot"] = c_snap
    return p


async def _serve2(payload, rot):
    async def _f(urls):
        return payload

    _cv._community_json = _f
    return await _cv._community_valence_parsed(rot)


# 较早换轮的那段在它自己的本轮内、但早于较晚那段的窗口起点；较晚那段在自己的本轮内
_e_ts = (
    (_early + _td(minutes=17)).isoformat()
    if _early + _td(minutes=17) < _late
    else _early.isoformat()
)
_l_ts = (_late + _td(minutes=17)).isoformat()
_seg = {_early_key: _e_ts, _late_key: _l_ts}
_r2 = _aio.run(_serve2(_payload2(_e_ts, t_snap=_seg["tenet"], c_snap=_seg["coda"]), _rot))
check(
    "★ 分段时间戳：published_at 早于较晚一段的窗口，但两段各自都在本轮 ⇒ 采用",
    _r2 is not None and _r2["snapshots"] == _seg,
    str(_r2 and _r2.get("snapshots")),
)
_r3 = _aio.run(_serve2(_payload2(_e_ts), _rot))
check(
    "旧发布器（无分段时间戳）：回退 published_at，较晚那段判上一轮 ⇒ 仍拒用（行为不变）",
    _r3 is None,
    str(_r3),
)
_rot_new = json.loads(json.dumps(_rot))
_rot_new[_late_key]["valence_snapshot"] = (_late + _td(hours=2)).isoformat()  # 本地这段比快照还新
_r4 = _aio.run(_serve2(_payload2(_l_ts, t_snap=_seg["tenet"], c_snap=_seg["coda"]), _rot_new))
check("分段时间戳：任一段比本地同段旧 ⇒ 拒用（防拿旧的覆盖新的）", _r4 is None, str(_r4))
check(
    "社区路径按段写回 valence_snapshot（不再两段同写一个时刻）",
    'tenet_data["valence_snapshot"] = sect_snap["tenet"]' in API_SRC
    and 'coda_data["valence_snapshot"] = sect_snap["coda"]' in API_SRC,
)
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

# ---------------------------------------------------------------------------
# ★ 2026-10-17：社区快照链路健康记录（生成 → 发布 → 远端内容 → 镜像）+ 独立检查器
#   —— 生成器/发布器把每次结果写成机读记录，community_health.py 独立复核并「状态变化才告警」。
#   全部用本地 git（bare origin + 克隆，file:// 协议）与临时 gen 目录，不联网。
# ---------------------------------------------------------------------------
_ch_path = ROOT / ".zcode/skills/astrbot-server-triage/scripts/automations/community_health.py"
if _ch_path.exists():
    import hashlib as _hl
    import importlib.util as _ilu3
    import os as _os2
    import subprocess as _sp
    from datetime import timedelta as _tdlt

    def _load_mod(_p, _name):
        _s = _ilu3.spec_from_file_location(_name, _p)
        _m = _ilu3.module_from_spec(_s)
        _s.loader.exec_module(_m)
        return _m

    _GEN = _load_mod(_ch_path.parent / "gen_static_snapshots.py", "_gen_static_t")
    _CH = _load_mod(_ch_path, "_community_health_t")
    _PUBW = _load_mod(_ch_path.parent / "publish_community_snapshot.py", "_pub_health_t")
    # 推送成功后请 CDN 清缓存是**真网络调用**：本地用例一律打桩（本文件全部离线跑）
    _PUBW.purge_cdn_cache = lambda *a, **k: None

    _GIT_ID = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def _git(*_args, _cwd=None):
        return _sp.run(
            ["git", *_args],
            cwd=_cwd,
            capture_output=True,
            text=True,
            env={**_os2.environ, **_GIT_ID, "GIT_CONFIG_NOSYSTEM": "1"},
        )

    def _mk_repo(_td, _files):
        """造「本地 bare origin + 克隆出的 bot-data 工作副本」，返回工作副本路径（零网络）。"""
        _bare, _seed = _td / "origin.git", _td / "seed"
        _git("init", "-q", "--bare", str(_bare))
        _seed.mkdir()
        _git("init", "-q", "-b", "bot-data", _cwd=str(_seed))
        for _n, _t in _files.items():
            (_seed / _n).write_text(_t, encoding="utf-8")
        _git("add", "-A", _cwd=str(_seed))
        _git("commit", "-q", "-m", "init", _cwd=str(_seed))
        _git("remote", "add", "origin", str(_bare), _cwd=str(_seed))
        _git("push", "-q", "origin", "bot-data", _cwd=str(_seed))
        _work = _td / "work"
        _git(
            "clone",
            "-q",
            "--depth",
            "1",
            "--branch",
            "bot-data",
            "file://" + str(_bare),
            str(_work),
        )
        return _work

    def _arb_doc(_nodes=60):
        return {
            "_updated": "2026-10-17",
            "nodes": {f"n{i}": {"n": 20 + i, "tier": "A"} for i in range(_nodes)},
        }

    def _baro_doc(_visits=12):
        return {
            "source": "wiki",
            "fetched": "2026-10-17",
            "visits": [{"d": "2026-10-17"} for _ in range(_visits)],
            "items": {f"i{i}": {"t": 1, "d": 1, "c": 1, "v": 1} for i in range(60)},
        }

    def _txt(_d):
        return _json.dumps(_d, ensure_ascii=False, indent=1) + "\n"

    _NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    # 窗口起点：tenet 2026-10-14T00:00Z、coda 2026-10-15T00:00Z → 取较晚者判定
    _ROT = {
        "tenet": {"epoch": "2026-09-12T00:00:00+00:00", "period_hours": 96},
        "coda": {"epoch": "2026-09-13T00:00:00+00:00", "period_hours": 96},
    }

    def _remote_files(
        _valence_pub="2026-10-16T00:00:00+00:00",
        _acrithis_exp="2026-10-19T00:00:00+00:00",
        _arb=None,
        _baro=None,
        _valence_snaps=None,
    ):
        # ★ 2026-10-19 分段时间戳：`_valence_snaps={"tenet": …, "coda": …}` 时给对应段写
        #   `<段>.snapshot`；不传则**不写该字段**（= 旧格式快照，检查器应回退 published_at）。
        _tenet = {"items": []}
        _coda = {"batch": "A", "items": []}
        for _sect, _blk in (("tenet", _tenet), ("coda", _coda)):
            _s = (_valence_snaps or {}).get(_sect)
            if _s:
                _blk["snapshot"] = _s
        return {
            "valence.json": _txt(
                {
                    "schema": 1,
                    "published_at": _valence_pub,
                    "tenet": _tenet,
                    "coda": _coda,
                }
            ),
            "acrithis_week.json": _txt(
                {
                    "schema": 1,
                    "published_at": "2026-10-12T00:00:00+00:00",
                    "expiry": _acrithis_exp,
                    "items": [{"name": "x"}],
                }
            ),
            "arb_ratings.json": _txt(_arb if _arb is not None else _arb_doc()),
            "baro_history.json": _txt(_baro if _baro is not None else _baro_doc()),
            "index.json": _txt({"schema": 1, "published_at": _valence_pub, "files": {}}),
        }

    def _sha(_s):
        return _hl.sha256(_s.encode("utf-8")).hexdigest()

    def _gen_rec(_last, _result="updated", _sha256="", _count=60):
        return {
            "last_run": _last,
            "last_success": _last,
            "result": _result,
            "reason": "",
            "stamp": "2026-10-17",
            "sha256": _sha256,
            "count": _count,
        }

    def _gen_health(_now=_NOW, _arb_sha="", _baro_sha="", _arb_last=None, _baro_last=None):
        return {
            "arb_ratings.json": _gen_rec((_arb_last or _now).isoformat(), _sha256=_arb_sha),
            "baro_history.json": _gen_rec((_baro_last or _now).isoformat(), _sha256=_baro_sha),
        }

    def _pub_health(_now=_NOW, _result="unchanged", _age_min=10):
        return {
            "last_run": (_now - _tdlt(minutes=_age_min)).isoformat(),
            "mode": "git",
            "dry_run": False,
            "result": _result,
            "head": "abc1234",
            "files": {},
        }

    def _case(
        _remote=None,
        _gh=None,
        _ph=None,
        _now=_NOW,
        _rotations=_ROT,
        _fetch=False,
        _alert=False,
        _work=None,
    ):
        _td = Path(_tmp.mkdtemp(prefix="_ch_"))
        _gen = _td / "gen"
        _gen.mkdir()
        if _gh is not None:
            (_gen / "_health.json").write_text(_json.dumps(_gh), encoding="utf-8")
        if _ph is not None:
            (_gen / "_publish_health.json").write_text(_json.dumps(_ph), encoding="utf-8")
        _files = _remote if _remote is not None else _remote_files()
        _work = _work if _work is not None else _mk_repo(_td, _files)
        _rc, _rep = _CH.run(
            gen_dir=_gen,
            work_dir=_work,
            fetch=_fetch,
            alert=_alert,
            now=_now,
            rotations=_rotations,
            quiet=True,
        )
        return _rc, _rep, _gen, _td

    def _st(_rep, _cid):
        return next((_c["status"] for _c in _rep["checks"] if _c["id"] == _cid), "（缺）")

    # —— 全绿场景（顺便跑真 fetch：本地 file:// origin）——
    _rf = _remote_files()
    _rc, _rep, _gen, _td = _case(
        _remote=_rf,
        _fetch=True,
        _gh=_gen_health(
            _arb_sha=_sha(_rf["arb_ratings.json"]), _baro_sha=_sha(_rf["baro_history.json"])
        ),
        _ph=_pub_health(),
    )
    check(
        "★ 健康检查器：全绿 → exit 0 / overall=ok（含真 fetch）",
        _rc == 0 and _rep["overall"] == "ok",
        _json.dumps(_rep["checks"], ensure_ascii=False)[:400],
    )
    check(
        "健康检查器：检查项 id 唯一（状态比对才有意义）",
        len({_c["id"] for _c in _rep["checks"]}) == len(_rep["checks"]),
        str([_c["id"] for _c in _rep["checks"]]),
    )
    check(
        "健康检查器：报告落盘（checked_at / overall / checks）",
        _json.loads((_gen / "_community_health.json").read_text(encoding="utf-8"))["overall"]
        == "ok",
    )
    check(
        "健康检查器：SUMMARY 行与 server_patrol 同款",
        _CH.summary_line(_rep["checks"]) == (0, "SUMMARY|exit=0|todo=0|全部正常"),
        str(_CH.summary_line(_rep["checks"])),
    )
    check(
        "健康检查器：--no-alert 连状态文件都不写（免得吞掉下一次真告警）",
        not (_gen / "_community_health_state.json").exists(),
    )
    _shutil.rmtree(_td, ignore_errors=True)

    # —— 发布器心跳 ——
    _rc, _rep, _gen, _td = _case(_gh=_gen_health(), _ph=_pub_health(_age_min=180))
    check(
        "★ 检查器：发布器 3h 没跑 → publisher_alive=fail / exit 2",
        _st(_rep, "publisher_alive") == "fail" and _rc == 2,
        str(_rep["checks"][0]),
    )
    _shutil.rmtree(_td, ignore_errors=True)
    _rc, _rep, _gen, _td = _case(_gh=_gen_health(), _ph=_pub_health(_result="failed"))
    check(
        "★ 检查器：上次发布 result=failed → publisher_alive=fail（哪怕刚跑过）",
        _st(_rep, "publisher_alive") == "fail" and "失败" in _rep["checks"][0]["detail"],
        _rep["checks"][0]["detail"],
    )
    _shutil.rmtree(_td, ignore_errors=True)
    _rc, _rep, _gen, _td = _case(_gh=_gen_health(), _ph=None)
    check(
        "★ 检查器：没有发布记录 → publisher_alive=fail（不能默认健康）",
        _st(_rep, "publisher_alive") == "fail",
        _rep["checks"][0]["detail"],
    )
    _shutil.rmtree(_td, ignore_errors=True)

    # —— 生成器心跳 ——
    _rc, _rep, _gen, _td = _case(_gh=_gen_health(_arb_last=_NOW - _tdlt(days=9)), _ph=_pub_health())
    check(
        "★ 检查器：仲裁评级 9 天没成功生成 → gen_arb_alive=fail",
        _st(_rep, "gen_arb_alive") == "fail",
        str(_rep["checks"][1]),
    )
    _shutil.rmtree(_td, ignore_errors=True)
    _rc, _rep, _gen, _td = _case(
        _gh={"baro_history.json": _gen_rec(_NOW.isoformat())}, _ph=_pub_health()
    )
    check(
        "★ 检查器：缺生成记录 → gen_arb_alive=warn（缺失不当成 fail，直说看不到）",
        _st(_rep, "gen_arb_alive") == "warn" and _st(_rep, "gen_baro_alive") == "ok",
        str(_rep["checks"][1:3]),
    )
    _shutil.rmtree(_td, ignore_errors=True)

    # —— 远端内容：相位（换轮）/ 失效阈值 ——
    _old_val = _remote_files(_valence_pub="2026-10-14T00:00:00+00:00")
    _rc, _rep, _gen, _td = _case(_remote=_old_val, _gh=_gen_health(), _ph=_pub_health())
    check(
        "★ 检查器：远端 valence 早于当前窗口起点且过 8h 宽限 → fail / exit 2",
        _st(_rep, "valence_current") == "fail" and _rc == 2,
        next(_c["detail"] for _c in _rep["checks"] if _c["id"] == "valence_current"),
    )
    _shutil.rmtree(_td, ignore_errors=True)
    # 刚换轮（10-15T00:00Z 窗口起点，取 02:00Z = 宽限内）
    _grace_now = datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc)
    _rc, _rep, _gen, _td = _case(
        _remote=_old_val,
        _now=_grace_now,
        _gh=_gen_health(_now=_grace_now),
        _ph=_pub_health(_now=_grace_now),
    )
    check(
        "★ 检查器：刚换轮 8h 宽限内 → valence_current=warn / exit 1",
        _st(_rep, "valence_current") == "warn" and _rc == 1,
        next(_c["detail"] for _c in _rep["checks"] if _c["id"] == "valence_current"),
    )
    _shutil.rmtree(_td, ignore_errors=True)

    # —— 分段时间戳（2026-10-19）：信条与终幕 epoch 差一天，各判各的窗口 ——
    # now=10-19T12:00Z 时窗口起点 = 信条 10-18T00:00 / 终幕 10-19T00:00（与线上实况同）。
    _seg_now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    _rc, _rep, _gen, _td = _case(
        _remote=_remote_files(
            # ★ published_at 故意留**信条时刻**（旧发布器的短路结果）—— 旧逻辑正是因此判 fail
            _valence_pub="2026-10-18T06:56:52+00:00",
            _acrithis_exp="2026-10-26T00:00:00+00:00",
            _valence_snaps={
                "tenet": "2026-10-18T06:56:52+00:00",
                "coda": "2026-10-19T00:17:00+00:00",
            },
        ),
        _now=_seg_now,
        _gh=_gen_health(_now=_seg_now),
        _ph=_pub_health(_now=_seg_now),
    )
    check(
        "★ 检查器：分段时间戳 —— 信条早/终幕晚但各在自己窗口内 ⇒ ok（旧逻辑会误判 fail）",
        _st(_rep, "valence_current") == "ok" and _rc == 0,
        next(_c["detail"] for _c in _rep["checks"] if _c["id"] == "valence_current"),
    )
    _shutil.rmtree(_td, ignore_errors=True)

    _rc, _rep, _gen, _td = _case(
        _remote=_remote_files(
            _valence_pub="2026-10-18T06:56:52+00:00",
            _acrithis_exp="2026-10-26T00:00:00+00:00",
            _valence_snaps={
                "tenet": "2026-10-18T06:56:52+00:00",
                "coda": "2026-10-18T00:00:00+00:00",  # 早于终幕窗口起点 10-19T00:00 且过 8h 宽限
            },
        ),
        _now=_seg_now,
        _gh=_gen_health(_now=_seg_now),
        _ph=_pub_health(_now=_seg_now),
    )
    _seg_detail = next(_c["detail"] for _c in _rep["checks"] if _c["id"] == "valence_current")
    check(
        "★ 检查器：分段时间戳 —— 终幕段真落后 ⇒ fail 且只点名终幕（信条在窗口内不冤枉）",
        _st(_rep, "valence_current") == "fail"
        and _rc == 2
        and "终幕" in _seg_detail
        and "信条" not in _seg_detail,
        _seg_detail,
    )
    _shutil.rmtree(_td, ignore_errors=True)

    _rc, _rep, _gen, _td = _case(
        _remote=_remote_files(
            # 旧格式快照：两段都没有 `<段>.snapshot` ⇒ 两段都回退 published_at（= 旧逻辑）
            _acrithis_exp="2026-10-26T00:00:00+00:00",
            _valence_pub="2026-10-19T00:17:00+00:00",
        ),
        _now=_seg_now,
        _gh=_gen_health(_now=_seg_now),
        _ph=_pub_health(_now=_seg_now),
    )
    check(
        "★ 检查器：分段时间戳 —— 缺分段字段（旧快照）⇒ 回退 published_at 旧逻辑（够新 ⇒ ok）",
        _st(_rep, "valence_current") == "ok" and _rc == 0,
        next(_c["detail"] for _c in _rep["checks"] if _c["id"] == "valence_current"),
    )
    _shutil.rmtree(_td, ignore_errors=True)

    _rc, _rep, _gen, _td = _case(
        _remote=_remote_files(_acrithis_exp="2026-10-16T00:00:00+00:00"),
        _gh=_gen_health(),
        _ph=_pub_health(),
    )
    check(
        "★ 检查器：言录使过期超 12h 宽限 → acrithis_current=fail / exit 2",
        _st(_rep, "acrithis_current") == "fail" and _rc == 2,
        next(_c["detail"] for _c in _rep["checks"] if _c["id"] == "acrithis_current"),
    )
    _shutil.rmtree(_td, ignore_errors=True)
    _rc, _rep, _gen, _td = _case(
        _remote=_remote_files(_acrithis_exp="2026-10-17T06:00:00+00:00"),
        _gh=_gen_health(),
        _ph=_pub_health(),
    )
    check(
        "★ 检查器：言录使刚过期（12h 宽限内）→ warn",
        _st(_rep, "acrithis_current") == "warn",
        next(_c["detail"] for _c in _rep["checks"] if _c["id"] == "acrithis_current"),
    )
    _shutil.rmtree(_td, ignore_errors=True)

    # —— 远端静态快照：校验 / 发布滞后 / 倾向年龄 ——
    _bad_arb = _remote_files(_arb=_arb_doc(3))
    _rc, _rep, _gen, _td = _case(_remote=_bad_arb, _gh=_gen_health(), _ph=_pub_health())
    check(
        "★ 检查器：远端静态文件校验不过 → static_valid_*=fail（消费端会拒收）",
        _st(_rep, "static_valid_arb_ratings.json") == "fail" and _rc == 2,
        next(_c["detail"] for _c in _rep["checks"] if _c["id"] == "static_valid_arb_ratings.json"),
    )
    _shutil.rmtree(_td, ignore_errors=True)

    _rc, _rep, _gen, _td = _case(_remote=_remote_files(), _gh=_gen_health(), _ph=_pub_health())
    check(
        "★ 检查器：倾向快照检查已退役（无 wiki_disp_age / static_valid_wiki_disp.json）",
        not any(
            _c["id"] in ("wiki_disp_age", "static_valid_wiki_disp.json") for _c in _rep["checks"]
        ),
        str([_c["id"] for _c in _rep["checks"]]),
    )
    _shutil.rmtree(_td, ignore_errors=True)

    # 已生成未发布：本地生成内容 ≠ 远端，且 last_success 已 3h
    _td2 = Path(_tmp.mkdtemp(prefix="_ch_pub2_"))
    _gen2 = _td2 / "gen"
    _gen2.mkdir()
    _new_arb = _arb_doc(70)  # 本地新内容（远端还是 60 节点的旧版）
    _gen2_text = _txt(_new_arb)
    (_gen2 / "arb_ratings.json").write_text(_gen2_text, encoding="utf-8")
    (_gen2 / "_health.json").write_text(
        _json.dumps(_gen_health(_arb_sha=_sha(_gen2_text), _arb_last=_NOW - _tdlt(hours=3))),
        encoding="utf-8",
    )
    (_gen2 / "_publish_health.json").write_text(_json.dumps(_pub_health()), encoding="utf-8")
    _work2 = _mk_repo(_td2, _remote_files())
    _rc, _rep = _CH.run(
        gen_dir=_gen2,
        work_dir=_work2,
        fetch=False,
        alert=False,
        now=_NOW,
        rotations=_ROT,
        quiet=True,
    )
    check(
        "★ 检查器：已生成未发布 > 2h → static_published_*=warn / exit 1",
        _st(_rep, "static_published_arb_ratings.json") == "warn" and _rc == 1,
        next(
            _c["detail"] for _c in _rep["checks"] if _c["id"] == "static_published_arb_ratings.json"
        ),
    )
    # 同一份，只是刚生成（< 2h）→ ok
    (_gen2 / "_health.json").write_text(
        _json.dumps(_gen_health(_arb_sha=_sha(_gen2_text), _arb_last=_NOW - _tdlt(minutes=30))),
        encoding="utf-8",
    )
    _rc, _rep = _CH.run(
        gen_dir=_gen2,
        work_dir=_work2,
        fetch=False,
        alert=False,
        now=_NOW,
        rotations=_ROT,
        quiet=True,
    )
    check(
        "检查器：刚生成 < 2h → static_published=ok（等下一轮发布即可）",
        _st(_rep, "static_published_arb_ratings.json") == "ok",
        next(
            _c["detail"] for _c in _rep["checks"] if _c["id"] == "static_published_arb_ratings.json"
        ),
    )
    # ★ 2026-10-23 线上误报复现：远端 = 同内容 + 发布端加的 `schema: 1`（生成器产物没有）
    #   ⇒ 必须判「已发布」，不能 3h 后报「已生成未发布」
    (_gen2 / "_health.json").write_text(
        _json.dumps(_gen_health(_arb_sha=_sha(_gen2_text), _arb_last=_NOW - _tdlt(hours=3))),
        encoding="utf-8",
    )
    _work3 = _mk_repo(
        Path(_tmp.mkdtemp(prefix="_ch_pub3_")), _remote_files(_arb={**_new_arb, "schema": 1})
    )
    _rc, _rep = _CH.run(
        gen_dir=_gen2,
        work_dir=_work3,
        fetch=False,
        alert=False,
        now=_NOW,
        rotations=_ROT,
        quiet=True,
    )
    check(
        "★ 检查器：远端只多发布端的 schema 键 ⇒ static_published=ok（不再永久误报）",
        _st(_rep, "static_published_arb_ratings.json") == "ok",
        next(
            _c["detail"] for _c in _rep["checks"] if _c["id"] == "static_published_arb_ratings.json"
        ),
    )
    _shutil.rmtree(_td2, ignore_errors=True)

    # —— 远端读不到（fetch 失败）→ 远端相关检查记 warn，不能崩 ——
    _td3 = Path(_tmp.mkdtemp(prefix="_ch_nofetch_"))
    _gen3 = _td3 / "gen"
    _gen3.mkdir()
    (_gen3 / "_health.json").write_text(_json.dumps(_gen_health()), encoding="utf-8")
    (_gen3 / "_publish_health.json").write_text(_json.dumps(_pub_health()), encoding="utf-8")
    _work3 = _td3 / "no_origin"
    _work3.mkdir()
    _git("init", "-q", "-b", "bot-data", _cwd=str(_work3))  # 有 .git 但没有 origin
    _rc, _rep = _CH.run(
        gen_dir=_gen3,
        work_dir=_work3,
        fetch=True,
        alert=False,
        now=_NOW,
        rotations=_ROT,
        quiet=True,
    )
    check(
        "★ 检查器：fetch 失败 → 远端相关检查全 warn（「无法读取远端」），本地检查照常 ok",
        _st(_rep, "valence_current") == "warn"
        and _st(_rep, "static_valid_arb_ratings.json") == "warn"
        and _st(_rep, "publisher_alive") == "ok"
        and _rc == 1,
        _json.dumps(_rep["checks"], ensure_ascii=False)[:400],
    )
    _rc, _rep = _CH.run(
        gen_dir=_gen3,
        work_dir=_td3 / "nope",
        fetch=True,
        alert=False,
        now=_NOW,
        rotations=_ROT,
        quiet=True,
    )
    check(
        "检查器：工作副本不存在也只是一片 warn（不抛异常）",
        _st(_rep, "valence_current") == "warn" and _rc == 1,
        next(_c["detail"] for _c in _rep["checks"] if _c["id"] == "valence_current"),
    )
    _shutil.rmtree(_td3, ignore_errors=True)

    # —— 镜像滞后（--mirrors；纯函数，不联网）——
    _idx = {k: v.encode("utf-8") for k, v in _remote_files().items()}  # 与 read_remote 同型（字节）
    check(
        "★ 检查器：镜像比 git 旧 24h → mirror_lag=warn",
        _CH.check_mirror_lag(_idx, "", {"published_at": "2026-10-15T00:00:00+00:00"})["status"]
        == "warn",
    )
    check(
        "检查器：镜像跟得上（0h）→ mirror_lag=ok",
        _CH.check_mirror_lag(_idx, "", {"published_at": "2026-10-16T00:00:00+00:00"})["status"]
        == "ok",
    )
    check(
        "检查器：镜像读不到 → mirror_lag=warn（不误报 fail）",
        _CH.check_mirror_lag(_idx, "", {})["status"] == "warn",
    )
    _chk_ids = [
        c["id"]
        for c in _CH.evaluate(_NOW, {}, {}, {}, _ROT, gen_dir=Path("/tmp"), mirror={}, mirrors=True)
    ]
    _chk_ids_nomirror = [
        c["id"] for c in _CH.evaluate(_NOW, {}, {}, {}, _ROT, gen_dir=Path("/tmp"))
    ]
    # ★ 2026-10-18：原断言写的是 `_chk_ids[-1] == "mirror_lag"`（假定它在末尾）；
    #   同日加了 `host_mem`（追加在末尾）后该位置假设失效 ⇒ 改为**与位置无关**的等价断言：
    #   开 --mirrors 时恰好出现一次、不开时一次都不出现。
    check(
        "检查器：--mirrors 才追加 mirror_lag 检查项",
        _chk_ids.count("mirror_lag") == 1 and "mirror_lag" not in _chk_ids_nomirror,
        str(_chk_ids),
    )
    check(
        "检查器：host_mem 每轮都在（追加在末尾，位置固定）",
        _chk_ids[-1] == "host_mem" and _chk_ids_nomirror[-1] == "host_mem",
        str(_chk_ids),
    )

    # —— 生成器健康记录：updated / unchanged / failed / 坏记录 / 读-改-写 ——
    _tdg = Path(_tmp.mkdtemp(prefix="_ch_gen_"))
    _gd = _tdg / "gen"
    _gd.mkdir()
    _part = _gd / "arb.part"
    _part.write_text(_txt(_arb_doc()), encoding="utf-8")
    _grc, _gres, _gwhy = _GEN._install("arb_ratings.json", _part, _gd)
    _GEN._record(_gd, "arb_ratings.json", _gres, _gwhy)
    _hg = _json.loads((_gd / "_health.json").read_text(encoding="utf-8"))
    _rec = _hg["arb_ratings.json"]
    check(
        "★ 生成健康：updated → result / last_success / sha256 / count / stamp 齐",
        _grc == 0
        and _gres == "updated"
        and _rec["result"] == "updated"
        and _rec["last_success"]
        and _rec["count"] == 60
        and _rec["stamp"] == "2026-10-17"
        and _rec["sha256"] == _hl.sha256((_gd / "arb_ratings.json").read_bytes()).hexdigest(),
        str(_rec),
    )
    check("生成健康：.part 用完即删（不在暂存目录留残渣）", not _part.exists())
    check("生成健康：健康记录原子写（不留 .tmp）", not (_gd / "_health.json.tmp").exists())

    _hg["arb_ratings.json"]["last_success"] = "2026-01-01T00:00:00+00:00"  # 手动压旧
    (_gd / "_health.json").write_text(_json.dumps(_hg), encoding="utf-8")
    _part2 = _gd / "arb.part"
    _part2.write_text(_txt({**_arb_doc(), "_updated": "2026-10-18"}), encoding="utf-8")
    _grc, _gres, _gwhy = _GEN._install("arb_ratings.json", _part2, _gd)
    _GEN._record(_gd, "arb_ratings.json", _gres, _gwhy)
    _hg2 = _json.loads((_gd / "_health.json").read_text(encoding="utf-8"))
    check(
        "★ 生成健康：内容没变（unchanged）→ last_success 前进，不被旧值卡住",
        _gres == "unchanged"
        and _hg2["arb_ratings.json"]["last_success"] != "2026-01-01T00:00:00+00:00"
        and _hg2["arb_ratings.json"]["last_success"] > _rec["last_success"],
        str(_hg2["arb_ratings.json"]),
    )
    check(
        "生成健康：unchanged 不动数据文件（幂等，时间戳保持）",
        _json.loads((_gd / "arb_ratings.json").read_text(encoding="utf-8"))["_updated"]
        == "2026-10-17",
    )
    _keep_ok = _hg2["arb_ratings.json"]["last_success"]
    _GEN._record(_gd, "arb_ratings.json", "failed", "生成失败（退出码 1）")
    _hg3 = _json.loads((_gd / "_health.json").read_text(encoding="utf-8"))
    check(
        "★ 生成健康：failed → 保留上一次 last_success（区分「今天挂了」与「一周没成功」）",
        _hg3["arb_ratings.json"]["last_success"] == _keep_ok
        and _hg3["arb_ratings.json"]["result"] == "failed"
        and "退出码 1" in _hg3["arb_ratings.json"]["reason"],
        str(_hg3["arb_ratings.json"]),
    )
    _part4 = _gd / "arb.part"
    _part4.write_text(_json.dumps({"_updated": "2026-10-18", "nodes": {}}), encoding="utf-8")
    _grc, _gres, _gwhy = _GEN._install("arb_ratings.json", _part4, _gd)
    check(
        "生成健康：校验不过 → _install 记 failed（不写坏文件）",
        (_grc, _gres, _gwhy) == (1, "failed", "节点太少"),
        f"{_grc} {_gres} {_gwhy}",
    )
    _part5 = _gd / "baro.part"
    _part5.write_text(_txt(_baro_doc()), encoding="utf-8")
    _b_rc, _b_res, _b_why = _GEN._install("baro_history.json", _part5, _gd)
    _GEN._record(_gd, "baro_history.json", _b_res, _b_why)
    _hg4 = _json.loads((_gd / "_health.json").read_text(encoding="utf-8"))
    check(
        "★ 生成健康：写一类不动另一类（读-改-写，两类条目都在）",
        set(_hg4) == {"arb_ratings.json", "baro_history.json"}
        and _hg4["arb_ratings.json"]["result"] == "failed"
        and _hg4["baro_history.json"]["result"] == "updated"
        and _hg4["baro_history.json"]["count"] == 12,
        str({k: v["result"] for k, v in _hg4.items()}),
    )
    (_gd / "_health.json").write_text("{ 这不是 JSON", encoding="utf-8")
    _part6 = _gd / "arb.part"
    _part6.write_text(_txt(_arb_doc()), encoding="utf-8")
    _grc, _gres, _gwhy = _GEN._install("arb_ratings.json", _part6, _gd)
    _GEN._record(_gd, "arb_ratings.json", _gres, _gwhy)
    _hg5 = _json.loads((_gd / "_health.json").read_text(encoding="utf-8"))
    check(
        "★ 生成健康：_health.json 损坏 → 生成器照跑（从 {} 重建，不抛）",
        _grc == 0
        and "arb_ratings.json" in _hg5
        and _hg5["arb_ratings.json"]["result"] == "unchanged",
        str(_hg5),
    )
    _shutil.rmtree(_tdg, ignore_errors=True)

    # —— 发布健康记录（直接调写函数；不跑 main 也能钉住契约）——
    _tdp = Path(_tmp.mkdtemp(prefix="_ch_pubh_"))
    _pd = _tdp / "gen"
    _pstat = {
        "valence.json": {"status": "ok", "reason": "", "stamp": "2026-10-16T00:00:00+00:00"},
        "acrithis_week.json": {"status": "invalid", "reason": "言录使快照已过期（…）", "stamp": ""},
        "arb_ratings.json": {"status": "missing", "reason": "未找到源文件", "stamp": ""},
    }
    _PUBW._write_publish_health(
        gen_dir=_pd, mode="git", dry_run=False, result="pushed", head="abc1234", files=_pstat
    )
    _ph = _json.loads((_pd / "_publish_health.json").read_text(encoding="utf-8"))
    check(
        "★ 发布健康：成功落盘（last_run/mode/dry_run/result/head/files 齐）",
        _ph["mode"] == "git"
        and _ph["dry_run"] is False
        and _ph["result"] == "pushed"
        and _ph["head"] == "abc1234"
        and _ph["last_run"]
        and _ph["files"]["arb_ratings.json"]["status"] == "missing"
        and _ph["files"]["acrithis_week.json"]["reason"],
        str(_ph),
    )
    _PUBW._write_publish_health(
        gen_dir=_pd, mode="api", dry_run=True, result="nothing", head="", files={}
    )
    _ph2 = _json.loads((_pd / "_publish_health.json").read_text(encoding="utf-8"))
    check(
        "★ 发布健康：「没东西可发」也落盘（result=nothing / files 空）",
        _ph2["result"] == "nothing" and _ph2["dry_run"] is True and _ph2["files"] == {},
        str(_ph2),
    )
    check("发布健康：写完不留 .tmp（原子写）", not (_pd / "_publish_health.json.tmp").exists())
    (_pd / "blocker").write_text("x", encoding="utf-8")
    try:  # 目标路径被文件占住 → mkdir 必失败
        _PUBW._write_publish_health(
            gen_dir=_pd / "blocker", mode="api", dry_run=True, result="failed", head="", files={}
        )
        _no_raise = True
    except Exception as _exc:  # noqa: BLE001
        _no_raise = False
    check("★ 发布健康：写不进去也不抛（绝不改退出码 / 掀翻发布）", _no_raise)

    # 「没有任何可发布的数据」这条退出路径：_publish 记 result=nothing（退出码仍是 1）
    _Ns = type("_Ns", (), {})
    _args_none = _Ns()
    _args_none.local, _args_none.direct, _args_none.via_git, _args_none.yes = (
        False,
        False,
        False,
        False,
    )
    _args_none.work_dir, _args_none.git_remote, _args_none.git_key = ".", "", ""
    _orig_ls = _PUBW.load_snapshot
    _PUBW.load_snapshot = lambda *a, **k: ({}, "（测试：无源）")
    try:
        _PUBW._LAST_PUSH.clear()
        _rc = _PUBW._publish(_args_none, _NOW, {})
    finally:
        _PUBW.load_snapshot = _orig_ls
    check(
        "★ 发布器：没有任何可发布的数据 → _publish 记 result=nothing（退出码 1 不变）",
        _rc == 1 and _PUBW._LAST_PUSH.get("result") == "nothing",
        str(_PUBW._LAST_PUSH),
    )
    _shutil.rmtree(_tdp, ignore_errors=True)

    # —— 发布器接线：push_via_git 真推（本地 file:// origin）→ _LAST_PUSH 记结果 ——
    _tdw = Path(_tmp.mkdtemp(prefix="_ch_pubg_"))
    _workw = _mk_repo(_tdw, _remote_files())
    _bare_url = "file://" + str(_tdw / "origin.git")
    _newval = _txt(
        {
            "schema": 1,
            "published_at": "2026-10-17T00:00:00+00:00",
            "tenet": {"items": []},
            "coda": {"batch": "A", "items": []},
        }
    )
    _PUBW._LAST_PUSH.clear()
    _rc = _PUBW.push_via_git(
        {"valence.json": _newval}, True, work=_workw, remote=_bare_url, key=None
    )
    check(
        "★ 发布器接线：git 通道真推 → result=pushed + 短 sha（健康记录要的两个字段）",
        _rc == 0
        and _PUBW._LAST_PUSH.get("result") == "pushed"
        and len(_PUBW._LAST_PUSH.get("head", "")) >= 7,
        str(_PUBW._LAST_PUSH),
    )
    _PUBW._LAST_PUSH.clear()
    _rc = _PUBW.push_via_git(
        {"valence.json": _newval}, True, work=_workw, remote=_bare_url, key=None
    )
    check(
        "★ 发布器接线：内容没变 → result=unchanged（不产生空提交）",
        _rc == 0 and _PUBW._LAST_PUSH.get("result") == "unchanged",
        str(_PUBW._LAST_PUSH),
    )
    _PUBW._LAST_PUSH.clear()
    _rc = _PUBW.push_via_git(
        {"valence.json": _txt({"schema": 1, "published_at": "2026-10-18T00:00:00+00:00"})},
        False,
        work=_workw,
        remote=_bare_url,
        key=None,
    )
    check(
        "★ 发布器接线：dry-run → result=dry_run（默认不写远端）",
        _rc == 0 and _PUBW._LAST_PUSH.get("result") == "dry_run",
        str(_PUBW._LAST_PUSH),
    )
    _shutil.rmtree(_tdw, ignore_errors=True)

    # —— 告警：只在状态变化时响（首次异常 / 无变化静默 / 恢复一条）——
    _tdal = Path(_tmp.mkdtemp(prefix="_ch_alert_"))
    _genal = _tdal / "gen"
    _genal.mkdir()
    _rfal = _remote_files()
    _workal = _mk_repo(_tdal, _rfal)
    (_genal / "_health.json").write_text(
        _json.dumps(
            _gen_health(
                _arb_sha=_sha(_rfal["arb_ratings.json"]), _baro_sha=_sha(_rfal["baro_history.json"])
            )
        ),
        encoding="utf-8",
    )
    (_genal / "_publish_health.json").write_text(
        _json.dumps(_pub_health(_age_min=180)), encoding="utf-8"
    )
    _alerts = []
    _orig_send = _CH.send_alert
    _deliver = {"ok": True}
    _CH.send_alert = lambda _t, _b: (
        _alerts.append((_t, _b)),
        "已推送，通道：stub" if _deliver["ok"] else "未推送：全部远程通道失败",
    )[1]
    try:
        _rc1, _rep1 = _CH.run(
            gen_dir=_genal,
            work_dir=_workal,
            fetch=False,
            alert=True,
            now=_NOW,
            rotations=_ROT,
            quiet=True,
        )
        check(
            "★ 告警：首次异常 → 一条告警（标题带项数，正文列变化项）",
            _rc1 == 2
            and len(_alerts) == 1
            and _alerts[0][0] == "社区快照健康：1 项异常"
            and "publisher_alive" in _alerts[0][1],
            str(_alerts),
        )
        check(
            "告警：状态落盘（状态文件逐项记状态）",
            _json.loads((_genal / "_community_health_state.json").read_text(encoding="utf-8"))[
                "checks"
            ]["publisher_alive"]
            == "fail",
        )
        _rc2, _rep2 = _CH.run(
            gen_dir=_genal,
            work_dir=_workal,
            fetch=False,
            alert=True,
            now=_NOW,
            rotations=_ROT,
            quiet=True,
        )
        check(
            "★ 告警：状态没变 → 一条都不推（不刷屏）", _rc2 == 2 and len(_alerts) == 1, str(_alerts)
        )
        (_genal / "_publish_health.json").write_text(
            _json.dumps(_pub_health(_age_min=5)), encoding="utf-8"
        )
        _rc3, _rep3 = _CH.run(
            gen_dir=_genal,
            work_dir=_workal,
            fetch=False,
            alert=True,
            now=_NOW,
            rotations=_ROT,
            quiet=True,
        )
        check(
            "★ 告警：恢复 → 一条恢复告警，exit 回 0",
            _rc3 == 0
            and len(_alerts) == 2
            and _alerts[1][0] == "社区快照健康：已恢复 publisher_alive",
            str(_alerts),
        )
        _rc4, _rep4 = _CH.run(
            gen_dir=_genal,
            work_dir=_workal,
            fetch=False,
            alert=True,
            now=_NOW,
            rotations=_ROT,
            quiet=True,
        )
        check("告警：恢复后重复跑不再推", _rc4 == 0 and len(_alerts) == 2, str(_alerts))
        # ★ 2026-10-17（Claude 审核补）：告警没送达 → 状态不前进，下一轮照样再推（不吞告警）
        (_genal / "_publish_health.json").write_text(
            _json.dumps(_pub_health(_age_min=180)), encoding="utf-8"
        )
        _deliver["ok"] = False
        _CH.run(
            gen_dir=_genal,
            work_dir=_workal,
            fetch=False,
            alert=True,
            now=_NOW,
            rotations=_ROT,
            quiet=True,
        )
        _st = _json.loads((_genal / "_community_health_state.json").read_text(encoding="utf-8"))
        check(
            "★ 告警没送达：状态停在 ok（不当成已通知）",
            _st["checks"]["publisher_alive"] == "ok",
            str(_st),
        )
        _deliver["ok"] = True
        _n_before = len(_alerts)
        _CH.run(
            gen_dir=_genal,
            work_dir=_workal,
            fetch=False,
            alert=True,
            now=_NOW,
            rotations=_ROT,
            quiet=True,
        )
        check(
            "★ 下一轮送达成功：同一异常再推一次",
            len(_alerts) == _n_before + 1 and "publisher_alive" in _alerts[-1][1],
            str(_alerts[-2:]),
        )
    finally:
        _CH.send_alert = _orig_send
        _shutil.rmtree(_tdal, ignore_errors=True)
else:
    print("[SKIP] 社区快照健康用例：本树无私有运维脚本（.zcode/.../community_health.py）")


# ---------------------------------------------------------------------------
# ★ 2026-10-18（ZCode 内存加固 §G）：主机内存告警（每 30 分钟健康检查的 host_mem 项）
#   原为独立文件 tests/test_mem_alert.py；它只测不进开源包的运维脚本、在包内恒为 SKIP，
#   Claude 审核时并入本文件（同为「本树无私有运维脚本则跳过」），开源 stage 基线不变。
#   钉住：host_metrics 读取与阈值（含读不到）/ check_host_mem 三态 / unknown 不误报 /
#   community_health 与 server_patrol 阈值同源。
# ---------------------------------------------------------------------------
def _mem_alert_checks():
    import importlib.util
    import json
    import shutil
    import tempfile

    AUTO = ROOT / ".zcode/skills/astrbot-server-triage/scripts/automations"

    def _load(path: Path, name: str):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    if not (AUTO / "community_health.py").is_file():
        print("[SKIP] 主机内存告警用例：本树无私有运维脚本（.zcode/.../automations/）")
    else:
        HM = _load(AUTO / "host_metrics.py", "_host_metrics_t")
        CH = _load(AUTO / "community_health.py", "_community_health_mem_t")
        SP = _load(AUTO / "server_patrol.py", "_server_patrol_mem_t")

        _TD = Path(tempfile.mkdtemp())

        def _meminfo_file(text: str, name: str = "meminfo") -> Path:
            p = _TD / name
            p.write_text(text, encoding="utf-8")
            return p

        _GOOD = (
            "MemTotal:        1752592 kB\n"
            "MemFree:           91164 kB\n"
            "MemAvailable:     700000 kB\n"
            "SwapTotal:       6228988 kB\n"
            "SwapFree:        5110000 kB\n"
        )

        try:
            # ---------------------------------------------------------------- 1. 读取
            mi = HM.read_meminfo(_meminfo_file(_GOOD))
            check(
                "read_meminfo 正常文件 ⇒ 解析出 kB 表",
                isinstance(mi, dict) and mi["MemAvailable"] == 700000,
                str(mi),
            )
            check(
                "read_meminfo 读得到 swap 两项（调用方要用）",
                mi.get("SwapTotal") == 6228988 and mi.get("SwapFree") == 5110000,
                str(mi),
            )
            check("read_meminfo 文件不存在 ⇒ None（不抛）", HM.read_meminfo(_TD / "nope") is None)
            check(
                "read_meminfo 文件格式坏 ⇒ None（不抛）",
                HM.read_meminfo(_meminfo_file("垃圾内容\n")) is None,
            )
            check(
                "read_meminfo 键无值 ⇒ None",
                HM.read_meminfo(_meminfo_file("MemTotal:\nMemAvailable:\n")) is None,
            )

            check(
                "mem_available_mb 正常 ⇒ MB", HM.mem_available_mb({"MemAvailable": 204800}) == 200.0
            )
            check("mem_available_mb 缺键 ⇒ None", HM.mem_available_mb({"MemTotal": 1}) is None)
            check("mem_available_mb 空表 ⇒ None", HM.mem_available_mb(None) is None)

            # ---------------------------------------------------------------- 2. 阈值
            check(
                "阈值常量 = 200（与 server_patrol 第 ⑦ 项口径）",
                HM.MEM_AVAIL_WARN_MB == 200,
                str(HM.MEM_AVAIL_WARN_MB),
            )
            check("mem_is_low：199.9 ⇒ True", HM.mem_is_low(199.9) is True)
            check("mem_is_low：正好 200 ⇒ False（等于阈值不算低）", HM.mem_is_low(200.0) is False)
            check("mem_is_low：201 ⇒ False", HM.mem_is_low(201.0) is False)
            check("mem_is_low：读不到(None) ⇒ False（不误报）", HM.mem_is_low(None) is False)

            # ---------------------------------------------------------------- 3. 三态
            _low = CH.check_host_mem({"MemAvailable": 150 * 1024})
            check("check_host_mem 低于阈值 ⇒ warn", _low["status"] == "warn", str(_low))
            check(
                "  warn 文案带具体数值与阈值",
                "150MB" in _low["detail"] and "200" in _low["detail"],
                _low["detail"],
            )

            _ok = CH.check_host_mem({"MemAvailable": 700 * 1024})
            check("check_host_mem 高于阈值 ⇒ ok", _ok["status"] == "ok", str(_ok))

            _eq = CH.check_host_mem({"MemAvailable": 200 * 1024})
            check("check_host_mem 正好等于阈值 ⇒ ok（不误报）", _eq["status"] == "ok", str(_eq))

            _chk_read = CH.read_meminfo
            CH.read_meminfo = lambda *a, **k: None
            try:
                _unk = CH.check_host_mem()
            finally:
                CH.read_meminfo = _chk_read
            check(
                "check_host_mem 读不到 meminfo ⇒ unknown（不误报）",
                _unk["status"] == "unknown",
                str(_unk),
            )

            # ------------------------------------------------- 4. unknown 不误报（聚合口径）
            _mix = [CH._check("a", "ok", ""), CH._check("host_mem", "unknown", "")]
            _code, _line = CH.summary_line(_mix)
            check("summary：只有 unknown ⇒ 退出码 0", _code == 0, f"{_code} {_line}")
            check("summary：unknown 不计入 todo", "todo=0" in _line, _line)
            check("summary：unknown 不列进摘要", "host_mem" not in _line, _line)
            check(
                "overall_of：unknown 视作 ok（严重度 0）",
                CH.overall_of(_mix) == "ok",
                CH.overall_of(_mix),
            )
            check("SEV：unknown = 0", CH.SEV.get("unknown") == 0, str(CH.SEV))

            _warn_mix = [CH._check("host_mem", "warn", "低"), CH._check("host_mem2", "unknown", "")]
            _code2, _line2 = CH.summary_line(_warn_mix)
            check(
                "summary：warn 仍照常计入（unknown 不掩盖真问题）",
                _code2 == 1 and "host_mem(warn)" in _line2,
                _line2,
            )

            # ------------------------------------------------- 5. 告警去重 / 恢复（复用现有机制）
            _pushed: list[tuple[str, str]] = []
            _orig_send = CH.send_alert
            CH.send_alert = lambda title, body: (
                _pushed.append((title, body)),
                "已推送，通道：test",
            )[1]
            try:

                def _state(gen: Path, statuses: dict):
                    CH._write_json_atomic(
                        gen / CH.STATE_NAME,
                        {"checked_at": "2026-10-18T00:00:00+00:00", "checks": statuses},
                    )

                def _alert(gen: Path, checks):
                    _pushed.clear()
                    CH._alert_on_change(gen, checks, quiet=True)
                    return list(_pushed)

                _g1 = _TD / "g1"
                _g1.mkdir()
                _state(_g1, {"host_mem": "ok"})
                _r = _alert(_g1, [CH._check("host_mem", "unknown", "读不到")])
                check("★ unknown 不触发降级告警（ok → unknown 静默）", _r == [], str(_r))

                _g2 = _TD / "g2"
                _g2.mkdir()
                _state(_g2, {"host_mem": "unknown"})
                _r = _alert(_g2, [CH._check("host_mem", "ok", "正常")])
                check("★ unknown → ok 不误发「已恢复」", _r == [], str(_r))

                _g3 = _TD / "g3"
                _g3.mkdir()
                _state(_g3, {"host_mem": "ok"})
                _r = _alert(_g3, [CH._check("host_mem", "warn", "可用内存仅 150MB")])
                check(
                    "★ ok → warn 照常告警（真问题不放过）",
                    len(_r) == 1 and "host_mem" in _r[0][1],
                    str(_r),
                )

                _g4 = _TD / "g4"
                _g4.mkdir()
                _state(_g4, {"host_mem": "warn"})
                _r = _alert(_g4, [CH._check("host_mem", "ok", "可用内存 700MB")])
                check("★ warn → ok 照常发恢复通知", len(_r) == 1 and "已恢复" in _r[0][0], str(_r))

                _g5 = _TD / "g5"
                _g5.mkdir()
                _state(_g5, {"host_mem": "warn"})
                _r = _alert(_g5, [CH._check("host_mem", "warn", "还是低")])
                check("★ warn 持续 ⇒ 不重复刷屏（去重）", _r == [], str(_r))
            finally:
                CH.send_alert = _orig_send

            # ------------------------------------------------- 6. 阈值同源 + evaluate 接线
            check(
                "阈值同源：community_health 与 host_metrics 是同一个常量",
                CH.MEM_AVAIL_WARN_MB == HM.MEM_AVAIL_WARN_MB,
                f"{CH.MEM_AVAIL_WARN_MB} vs {HM.MEM_AVAIL_WARN_MB}",
            )
            check(
                "阈值同源：server_patrol 也取自 host_metrics",
                SP.MEM_AVAIL_WARN_MB == HM.MEM_AVAIL_WARN_MB,
                f"{SP.MEM_AVAIL_WARN_MB} vs {HM.MEM_AVAIL_WARN_MB}",
            )
            _sp_src = (AUTO / "server_patrol.py").read_text(encoding="utf-8")
            _sp_mem = "\n".join(
                ln
                for ln in _sp_src.splitlines()
                if "avail_mb" in ln or "composite" in ln or "read_meminfo" in ln
            )
            check(
                "server_patrol 内存段不再有裸 200（已改为共享常量）",
                "200" not in _sp_mem and "MEM_AVAIL_WARN_MB" in _sp_mem,
                _sp_mem,
            )
            check(
                "server_patrol 不再内联解析 /proc/meminfo（改用 host_metrics.read_meminfo）",
                'Path("/proc/meminfo").read_text' not in _sp_src
                and "read_meminfo()" in _sp_src
                and "MEMINFO_PATH.exists()" in _sp_src,
                "",
            )

            _gen = _TD / "gen"
            _gen.mkdir()
            _checks = CH.evaluate(
                CH._now_utc(), {}, {}, {}, {}, gen_dir=_gen, meminfo={"MemAvailable": 700 * 1024}
            )
            _ids = [c["id"] for c in _checks]
            check("evaluate 末尾接上 host_mem", _ids[-1] == "host_mem", str(_ids))
            check(
                "evaluate 注入高内存 ⇒ host_mem ok",
                [c for c in _checks if c["id"] == "host_mem"][0]["status"] == "ok",
                str([c for c in _checks if c["id"] == "host_mem"]),
            )
            _checks2 = CH.evaluate(
                CH._now_utc(), {}, {}, {}, {}, gen_dir=_gen, meminfo={"MemAvailable": 100 * 1024}
            )
            check(
                "evaluate 注入低内存 ⇒ host_mem warn",
                [c for c in _checks2 if c["id"] == "host_mem"][0]["status"] == "warn",
                str([c for c in _checks2 if c["id"] == "host_mem"]),
            )

            # 报告文件里也要能带上 unknown（JSON 可序列化、状态落盘不炸）
            _report = {"checks": [CH._check("host_mem", "unknown", "读不到")]}
            check("unknown 可 JSON 序列化（落报告/状态不炸）", json.dumps(_report) != "", "")
        finally:
            shutil.rmtree(_TD, ignore_errors=True)


_mem_alert_checks()

print()

# ★ 2026-10-25（Astra 评估 S4）：内网地址判定（纯函数，IP 直写不联网）
_why = {
    u: _aio.run(_AC.private_url_reason(u))
    for u in (
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://169.254.169.254/latest",
        "http://[::1]/x",
        "http://user:pw@8.8.8.8/x",
        "file:///etc/passwd",
        "https://8.8.8.8/x.ttf",
    )
}
check(
    "★ 内网 / 本机 / 元数据地址 / 账号密码 / 非 http 一律拒，公网 IP 放行",
    all(_why[u] for u in list(_why)[:6]) and _why["https://8.8.8.8/x.ttf"] == "",
    str(_why),
)

if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 社区快照守卫全部通过")
