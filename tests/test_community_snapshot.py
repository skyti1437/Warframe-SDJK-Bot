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
    COMMUNITY_ACRITHIS_URL, COMMUNITY_VALENCE_URL,
    community_acrithis_ok, community_valence_newer, parse_community_valence,
)

# 发布侧：scripts/publish_community_snapshot.py（离线载入，不执行 main）
_spec = importlib.util.spec_from_file_location(
    "publish_community_snapshot",
    ROOT / ".zcode" / "skills" / "astrbot-server-triage" / "scripts"
    / "automations" / "publish_community_snapshot.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-09-25T02:07:46+00:00"


def _rot(*, filled_idx: int = 1, anchor: int = 0) -> dict:
    """构造一份与线上同形的 rotations.json（coda 只填当前批）。"""
    batches = []
    for i, names in enumerate((("Coda Hema", "Coda Sporothrix"),
                               ("Coda Bassocyst", "Coda Catabolyst"))):
        batches.append([{"en": n, "cn": f"终幕·{n[5:]}",
                         **({"element": "Heat", "bonus": 27.4} if i == filled_idx else {})}
                        for n in names])
    return {
        "tenet": {"valence_snapshot": STAMP, "items": [
            {"en": "Tenet Agendus", "cn": "信条·集议",
             "element": "Electricity", "bonus": 26.2}]},
        "coda": {"epoch": "2026-09-13T00:00:00+00:00", "period_hours": 96,
                 "anchor_idx": anchor, "batch_label": ["A", "B"],
                 "valence_snapshot": STAMP, "batches": batches},
    }


# ---------------------------------------------------------------------------
# 1. 消费侧：valence 快照的解析与「不比本地旧」判据
# ---------------------------------------------------------------------------
GOOD_VALENCE = {
    "schema": 1, "published_at": "2026-09-25T12:00:00+00:00",
    "tenet": {"snapshot": STAMP, "items": [
        {"en": "Tenet Agendus", "cn": "信条·集议",
         "element": "Electricity", "bonus": 26.2}]},
    "coda": {"snapshot": STAMP, "batch": "B", "items": [
        {"en": "Coda Bassocyst", "cn": "终幕·低音爆囊",
         "element": "Magnetic", "bonus": 31.6}]},
}
parsed = parse_community_valence(GOOD_VALENCE)
check("★ 消费侧：合格快照解析出 tenet/coda 映射与批次",
      parsed["tenet"]["Tenet Agendus"] == ("Electricity", 26.2)
      and parsed["coda_batch"] == "B", str(parsed))

for bad, why in (
    ({}, "空对象"),
    ({"tenet": {"items": []}, "coda": {"items": [{"en": "x"}], "batch": "B"}}, "tenet 无物品"),
    ({"tenet": {"items": [{"en": "x", "element": "Heat"}]},
      "coda": {"items": [{"en": "y", "element": "Cold", "bonus": 1}], "batch": "B"}},
     "tenet 缺 bonus"),
    ({"tenet": {"items": [{"en": "x", "element": "Heat", "bonus": 1}]},
      "coda": {"items": [{"en": "y", "element": "Cold", "bonus": 1}]}}, "coda 缺 batch"),
):
    try:
        parse_community_valence(bad)
        check(f"★ 消费侧：残缺快照必须被拒（{why}）", False, "没抛异常")
    except ValueError:
        check(f"★ 消费侧：残缺快照必须被拒（{why}）", True)

check("★ 消费侧：比本地新 → 采用",
      community_valence_newer("2026-09-25T12:00:00+00:00", STAMP) is True)
check("★ 消费侧：比本地旧 → 不采用",
      community_valence_newer("2026-09-20T00:00:00+00:00", STAMP) is False)
check("★ 消费侧：与本地同刻 → 不采用（不无谓覆盖）",
      community_valence_newer(STAMP, STAMP) is False)
check("★ 消费侧：时间戳缺失/畸形 → 不采用",
      community_valence_newer("", STAMP) is False
      and community_valence_newer("not-a-date", STAMP) is False)
check("消费侧：本地无时间戳 → 不构成拒绝理由",
      community_valence_newer("2026-09-25T12:00:00+00:00", "") is True)

# ---------------------------------------------------------------------------
# 2. 消费侧：言录使快照判据（未过期 + 有货 + observed 不早于本周一）
# ---------------------------------------------------------------------------
EXPIRY = "2026-09-28T00:00:00+00:00"
GOOD_ACR = {"expiry": EXPIRY, "observed": "September 21, 2026",
            "items": [{"name": "Orokin Catalyst", "price": 20}]}
ok, why = community_acrithis_ok(GOOD_ACR, NOW)
check("★ 消费侧：合格言录使快照被接受", ok is True, why)
ok, why = community_acrithis_ok({**GOOD_ACR, "expiry": "2026-09-21T00:00:00+00:00"}, NOW)
check("★ 消费侧：已过期 → 拒绝", ok is False and "过期" in why, why)
ok, why = community_acrithis_ok({**GOOD_ACR, "items": []}, NOW)
check("★ 消费侧：无货 → 拒绝", ok is False, why)
ok, why = community_acrithis_ok({**GOOD_ACR, "observed": "September 14, 2026"}, NOW)
check("★ 消费侧：observed 早于本周一 → 拒绝（防拿上周货单当本周）",
      ok is False and "本周一" in why, why)
ok, why = community_acrithis_ok({"expiry": "bogus", "items": [{"name": "x"}]}, NOW)
check("★ 消费侧：expiry 畸形 → 拒绝", ok is False, why)
ok, why = community_acrithis_ok({**GOOD_ACR, "observed": ""}, NOW)
check("消费侧：observed 缺失不算问题（expiry 才是硬判据）", ok is True, why)

check("消费侧 URL 指向 bot-data 分支",
      "/bot-data/" in COMMUNITY_VALENCE_URL and "/bot-data/" in COMMUNITY_ACRITHIS_URL,
      COMMUNITY_VALENCE_URL)

# ---------------------------------------------------------------------------
# 3. 发布侧：当前批推导必须与卡面同款（锚点+已过轮数，不是 anchor_idx）
# ---------------------------------------------------------------------------
coda = _rot(anchor=0)["coda"]
check("★ 发布侧：当前批 = (anchor + passed) % 批数（照抄卡面公式）",
      P.current_coda_index(coda, NOW) == 1,
      f"得到 {P.current_coda_index(coda, NOW)}，期望 1")
check("  （若误用 anchor_idx 会拿到没填元素的那批）",
      coda["anchor_idx"] == 0 and P.current_coda_index(coda, NOW) == 1)
coda2 = _rot(anchor=1)["coda"]
check("发布侧：相位基准变一档 → 当前批跟着变",
      P.current_coda_index(coda2, NOW) == 0)

# ---------------------------------------------------------------------------
# 4. 发布侧：校验不过就**不发**（残缺 / 相位不符 / 已过期）
# ---------------------------------------------------------------------------
payload = P.build_valence(_rot(filled_idx=1), STAMP, NOW)
check("★ 发布侧：相位与填充吻合 → 构建成功",
      payload["coda"]["batch"] == "B" and len(payload["coda"]["items"]) == 2, str(payload)[:120])
check("  带来源与许可署名", "CC BY-NC-SA" in payload["license_note"]
      and "wiki" in payload["source"])

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

acr = P.build_acrithis({"expiry": EXPIRY, "observed": "September 21, 2026",
                        "items": [{"name": "Orokin Catalyst", "qty": 1, "price": 20}]},
                       STAMP, NOW)
check("★ 发布侧：合格言录使 → 构建成功",
      acr["expiry"] == EXPIRY and len(acr["items"]) == 1, str(acr)[:120])
for bad, why in (({"expiry": "2026-09-21T00:00:00+00:00", "items": [{"name": "x"}]}, "已过期"),
                 ({"expiry": EXPIRY, "items": []}, "无货"),
                 ({"items": [{"name": "x"}]}, "缺 expiry")):
    try:
        P.build_acrithis(bad, STAMP, NOW)
        check(f"★ 发布侧：言录使{why} → 拒绝发布", False, "没抛异常")
    except ValueError:
        check(f"★ 发布侧：言录使{why} → 拒绝发布", True)

# ---------------------------------------------------------------------------
# 5. 发布侧：默认 dry-run（不加 --yes 一个字节都不写远端）
# ---------------------------------------------------------------------------
_src = (ROOT / ".zcode" / "skills" / "astrbot-server-triage" / "scripts"
        / "automations" / "publish_community_snapshot.py").read_text(encoding="utf-8")
check("★ 发布侧：默认 dry-run，--yes 才写远端",
      'ap.add_argument("--yes", action="store_true"' in _src
      and "DRY-RUN（默认）" in _src)
check("★ 发布侧：只写 bot-data 分支（不碰公开仓 main）",
      'BRANCH = "bot-data"' in _src and "refs/heads/main" not in _src)
check("发布侧：读服务器走只读 ssh（BatchMode）",
      '"BatchMode=yes"' in _src)
check("发布侧：孤儿分支（首次提交无 parents）",
      '"parents": [head] if head else []' in _src)
check("发布侧：零变化不产生空提交", "无需提交（不产生空提交）" in _src)

# 6. 发布侧：时间戳取「数据自身」的时刻 → 数据没变就不重发（幂等）
check("★ 发布侧：observed_to_iso 解析 wiki 上报日期",
      P.observed_to_iso("September 21, 2026") == "2026-09-21T00:00:00+00:00",
      P.observed_to_iso("September 21, 2026"))
check("发布侧：解析不了 → 空串（调用方回落 expiry/now）",
      P.observed_to_iso("") == "" and P.observed_to_iso("上周一") == "")
check("★ 发布侧：payload 时间戳不用「本次运行时间」（否则每轮都产生新提交）",
      "val_pub" in _src and "acr_pub" in _src
      and "published_at = now.isoformat()" not in _src)
_a = json.dumps(P.build_valence(_rot(filled_idx=1), STAMP, NOW),
                ensure_ascii=False, sort_keys=True)
_b = json.dumps(P.build_valence(_rot(filled_idx=1), STAMP, NOW),
                ensure_ascii=False, sort_keys=True)
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
check("★ 接线：非 ready 分支里先走社区快照、再 sleep（否则整段跳过）",
      _i_phase < _i_call < _i_sleep, f"phase={_i_phase} call={_i_call} sleep={_i_sleep}")
check("★ 接线：两个刷新都支持 community_only（跳过 FS 的直通路径）",
      "async def refresh_valence(self, community_only: bool = False)" in API_SRC
      and "async def refresh_acrichis_week(self, community_only: bool = False)"
      in API_SRC)
check("接线：取不到社区快照 → 返回 no-community 静默跳过（不告警）",
      'return "no-community"' in API_SRC
      and "no-community" in API_SRC)
check("接线：社区快照刷新只在成功时记 INFO（其余沉默）",
      'if isinstance(st, str) and st.startswith("refreshed")' in MAIN_SRC)
check("文案：FS 缺失提示已改为「社区快照或随包快照」（不再说「不再自动刷新」）",
      "社区快照或随包快照" in MAIN_SRC and "将沿用随包快照、不再自动刷新" not in MAIN_SRC)

# ---------------------------------------------------------------------------
# 8. 可达性：社区快照走**三条通道**（国内实测读不到 raw）
# ---------------------------------------------------------------------------
from core.api_client import (  # noqa: E402
    COMMUNITY_ACRITHIS_URLS, COMMUNITY_VALENCE_URLS,
)
check("★ 消费侧：两条数据各有 3 条通道（raw → jsDelivr → statically）",
      len(COMMUNITY_VALENCE_URLS) == 3 and len(COMMUNITY_ACRITHIS_URLS) == 3,
      f"{len(COMMUNITY_VALENCE_URLS)} / {len(COMMUNITY_ACRITHIS_URLS)}")
check("  主通道是 raw，三条都指向同一 bot-data 分支",
      COMMUNITY_VALENCE_URLS[0].startswith("https://raw.githubusercontent.com/")
      and all("bot-data" in u or "@bot-data" in u for u in COMMUNITY_VALENCE_URLS),
      str(COMMUNITY_VALENCE_URLS))
check("  备用通道是 jsDelivr 与 statically",
      "cdn.jsdelivr.net" in COMMUNITY_VALENCE_URLS[1]
      and "statically.io" in COMMUNITY_VALENCE_URLS[2])
check("★ 接线：_community_json 按序试通道（命中备用通道会记一条 INFO）",
      "for i, url in enumerate(urls)" in API_SRC and "备用通道" in API_SRC
      and "_community_json(COMMUNITY_VALENCE_URLS)" in API_SRC
      and "_community_json(COMMUNITY_ACRITHIS_URLS)" in API_SRC)
check("发布侧：支持 --direct（在服务器上直读本机绝对路径，不走 ssh）",
      '"--direct"' in _src and "direct: bool = False" in _src
      and 'return (ROOT / "core/data/rotations.json",' not in _src)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 社区快照守卫全部通过")
