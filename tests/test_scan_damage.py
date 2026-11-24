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
import base64
import copy
import io
import re
import sys
import time
import types
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from core import damage_calc as dc  # noqa: E402
from core import damage_engine as de  # noqa: E402
from core import loadout_ocr as lo  # noqa: E402
from core import pips  # noqa: E402
from core.commands.scan import ScanCommands  # noqa: E402
from core.commands.vision import VisionCommands  # noqa: E402
from core.i18n import use_lang  # noqa: E402
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


async def _check_experimental_help():
    host = types.SimpleNamespace(_event_has_image=lambda _: False, _safe_sender=lambda _: "u1")
    event = types.SimpleNamespace(unified_msg_origin="umo")
    for language, terms in (
        ("zh", ("实验功能", "默认关闭")),
        ("en", ("experimental", "off by default")),
    ):
        with use_lang(language):
            scan = await ScanCommands._h_scan(host, parse("识卡"), event, None)
            damage = await ScanCommands._h_scan_damage(host, parse("识卡伤害"), event, None)
        for reply in (scan, damage):
            check(
                f"{language} {reply.title} 用法标注实验阶段和默认关闭",
                any(all(term in line for term in terms) for line in reply.lines),
            )


asyncio.run(_check_experimental_help())


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
    "识卡伤害重击使用命中及攻击速度措辞",
    any(ln.startswith("◆ 单次命中") for ln in _lines)
    and any("攻击速度" in ln for ln in _lines)
    and not any("射速" in ln or "单发" in ln for ln in _lines),
)
check(
    "无「未识别」行（不再逐字符拆分）",
    not any("未识别" in ln for ln in _lines),
    str([ln for ln in _lines if "未识别" in ln]),
)

# 不带参数：沿用识卡缓存里的默认标准靶
_plain = _run(_w_mel, _c_mel, "识卡伤害")
check(
    "无参数 → 默认 170 级钢路百夫长去超宏无盾",
    any(ln.startswith("· 目标") and "170级" in ln for ln in _plain)
    and any("凶魂百夫长" in ln for ln in _plain)
    and any("钢铁之路 · 去超宏 · 无盾" in ln for ln in _plain),
)

_cached_rank, _ = dc.parse_args(["百夫长9999", "有盾", "普通"])
_cached_rank.update(base_dmg=_c_mel["base_dmg"])
_cached_rank["combat_options"]["distance"] = 10.0
_before_rank = copy.deepcopy(_cached_rank)
_reset_lines = _run(_w_mel, _cached_rank, "识卡伤害 标准靶 有超宏")
_reset_text = "\n".join(_reset_lines)
check(
    "识卡伤害预设能用显式默认字段覆盖缓存，单项开关继续覆盖且缓存不变",
    "170级" in _reset_text
    and "钢铁之路 · 带超宏 · 无盾" in _reset_text
    and "9999" not in _reset_text
    and "（身体、含暴击期望）" in _reset_text
    and _cached_rank == _before_rank,
)
_changed = "\n".join(_run(_w_mel, _cached_rank, "识卡伤害 C系 去超宏 无盾"))
check(
    "识卡伤害显式派系会清除缓存的具名敌人",
    "C系" in _changed and "凶魂百夫长" not in _changed and "去超宏 · 无盾" in _changed,
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


async def _damage(host, text="识卡伤害", event=None, *, platform=None):
    parsed = parse(text)
    assert parsed.command == "scandamage", parsed.command
    return await host._h_scan_damage(parsed, event or _event(), platform)


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


async def _engine_checks():
    print("⑪ 独立引擎：从同一快照传原等级，故障不串入内置估算")
    host = _BusinessScanHost("lazy")
    now = time.time()
    cached = copy.deepcopy(_c_mel)
    cached["mods"] = [
        {"name": "Sacrificial Steel", "rank": 7, "max_rank": 10},
        {"name": "North Wind", "rank": 1, "max_rank": 5},
        {"name": "Enduring Affliction", "rank": 0, "max_rank": 5},
    ]
    cached["arcanes"] = [{"name": "Melee Exposure", "zh": "近战·暴露", "rank": 2}]
    cached["loadout_slots"] = [{"kind": "arcane", "state": "installed", "rank": 2}]
    event = _event()
    host._last_scan = {(event.unified_msg_origin, event.sender): (now, _w_mel, cached)}
    host.cfg["damage_engine_mode"] = "simulation"
    response = {"ok": True, "dps": 125.0, "kills": 1.0}
    with patch.object(de, "_request", AsyncMock(return_value=response)) as send:
        reply = await _damage(host, "识卡伤害 对 重机枪手 150级")
        args = send.await_args.args
        check("复算调用模拟服务", args[1] == "/api/simulate")
        check(
            "MOD 7、1、0 级逐张传入",
            args[2]["mods"]
            == [
                "sacrificial_steel@7",
                "north_wind@1",
                "enduring_affliction@0",
            ],
        )
        check(
            "赋能使用原等级和规范 ID",
            args[2]["arcane_rank"] == [2] and args[2]["arcane"] == ["melee_exposure"],
        )
        check(
            "模拟显示实用后端和指定目标",
            any("完整模拟" in ln for ln in reply.lines) and any("150" in ln for ln in reply.lines),
        )
        status = cached["loadout_slots"][0]
        status.update(rank=None, name="近战·暴露")
        cached["arcanes"] = []
        reply = await _damage(host)
        args = send.await_args.args
        check(
            "未知赋能等级不发到上游的满级兜底", not args[2]["arcane"] and not args[2]["arcane_rank"]
        )
        check("未知赋能写在回执", any("等级未知" in ln for ln in reply.lines))
    with (
        patch.object(de, "_request", AsyncMock(side_effect=de.EngineError("服务超时"))),
        patch.object(dc, "calculate", wraps=dc.calculate) as builtin,
    ):
        failed = await _damage(host)
        check("完整模拟不可用时明确提示", any("服务超时" in ln for ln in failed.lines))
        check("完整模拟故障不调用内置", builtin.call_count == 0)
    host.cfg["damage_engine_mode"] = "builtin"
    host._last_scan[(event.unified_msg_origin, event.sender)] = (now, _w_mel, _c_mel)
    with patch.object(de, "_request", AsyncMock()) as send:
        estimate = await _damage(host)
        check(
            "切回内置立刻生效",
            any("内置估算" in ln for ln in estimate.lines) and send.await_count == 0,
        )


asyncio.run(_engine_checks())


async def _engine_queue_checks():
    print("⑫ 完整模拟队列：单路、排位、满载、取消和失败收尾")
    host = _BusinessScanHost("lazy")
    host.cfg["damage_engine_mode"] = "simulation"
    levels = list(range(100, 106))
    arrivals, notes = [], {}
    started = {level: asyncio.Event() for level in levels}
    finished = {level: asyncio.Event() for level in levels}
    notified = {level: asyncio.Event() for level in levels}
    host._last_scan = {("qq_official:GroupMessage:group", "u1"): (time.time(), _w_mel, _c_mel)}

    def event_for(level):
        async def send(text):
            notes[level] = text
            notified[level].set()

        event = _event()
        event.send, event.plain_result = send, lambda text: text
        return event

    async def request(cfg, path, body, *, timeout):
        assert path == "/api/simulate" and timeout == 32
        level = body["level"]
        arrivals.append(level)
        started[level].set()
        await finished[level].wait()
        if level == 103:
            raise de.EngineError("测试失败")
        return {"ok": True, "dps": 125.0, "kills": 1.0}

    with TemporaryDirectory() as tmp, patch.object(de, "_request", request):
        host.damage_engine = de.EngineService(Path(tmp))
        jobs = {
            level: asyncio.create_task(
                _damage(host, f"识卡伤害 对 重机枪手 {level}级", event_for(level))
            )
            for level in levels
        }
        try:
            for level in levels[1:5]:
                await asyncio.wait_for(notified[level].wait(), 2)
            check(
                "忙时即时回复等待排位，四条排队且首条直接开始",
                arrivals == [100]
                and all(f"第 {level - 100} 位" in notes[level] for level in levels[1:5])
                and 100 not in notes,
            )
            busy = await asyncio.wait_for(jobs[105], 2)
            check("第五条等待请求收到队列已满", any("排队已满" in line for line in busy.lines))
            jobs[102].cancel()
            await asyncio.gather(jobs[102], return_exceptions=True)
            jobs[100].cancel()
            await asyncio.sleep(0)
            jobs[100].cancel()
            await asyncio.sleep(0)
            check("运行请求取消仍等到引擎收尾", arrivals == [100] and not jobs[100].done())
            finished[100].set()
            for level in (101, 103, 104):
                await asyncio.wait_for(started[level].wait(), 2)
                finished[level].set()
                reply = await asyncio.wait_for(jobs[level], 2)
                expected = "测试失败" if level == 103 else "平均 DPS"
                check(f"{level} 级的等待任务自动返回", any(expected in ln for ln in reply.lines))
            check("先到先算且取消的等待请求没有调用引擎", arrivals == [100, 101, 103, 104])
        finally:
            for done in finished.values():
                done.set()
            for job in jobs.values():
                job.cancel()
            await asyncio.gather(*jobs.values(), return_exceptions=True)
            await host.damage_engine.close()


asyncio.run(_engine_queue_checks())


def _confirmed_dps_scan_fixture():
    names = [("膛线", 4), ("地狱火", 6), ("低温弹头", 6), ("弹匣增幅", 4)]
    fields = {
        "weapon": "卡拉克",
        "capacity": "40/60",
        "mods": [{"name": name, "drain": drain} for name, drain in names],
    }
    tiles, reads = [], []
    for index, slot_id in enumerate(
        [f"mod-{row}-{col}" for row in (1, 2) for col in range(1, 5)] + ["exilus", "arcane"]
    ):
        label = f"T{index + 1}"
        kind = "regular" if slot_id.startswith("mod-") else slot_id
        x, y = 0.30 + index % 4 * 0.12, 0.35 + index // 4 * 0.20
        tile = {"id": slot_id, "label": label, "kind": kind, "box": [x, y, x + 0.1, y + 0.1]}
        if index < len(names):
            name, drain = names[index]
            tiles.append(
                {
                    **tile,
                    "rank_observation": {"count": 0, "maxed": False},
                    "capacity_badge": {"color": "白"},
                }
            )
            reads.append({"tile": label, "state": "installed", "name": name, "drain": drain})
        else:
            tiles.append(tile)
            state, mark = ("empty", "empty_frame") if kind == "regular" else ("locked", "padlock")
            reads.append({"tile": label, "state": state, "mark": mark})
    fields["_upgrade_slots"] = lo.verify_upgrade_slots(fields, {"slots": reads}, tiles, None)
    return fields


async def _dps_comparison_checks():
    print("⑬ 识卡首图：已确认配装逐张复算，同工况与同指标")
    host = _ScanHost("lazy")
    host.cfg["damage_engine_mode"] = "simulation"
    host.ocr = _confirmed_dps_scan_fixture()
    with (
        patch.object(dc, "calculate", wraps=dc.calculate) as calculate,
        patch.object(de, "_request", AsyncMock()) as remote,
    ):
        reply = await _scan(host)
        cached = next(iter(host._last_scan.values()))[2]
        calls = [call.args[0] for call in calculate.call_args_list]
        check(
            "完整模拟启用时首图照样用内置估算且不请求模拟服务",
            len(calls) == 5
            and remote.await_count == 0
            and all(
                s["level"] == 170
                and s["enemy"]["name"] == "Thrax Centurion"
                and s["steel_path"]
                and not s["headshot"]
                and s["fire_seconds"] == 30
                for s in calls
            ),
        )
        check(
            "仅拆下该槽 MOD，保留其他等级并重新组合元素",
            [(s["base_dmg"], s["singles"]) for s in calls]
            == [
                (15, {"heat": 15, "cold": 15}),
                (0, {"heat": 15, "cold": 15}),
                (15, {"cold": 15}),
                (15, {"heat": 15}),
                (15, {"heat": 15, "cold": 15}),
            ]
            and [c["_scan_magazine_pct"] for c in calls] == [5, 5, 5, 5, 0]
            and cached["base_dmg"] == 15
            and cached["loadout_slots"][0]["rank"] == 0,
        )
    serration = next((ln for ln in reply.lines if ln.startswith("· 膛线　")), "")
    check(
        "整套 DPS 和拆卡下降百分比回图",
        any("整套持续 DPS" in ln for ln in reply.lines)
        and any("拆下后 DPS / 变化" in ln for ln in reply.lines)
        and "0/10" in serration
        and "⟦m⟧-" in serration,
        serration,
    )
    check(
        "已确认的弹匣加成与拆卡结果一起显示，复算入口保留",
        any(
            ln.startswith("· 弹匣增幅　")
            and "弹匣容量" in ln
            and "DPS" not in ln
            and "%" in ln
            and not ln.endswith("⟦n⟧—⟦/n⟧")
            for ln in reply.lines
        )
        and bool(reply.actions),
    )

    host.ocr["capacity"] = "41/60"
    with patch.object(dc, "calculate", wraps=dc.calculate) as calculate:
        reply = await _scan(host)
    check(
        "容量冲突时保留识卡，首图无 DPS 变化且不计算",
        any("膛线" in ln for ln in reply.lines)
        and not any("拆下后 DPS" in ln for ln in reply.lines)
        and not reply.actions
        and calculate.call_count == 0,
    )


asyncio.run(_dps_comparison_checks())


async def _portable_scan_checks():
    print("⑭ 独立引擎识卡复算：实际槽位、面板与进化")
    ocr = _confirmed_dps_scan_fixture()
    exilus, arcane = ocr["_upgrade_slots"][-2:]
    exilus.update(
        name="私法 补给",
        mod_name="Vigilante Supplies",
        state="installed",
        rank=5,
        drain=5,
        color="绿",
    )
    arcane.update(name="主要·无情", state="installed", rank=5)
    ocr["mods"].append({"name": exilus["name"], "drain": 5})
    ocr["arcanes"] = [{"name": arcane["name"]}]
    ocr["capacity"] = "35/60"
    ocr["panel"] = {"crit_chance": "50% > 20%", "status_chance": "20%"}
    analysis = lo.analyze(ocr, calculate_reference=False)
    spec = lo.to_damage_spec(analysis, simulation=True)
    check(
        "普通与特殊槽分别传递，截取当前配装列",
        (
            spec["scan_capacity"] == (25, 60)
            and [i for i, _ in spec["confirmed_replay_slots"]] == list(range(4))
            and spec["scan_panel"]["crit_chance"] == 20
        ),
    )
    utility = {"path": "/Exilus", "maxRank": 5, "drain": 9, "polarity": "AP_TACTIC"}
    arc = {"path": "/Arcane", "maxRank": 5}
    equipment = {
        "utilityMods": {"Vigilante Supplies": [utility]},
        "arcanes": {"Primary Merciless": [arc]},
    }
    request, drains = de._scan_replay_equipment(spec, equipment)
    check(
        "扩展槽与赋能使用实际等级，普通槽保留0级",
        (
            request["exilus"] == {"path": "/Exilus", "rank": 5}
            and request["arcanes"] == [{"path": "/Arcane", "rank": 5}]
            and request["specialSlotPolarities"] == {"exilus": "AP_TACTIC"}
            and drains == {0: 4, 1: 6, 2: 6, 3: 4, 8: 5}
        ),
    )
    check(
        "红槽只接受可解释的向上取整差异",
        (de._scan_slot_polarity(11, 9, "AP_DEFENSE") == ("AP_ATTACK", 12)),
    )
    for invalid in (None, -1, 6):
        bad = copy.deepcopy(spec)
        bad["loadout_slots"][-1]["rank"] = invalid
        try:
            de._scan_replay_equipment(bad, equipment)
        except de.ScanReplayUnconfirmed:
            refused = True
        else:
            refused = False
        check(f"不猜赋能等级 {invalid}", refused)

    modes = [
        {"index": 0, "available": True},
        {"index": 1, "isIncarnonForm": True, "available": True},
    ]
    catalog = {
        "fireModes": modes,
        "incarnon": [{"tier": 2, "selectable": True, "options": [{"id": "2-1"}, {"id": "2-2"}]}],
    }
    same_panel = False

    def panel_result(_, _path, body, **kwargs):
        crit = (
            0.2
            if body["fireMode"] == 1 and (same_panel or body["incarnonChoices"]["tier2"] == "2-2")
            else 0.1
        )
        return {
            "buildLabel": de.PORTABLE_DATA,
            "selection": {"fireMode": body["fireMode"]},
            "incarnonChoices": body["incarnonChoices"],
            "moddedStats": {"critChance": crit, "statusChance": 0.2, "magazine": 10},
        }

    with TemporaryDirectory() as tmp:
        service = de.EngineService(Path(tmp))
        with patch.object(de, "portable_request", AsyncMock(side_effect=panel_result)) as send:
            mode, route, context = await de._resolve_scan_context(
                service, 18764, request, catalog, spec
            )
            check(
                "同图面板唯一确定灵化及进化",
                mode["index"] == 1 and route == {"tier2": "2-2"} and context == "panel",
            )
            check(
                "换靶时复用与敌人无关的面板匹配",
                (
                    (
                        await de._resolve_scan_context(
                            service, 18764, {**request, "enemy": "/Second"}, catalog, spec
                        )
                    )[1]
                    == route
                    and send.await_count == 4
                ),
            )
            same_panel = True
            ambiguous = {**spec, "scan_panel": {**spec["scan_panel"], "magazine": 10}}
            best_mode, alternatives, basis = await de._resolve_scan_context(
                service, 18764, request, catalog, ambiguous
            )
            check(
                "面板相同的进化全部交给同靶输出比较",
                (
                    best_mode["index"] == 1
                    and alternatives == [{"tier2": "2-1"}, {"tier2": "2-2"}]
                    and basis == "optimized"
                ),
            )
            called = send.await_count
            manual = {**ambiguous, "form": "incarnon", "evo_keys": ["2-1"]}
            mode, route, context = await de._resolve_scan_context(
                service, 18764, request, catalog, manual
            )
            check(
                "显式模式和进化直接复算",
                mode["index"] == 1
                and route == {"tier2": "2-1"}
                and context == "explicit"
                and send.await_count == called,
            )
            same_panel = False
            split_panel = {
                **spec,
                "scan_panel": {
                    "_section": "primary_fire",
                    "crit_chance": 10,
                    "status_chance": 20,
                    "_sections": [
                        {
                            "_section": "incarnon",
                            "crit_chance": 20,
                            "status_chance": 20,
                        }
                    ],
                },
            }
            mode, route, context = await de._resolve_scan_context(
                service, 18764, request, catalog, split_panel
            )
            check(
                "灵化分段收窄进化后，主射仍用它自己的面板与形态",
                mode["index"] == 0 and route == {"tier2": "2-2"} and context == "panel",
            )
        await service.close()

    check(
        "面板分项的同比比例可核对，显著的总量偏差仍拒绝",
        (
            de._matches_scan_panel(
                {**spec["scan_panel"], "damage_types": {"impact": 33, "slash": 27}},
                {
                    "critChance": 0.2,
                    "statusChance": 0.2,
                    "damageTypes": {"DT_IMPACT": 30, "DT_SLASH": 25},
                },
            )
            and not de._matches_scan_panel(
                {**spec["scan_panel"], "damage_types": {"impact": 60, "slash": 50}},
                {
                    "critChance": 0.2,
                    "statusChance": 0.2,
                    "damageTypes": {"DT_IMPACT": 30, "DT_SLASH": 25},
                },
            )
        ),
    )


asyncio.run(_portable_scan_checks())


async def _evolution_output_checks():
    print("⑮ 面板无法区分的进化：按同靶输出选最高")
    first = {"tier2": "2-1", "tier4": "4-2"}
    second = {"tier2": "2-2", "tier4": "4-2"}
    request = {
        "enemy": "target",
        "fireMode": 0,
        "windowSeconds": 12,
        "mods": [{"slot": 0, "path": "mod", "rank": 0}],
    }
    calls = []

    def replay(_, path, body):
        calls.append((path, body))
        return {
            "incarnonChoices": body["incarnonChoices"],
            "value": 90 if body["incarnonChoices"] == second else 20,
        }

    def validate(result, body):
        assert body["incarnonChoices"] == result["incarnonChoices"]
        return {"windowDps": result["value"]}, {"assignments": [{"slot": 0, "drain": 4}]}

    with (
        patch.object(de, "portable_request", AsyncMock(side_effect=replay)),
        patch.object(de, "validate_fixed_replay", side_effect=validate),
    ):
        selected, reply = await de._best_scan_replay(18764, request, [first, second], {0: 4})
        check(
            "独立引擎比较全部兼容进化并取最高窗口 DPS",
            (
                selected["incarnonChoices"] == second
                and reply["value"] == 90
                and calls
                == [
                    ("/api/simulate", {**request, "incarnonChoices": route})
                    for route in (first, second)
                ]
            ),
        )

    spec = {
        "level": 170,
        "form": "keep",
        "notes": [],
        "builtin_scan_context": {
            "form": "base",
            "routes": [tuple(first.values()), tuple(second.values())],
            "matched_codes": ["4-2"],
        },
    }

    def calculate(candidate, _):
        value = 90 if candidate["evo_keys"][0] == "2-2" else 20
        return {"ok": True, "dps_sustained": value, "crit_exp": 1, "notes": []}

    with patch.object(dc, "calculate", side_effect=calculate) as calc:
        result = lo.calculate_bundled_scan(spec, {})
        check(
            "随包模型按同目标持续 DPS 选最高，保持可见进化",
            (
                result["_scan_evo_keys"] == tuple(second.values())
                and any("最高 2-2" in note for note in result["notes"])
                and all(
                    call.args[0]["level"] == 170 and call.args[0]["form"] == "base"
                    for call in calc.call_args_list
                )
            ),
        )

    burston = dc.find_weapon_exact("Burston Prime")
    options, _ = dc.parse_args([])
    panel = dc._apply_panel_overrides(
        {**options, "form": "base", "evo_keys": ["2-2", "3-3", "4-2"]}, burston
    )
    check(
        "随包数字进化使用实际分支，只加一次武器基础属性",
        (
            abs(panel["damage"]["total"] - 88) < 1e-4
            and abs(panel["criticalChance"] - 0.40) < 1e-6
            and panel["magazineSize"] == 45
        ),
    )


asyncio.run(_evolution_output_checks())


# 拉特昂装备、极性及面板取自 2026-11-01 三图验收中的图 3。
# 伯斯顿沿用同一套实体槽位，主射伤害行对应随包回归中的 1,364.2 面板。
# 像素与 OCR 回执在内存中复现，测试不依赖本机的旧截图或视觉渠道。
_UPGRADE_CARDS = (
    ("镀层 氩晶瞄具", 10, 6),
    ("关键延迟", 5, 5),
    ("低温弹头 Prime", 10, 8),
    ("猎人 战备", 5, 5),
    ("镀层 分裂膛室", 10, 8),
    ("膛线", 10, 7),
    ("弱点感应", 5, 5),
    ("致命火力", 3, 7),
    ("私法 补给", 0, 5),
)
_UPGRADE_ARCANE = "主要·无情"
_UPGRADE_ARCANE_PATH = "/Fixture/PrimaryMerciless"
_UPGRADE_EXILUS_PATH = "/Fixture/VigilanteSupplies"
_UPGRADE_PANEL = {
    "伯斯顿": (120, 3.96, 48, 4, 45, 1.54, (70, 70, 93.3, 524.7), 1364.2),
    "拉特昂": (138, 6.6, 41.6, 3.33, 30, 1.85, (25.4, 203.5, 25.4, 572.4), 1488.3),
}


def _upgrade_image(first_visible):
    image = Image.new("RGB", (1920, 1080), (18, 18, 22))
    draw = ImageDraw.Draw(image)
    for index, (name, rank, _) in enumerate(_UPGRADE_CARDS):
        if index == 0 and not first_visible:
            continue
        x, y = (1692, 517) if index == 8 else (712 + index % 4 * 245, 450 + index // 4 * 134)
        draw.rectangle((x - 76, y - 98, x + 76, y + 13), fill=(32, 32, 36))
        color = (191, 38, 36) if index == 8 else (238, 238, 238) if index == 7 else (72, 215, 65)
        draw.rectangle((x + 76, y - 84, x + 88, y - 72), fill=color)
        lit = (143, 191, 229)
        if rank == dc.find_mod_exact(name)["max_rank"]:
            draw.rectangle((x - 76, y, x + 76, y + 1), fill=lit)
        for p in range(rank):
            px = x + 12 * p - 6 * (rank - 1)
            draw.polygon([(px, y - 5), (px + 4, y), (px, y + 5), (px - 4, y)], fill=lit)
    for p in range(5):
        x, y = 1643 + p * 25, 381 + p % 2 * 2
        draw.polygon(
            [(x, y - 10), (x + 9, y), (x, y + 10), (x - 9, y)],
            outline=(61, 55, 50),
            width=2,
        )
        draw.polygon([(x, y - 6), (x + 6, y), (x, y + 6), (x - 6, y)], fill=(67, 61, 54))
    return image


def _upgrade_ocr(weapon, omitted_first, *, wrong_capacity=False):
    crit, multiplier, status, rate, magazine, reload, amounts, total = _UPGRADE_PANEL[weapon]
    lines = [
        f"105,55,80,22,0,升级/{weapon}",
        "173,55,48,22,0,Prime",
        "30,130,40,18,0,容量",
        "235,130,40,18,0," + ("5/60" if wrong_capacity else "4/60"),
        "45,200,70,18,0,Primary",
    ]
    fields = (
        ("暴击几率", f"{crit}%"),
        ("暴击倍率", f"{multiplier}倍"),
        ("触发几率", f"{status}%"),
        ("射击速度", rate),
        ("弹匣", magazine),
        ("装填", reload),
        ("多重射击", 1.8),
        *zip(("冲击", "穿刺", "切割", "病毒"), amounts),
        ("总计", total),
    )
    for index, (label, value) in enumerate(fields):
        y = 222 + 22 * index
        lines += [f"50,{y},72,18,0,{label}", f"220,{y},72,18,0,{value}"]
    if weapon == "伯斯顿":
        lines += [
            "45,525,70,18,0,次要射击",
            "50,550,70,18,0,暴击几率",
            "220,550,70,18,0,50%",
            "45,600,70,18,0,灵化",
            "50,625,70,18,0,暴击几率",
            "220,625,70,18,0,99%",
            "50,650,70,18,0,总计",
            "220,650,70,18,0,500",
        ]
    for index, (name, _, _) in enumerate(_UPGRADE_CARDS):
        if index == 0 and omitted_first:
            continue
        x, y = (880, 428) if index == 8 else (371 + index % 4 * 128, 375 + index // 4 * 124)
        if index == 2:
            lines += [f"{x - 14},{y},62,19,0,低温弹头", f"{x + 39},{y + 3},38,19,0,Prime"]
        else:
            lines.append(f"{x},{y},65,19,0,{name}")
    lines += [f"881,331,54,18,0,{_UPGRADE_ARCANE}", "720,795,50,19,0,所有"]
    return "\n".join(lines)


class _UpgradeFixtureProvider:
    def __init__(self, image, *, omit_first, first_visible):
        rows = pips.detect_pips(image)
        self.tiles = pips.equipment_tiles(image, rows, {}, melee=False)[1]
        self.focus_tiles = pips.equipment_tiles(image, rows, {}, melee=False, slot_ids={"mod-1-1"})[
            1
        ]
        self.omit_first = omit_first
        self.first_visible = first_visible
        self.focus_calls = 0

    @staticmethod
    def meta():
        return types.SimpleNamespace(id="offline-upgrade-fixture")

    async def text_chat(self, *, prompt, image_urls, session_id):
        focus = session_id.startswith("sdjk-slot-focus-")
        if focus:
            self.focus_calls += 1
        tiles = self.focus_tiles if focus else self.tiles
        if not tiles:
            raise AssertionError("未定位到离线升级槽位")
        with Image.open(io.BytesIO(base64.b64decode(image_urls[0].split(",", 1)[1]))) as sheet:
            assert sheet.size == tiles[0]["sheet_size"]
        lines = []
        for index, tile in enumerate(tiles):
            left, top, right, bottom = tile["sheet_box"]
            w, h = tile["sheet_size"]

            def add(x, y, text, width=60, height=15):
                cx, cy = left + (right - left) * x, top + (bottom - top) * y
                lines.append(
                    f"{1000 * cx / w},{1000 * cy / h},{1000 * width / w},{1000 * height / h},0,{text}"
                )

            if tile["kind"] == "arcane":
                add(0.53, 0.62, _UPGRADE_ARCANE, height=19)
                continue
            name, _, drain = _UPGRADE_CARDS[index]
            if index == 0 and not self.first_visible:
                continue
            add(0.80, 0.26, drain, width=14)
            if index == 0 and self.omit_first and not focus:
                continue
            if index == 2:
                add(0.41, 0.59, "低温弹头", width=86)
                add(0.62, 0.60, "Prime", width=65)
            elif tile["kind"] == "exilus":
                add(0.50, 0.65, "私法", width=36)
                add(0.47, 0.75, "补给", width=36)
            else:
                add(0.21 if index == 0 else 0.5, 0.57, name)
        return types.SimpleNamespace(completion_text="\n".join(lines))


async def _upgrade_scan(host, weapon, *, first_visible=True, wrong_capacity=False):
    omit = weapon == "拉特昂"
    image = _upgrade_image(first_visible)
    stream = io.BytesIO()
    image.save(stream, "PNG")
    image_url = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
    provider = _UpgradeFixtureProvider(image, omit_first=omit, first_visible=first_visible)
    whole_read = lo.parse_coordinate_ocr(_upgrade_ocr(weapon, omit, wrong_capacity=wrong_capacity))
    with (
        patch.object(host, "_image_data_urls", AsyncMock(return_value=[image_url])),
        patch.object(
            host,
            "_extract_loadout_validated",
            ScanCommands._extract_loadout_validated.__get__(host),
        ),
        patch.object(host, "_scan_vision_providers", return_value=[provider]),
        patch.object(host, "_scan_equipment_providers", return_value=[provider]),
        patch.object(host, "_extract_loadout_from_image", AsyncMock(return_value=whole_read)),
        patch.object(host, "_read_scan_focus", AsyncMock(return_value=None)),
    ):
        return await _scan(host), provider.focus_calls


class _UpgradeFixtureEngine:
    def __init__(self, weapon):
        self.weapon = weapon
        self.record = dc.find_weapon_exact(weapon + " Prime")
        self.target = dc.DEFAULT_ENEMY_PATH
        self.replays = []
        self.hunter = "/Fixture/HunterMunitions"
        self.catalog = {
            "buildLabel": de.PORTABLE_DATA,
            "schemaVersion": 3,
            "weapons": [
                {
                    "path": self.record["uniqueName"],
                    "nameEn": self.record["name"],
                    "available": True,
                    "buildAvailable": True,
                    "fireModes": [
                        {"index": 0, "available": True},
                        {"index": 1, "available": True, "isIncarnonForm": True},
                    ],
                    "incarnon": [
                        {
                            "tier": tier,
                            "selectable": True,
                            "options": [
                                {"id": f"{tier}-{option}"} for option in range(1, count + 1)
                            ],
                        }
                        for tier, count in ((2, 2), (3, 3), (4, 3))
                    ],
                }
            ],
            "enemies": [
                {
                    "nameEn": "Thrax Centurion",
                    "path": self.target,
                    "complete": True,
                    "scalingSupported": True,
                }
            ],
            "manualMods": [
                {
                    "nameEn": "Hunter Munitions",
                    "path": self.hunter,
                    "baseDrain": 4,
                    "maxRank": 5,
                    "polarity": "AP_ATTACK",
                }
            ],
            "utilityMods": [
                {
                    "nameEn": "Vigilante Supplies",
                    "path": _UPGRADE_EXILUS_PATH,
                    "drain": 9,
                    "maxRank": 5,
                    "polarity": "AP_TACTIC",
                }
            ],
            "arcanes": [
                {
                    "nameEn": "Primary Merciless",
                    "path": _UPGRADE_ARCANE_PATH,
                    "maxRank": 5,
                }
            ],
        }

    async def request(self, port, path, body=None, **_kwargs):
        if path == "/api/catalog":
            return self.catalog
        route = body["incarnonChoices"]
        crit, multiplier, status, rate, magazine, reload, amounts, total = _UPGRADE_PANEL[
            self.weapon
        ]
        if route["tier4"] != ("4-2" if self.weapon == "伯斯顿" else "4-3"):
            crit /= 2
        if route["tier3"] == ("3-1" if self.weapon == "伯斯顿" else "3-2"):
            magazine = 66 if self.weapon == "伯斯顿" else 30
        elif self.weapon == "拉特昂":
            magazine = 15
        if body["fireMode"] == 1:
            crit = 99
        stats = {
            "critChance": crit / 100,
            "critMultiplier": multiplier,
            "statusChance": status / 100,
            "fireRate": rate,
            "magazine": magazine,
            "reloadTime": reload,
            "multishot": 1.8,
            "damageTypes": dict(zip(("DT_IMPACT", "DT_PUNCTURE", "DT_SLASH", "DT_VIRAL"), amounts)),
            "totalDamage": total,
        }
        result = {
            "buildLabel": de.PORTABLE_DATA,
            "incarnonChoices": route,
            "moddedStats": stats,
            "selection": {
                "fireMode": body["fireMode"],
                "weapon": {"path": body["weapon"]},
                "scenario": body.get("scenario", {}),
            },
        }
        if body["damageModel"] == "panel":
            assert body["arcanes"] == [{"path": _UPGRADE_ARCANE_PATH, "rank": 5, "stacks": 0}]
            return result
        self.replays.append(copy.deepcopy(body))
        assert body["mods"][3]["path"] == self.hunter
        assert body["exilus"] == {"path": _UPGRADE_EXILUS_PATH, "rank": 0}
        assert body["arcanes"] == [{"path": _UPGRADE_ARCANE_PATH, "rank": 5}]
        dps = 2100 + (route["tier2"] == "2-2") * 600 + (route["tier3"] == "3-3") * 20
        exilus = {**body["exilus"], "slot": 8, "drain": 5}
        assignments = [
            {**mod, "drain": _UPGRADE_CARDS[index][2]} for index, mod in enumerate(body["mods"])
        ]
        result["selection"]["mods"] = [
            {**mod, "nameEn": _UPGRADE_CARDS[index][0]} for index, mod in enumerate(body["mods"])
        ]
        result.update(
            damageModel="target",
            headshotMix={"chance": body["headshotChance"]},
            enemyEnhancements=body["enemyEnhancements"],
            loadoutValidation={
                "assignments": [*assignments, exilus],
                "capacity": 56,
                "capacityLimit": 60,
                "valid": True,
            },
            benchmarkTimeline={
                "windowDps": dps,
                "windowSeconds": body["windowSeconds"],
                "shots": 21,
                "reloads": 1,
                "context": {
                    "enemyPath": body["enemy"],
                    "level": body["level"],
                    "steelPath": body["steelPath"],
                },
            },
            exilus={
                **exilus,
                "slotPolarity": body["specialSlotPolarities"]["exilus"],
                "catalogJoined": True,
                "nameEn": "Vigilante Supplies",
            },
            arcanes=[{**body["arcanes"][0], "applied": True, "nameEn": "Primary Merciless"}],
        )
        return result


async def _upgrade_round_trips():
    print("⑯ 伯斯顿与拉特昂升级图：实际槽位裁剪、同会话双引擎复算")
    for name, focus_count, replay_count, chosen_codes in (
        ("伯斯顿", 0, 4, "2-2, 3-3"),
        ("拉特昂", 1, 2, "2-2"),
    ):
        host = _ScanHost("lazy")
        scan, calls = await _upgrade_scan(host, name)
        check(
            f"{name}识卡逐槽通过，按需要聚焦补读并写入两份记录",
            calls == focus_count
            and bool(scan.actions)
            and len(host._last_scan) == len(host._scan_records) == 1,
            str(scan.lines),
        )
        if not host._last_scan:
            continue
        cached = next(iter(host._last_scan.values()))[2]
        check(
            f"{name}八主槽、零级红色扩展槽与五级赋能的截图账本",
            cached["scan_capacity"] == (56, 60)
            and [s["rank"] for s in cached["loadout_slots"]] == [10, 5, 10, 5, 10, 10, 5, 3, 0, 5]
            and cached["loadout_slots"][2]["mod_name"] == "Primed Cryo Rounds"
            and cached["loadout_slots"][8]["color"] == "红"
            and cached["loadout_slots"][8]["drain"] == 5,
        )
        bundled = await _damage(host, "识卡伤害 150级")
        check(
            f"{name}同会话随包复算成功",
            bool(bundled.actions)
            and any("内置估算" in line for line in bundled.lines)
            and any("150级" in line for line in bundled.lines),
            str(bundled.lines),
        )
        engine = _UpgradeFixtureEngine(name)
        host.cfg.update(damage_engine_mode="simulation", damage_engine_portable_port=18764)
        with TemporaryDirectory() as root:
            host.damage_engine = de.EngineService(Path(root))
            try:
                with (
                    patch.object(host.damage_engine, "ensure_portable", AsyncMock()),
                    patch.object(de, "portable_request", new=engine.request),
                ):
                    portable = await _damage(host, "识卡伤害 150级", platform="pc")
                    default_replays = engine.replays[:]
                    selected = await _damage(host, "识卡伤害 百夫长9999 有盾 去超宏", platform="pc")
                    selected_replays = engine.replays[len(default_replays) :]
            finally:
                await host.damage_engine.close()
        check(
            f"{name}独立引擎按同靶同窗口比较所有等价进化并标注选中编号",
            bool(portable.actions)
            and any("独立引擎" in line for line in portable.lines)
            and any("最高窗口 DPS" in line and chosen_codes in line for line in portable.lines)
            and len(default_replays) == replay_count
            and all(
                {k: v for k, v in request.items() if k != "incarnonChoices"}
                == {k: v for k, v in default_replays[0].items() if k != "incarnonChoices"}
                for request in default_replays
            ),
            str(portable.lines),
        )

        check(
            f"{name} 识卡缓存复算透传 9999 级、瞄准、去超宏和模拟盾",
            len(selected_replays) == replay_count
            and all(
                request["enemy"] == dc.DEFAULT_ENEMY_PATH
                and request["level"] == 9999
                and request["headshotChance"] == 1
                and request["windowSeconds"] == 180
                and request["scenario"]["aiming"] is True
                and request["enemyEnhancements"]
                == {"eximus": False, "noOverguard": True, "shieldOverride": 3600.0}
                for request in selected_replays
            )
            and any("去超宏 · 模拟有盾" in line for line in selected.lines),
            str(selected.lines),
        )

    for options, expected_calls in (({"first_visible": False}, 0), ({"wrong_capacity": True}, 1)):
        host = _ScanHost("lazy")
        host.cfg.update(damage_engine_mode="simulation", damage_engine_portable_port=18764)
        scan, calls = await _upgrade_scan(host, "拉特昂", **options)
        check(
            f"拉特昂拒绝无首槽证据或账本不平 {options}",
            calls == expected_calls
            and not scan.actions
            and not host._last_scan
            and not host._scan_records
            and _denied(await _damage(host)),
        )


asyncio.run(_upgrade_round_trips())


async def _manual_portable_round_trips():
    from core.commands.wiki_misc import WikiMiscCommands
    from core.i18n import use_lang

    class Host(WikiMiscCommands):
        cfg = {"damage_engine_mode": "simulation", "damage_engine_portable_port": 18764}

    fixtures = {}
    for name in ("Grimoire", "Amprex", "Braton Prime", "Skana"):
        weapon = dc.find_weapon_exact(name)
        path = weapon["uniqueName"].replace("TnDoppelgangerGrimoire", "TnGrimoire")
        fixtures[name] = {
            "nameEn": name,
            "path": path,
            "available": True,
            "buildAvailable": True,
            "fireModes": [{"index": 0, "available": True}],
        }
    grimoire = fixtures["Grimoire"]
    copycat = {
        **grimoire,
        "path": dc.find_weapon_exact("Grimoire")["uniqueName"],
        "nameInternal": "TnDoppelgangerGrimoire",
        "fireModes": grimoire["fireModes"][:],
    }
    copycat["ancestry"] = [copycat["path"], grimoire["path"]]
    grimoire["fireModes"].append({"index": 1, "available": True, "isAlternateFire": True})
    fixtures["Braton Prime"]["fireModes"].append(
        {"index": 1, "available": True, "isIncarnonForm": True}
    )
    fixtures["Braton Prime"]["incarnon"] = [
        {
            "tier": 2,
            "selectable": True,
            "options": [{"id": "2-2", "label": "进化甲", "labelEn": "Evolution A"}],
        },
    ]

    def equipment(name, zh, path, rank):
        return {"nameEn": name, "nameZh": zh, "path": "/Lotus/" + path, "maxRank": rank}

    arc = equipment("Primary Deadhead", "主要·死首", "Arcane", 5)
    utility = equipment("Sinister Reach", "凶恶延伸", "RifleExilus", 3)
    melee_utility = equipment("Dispatch Overdrive", "超速击杀", "MeleeExilus", 5)
    stance = equipment("Iron Phoenix", "钢铁凤凰", "Stance", 3)
    serration = equipment("Serration", "膛线", "Serration", 10)
    serration["path"] = dc.find_mod_exact("膛线")["_source_unique_name"]
    catalog = {
        "buildLabel": de.PORTABLE_DATA,
        "schemaVersion": 3,
        "weapons": [*fixtures.values(), copycat],
        "enemies": [
            {
                "nameEn": name,
                "path": path,
                "complete": True,
                "scalingSupported": True,
            }
            for name, path in (
                ("Heavy Gunner", "/Lotus/Enemy"),
                ("Thrax Centurion", dc.DEFAULT_ENEMY_PATH),
                ("Crewman", "/Lotus/Crewman"),
            )
        ],
        "arcanes": [arc],
        "utilityMods": [utility, melee_utility],
        "stances": [stance],
        "manualMods": [serration],
    }
    calls = []

    async def engine(_port, endpoint, body=None, **_kwargs):
        if endpoint == "/api/catalog":
            return catalog
        assert endpoint == "/api/simulate"
        calls.append(body)
        mods = [{**serration, **row} for row in body["mods"]]
        special = {**stance, "rank": 3} if body.get("stancePath") else None
        selected_utility = {row["path"]: row for row in (utility, melee_utility)}
        return {
            "buildLabel": de.PORTABLE_DATA,
            "damageModel": body["damageModel"],
            "headshotMix": {"chance": body["headshotChance"]},
            "enemyEnhancements": body["enemyEnhancements"],
            "selection": {
                "weapon": {"path": body["weapon"]},
                "fireMode": body["fireMode"],
                "mods": mods,
                "stance": special,
                "scenario": body.get("scenario", {}),
            },
            "loadoutValidation": {
                "assignments": mods,
                "valid": True,
                "capacity": 13,
                "capacityLimit": 60,
            },
            "benchmarkTimeline": {
                "windowSeconds": body["windowSeconds"],
                "windowDps": 160.0,
                "shots": 12,
                "reloads": 1,
                "context": {
                    "enemyPath": body["enemy"],
                    "level": body["level"],
                    "steelPath": body["steelPath"],
                },
            },
            "incarnonChoices": body.get("incarnonChoices", {}),
            "arcanes": [{**arc, **row, "applied": True} for row in body.get("arcanes", ())],
            "exilus": {**selected_utility[body["exilus"]["path"]], **body["exilus"]}
            if "exilus" in body
            else None,
        }

    with TemporaryDirectory() as tmp, patch.object(de, "portable_request", engine):
        host = Host()
        host.damage_engine = de.EngineService(Path(tmp))
        host.damage_engine.ensure_portable = AsyncMock()
        cases = (
            ("en", "damage Grimoire vs Heavy Gunner alt fire", "Grimoire", 1, ("Alternate fire",)),
            (
                "zh",
                "伤害 安培克斯 对 重机枪手 膛线 凶恶延伸 赋能 主要·死首 5级 距离5米",
                "Amprex",
                0,
                ("膛线 · 等级 10", "扩展槽 凶恶延伸 · 3 级", "主要·死首 · 5 级", "距离 5 米"),
            ),
            (
                "zh",
                "伤害 布莱顿p 对 重机枪手 灵化 进化 2-2",
                "Braton Prime",
                1,
                ("灵化形态", "2-2"),
            ),
            (
                "en",
                "damage Skana vs Heavy Gunner Iron Phoenix Dispatch Overdrive heavy attack combo120",
                "Skana",
                0,
                ("Heavy attack", "Combo ×7", "Iron Phoenix", "Exilus Dispatch Overdrive"),
            ),
        )
        for language, command, name, index, labels in cases:
            with use_lang(language):
                reply = await host._h_damage(parse(command), None, "pc")
            shown = "\n".join(reply.lines)
            check(
                f"{name} 手输从正常指令完成同靶步进回放并回读装备",
                calls[-1]["weapon"] == fixtures[name]["path"]
                and calls[-1]["fireMode"] == index
                and all(label in shown for label in labels)
                and ("30 秒平均 DPS" if language == "zh" else "Average DPS over 30 s") in shown
                and "/Lotus/" not in shown,
                shown,
            )
            if name in {"Amprex", "Braton Prime", "Skana"}:
                bundled = host._damage_reply(parse(command)).lines if language == "zh" else []
                if language == "en":
                    with use_lang("en"):
                        bundled = host._damage_reply(parse(command)).lines
                check(
                    f"{name} 两个后端分别标注持续公式与独立引擎",
                    any(
                        ("内置估算" if language == "zh" else "Built-in estimate") in line
                        for line in bundled
                    )
                    and ("独立引擎" if language == "zh" else "Standalone engine") in shown,
                )
        for args, expected_target, expected_defense, window, labels in (
            (
                "",
                dc.DEFAULT_ENEMY_PATH,
                {"eximus": False, "noOverguard": True},
                30,
                ("凶魂百夫长 Lv170", "去超宏 · 无盾"),
            ),
            (
                "百夫长9999 有盾",
                dc.DEFAULT_ENEMY_PATH,
                {"eximus": False, "noOverguard": False, "shieldOverride": 3600.0},
                180,
                ("凶魂百夫长 Lv9999", "带超宏 · 模拟有盾", "瞄准"),
            ),
            (
                "百夫长9999 去超宏",
                dc.DEFAULT_ENEMY_PATH,
                {"eximus": False, "noOverguard": True, "shieldOverride": 0.0},
                180,
                ("凶魂百夫长 Lv9999", "去超宏 · 无盾", "瞄准"),
            ),
            (
                "对 船员 无盾 普通",
                "/Lotus/Crewman",
                {"eximus": False, "noOverguard": True, "shieldOverride": 0.0},
                30,
                ("船员 Lv170", "普通", "去超宏 · 无盾"),
            ),
            (
                "C系 120级",
                "__custom_enemy__",
                {"eximus": False, "noOverguard": True},
                30,
                ("Corpus 合成靶", "Lv120"),
            ),
        ):
            reply = await host._h_damage(parse(f"伤害 布莱顿p {args}"), None, "pc")
            body = calls[-1]
            check(
                f"独立引擎从伤害入口透传工况 {args or '默认'}",
                body["enemy"] == expected_target
                and body["windowSeconds"] == window
                and body["enemyEnhancements"] == expected_defense
                and body["headshotChance"] == (1.0 if window == 180 else 0.0)
                and bool(body.get("scenario", {}).get("aiming")) == (window == 180)
                and all(label in "\n".join(reply.lines) for label in labels),
                str(reply.lines),
            )
        before = len(calls)
        invalid = await host._h_damage(parse("伤害 布莱顿p 超宏50000"), None, "pc")
        check(
            "手填数值超宏由内置估算，独立引擎明确提示",
            len(calls) == before
            and any("超宏防护 暂不支持手动覆盖" in line for line in invalid.lines)
            and any("超宏池 50000" in line for line in invalid.lines),
            str(invalid.lines),
        )
        check(
            "手输扩展槽不占定序普通槽，架势和连击按目录透传",
            calls[1]["mods"] == [{"slot": 0, "path": serration["path"], "rank": 10}]
            and calls[1]["arcanes"] == [{"path": arc["path"], "rank": 5}]
            and calls[2]["incarnonChoices"] == {"tier2": "2-2"}
            and calls[3]["mods"] == []
            and calls[3]["stancePath"] == stance["path"]
            and calls[3]["scenario"] == {"heavyAttack": True, "comboTier": 6},
        )
        before = len(calls)
        reply = await host._h_damage(parse("伤害 布莱顿p 对 重机枪手 超宏5000"), None, "pc")
        check(
            "自订超宏明确回退，完全不调用独立引擎",
            before == len(calls) and "超宏防护" in reply.lines[0],
        )
        await host.damage_engine.close()


asyncio.run(_manual_portable_round_trips())

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
