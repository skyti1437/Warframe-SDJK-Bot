# -*- coding: utf-8 -*-
"""「识卡伤害」复算回归（python3 tests/test_scan_damage.py）

背景（2026-10-09）：
  ① `_h_scan_damage` 把 `parsed.content_str`（字符串）传给 `dc.parse_args`，
     而后者要的是 token 列表 —— 字符串被逐字符拆开，「150级 / 爆头 / 重击」
     全部失效，单字还进了卡面「未识别」行；
  ② `dc.calculate` 直接往入参 spec 写 note / `_armor` / `evo_headshot`，
     识卡缓存的 spec 每复算一次，notes 就多一份。

参数/复算用显式手工配卡快照；缓存/按钮业务用已许可快照桩。
另用未许可默认 host 验证真实 analyze 未知布局不能计算或保存。内嵌桩，不联网。
"""

from __future__ import annotations

import asyncio
import copy
import re
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import damage_calc as dc  # noqa: E402
from core import loadout_ocr as lo  # noqa: E402
from core.commands.scan import ScanCommands  # noqa: E402
from core.commands.vision import VisionCommands  # noqa: E402
from core.parser import parse  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"  —— {detail}"))
    if not cond:
        FAILED.append(name)


def _scan_cache(weapon: str, mods: list[dict]) -> tuple[dict, dict]:
    """参数/复算用手工配卡快照，不证明截图布局完整。"""
    an = lo.analyze({"weapon": weapon, "mods": mods, "panel": {}})
    assert an["ok"], an.get("errors")
    return an["weapon"], lo.to_damage_spec({k: v for k, v in an.items() if k != "quality"})


def _run(weapon: dict, cached: dict, text: str) -> list[str]:
    host = types.SimpleNamespace(
        _last_scan={("umo", "u1"): (time.time(), weapon, cached)},
        _safe_sender=lambda _e: "u1",
    )
    event = types.SimpleNamespace(unified_msg_origin="umo")
    parsed = parse(text)
    assert parsed.command == "scandamage", parsed.command
    reply = asyncio.run(ScanCommands._h_scan_damage(host, parsed, event, None))
    return list(reply.lines or [])


# ---------------------------------------------------------------------------
# ① 参数真正生效（近战：重击段才有数据）
# ---------------------------------------------------------------------------
print("① 识卡伤害：参数生效")
_w_mel, _c_mel = _scan_cache("迦伦提恩", [{"name": "压迫点", "drain": "9", "color": "白"}])
_lines = _run(_w_mel, _c_mel, "识卡伤害 对 重机枪手 150级 爆头 重击")
_target = next((ln for ln in _lines if ln.startswith("· 目标")), "")
check("等级 150级 生效（目标行）", "150级" in _target, _target)
check("敌人 重型机枪手 生效", any("敌人：重型机枪手" in ln for ln in _lines))
# 「爆头期望」行恒显示；爆头开关只体现在 DoT（继承爆头倍率）上 ⇒ 对照组比 DoT 行
_no_hs = _run(_w_mel, _c_mel, "识卡伤害 对 重机枪手 150级 重击")


def _dot(ls: list[str]) -> str:
    return next((ln for ln in ls if ln.startswith("· 异常 DoT")), "")


check(
    "爆头 生效（DoT 行随爆头放大，与不带爆头的对照组不同）",
    bool(_dot(_lines)) and _dot(_lines) != _dot(_no_hs),
    f"{_dot(_lines)} vs {_dot(_no_hs)}",
)
check("重击 生效（出现重击段）", any(ln.startswith("· 重击") for ln in _lines))
check(
    "无「未识别」行（不再逐字符拆分）",
    not any("未识别" in ln for ln in _lines),
    str([ln for ln in _lines if "未识别" in ln]),
)

# 不带参数：沿用识卡缓存，默认 100 级
_plain = _run(_w_mel, _c_mel, "识卡伤害")
check(
    "无参数 → 默认 100级",
    any(ln.startswith("· 目标") and "100级" in ln for ln in _plain),
)

# ---------------------------------------------------------------------------
# ② 连续复算 notes 不累积（非近战 + 重击 → calculate 会写「没有重击数据」）
# ---------------------------------------------------------------------------
print("② 识卡伤害：连续两次复算，缓存不被污染")
_w_gun, _c_gun = _scan_cache("雷克斯", [{"name": "黄蜂蛰刺", "drain": "14", "color": "白"}])
_snap = copy.deepcopy(_c_gun)
_l1 = _run(_w_gun, _c_gun, "识卡伤害 重击")
_l2 = _run(_w_gun, _c_gun, "识卡伤害 重击")
_n1 = sum("没有重击数据" in ln for ln in _l1)
_n2 = sum("没有重击数据" in ln for ln in _l2)
check("第一次卡面含「没有重击数据」1 次", _n1 == 1, str(_n1))
check("第二次卡面仍只 1 次（不累积）", _n2 == 1, str(_n2))
check("两次卡面完全一致", _l1 == _l2)
check("识卡缓存 spec 未被改动", _c_gun == _snap, str(_c_gun.get("notes")))

# ---------------------------------------------------------------------------
# ③ calculate 不改入参（#4 根因）：note 走 res["notes"] 返回
# ---------------------------------------------------------------------------
print("③ calculate：入参 spec 不被改动")
_s, _ = dc.parse_args(["重击"])
_s_snap = copy.deepcopy(_s)
_r1 = dc.calculate(_s, _w_gun)
_r2 = dc.calculate(_s, _w_gun)
check("入参 spec 前后相等", _s == _s_snap, str(sorted(k for k in _s if _s[k] != _s_snap.get(k))))
check("不再泄漏 _armor 到入参", _s.get("_armor") == _s_snap.get("_armor"))
check("note 经 res 返回", any("没有重击数据" in n for n in _r1["notes"]), str(_r1["notes"]))
check("两次 res 的 notes 相同", _r1["notes"] == _r2["notes"])
check("两次数值相同", _r1["health"] == _r2["health"] and _r1["dps"] == _r2["dps"])

# ④ 识卡「认不全」回显（2026-10-10 L3）：只回显白名单结构化字段并截断，
#    视觉输出里被图中文字注入的其它字段/换行不得原样进群
print("④ 识卡回显：白名单 + 截断")
_echo = ScanCommands._ocr_echo(
    {
        "weapon": "绝路 [30]\n@全体成员 " + "注" * 80,
        "capacity": "60/70",
        "mods": [{"name": "膛线", "drain": 12, "color": "绿", "polarity": "V"}] * 20,
        "panel": {"total": ["注入文本panel", "x"]},
        "evil": "注入文本evil",
    }
)
check("回显不含非白名单字段", "注入文本" not in _echo, _echo)
check("回显不含换行", "\n" not in _echo, _echo)
check("回显总长 ≤ 301", len(_echo) <= 301, str(len(_echo)))
check("回显含武器与 MOD 摘要", "绝路" in _echo and "膛线(12绿)" in _echo, _echo)


class _ScanHost(ScanCommands, VisionCommands):
    """仅桩视觉输入与平台能力；识卡、参数解析、计算、格式化走真实入口。"""

    def __init__(self, mode="auto", buttons=True):
        self.cfg = {"scan_detail_mode": mode, "scan_cooldown": 0}
        self.buttons = buttons
        self._ocr_busy = set()
        self._ocr_last = {}
        self.detail_calls = 0
        self.verified_layout_fixture = False
        self.ocr = {
            "weapon": "雷克斯",
            "mods": [{"name": "黄蜂蛰刺", "drain": "14", "color": "白"}],
            "panel": {},
        }

    @staticmethod
    def _safe_sender(event):
        return event.sender

    @staticmethod
    def _event_has_image(event):
        return True

    @staticmethod
    def _vision_gate(event, _name, *, task="scan"):
        return None, event.sender

    @staticmethod
    async def _image_data_urls(event):
        return ["离线识别产物"]

    async def _extract_loadout_validated(self, _image):
        return copy.deepcopy(self.ocr)

    def _can_send_actions(self, event):
        if isinstance(self.buttons, Exception):
            raise self.buttons
        return self.buttons

    def _loadout_detail_lines(self, an):
        self.detail_calls += 1
        return super()._loadout_detail_lines(an)


class _BusinessScanHost(_ScanHost):
    """只用于缓存/按钮业务隔离的已许可快照桩；不是布局检测证据。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.verified_layout_fixture = True


def _business_snapshot(an):
    # 测试边界：保留质量失败，显式许可字段自洽快照进入缓存业务。
    # 不修改 layout_complete，不宣称 analyze 已能证明截图完整。
    an["quality"]["calculation_allowed"] = an["quality"]["snapshot_safe"]
    an["quality"]["layout_reasons"] = []
    return an


def _event(sender="u1", umo="qq_official:GroupMessage:group"):
    return types.SimpleNamespace(sender=sender, unified_msg_origin=umo)


async def _scan(host, event=None):
    if not host.verified_layout_fixture:
        return await host._h_scan(parse("识卡"), event or _event(), None)
    analyze = lo.analyze

    def permitted_business_snapshot(*args, **kwargs):
        return _business_snapshot(analyze(*args, **kwargs))

    with patch.object(lo, "analyze", side_effect=permitted_business_snapshot):
        return await host._h_scan(parse("识卡"), event or _event(), None)


async def _damage(host, text="识卡伤害", event=None):
    parsed = parse(text)
    assert parsed.command == "scandamage", parsed.command
    return await host._h_scan_damage(parsed, event or _event(), None)


def _denied(reply):
    return any(line.startswith("◆ 原因：") for line in reply.lines)


async def _entry_checks():
    print("⑤ 已许可快照桩：按能力延迟详情；真实未知布局另验拒绝")
    unknown_layout = _ScanHost("lazy")
    with patch.object(dc, "calculate", wraps=dc.calculate) as calculate:
        rejected = await _scan(unknown_layout)
    check("真实分析未知布局不计算", calculate.call_count == 0)
    check("真实分析未知布局不发按钮或详情", not rejected.actions and not rejected.pages)
    check(
        "真实分析未知布局不写任何伤害快照",
        not getattr(unknown_layout, "_last_scan", {})
        and not getattr(unknown_layout, "_scan_records", {}),
    )
    check("真实分析未知布局仍保留可见识别清单", any("黄蜂蛰刺" in line for line in rejected.lines))
    baseline = lo.card_lines(_business_snapshot(lo.analyze(_BusinessScanHost().ocr)))
    reference_calls = {}
    modes = [
        ("auto", True, True),
        ("auto", False, False),
        ("lazy", True, True),
        ("lazy", False, True),
        ("eager", True, False),
        ("eager", False, False),
        ("非法配置", True, True),
        ("非法配置", False, False),
        (None, True, True),
        (42, False, False),
    ]
    for mode, buttons, lazy in modes:
        host = _BusinessScanHost(mode, buttons)
        with patch.object(dc, "calculate", wraps=dc.calculate) as calculate:
            reply = await _scan(host)
        label = f"{mode!r}/按钮{buttons}"
        check(label + "：详情计算次数", host.detail_calls == (0 if lazy else 1))
        check(label + "：页数", not reply.pages if lazy else len(reply.pages) == 2)
        first_lines = reply.lines if lazy else reply.pages[0][1]
        check(label + "：保留原识别首屏", first_lines[: len(baseline)] == baseline)
        check(label + "：仅详情按需计算", calculate.call_count == (0 if lazy else 1))
        if mode in {"auto", "eager"} and buttons:
            reference_calls[mode] = calculate.call_count
        if lazy:
            check(label + "：手动详情入口", any("识卡伤害 #" in x for x in reply.lines))
            check(label + "：按钮按能力输出", len(reply.actions) == (2 if buttons else 0))
            if buttons:
                check(label + "：按钮标记本人限制", all(x.owner_only for x in reply.actions))
                check(label + "：改目标是填入操作", reply.actions[1].mode == "fill")
            else:
                command = re.search(r"识卡伤害 #[A-Za-z0-9_-]+", "\n".join(reply.lines))
                check(label + "：手动入口能复算", not _denied(await _damage(host, command[0])))
        else:
            check(label + "：保留旧两页输出", not reply.actions)
    check(
        "延迟模式仅少一次详情计算",
        reference_calls["eager"] == reference_calls["auto"] + 1,
        str(reference_calls),
    )
    unconfirmed = _BusinessScanHost("auto", RuntimeError("平台能力不可确认"))
    failed_bridge = await _scan(unconfirmed)
    check("能力查询异常时auto保留两页", len(failed_bridge.pages) == 2)
    missing_bridge = _BusinessScanHost("auto")
    missing_bridge._can_send_actions = None
    check("没有按钮桥接时auto保留两页", len((await _scan(missing_bridge)).pages) == 2)

    changing = _BusinessScanHost("eager")
    await _scan(changing)
    changing.cfg["scan_detail_mode"] = "lazy"
    changed_reply = await _scan(changing)
    check("详情模式读取运行时配置", not changed_reply.pages and changing.detail_calls == 1)

    print("⑥ 缓存：完整会话+发送者隔离，按钮只读当时的卡")
    host = _BusinessScanHost("lazy")
    first = await _scan(host)
    first_command = first.actions[0].send
    first_weapon = copy.deepcopy(_w_gun)
    host.ocr = {
        "weapon": "迦伦提恩",
        "mods": [{"name": "压迫点", "drain": "9", "color": "白"}],
        "panel": {},
    }
    second = await _scan(host)
    check("连续识卡获得独立按钮", first_command != second.actions[0].send)
    with patch.object(dc, "calculate", wraps=dc.calculate) as calculate:
        old_reply = await _damage(host, first_command)
    check("旧按钮仍计算上一张卡", calculate.call_args.args[1] == first_weapon)
    check("旧按钮可正常查看详情", not _denied(old_reply))
    with patch.object(dc, "calculate", wraps=dc.calculate) as calculate:
        await _damage(host)
    check("手输无token使用本人最新卡", calculate.call_args.args[1] == _w_mel)
    overridden = await _damage(host, first_command + " 对 重机枪手 150级 爆头")
    check("token后参数仍有效", any("150级" in line for line in overridden.lines))

    bob = _event("u2")
    await _scan(host, bob)
    other_room = _event(umo="aiocqhttp:GroupMessage:group")
    await _scan(host, other_room)
    other_group = _event(umo="qq_official:GroupMessage:another_group")
    await _scan(host, other_group)
    before_denied = copy.deepcopy((host._last_scan, host._scan_records))
    denied_cases = [
        ("本人无记录不借别人", "识卡伤害", _event("u3")),
        ("他人token不借本人最新卡", first_command, bob),
        ("跨平台会话token拒绝", first_command, other_room),
        ("同平台跨群token拒绝", first_command, other_group),
        ("未知token不借本人最新卡", "识卡伤害 #不存在", _event()),
        ("空token不借本人最新卡", "识卡伤害 #", _event()),
        ("空sender不借用token", first_command, _event("")),
        ("空sender不借用最新卡", "识卡伤害", _event("")),
        ("空会话不借用token", first_command, _event(umo="")),
    ]
    for name, command, event in denied_cases:
        with patch.object(dc, "calculate", wraps=dc.calculate) as calculate:
            reply = await _damage(host, command, event)
        check(name, _denied(reply) and calculate.call_count == 0)
    check("拒绝访问不改变有效缓存", (host._last_scan, host._scan_records) == before_denied)
    collision_a = _event("c", "a|b")
    collision_b = _event("b|c", "a")
    isolated = _BusinessScanHost("lazy")
    collide_button = (await _scan(isolated, collision_a)).actions[0].send
    check(
        "拼接分隔符不能伪造同一身份", _denied(await _damage(isolated, collide_button, collision_b))
    )
    check("拼接分隔符不能借最近记录", _denied(await _damage(isolated, event=collision_b)))
    anonymous = _BusinessScanHost("lazy")
    anonymous_reply = await _scan(anonymous, _event(""))
    check("匿名识卡不生成可共享按钮", not anonymous_reply.actions)
    check("匿名识卡不写缓存", not getattr(anonymous, "_last_scan", {}))
    check("匿名识卡说明手动计算入口", any("手动配卡计算" in x for x in anonymous_reply.lines))

    print("⑦ 缓存深拷贝：原识别产物与下游计算均不能污染快照")
    pristine = copy.deepcopy((host._last_scan, host._scan_records))
    real_calculate = dc.calculate

    def mutating_calculate(spec, weapon):
        spec.setdefault("notes", []).append("测试下游原地修改")
        weapon["_test_mutation"] = {"nested": [1]}
        return real_calculate(spec, weapon)

    with patch.object(dc, "calculate", side_effect=mutating_calculate):
        await _damage(host, first_command)
        await _damage(host)
    check("计算器原地改动不污染两种缓存", (host._last_scan, host._scan_records) == pristine)
    captured_analysis = []
    captured_specs = []
    real_analyze = lo.analyze
    real_to_spec = lo.to_damage_spec

    def capture_analyze(*args, **kwargs):
        an = copy.deepcopy(real_analyze(*args, **kwargs))
        an["weapon"]["_test_nested"] = {"values": [1]}
        captured_analysis.append(an)
        return an

    def capture_spec(*args, **kwargs):
        spec = real_to_spec(*args, **kwargs)
        if spec is not None:
            spec["_test_nested"] = {"values": [1]}
            captured_specs.append(spec)
        return spec

    detached = _BusinessScanHost("lazy")
    with patch.object(lo, "analyze", side_effect=capture_analyze):
        with patch.object(lo, "to_damage_spec", side_effect=capture_spec):
            await _scan(detached)
    detached_snapshot = copy.deepcopy((detached._last_scan, detached._scan_records))
    captured_analysis[0]["weapon"]["_test_nested"]["values"].append(2)
    for spec in captured_specs:
        spec["_test_nested"]["values"].append(2)
    check(
        "识别产物后续修改不影响快照",
        (detached._last_scan, detached._scan_records) == detached_snapshot,
    )

    print("⑧ 过期与容量：只清过期项，旧按钮不切到新卡")
    expiring = _BusinessScanHost("lazy")
    clock = [10000.0]
    with patch("core.commands.scan.time.time", side_effect=lambda: clock[0]):
        expired_command = (await _scan(expiring)).actions[0].send
        clock[0] += 60
        bob_command = (await _scan(expiring, bob)).actions[0].send
        clock[0] = 11800.0
        check("本人记录满30分钟后失效", _denied(await _damage(expiring)))
        check("本人过期不删别人的新记录", not _denied(await _damage(expiring, bob_command, bob)))
        await _scan(expiring)
        check("旧token过期不借新卡", _denied(await _damage(expiring, expired_command)))
        check("过期按钮不清本人新记录", not _denied(await _damage(expiring)))
        check("过期按钮不清别人记录", not _denied(await _damage(expiring, event=bob)))

    bounded = _BusinessScanHost("lazy")
    seed_analysis = lo.analyze(bounded.ocr)
    with patch.object(lo, "analyze", side_effect=lambda *a: copy.deepcopy(seed_analysis)):
        oldest_command = (await _scan(bounded, _event("user0"))).actions[0].send
        for index in range(1, 130):
            await _scan(bounded, _event(f"user{index}"))
    check(
        "最近记录与按钮快照都最多128项",
        len(bounded._last_scan) == len(bounded._scan_records) == 128,
    )
    check(
        "容量淘汰后旧按钮不能借用", _denied(await _damage(bounded, oldest_command, _event("user0")))
    )
    check("容量淘汰保留最新记录", not _denied(await _damage(bounded, event=_event("user129"))))

    print("⑥ 识卡前端收尾：记录号、详情后续按钮、无按钮降级")
    tokens = set()
    for index in range(40):
        command = (await _scan(_BusinessScanHost("lazy"), _event(f"t{index}"))).actions[0].send
        tokens.add(command.split("#", 1)[1])
    check(
        "记录号只含字母数字（不让 _ / - 触发 Markdown 整卡退回）",
        all(t.isalnum() for t in tokens) and len(tokens) == 40,
        str(tokens),
    )
    host = _BusinessScanHost("lazy")
    command = (await _scan(host)).actions[0].send
    detail = await _damage(host, command)
    check(
        "详情结果带「换目标复算」填入按钮，仅本人、绑定同一记录",
        [(a.label, a.send, a.mode, a.owner_only) for a in detail.actions]
        == [("换目标复算", command + " 对 ", "fill", True)],
        str(detail.actions),
    )
    recalc = await _damage(host, command + " 对 重机枪手 150级")
    check(
        "换目标后的结果再给「默认目标」执行按钮",
        [(a.label, a.send, a.mode, a.owner_only) for a in recalc.actions]
        == [
            ("换目标复算", command + " 对 ", "fill", True),
            ("默认目标", command, "run", True),
        ],
        str(recalc.actions),
    )
    latest = await _damage(host, "识卡伤害 对 重机枪手 150级")
    check(
        "不带记录号的复算按钮也不伪造记录号",
        [a.send for a in latest.actions] == ["识卡伤害 对 ", "识卡伤害"],
        str(latest.actions),
    )
    host.buttons = False
    plain = await _damage(host, command)
    check(
        "平台不支持按钮时详情不带按钮，正文不变", not plain.actions and plain.lines == detail.lines
    )
    expired = await _damage(_BusinessScanHost("lazy"), "识卡伤害 #nope")
    check("过期 / 不属于本人：只给原因与用法，不给按钮", _denied(expired) and not expired.actions)

    print("⑩ 低质量识别只保留清单，不借用上一张的伤害记录")
    safe = _BusinessScanHost("lazy")
    old_command = (await _scan(safe)).actions[0].send
    bob_command = (await _scan(safe, bob)).actions[0].send
    records = copy.deepcopy(safe._scan_records)
    safe.ocr["mods"] = [{"name": "9", "drain": 6}]
    partial = await _scan(safe)
    check("未知卡仍显示识别清单", any("9" in line for line in partial.lines))
    check("不可信识别不发详情或复算按钮", not partial.actions and not partial.pages)
    check("不可信识别明确说明", any("不生成识卡伤害记录" in line for line in partial.lines))
    check("失败新图不借上一张最近记录", _denied(await _damage(safe)))
    check("失败不删除旧卡专属token", not _denied(await _damage(safe, old_command)))
    check("失败不删除别人的最近记录", not _denied(await _damage(safe, event=bob)))
    check("失败不删除历史token", safe._scan_records == records)
    safe.ocr = copy.deepcopy(_BusinessScanHost().ocr)
    safe.ocr["panel"] = {"total": ["2倍", "70%", "90%"]}
    corrupt = await _scan(safe)
    check("面板量纲冲突不生成可信详情", not corrupt.actions and _denied(await _damage(safe)))
    rows = ScanCommands._parse_damage_rows(
        '{"rows":[{"行名":"冲击","数值":"2 > 6"},{"label":"切割","value":4}]}'
    )
    check("聚焦补读兼容字典行且保留左右值", rows == [["冲击", "2 > 6"], ["切割", 4]], str(rows))


asyncio.run(_entry_checks())

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
