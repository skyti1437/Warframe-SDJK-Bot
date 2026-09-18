# -*- coding: utf-8 -*-
"""信条 / 终幕 / 灵化轮换卡离线测试（python3 tests/test_rotation_batches.py）

覆盖本轮新接线的两个轮换模式（版式对齐既有「灵化回廊」）：

1. **信条（Tenet，``mode=refresh_only``）**：Ergo Glast 在任意中继站**常驻** 5 把
   （Agendus / Exec / Ferrox / Grigori / Livia），每把 40 个腐化全息密钥；只有
   **元素加成**每 4 天 00:00 UTC 重生成。所以卡面不该出现「A/B 批」这类
   不存在的分批描述。
2. **终幕（Coda，``mode=batch_cycle``）**：Eleanor 的 Coda 武器按 **A/B 两批**
   每 4 天换批，A ∪ B = 14 把（各 7 把），批次表来自 wiki.warframe.com/w/Reset。
   ⚠️ 终幕的换批边界**比信条的元素加成重生成晚 24 小时**（2026-09-14 实测：
   终幕 09-17 00:00 UTC / 信条 09-16 00:00 UTC），两者**不是**同一个锚点，
   别为了"看起来整齐"去对齐 epoch —— 对齐会让终幕整体错位一天。

断言对「今天是哪天」不敏感：批次下标按 epoch + anchor_idx 在测试里独立重算，
再和卡面文字对齐，避免把某一天的快照写死。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return
    pkg = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    mc_mod = types.ModuleType("astrbot.api.message_components")
    star_mod = types.ModuleType("astrbot.api.star")

    class AstrBotConfig(dict):
        pass

    class _Logger:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass

    class AstrMessageEvent:
        def __init__(self, umo: str = "group://test", sender: str = "tester"):
            self.unified_msg_origin = umo
            self._sender = sender
        def get_sender_name(self) -> str:
            return self._sender

    class MessageChain:
        def message(self, text):
            return text

    class _EventMessageType:
        ALL = "ALL"

    class _Filter:
        EventMessageType = _EventMessageType
        event_message_type = staticmethod(lambda spec: (lambda fn: fn))

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: (lambda fn: fn)
    event_mod.filter = _Filter()

    class Image:
        pass

    class Plain:
        pass

    mc_mod.Image = Image
    mc_mod.Plain = Plain

    class Context:
        pass

    class Star:
        def __init__(self, *a, **k): pass

    def register(*a, **k):
        def deco(cls):
            return cls
        return deco

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register
    api.AstrBotConfig = AstrBotConfig
    api.logger = _Logger()
    sys.modules.setdefault("astrbot", pkg)
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = mc_mod
    sys.modules["astrbot.api.star"] = star_mod


_install_astrbot_stub()

import main as plugin  # noqa: E402
from core import calculators as calc  # noqa: E402

ROT = json.loads((ROOT / "core" / "data" / "rotations.json")
                 .read_text(encoding="utf-8"))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _obj():
    obj = plugin.WarframeQuery.__new__(plugin.WarframeQuery)
    obj._dir = Path(tempfile.mkdtemp())
    return obj


OBJ = _obj()


# ------------------------------------------------------------------ 表结构
tenet = ROT.get("tenet") or {}
coda = ROT.get("coda") or {}
inc = ROT.get("incarnon") or {}

check("rotations.json 有 tenet/coda/incarnon 三段",
      all(k in ROT for k in ("tenet", "coda", "incarnon")))
check("信条 mode = refresh_only", tenet.get("mode") == "refresh_only",
      str(tenet.get("mode")))
check("信条周期 96 小时（4 天）", int(tenet.get("period_hours", 0)) == 96)
check("信条 5 把常驻", len(tenet.get("items") or []) == 5,
      str(len(tenet.get("items") or [])))
check("信条无 batches 字段（不存在分批）", not tenet.get("batches"))
_TENET_EN = {i.get("en") for i in (tenet.get("items") or [])}
check("信条武器清单与 wiki 一致",
      _TENET_EN == {"Tenet Agendus", "Tenet Exec", "Tenet Ferrox",
                    "Tenet Grigori", "Tenet Livia"}, str(sorted(_TENET_EN)))
check("信条每把都有官方简中名",
      all(i.get("cn") for i in (tenet.get("items") or [])))
check("信条简中名都带「信条·」前缀",
      all((i.get("cn") or "").startswith("信条·") for i in (tenet.get("items") or [])),
      str([i.get("cn") for i in (tenet.get("items") or [])]))

check("终幕 mode = batch_cycle", coda.get("mode") == "batch_cycle",
      str(coda.get("mode")))
check("终幕周期 96 小时（4 天）", int(coda.get("period_hours", 0)) == 96)
_batches = coda.get("batches") or []
check("终幕 2 批", len(_batches) == 2, str(len(_batches)))
check("终幕每批 7 把", all(len(b) == 7 for b in _batches),
      str([len(b) for b in _batches]))
_ALL_CODA = [x.get("en") for b in _batches for x in b]
check("终幕两批共 14 把且不重复", len(set(_ALL_CODA)) == 14,
      str(len(set(_ALL_CODA))))
check("终幕批次标签为 A/B", list(coda.get("batch_label") or []) == ["A", "B"],
      str(coda.get("batch_label")))
check("终幕锚点下标在范围内", 0 <= int(coda.get("anchor_idx", -1)) < len(_batches))
check("终幕每把都有官方简中名（终幕·XXX）",
      all((x.get("cn") or "").startswith("终幕·") for b in _batches for x in b),
      str([x.get("cn") for b in _batches for x in b if not (x.get("cn") or "").startswith("终幕·")]))


# ------------------------------------------------------------- 卡面（信条）
title, lines = OBJ._rotation_lines("tenet", "Ergo 信条武器轮换").title, \
    OBJ._rotation_lines("tenet", "Ergo 信条武器轮换").lines
check("信条卡标题", title == "Ergo 信条武器轮换", title)
check("信条卡说明「常驻」", any("常驻" in ln for ln in lines), str(lines))
check("信条卡列出 5 把",
      sum(1 for ln in lines if ln.startswith("· 信条·")) == 5, str(lines))
check("信条卡不含「批」字（常驻无分批）",
      not any("批" in ln for ln in lines), str(lines))
check("信条卡标注元素加成刷新周期",
      any("元素加成" in ln and "4 天" in ln for ln in lines), str(lines))
check("信条卡说明获取成本（40 个腐化全息密钥）",
      any("腐化全息密钥" in ln for ln in lines), str(lines))
check("信条卡用 · 列项（次级层级）",
      all(ln.startswith("· ") or ln.startswith("◆") or ln.startswith("※")
          for ln in lines), str(lines))


# ------------------------------------------------------------- 卡面（终幕）
reply = OBJ._rotation_lines("coda", "Coda 终幕武器轮换")
lines = reply.lines
check("终幕卡标题", reply.title == "Coda 终幕武器轮换", reply.title)

# 独立重算当前批次下标，再和卡面对齐（不写死某一天）
_epoch = datetime.fromisoformat(coda["epoch"])
_period = timedelta(hours=int(coda["period_hours"]))
_now = datetime.now(timezone.utc)
_passed = int((_now - _epoch) // _period)
_idx = (int(coda["anchor_idx"]) + _passed) % len(_batches)
_labels = coda["batch_label"]
check(f"终幕卡标注当前批次（{_labels[_idx]} 批）",
      any(f"当前为 {_labels[_idx]} 批" in ln for ln in lines), str(lines[:1]))
check("终幕卡列出当前批 7 把",
      sum(1 for ln in lines if ln.startswith("· 终幕·")) == 7, str(lines))
_next = _labels[(_idx + 1) % len(_batches)]
check(f"终幕卡给出下一批（{_next}）",
      any(ln.startswith("◆ 下一批") and _next in ln for ln in lines), str(lines))
check("终幕卡标注换批周期（每 4 天）",
      any("每 4 天" in ln for ln in lines), str(lines))
check("终幕卡给出换批倒计时",
      any("距换批" in ln for ln in lines), str(lines))

# 下一批列出的 7 把 = 另一批
_next_line = [ln for ln in lines if ln.startswith("◆ 下一批")][0]
_other = _batches[(_idx + 1) % len(_batches)]
check("终幕「下一批」列全另一批 7 把",
      all((x.get("cn") or "") in _next_line for x in _other), _next_line)

# 两次调用的当前批一致（幂等，无随机）
again = OBJ._rotation_lines("coda", "Coda 终幕武器轮换").lines
check("终幕卡幂等（同刻两次结果一致）", again == lines)


# ------------------------------------------------------------- 卡面（灵化）
reply = OBJ._rotation_lines("incarnon", "本周钢铁回廊灵化")
check("灵化卡仍是周回环", any("第 " in ln and "/" in ln for ln in reply.lines),
      str(reply.lines[:1]))
# 三种模式都遵守同一版式骨架：◆ 段标题 + · 列项
for key, ttl in (("incarnon", "本周钢铁回廊灵化"),
                 ("tenet", "Ergo 信条武器轮换"),
                 ("coda", "Coda 终幕武器轮换")):
    ls = OBJ._rotation_lines(key, ttl).lines
    check(f"{key} 版式对齐（首行是 ◆ 段标题）", ls and ls[0].startswith("◆"),
          str(ls[:1]))
    check(f"{key} 有 ※ 数据来源/说明行", any(x.startswith("※") for x in ls),
          str(ls))


# --------------------------------------------------------- 元素加成（快照）
# 用户 2026-09-12 反馈「信条和终幕缺失属性和加成数值」。加成值没有官方 API，
# 只有 wiki「Reset」页的玩家上报表（插件侧访问被 Cloudflare 拦 → 存快照）。
# 这里守住三件事：表里有值、值在合法区间、卡面真的印出来了。
_tenet_items = tenet.get("items") or []
check("信条每把都有元素", all(i.get("element") for i in _tenet_items),
      str([i.get("en") for i in _tenet_items if not i.get("element")]))
check("信条每把都有加成数值",
      all(isinstance(i.get("bonus"), (int, float)) for i in _tenet_items),
      str([i.get("en") for i in _tenet_items
           if not isinstance(i.get("bonus"), (int, float))]))
check("信条元素都在展示表中",
      all(i["element"].lower() in calc.ELEM_ZH for i in _tenet_items
          if i.get("element")),
      str([i["element"] for i in _tenet_items
           if i.get("element") and i["element"].lower() not in calc.ELEM_ZH]))
check("信条加成在 25~60 区间",
      all(25 <= float(i["bonus"]) <= 60 for i in _tenet_items if i.get("bonus")),
      str([i.get("bonus") for i in _tenet_items]))

_cur_batch = _batches[int(coda.get("anchor_idx", 0))]
check("终幕当前批每把都有元素+加成",
      all(i.get("element") and isinstance(i.get("bonus"), (int, float))
          for i in _cur_batch),
      str([i.get("en") for i in _cur_batch
           if not (i.get("element") and isinstance(i.get("bonus"), (int, float)))]))
_other_batch = _batches[(int(coda.get("anchor_idx", 0)) + 1) % len(_batches)]
# ⚠️ 2026-09-17 更新：旧断言是「另一批不许填加成」，那是因为当时只抓得到
# 当前批。后来核对 wiki「Reset」页发现它**同时公布 Batch A / Batch B 两张表**，
# 两批都能拿到真实值（scripts/fetch_valence.py 会把两批都写回）。
# 旧断言已失效，但「不许瞎编」的本意要留住 —— 改为**整批完整性**约束：
#   一批要么全空（压根没抓到），要么 7 把全有；**半填 = 漏抓或瞎编**。
for _bi, _b in enumerate(_batches):
    _blabel = (coda.get("batch_label") or ["A", "B"])[_bi]
    _filled = [i for i in _b if i.get("element")]
    check(f"终幕 {_blabel} 批要么全空要么全有（禁止半填/瞎编）",
          len(_filled) in (0, len(_b)),
          f"filled={len(_filled)}/{len(_b)}")
    _bad_vals = [(i.get("en"), i.get("element"), i.get("bonus")) for i in _filled
                 if not ((i.get("element") or "").lower() in calc.ELEM_ZH
                         and isinstance(i.get("bonus"), (int, float))
                         and 25 <= float(i["bonus"]) <= 60)]
    check(f"终幕 {_blabel} 批已填的值都合法（元素在展示表内、加成 25~60）",
          not _bad_vals, str(_bad_vals))

for _k, _d in (("tenet", tenet), ("coda", coda)):
    _raw = _d.get("valence_snapshot") or ""
    check(f"{_k} 有加成快照时间", bool(_raw), str(_raw))
    try:
        _snap = datetime.fromisoformat(_raw)
        _ok = True
    except (TypeError, ValueError):
        _snap, _ok = None, False
    check(f"{_k} 快照时间可解析", _ok, str(_raw))
    if _snap:
        if _snap.tzinfo is None:
            _snap = _snap.replace(tzinfo=timezone.utc)
        _ep = datetime.fromisoformat(_d["epoch"])
        _pd = timedelta(hours=int(_d["period_hours"]))
        _win_start = _ep + _pd * int((datetime.now(timezone.utc) - _ep) // _pd)
        check(f"{_k} 快照落在当前轮次内（未过期）", _snap >= _win_start,
              f"snapshot={_snap.isoformat()} window={_win_start.isoformat()}")

# —— 卡面：每条武器行都带「元素 百分比」，并有一行快照说明 ——
_reply_t = OBJ._rotation_lines("tenet", "Ergo 信条武器轮换")
_reply_c = OBJ._rotation_lines("coda", "Coda 终幕武器轮换")
for _name, _rep, _n in (("信条", _reply_t, 5), ("终幕", _reply_c, 7)):
    _item_rows = [ln for ln in _rep.lines if ln.startswith("· ")]
    check(f"{_name} 卡列出 {_n} 把", len(_item_rows) == _n, str(len(_item_rows)))
    check(f"{_name} 卡每把都带元素与百分比",
          all(re.search(r"　\S+ \d+(\.\d+)?%$", ln) for ln in _item_rows),
          str([ln for ln in _item_rows
               if not re.search(r"　\S+ \d+(\.\d+)?%$", ln)]))
    check(f"{_name} 卡元素名是中文展示名",
          all(any(zh in ln for zh in calc.ELEM_ZH.values()) for ln in _item_rows),
          str(_item_rows[:2]))
    check(f"{_name} 卡有快照说明行",
          any("快照" in ln for ln in _rep.lines), str(_rep.lines[-3:]))

# 具体值抽查（对照 wiki「Reset」页 2026-09-16 快照）
_tmap = {i["en"]: (i.get("element"), i.get("bonus")) for i in _tenet_items}
check("信条·铁晶磁轨炮 = 辐射 36.1%（wiki 快照）",
      _tmap.get("Tenet Ferrox") == ("Radiation", 36.1), str(_tmap.get("Tenet Ferrox")))
check("信条·枢密 = 火焰 27%（wiki 快照）",
      _tmap.get("Tenet Exec") == ("Heat", 27), str(_tmap.get("Tenet Exec")))
_cmap = {i["en"]: (i.get("element"), i.get("bonus")) for i in _cur_batch}
check("终幕当前批·异化者 = 磁力 38.5%（A 批快照）",
      _cmap.get("Coda Catabolyst") == ("Magnetic", 38.5),
      str(_cmap.get("Coda Catabolyst")))
check("终幕当前批·噬轮 = 辐射 48.8%（A 批快照）",
      _cmap.get("Coda Motovore") == ("Radiation", 48.8),
      str(_cmap.get("Coda Motovore")))
check("终幕当前批元素都在展示表中（电击/辐射不许是裸英文）",
      all((v[0] or "").lower() in calc.ELEM_ZH for v in _cmap.values()),
      str({k: v[0] for k, v in _cmap.items()
           if (v[0] or "").lower() not in calc.ELEM_ZH}))

# 2026-09-14 实测纠错：信条与终幕的换轮边界**相差 24 小时**（终幕晚一天），
# 别"顺手"把两者 epoch 对齐 —— 那会把终幕整体错位一天（当日线上事故就是这么来的）。
check("终幕与信条的换轮锚点相差 24 小时（不是同一锚点）",
      abs((datetime.fromisoformat(coda["epoch"])
           - datetime.fromisoformat(tenet["epoch"])).total_seconds()) == 86400,
      f"coda={coda['epoch']} tenet={tenet['epoch']}")
check("终幕锚点对齐到 00:00 UTC（4 天网格整点）",
      (datetime.fromisoformat(coda["epoch"]).hour,
       datetime.fromisoformat(coda["epoch"]).minute) == (0, 0),
      coda["epoch"])
# ⚠️ 2026-09-17 更新：旧断言「周期内只有一批挂加成」已随 wiki 同时公布
# A/B 两表而失效（见上文整批完整性约束）。现在只要求一件事：
# **当前生效的那一批必须有值** —— 因为卡面印的就是它。
check("终幕当前生效批必须有元素加成（卡面要印）",
      all(i.get("element") and isinstance(i.get("bonus"), (int, float))
          for i in _batches[_idx]),
      str([i.get("en") for i in _batches[_idx]
           if not (i.get("element") and isinstance(i.get("bonus"), (int, float)))]))

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
