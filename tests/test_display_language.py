# -*- coding: utf-8 -*-
"""v1.3.1 显示语言（国际服英文）骨架回归：python3 tests/test_display_language.py

覆盖：-cn/-en 后缀、英文指令别名只认首词、会话语言读写、旧订阅缺字段按中文、
推送基线按「平台 × 语言」分组、DE 解析缓存按语言分键、上游请求语言码随当前语言。
不联网。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import damage_calc, i18n  # noqa: E402
from core.commands.wiki_misc import WikiMiscCommands, _dmg_en_toks  # noqa: E402
from core.parser import parse  # noqa: E402
from core.store import GroupStore, Subscription  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------- 后缀与别名
p = parse("裂隙 -en")
check("-en 后缀：语言 en，指令不变", p.command == "fissures" and p.lang == "en", p)
p = parse("裂隙 -cn")
check("-cn 后缀：语言归一为 zh", p.lang == "zh", p.lang)
p = parse("baro -zh")
check("-zh 与 -cn 同义", p.command == "voidtrader" and p.lang == "zh", p)
check("没写后缀时 lang 为 None（跟随会话 / 全局）", parse("裂隙").lang is None)
check("-en 记入修饰符，不进内容", parse("wm soma prime -en").content == ["soma", "prime"])
check("英文别名在首词命中", parse("fissures").command == "fissures")
check("英文别名不在首词：不触发（闲聊不误判）", parse("what is the news").command is None)
check("英文别名不做无空格连写切分", parse("sorties").command is None)
check("原有 ASCII 指令 wm 不受影响", parse("wm水晶p头").command == "wm")
check("中文指令任意位置照旧", parse("帮我查 裂隙").command == "fissures")

# ---------------------------------------------------------------- i18n 基础
check(
    "normalize_lang：cn / 中文 / english",
    [i18n.normalize_lang(x) for x in ("cn", "中文", "English")] == ["zh", "zh", "en"],
)
check(
    "coerce_lang：非法值回落中文", i18n.coerce_lang("fr") == "zh" and i18n.coerce_lang(None) == "zh"
)
check("lang_suffix：中文写 cn", i18n.lang_suffix("zh") == "cn" and i18n.lang_suffix("en") == "en")
with i18n.use_lang("en"):
    inner = (i18n.current_lang(), i18n.L("突击", "Sortie"))
check("use_lang 内切换、退出恢复", inner == ("en", "Sortie") and i18n.current_lang() == "zh")
check("L() 缺英文时回落中文", i18n.L("仅中文") == "仅中文")


async def _ctx_in_thread():
    with i18n.use_lang("en"):
        return await asyncio.to_thread(i18n.current_lang)


check(
    "asyncio.to_thread 继承当前语言（解析线程按语言本地化的前提）",
    asyncio.run(_ctx_in_thread()) == "en",
)

# ---------------------------------------------------------------- 英文伤害入口
with i18n.use_lang("en"):
    for mode in ("heavy", "heavy attack"):
        parsed = parse(f"damage Ruvox incarnon {mode} -en")
        reply = asyncio.run(WikiMiscCommands()._h_damage(parsed, None, "pc"))
        body = reply.raw_text or "\n".join(reply.lines)
        check(
            f"伤害入口识别 {mode}，不按普通攻击误算古声灵化",
            "Ruvox's Incarnon form currently supports normal attacks only" in body,
            body.splitlines()[0],
        )
    enemy_spec, _ = damage_calc.parse_args(
        _dmg_en_toks(parse("damage Soma vs Heavy Gunner").content)
    )
    mod_spec, _ = damage_calc.parse_args(_dmg_en_toks(parse("damage Soma Heavy Caliber").content))
check(
    "重击写法不截断 Heavy Gunner 或 Heavy Caliber",
    (enemy_spec["enemy"] or {}).get("name") == "Heavy Gunner"
    and mod_spec["base_dmg"] == 165.0
    and not enemy_spec["heavy"]
    and not mod_spec["heavy"],
)

# ---------------------------------------------------------------- 会话设置
with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "groups.json"
    path.write_text(json.dumps({"g1": {"platform": "pc", "push": True}}), "utf-8")
    gs = GroupStore(path)
    check(
        "旧会话记录没有 lang：返回 None（跟随全局）",
        gs.lang("g1") is None and gs.lang("新群") is None,
    )
    asyncio.run(gs.set_lang("g1", "en"))
    check("set_lang 写入后读回 en", GroupStore(path).lang("g1") == "en")
    check("set_lang 不动其它键", GroupStore(path).get("g1")["push"] is True)
    asyncio.run(gs.set_lang("g1", None))
    check(
        "set_lang(None) 清除设置",
        GroupStore(path).lang("g1") is None
        and "lang" not in json.loads(path.read_text("utf-8"))["g1"],
    )
    path.write_text(json.dumps({"g2": {"lang": "fr"}}), "utf-8")
    check("存档里的非法语言值按未设置处理", GroupStore(path).lang("g2") is None)

# ---------------------------------------------------------------- 订阅
old = {"umo": "u", "platform": "pc", "event": "裂隙", "sid": "abcd1234"}
sub = Subscription.from_dict(old)
check("旧订阅缺 lang/region：按 zh / intl", sub.lang == "zh" and sub.region == "intl")
check("新订阅字段可往返", Subscription.from_dict({**old, "lang": "en"}).to_dict()["lang"] == "en")
check("订阅里的非法语言值归一为 zh", Subscription.from_dict({**old, "lang": "xx"}).lang == "zh")

# ---------------------------------------------------------------- 推送分组
from core.push import PushDaemon  # noqa: E402

key = PushDaemon._baseline_key
check("中文订阅沿用旧基线键（平台名）", key(Subscription.from_dict(old)) == "pc")
check("英文订阅用独立基线键", key(Subscription.from_dict({**old, "lang": "en"})) == "pc|en")

# ---------------------------------------------------------------- 客户端
from core import api_client as A  # noqa: E402


async def _client_checks():
    client = A.WarframeClient()
    try:
        zh_code = client._lang_code()
        with i18n.use_lang("en"):
            en_code = client._lang_code()
        calls = []

        async def fake_raw():
            return {"Time": 1, "BuildLabel": "b"}

        def fake_parse(raw):
            calls.append(i18n.current_lang())
            return {"lang": i18n.current_lang()}

        client._de_raw = fake_raw
        orig = A.de_worldstate.parse_worldstate
        A.de_worldstate.parse_worldstate = fake_parse
        try:
            a = await client._de_bundle()
            with i18n.use_lang("en"):
                b = await client._de_bundle()
            c = await client._de_bundle()
        finally:
            A.de_worldstate.parse_worldstate = orig
        return zh_code, en_code, a, b, c, calls
    finally:
        await client.close()


zh_code, en_code, a, b, c, calls = asyncio.run(_client_checks())
check(
    "请求语言码：中文沿用 zh-hans、英文发 en",
    zh_code == "zh-hans" and en_code == "en",
    (zh_code, en_code),
)
check("DE 解析缓存按语言分键：两种语言各解析一次", calls == ["zh", "en"], calls)
check("解析结果不串语言", a["lang"] == "zh" and b["lang"] == "en" and c["lang"] == "zh", (a, b, c))

# ---------------------------------------------------------------- 英文解析（离线夹具）
import re  # noqa: E402

from core import de_worldstate as W  # noqa: E402

_raw = json.loads((Path(__file__).parent / "fixtures" / "de_worldstate.json").read_text("utf-8"))
with i18n.use_lang("en"):
    _en = W.parse_worldstate(_raw)
_zh = W.parse_worldstate(_raw)
_cjk = re.compile(r"[\u4e00-\u9fff]")
_left = _cjk.findall(json.dumps(_en, ensure_ascii=False))
check("英文模式解析 DE 世界状态：结果里没有中文", not _left, "".join(_left[:40]))
check("同一份数据中文模式照常是中文", bool(_cjk.search(json.dumps(_zh, ensure_ascii=False))))
check(
    "英文模式节点名用半角括号",
    all("（" not in (f.get("node") or "") for f in _en.get("fissures") or []),
)
check(
    "英文表切换不污染中文缓存：再解析中文仍一致",
    W.parse_worldstate(_raw).get("fissures") == _zh.get("fissures"),
)

# ── 紫卡主读提示词：中英文各一份，按会话语言选，中文版不变 ──
from core.commands.vision import VisionCommands as _VC  # noqa: E402

check("中文会话用中文主读提示词", _VC.riven_json_prompt() is _VC._RIVEN_JSON_PROMPT)
with i18n.use_lang("en"):
    _en_prompt = _VC.riven_json_prompt()
check("英文会话用英文主读提示词", _en_prompt is _VC._RIVEN_JSON_PROMPT_EN)
check(
    "英文提示词与中文版同样的输出字段",
    all(k in _en_prompt for k in ('"weapon"', '"stat_lines"')),
)
check(
    "英文提示词不含具体数值（防过拟合）",
    not re.search(r"\d", _en_prompt) and not _cjk.search(_en_prompt),
    _en_prompt,
)

# ── DE 后备（社区 warframestat）日历：转成内部结构，卡上不能出现「?」──
from core import formatters as fmt  # noqa: E402

_wfs_cal = {
    "activation": "2026-11-09T00:00:00.000Z",
    "expiry": "2026-11-16T00:00:00.000Z",
    "season": "Fall",
    "yearIteration": 24,
    "days": [
        {
            "date": "1999-10-08T00:00:00.000Z",
            "events": [
                {
                    "type": "To Do",
                    "challenge": {
                        "title": "Null and Void",
                        "description": "Kill 100 Techrot with Abilities",
                    },
                }
            ],
        },
        {
            "date": "1999-10-14T00:00:00.000Z",
            "events": [
                {
                    "type": "Big Prize!",
                    "reward": "Vosfor Cache",
                    "uniqueName": "/Lotus/Types/StoreItems/Packages/Calendar/CalendarVosforPack",
                }
            ],
        },
        {
            "date": "1999-10-20T00:00:00.000Z",
            "events": [
                {
                    "type": "Override",
                    "upgrade": {"title": "Harden Up", "description": "Overshield cap +50%."},
                }
            ],
        },
    ],
}
_cal_zh = "\n".join(fmt.fmt_calendar(W.community_calendar(_wfs_cal))[1])
check("后备日历中文：季节、类型标签正常", "秋季" in _cal_zh and "待办清单" in _cal_zh, _cal_zh)
check(
    "后备日历中文：挑战与奖励反查到官方简中",
    "化为乌有　使用技能击杀 100 名科腐者敌人" in _cal_zh and "荧尘储藏箱" in _cal_zh,
    _cal_zh,
)
check("后备日历：没有「?」占位", "· ?" not in _cal_zh, _cal_zh)
with i18n.use_lang("en"):
    _cal_en = "\n".join(fmt.fmt_calendar(W.community_calendar(_wfs_cal))[1])
check(
    "后备日历英文：全英文",
    "Null and Void" in _cal_en and "Vosfor Cache" in _cal_en and not _cjk.search(_cal_en),
    _cal_en,
)

# ---- 社区整包：警报 / 入侵 / 每日特惠 / 周期转成 DE 解析结构 ----
_node = lambda v: W._node_name("SolNode64") if v == "X" else v  # noqa: E731
_c_alert = [
    {
        "id": "a1",
        "activation": "2026-11-02T19:00:00.000Z",
        "expiry": "2026-11-16T19:00:00.000Z",
        "mission": {
            "node": "X",
            "type": "Defense",
            "faction": "Grineer",
            "minEnemyLevel": 20,
            "maxEnemyLevel": 30,
            "reward": {
                "credits": 10000,
                "countedItems": [{"uniqueName": "/Lotus/Types/Items/MiscItems/Forma", "count": 3}],
            },
        },
    }
]
_a = W.community_alerts(_c_alert, _node)[0]["mission"]
check(
    "社区警报：等级与奖励路径/数量转成 DE 结构",
    (_a["min_level"], _a["max_level"]) == (20, 30)
    and _a["reward"]["items"] == ["/Lotus/Types/Items/MiscItems/Forma"]
    and _a["reward"]["counts"] == [3],
    _a,
)
_c_inv = [
    {
        "id": "i1",
        "node": "X",
        "completed": False,
        "attacker": {"faction": "Infested", "factionKey": "Infested"},
        "defender": {
            "faction": "Corpus",
            "factionKey": "Corpus",
            "reward": {
                "credits": 0,
                "countedItems": [
                    {"uniqueName": "/Lotus/Types/Items/Research/EnergyComponent", "count": 3}
                ],
            },
        },
        "count": -15000,
        "requiredRuns": 30000,
    },
    {"id": "i2", "node": "X", "completed": True, "attacker": {}, "defender": {}, "count": 0},
]
_inv = W.community_invasions(_c_inv, _node)
check(
    "社区入侵：已完成的剔除，进度与 DE 口径一致",
    len(_inv) == 1 and round(_inv[0]["completion"]) == 50,
    _inv,
)
_inv_zh = "\n".join(fmt.fmt_invasions(_inv)[1])
check("社区入侵中文：奖励为官方简中名", "电磁力场装置 ×3" in _inv_zh, _inv_zh)
with i18n.use_lang("en"):
    _inv_en = "\n".join(fmt.fmt_invasions(W.community_invasions(_c_inv, _node))[1])
check(
    "社区入侵英文：奖励为官方英文名", "Fieldron ×3" in _inv_en and not _cjk.search(_inv_en), _inv_en
)
_deal = [
    {
        "item": "Dual Heat Swords",
        "uniqueName": "/Lotus/StoreItems/Weapons/Tenno/Melee/DualShortSword/DualHeatSwords",
    }
]
check(
    "社区每日特惠中文：商品名官方简中",
    W.community_daily_deals(_deal)[0]["item"] == "烈焰双剑",
    W.community_daily_deals(_deal),
)
with i18n.use_lang("en"):
    check(
        "社区每日特惠英文：商品名官方英文",
        W.community_daily_deals(_deal)[0]["item"] == "Dual Heat Swords",
    )
_dv = W.community_cycle("duviri", {"state": "anger", "expiry": "2026-11-09T04:00:00.000Z"})
check(
    "社区双衍王境周期：状态与 DE 路径同标签",
    (_dv["state"], _dv["stateCn"]) == ("Anger", "愤怒"),
    _dv,
)
_zr = W.community_cycle("zariman", {"state": "corpus"})
check("社区扎里曼周期：派系标签", (_zr["state"], _zr["stateCn"]) == ("corpus", "Corpus"), _zr)

# ---- 社区整包 2b：武形秘仪 / 商城礼包 / 沉沦之地 / 科研 ----
_cv = [
    {
        "id": "c1",
        "expiry": "2026-11-13T02:05:03.195Z",
        "amount": 6,
        "mode": "Any Mode",
        "categoryKey": "week",
        "description": "Win 6 matches of any Game Type",
        "title": "Match Won",
    },
    {
        "id": "c2",
        "expiry": "2026-11-09T05:10:03.185Z",
        "amount": 3,
        "mode": "Lunaro",
        "categoryKey": "day",
        "description": "Steal the ball 3 times",
        "title": "Snatch and Grab",
    },
]
_cv_out = W.community_conclave(_cv)
_cv_zh = "\n".join(fmt.fmt_conclave(_cv_out)[1])
check(
    "社区武形秘仪：模式官方简中、说明保留英文且不出现 None",
    "全部模式　Win 6 matches" in _cv_zh and "月动球" in _cv_zh and "None" not in _cv_zh,
    _cv_zh,
)
check("社区武形秘仪：次数已在说明里就不再追加 ×N", "×" not in _cv_zh.split("※")[0], _cv_zh)
with i18n.use_lang("en"):
    _cv_en = "\n".join(fmt.fmt_conclave(W.community_conclave(_cv))[1])
check("社区武形秘仪英文：模式为英文名", "All Modes　Win 6" in _cv_en and "Lunaro" in _cv_en, _cv_en)
_cv_g = W.community_conclave(
    [
        {
            "mode": "Annihilation",
            "categoryKey": "day",
            "amount": 3,
            "title": "P V P Timed Challenge Kills Streak Domination_ M E D I U M",
            "description": "P V P Timed Challenge Kills Streak Domination_ M E D I U M",
        }
    ]
)
check("社区武形秘仪：被拆字母的标题还原成官方简中", _cv_g[0]["text"] == "以连杀压制对手", _cv_g)

_fs = [
    {
        "item": "x",
        "uniqueName": "/Lotus/Types/StoreItems/Packages/TnDeimosSupporterArmourBundle",
        "activation": "2024-03-01T16:00:00.000Z",
        "expiry": "2030-01-13T23:15:00.000Z",
        "isShownInMarket": True,
    },
    {
        "item": "hidden",
        "uniqueName": "/Lotus/Types/StoreItems/Packages/Hidden",
        "activation": "2024-03-01T16:00:00.000Z",
        "expiry": "2030-01-13T23:15:00.000Z",
    },
]
_fs_out = W.community_flash_sales(_fs, 1794000000000)
check(
    "社区商城礼包：只收游戏内商店展示的，带起止时间",
    len(_fs_out) == 1 and _fs_out[0]["end"].startswith("2030-01-13") and _fs_out[0]["name"],
    _fs_out,
)
_fs_card = "\n".join(fmt.fmt_flash_sales(_fs_out)[1])
check(
    "社区商城礼包：卡面无 None 且期限不为空",
    "None" not in _fs_card and "长期在售" in _fs_card,
    _fs_card,
)

_ds = {
    "activation": "2026-11-09T00:00:00.000Z",
    "expiry": "2099-11-16T00:00:00.000Z",
    "challenges": [
        {"index": 1, "type": "D T_ R A C E", "typeKey": "DT_RACE", "challengeKey": "BasicRace"},
        {"index": 2, "type": "x", "typeKey": "DT_CAPTURE", "challengeKey": "SecuritySpin"},
    ],
}
_ds_out = W.community_descendia(_ds, 1794000000000)
_ds_card = "\n".join(fmt.fmt_descendia(_ds_out)[1])
check(
    "社区沉沦之地：类型代码规整，卡面无拆字母",
    _ds_out["challenges"][0]["Type"] == "DT_RACE" and "D T_" not in _ds_card,
    _ds_card,
)

_ar_row = lambda kind: {  # noqa: E731
    "id": "a1",
    "activation": "2026-11-09T00:00:00.000Z",
    "expiry": "2099-11-16T00:00:00.000Z",
    "type": kind,
    "typeKey": kind,
    "missions": [
        {
            "faction": "Man in the Wall",
            "factionKey": "Man in the Wall",
            "missionType": "Survival",
            "missionTypeKey": "Survival",
            "deviation": {"key": "UnpoweredCapsules", "name": "Parasitic Towers"},
            "risks": [
                {"key": "DrainingResiduals", "isHard": False},
                {"key": "RegeneratingEnemies", "isHard": True},
            ],
        }
    ],
    "personalModifiers": [{"key": "ShieldDelay", "name": "Lethargic Shields"}],
}
_ar = W.community_archimedeas([_ar_row("C T_ L A B"), _ar_row("C T_ H E X")])
check(
    "社区科研：按类型拆成深层 / 时光两项，难度分档",
    set(_ar) == {"deepArchimedea", "temporalArchimedea"}
    and [len(d["risks"]) for d in _ar["deepArchimedea"]["missions"][0]["difficulties"]] == [1, 2]
    and _ar["deepArchimedea"]["kind"] == "CT_LAB"
    and _ar["temporalArchimedea"]["kind"] == "CT_HEX",
    _ar,
)
_ar_card = "\n".join(fmt.fmt_archimedea(_ar["deepArchimedea"], "Deep")[1])
check(
    "社区科研中文：偏差为官方简中、卡面不是「暂未开放」",
    "暂未开放" not in _ar_card and "Parasitic" not in _ar_card,
    _ar_card,
)

if FAILED:
    print(f"\n{len(FAILED)} 项失败")
    sys.exit(1)
print("\n全部通过")
