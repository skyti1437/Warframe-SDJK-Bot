# -*- coding: utf-8 -*-
"""紫卡主读、词条窄读与识卡渠道的离线隔离回归。

保留旧窄读配置链断言，并经真实识别入口与本地 text_chat 桩核对实际收图方：
默认兼容、专用项双向隔离、无效主读不回落、窄读独立或回落紫卡主读。
"""

from __future__ import annotations

import asyncio
import json
import types
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.commands.scan import ScanCommands  # noqa: E402
from core.commands.vision import VisionCommands  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class _Prov:
    """假 vision 渠道：只用于 id 与身份判定。"""

    def __init__(self, pid: str):
        self._pid = pid

    def meta(self):
        class _M:
            id = self._pid

        return _M()


class _Ctx:
    def __init__(self, provs: dict[str, _Prov]):
        self._provs = provs

    def get_provider_by_id(self, pid: str):
        return self._provs.get(pid)

    def get_all_providers(self):
        return list(self._provs.values())


class _Cfg(dict):
    """可编程配置：boom_keys 里的键读取即抛（模拟配置读取异常）。"""

    def __init__(self, *a, boom_keys=(), **k):
        super().__init__(*a, **k)
        self._boom = set(boom_keys)

    def get(self, key, default=None):
        if key in self._boom:
            raise RuntimeError(f"cfg boom: {key}")
        return super().get(key, default)


MAIN = _Prov("main/json-ok")
LINE = _Prov("line/only")


def mk(cfg: dict, provs: dict[str, _Prov]) -> VisionCommands:
    inst = VisionCommands.__new__(VisionCommands)
    inst.cfg = _Cfg(cfg, boom_keys=cfg.pop("__boom__", []) if "__boom__" in cfg else ())
    inst.context = _Ctx(provs)
    return inst


# ---------------------------------------------------------------- ① 未配置
inst = mk({"vision_provider_id": "main/json-ok"}, {"main/json-ok": MAIN})
main_list = inst._vision_providers()
line_list = inst._line_vision_providers()
check(
    "① 未配置 vision_provider_id_line ⇒ 窄读路与主渠道同源",
    line_list == main_list,
    f"main={[p.meta().id for p in main_list]} line={[p.meta().id for p in line_list]}",
)

# ------------------------------------------------------------ ② 配置且可用
inst = mk(
    {"vision_provider_id": "main/json-ok", "vision_provider_id_line": "line/only"},
    {"main/json-ok": MAIN, "line/only": LINE},
)
main_list = inst._vision_providers()
line_list = inst._line_vision_providers()
check(
    "② 配置了窄读渠道 ⇒ 窄读路只用它",
    line_list == [LINE],
    f"line={[p.meta().id for p in line_list]}",
)
check(
    "② 主渠道不受窄读配置影响",
    main_list and main_list[0] is MAIN,
    f"main={[p.meta().id for p in main_list]}",
)

# ------------------------------------------------------- ③ 配置了但取不到
inst = mk(
    {"vision_provider_id": "main/json-ok", "vision_provider_id_line": "ghost/nope"},
    {"main/json-ok": MAIN},
)
line_list = inst._line_vision_providers()
check(
    "③ 窄读渠道取不到 ⇒ 回落主渠道（不抛）",
    line_list and line_list[0] is MAIN,
    f"line={[p.meta().id for p in line_list]}",
)

# --------------------------------------------------------- ④ 配置读取抛错
inst = mk(
    {"__boom__": ["vision_provider_id_line"], "vision_provider_id": "main/json-ok"},
    {"main/json-ok": MAIN},
)
try:
    line_list = inst._line_vision_providers()
    ok = line_list and line_list[0] is MAIN
    detail = f"line={[p.meta().id for p in line_list]}"
except Exception as exc:  # noqa: BLE001
    ok, detail = False, f"抛了 {type(exc).__name__}: {exc}"
check("④ 配置读取抛错 ⇒ 当空串、回落主渠道、不炸", ok, detail)

# ------------------------------------------------------------ ⑤ 端到端两路
inst = mk(
    {"vision_provider_id": "main/json-ok", "vision_provider_id_line": "line/only"},
    {"main/json-ok": MAIN, "line/only": LINE},
)
seen: list[tuple[str, list[str]]] = []


async def _fake_race(
    prompt, image_url, provs, *, k=2, tag="识别", window=None, parse=None, task=None
):
    seen.append((tag, [p.meta().id for p in provs]))
    if tag == "紫卡行读":
        return {
            "lines": ["+189.9% 暴击几率", "+118% 冰冻伤害", "+142.8% 暴击伤害", "-121.6% 切割伤害"]
        }
    return {"weapon": "伯斯顿", "positive": [], "negative": []}


inst._vision_race_json = _fake_race
inst.RACE_WINDOW_S = 5.0
out = asyncio.run(inst._extract_riven_from_image("data:image/png;base64,x"))
by_tag = dict(seen)
check("⑤ 语义 JSON 路走主渠道", by_tag.get("紫卡识别") == ["main/json-ok"], f"seen={seen}")
check("⑤ 窄读路走独立渠道", by_tag.get("紫卡行读") == ["line/only"], f"seen={seen}")
check("⑤ 两路结果合并（lines 来自窄读路）", bool(out and out.get("lines")), f"out={out}")


# ------------------------------------------------ ⑥ 真实入口双向隔离（本地渠道桩）
class _RoutingHost(ScanCommands, VisionCommands):
    pass


async def _routing_case(
    label, overrides, riven_id, line_id, scan_id, *, boom_keys=(), lookup_boom=()
):
    seen = []

    class Provider(_Prov):
        async def text_chat(self, *, prompt, session_id, image_urls):
            kind = session_id.rsplit("-", 1)[0]
            seen.append((self._pid, kind, list(image_urls)))
            if kind == "sdjk-紫卡识别":
                response = '{"weapon":"伯斯顿","positive":[["暴击",100],["暴伤",80]],"negative":[["变焦",30]]}'
            elif kind == "sdjk-紫卡行读":
                response = "+100% 暴击几率\n+80% 暴击伤害\n-30% 变焦"
            elif kind == "sdjk-focus":
                if "已装备卡片" in prompt:
                    response = "黄蜂蛰刺 | 14"
                elif "赋能图标" in prompt:
                    response = "次要·无情"
                else:
                    response = '{"panel":{"crit_chance":20,"crit_damage":2,"status_chance":10}}'
            else:
                response = '{"weapon":"雷克斯","mods":[{"name":"黄蜂蛰刺","drain":14,"color":"白"}],"panel":{},"arcanes":[{"name":"次要·无情"}]}'
            return types.SimpleNamespace(completion_text=response)

    providers = {
        pid: Provider(pid) for pid in ("legacy/only", "riven/only", "scan/only", "line/only")
    }

    class Context(_Ctx):
        def get_provider_by_id(self, pid):
            if pid in lookup_boom:
                raise RuntimeError("渠道暂不可用")
            return super().get_provider_by_id(pid)

    inst = _RoutingHost()
    inst.cfg = _Cfg({"vision_provider_id": "legacy/only", **overrides}, boom_keys=boom_keys)
    inst.context = Context(providers)
    inst.RACE_WINDOW_S = 0.8
    # 图片处理不参与渠道测试；识别、评分、补读与紫卡两路竞速均走真实方法。
    inst._fit_scan_image = lambda image: image
    inst._scan_focus_image = lambda image, part: image

    async def no_pips(image):
        return []

    inst._detect_pips = no_pips
    image = "data:image/png;base64,local-routing-probe"
    out = await inst._extract_riven_from_image(image)
    expected = ([] if riven_id is None else [(riven_id, "sdjk-紫卡识别")]) + (
        [] if line_id is None else [(line_id, "sdjk-紫卡行读")]
    )
    check(
        f"⑥ {label}：紫卡真实两路仅发给各自配置渠道",
        sorted((pid, kind) for pid, kind, _ in seen) == sorted(expected)
        and (bool(out) if expected else out is None),
        repr(seen),
    )
    if riven_id is not None:
        check(f"⑥ {label}：紫卡主读返回武器名", out and out.get("weapon") == "伯斯顿", repr(out))
    if line_id is not None:
        check(
            f"⑥ {label}：窄读保留完整词条与负号",
            out and out.get("lines") == ["+100% 暴击几率", "+80% 暴击伤害", "-30% 变焦"],
            repr(out),
        )
    before = len(seen)
    scan = await inst._extract_loadout_validated(image)
    scan_seen = seen[before:]
    check(
        f"⑥ {label}：识卡真实主读和补读保持独立",
        (scan is not None and bool(scan_seen) and {pid for pid, _, _ in scan_seen} == {scan_id})
        if scan_id is not None
        else scan is None and not scan_seen,
        repr(scan_seen),
    )
    legacy = inst._vision_providers()
    check(
        f"⑥ {label}：兼容选择器不受专用项改写",
        legacy
        == (
            []
            if overrides.get("vision_provider_id") == "missing/legacy"
            else [providers["legacy/only"]]
        ),
        repr([p.meta().id for p in legacy]),
    )


_cases = [
    ("未设置专用项", {}, "legacy/only", "legacy/only", "legacy/only"),
    (
        "专用项空白",
        {
            "vision_provider_id_riven": "  ",
            "vision_provider_id_scan": "",
            "vision_provider_id_line": "",
        },
        "legacy/only",
        "legacy/only",
        "legacy/only",
    ),
    (
        "紫卡与识卡及窄读分别指定",
        {
            "vision_provider_id_riven": "riven/only",
            "vision_provider_id_scan": "scan/only",
            "vision_provider_id_line": "line/only",
        },
        "riven/only",
        "line/only",
        "scan/only",
    ),
    (
        "只指定紫卡不影响识卡",
        {"vision_provider_id_riven": "riven/only"},
        "riven/only",
        "riven/only",
        "legacy/only",
    ),
    (
        "只指定识卡不影响紫卡",
        {"vision_provider_id_scan": "scan/only"},
        "legacy/only",
        "legacy/only",
        "scan/only",
    ),
    (
        "无效紫卡主读不回落",
        {"vision_provider_id_riven": "missing/riven"},
        None,
        None,
        "legacy/only",
    ),
    (
        "无效窄读回落紫卡专用主读",
        {"vision_provider_id_riven": "riven/only", "vision_provider_id_line": "missing/line"},
        "riven/only",
        "riven/only",
        "legacy/only",
    ),
    (
        "主读无效仍可独立窄读",
        {"vision_provider_id_riven": "missing/riven", "vision_provider_id_line": "line/only"},
        None,
        "line/only",
        "legacy/only",
    ),
    (
        "识卡无效不影响紫卡",
        {"vision_provider_id_riven": "riven/only", "vision_provider_id_scan": "missing/scan"},
        "riven/only",
        "riven/only",
        None,
    ),
    (
        "兼容渠道无效仍用紫卡专用",
        {"vision_provider_id": "missing/legacy", "vision_provider_id_riven": "riven/only"},
        "riven/only",
        "riven/only",
        None,
    ),
]
for _case in _cases:
    asyncio.run(_routing_case(*_case))
asyncio.run(
    _routing_case(
        "紫卡取渠道异常不回落",
        {"vision_provider_id_riven": "riven/only"},
        None,
        None,
        "legacy/only",
        lookup_boom=("riven/only",),
    )
)
asyncio.run(
    _routing_case(
        "紫卡配置读取异常按空处理",
        {"vision_provider_id_riven": "riven/only"},
        "legacy/only",
        "legacy/only",
        "legacy/only",
        boom_keys=("vision_provider_id_riven",),
    )
)


async def _prompt_overrides_case():
    from core.commands.vision import original_vision_prompt

    class Store:
        def effective(self, pid, task, original):
            return f"自定义:{task}" if pid == "target" else original

    class Provider(_Prov):
        def meta(self):
            if self._pid == "broken":
                raise RuntimeError("无渠道元信息")
            return super().meta()

        async def text_chat(self, *, prompt, session_id, image_urls):
            seen.append(prompt)
            return types.SimpleNamespace(completion_text='{"weapon":"雷克斯","mods":[]}')

    inst = _RoutingHost()
    inst.vision_prompts = Store()
    inst.cfg = _Cfg()
    inst._scan_focus_image = lambda image, part: image
    inst.RACE_WINDOW_S = 1
    for pid in ("target", "other", "", None, "broken"):
        prov = Provider(pid)
        inst._scan_vision_providers = lambda: [prov]
        inst._riven_vision_providers = lambda: [prov]
        for task in (
            "riven_json",
            "riven_lines",
            "scan_main",
            "scan_title",
            "scan_panel",
            "scan_mods",
            "scan_arcanes",
        ):
            seen = []
            if task.startswith("riven_"):
                await inst._extract_riven_from_image("local-image")
                actual = seen[0 if task == "riven_json" else 1]
                check(f"提示覆盖 {pid!r}/{task}：两路调用数不变", len(seen) == 2)
            elif task == "scan_main":
                await inst._extract_loadout_from_image("local-image")
                actual = seen[0]
            elif task == "scan_title":
                await inst._read_weapon_title("local-image")
                actual = seen[0]
            else:
                await inst._read_scan_focus("local-image", task.removeprefix("scan_"))
                actual = seen[0]
            expected = f"自定义:{task}" if pid == "target" else original_vision_prompt(task)
            check(f"提示覆盖 {pid!r}/{task}：实际收图渠道隔离", actual == expected)
    try:
        original_vision_prompt("scan_damage_rows")
    except ValueError:
        check("旧伤害行任务不暴露", True)
    else:
        check("旧伤害行任务不暴露", False)


asyncio.run(_prompt_overrides_case())


PROMPT_INTEGRATION_REPORT = {}


async def _panel_store_runtime_case():
    import hashlib
    import tempfile

    from core.commands.vision import original_vision_prompt
    from core.vision_prompts import VisionPromptStore
    from core.webpanel import WebPanel

    seen = []

    class Provider(_Prov):
        async def text_chat(self, *, prompt, session_id, image_urls):
            seen.append((self._pid, prompt))
            return types.SimpleNamespace(
                completion_text='{"weapon":"雷克斯","mods":[{"name":"黄蜂蛰刺","drain":14}]}'
            )

    providers = {pid: Provider(pid) for pid in ("synthetic/target", "synthetic/other")}
    inst = _RoutingHost()
    inst.context = _Ctx(providers)
    task = "scan_main"
    text = "  合成组合验收：只输出原约定 JSON。\n保留换行与空白。  "
    original = original_vision_prompt(task)
    phases = []
    with tempfile.TemporaryDirectory() as folder:
        inst.vision_prompts = VisionPromptStore(Path(folder) / "vision_prompts.json")
        panel = WebPanel(inst, "synthetic_plugin")
        query = {"provider_id": "synthetic/target", "task": task}
        panel._query = lambda key, default="": query.get(key, default)
        initial = await panel.vision_prompts()
        check("组合验收：页面 GET 原文来自生产唯一真源", initial["original"] == original)

        async def post(action, revision, **extra):
            async def body():
                return {**query, "action": action, "revision": revision, **extra}

            panel._body = body
            return await panel.vision_prompt_update()

        async def observe(phase, expected):
            before = len(seen)
            for pid in providers:
                out = await inst._extract_loadout_from_image("synthetic-image", providers[pid])
                check(
                    f"组合验收 {phase}/{pid}：响应仍走原解析器",
                    out
                    and out.get("weapon") == "雷克斯"
                    and out.get("mods") == [{"name": "黄蜂蛰刺", "drain": 14}],
                )
            calls = seen[before:]
            check(
                f"组合验收 {phase}：exact UTF-8 字节与另渠道隔离",
                calls == [("synthetic/target", expected), ("synthetic/other", original)]
                and calls[0][1].encode("utf-8") == expected.encode("utf-8"),
            )
            phases.append(
                {
                    "phase": phase,
                    "target_utf8_sha256": hashlib.sha256(calls[0][1].encode()).hexdigest(),
                    "other_utf8_sha256": hashlib.sha256(calls[1][1].encode()).hexdigest(),
                    "calls": len(calls),
                    "parsed_weapon": "雷克斯",
                }
            )

        await observe("initial", original)
        saved = await post("save", initial["revision"], text=text)
        check("组合验收：保存不触发 provider", len(seen) == 2 and saved["status"] == "ok")
        await observe("saved_draft", original)
        active = await post("activate", saved["revision"])
        check("组合验收：启用不触发 provider", len(seen) == 4 and active["status"] == "ok")
        await observe("activated", text)
        reset = await post("reset", active["revision"])
        check("组合验收：恢复不触发 provider", len(seen) == 6 and reset["status"] == "ok")
        await observe("reset", original)
        after = await panel.vision_prompts()
        check(
            "组合验收：恢复后草稿保留、启用为空", after["draft"] == text and after["active"] is None
        )
    PROMPT_INTEGRATION_REPORT.update(
        task=task,
        input="synthetic-only",
        providers=list(providers),
        custom_text=text,
        phases=phases,
        failures=list(FAILED),
    )


asyncio.run(_panel_store_runtime_case())


# 日志最小化：真实竞速入口，仅用本地 provider 桩，不发请求。
async def _minimal_logs_case():
    from unittest.mock import patch
    from core.commands import vision as vision_module

    logs = []

    class Log:
        def info(self, fmt, *args, **kwargs):
            logs.append(fmt % args if args else fmt)

        warning = info

    secret = "Authorization: Bearer PRIVATE_TOKEN https://fake.invalid/?signature=SECRET"

    class Provider(_Prov):
        async def text_chat(self, **kwargs):
            if self._pid == "error":
                raise RuntimeError(secret)
            return types.SimpleNamespace(
                completion_text=json.dumps({"weapon": secret}) if self._pid == "ok" else secret
            )

    inst = mk({}, {})
    with patch.object(vision_module, "logger", Log()):
        for pid in ("ok", "invalid", "error"):
            result = await inst._vision_race_json(
                "本地测试", "local-only", [Provider(pid)], tag="回归"
            )
            check(f"日志最小化：{pid}结果保持", bool(result) == (pid == "ok"))
        inst.context.get_provider_by_id = lambda pid: (_ for _ in ()).throw(ValueError(secret))
        check("日志最小化：渠道获取失败保持回落", inst._try_get_provider("local") is None)
    joined = "\n".join(logs)
    check(
        "日志最小化：视觉日志不含凭据或模型原文",
        all(
            x not in joined
            for x in ("Authorization", "PRIVATE_TOKEN", "signature=", "SECRET", "fake.invalid")
        ),
        joined,
    )
    check(
        "日志最小化：保留错误类型与解析状态",
        all(
            x in joined
            for x in ("RuntimeError", "ValueError", "解析有效", "JSON 解析失败", "渠道 ok")
        ),
        joined,
    )


asyncio.run(_minimal_logs_case())

schema = json.loads((ROOT / "_conf_schema.json").read_text())
for _key in (
    "vision_provider_id",
    "vision_provider_id_riven",
    "vision_provider_id_line",
    "vision_provider_id_scan",
):
    check(
        f"⑦ {_key}：空默认与渠道选择器",
        schema[_key]["default"] == "" and schema[_key].get("_special") == "select_provider",
    )

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("ALL PASS")
