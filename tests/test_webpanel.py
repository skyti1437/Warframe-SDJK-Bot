# -*- coding: utf-8 -*-
"""插件页面「运维面板」（core/webpanel.py + pages/sdjk/index.html）回归测试，不联网。

覆盖：路由命名（必须以插件名开头）/ 注册容错 / 停用后拒绝 / 新鲜度判定 / 订阅打码 /
推送状态只读 / 刷新只走社区快照 / 连通性测试三项 / 页面不拿用户文本拼 HTML。
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import json
import re
import struct as _st
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import paths  # noqa: E402
from core import webpanel as W  # noqa: E402
from core.store import Subscription  # noqa: E402

PLUGIN = "astrbot_plugin_warframe_sdjkbot"
fails = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global fails
    if cond:
        print(f"[PASS] {name}")
    else:
        fails += 1
        print(f"[FAIL] {name}  {detail}")


class _Prov:
    def __init__(self, pid, reply="OK", boom=None):
        self.pid, self.reply, self.boom = pid, reply, boom
        self.calls = 0

    def meta(self):
        return type("M", (), {"id": self.pid})()

    async def text_chat(self, **kw):
        self.calls += 1
        if self.boom:
            raise self.boom
        return type("R", (), {"completion_text": self.reply})()


class _Client:
    def start_riven_rt_crawl(self, *, force=False):
        self.calls.append(("riven_rt", force))
        return "started", 5, 101

    def start_rank_crawl(self):
        self.calls.append(("wm_ranks",))
        return False, 100, 3200

    def __init__(self, flare=True, reach=True):
        self.flare_enabled = flare
        self._reach = reach
        self._flare_url = ""
        self._flare_urls = ["http://a:8191/v1", "http://b:8191/v1"]
        self.calls = []

    async def flare_reachable(self, timeout=6.0):
        if self._reach:
            self._flare_url = self._flare_urls[0]
        return self._reach

    async def refresh_valence(self, community_only=False, force=False):
        self.calls.append(("valence", community_only) + (("force",) if force else ()))
        return "not-updated 2026-10-18T00:17:04+00:00 batch=A" if force else "fresh"

    async def refresh_acrichis_week(self, community_only=False, force=False):
        self.calls.append(("acrithis", community_only) + (("force",) if force else ()))
        raise RuntimeError("boom")


class _Store:
    def __init__(self, subs):
        self._s = subs

    def all(self):
        return list(self._s)


class _Push:
    interval = 45
    _tick_no, _tick_ok, _tick_fail, _fail_streak = 3, 2, 1, 1
    last_tick_at = 1760000000.0
    last_tick_idle = False
    _pending = [{"x": 1}]
    _fail_count = {"a": 2, "b": 0}
    state_path = None
    task = None

    def next_delay(self):
        return 90.0

    async def tick(self):  # 面板绝不能调到它
        raise AssertionError("panel must not tick")


class _Plugin:
    def __init__(self, client=None, provs=None):
        self.client = client or _Client()
        self.subs = _Store([])
        self.groups = None
        self.push = _Push()
        self._provs = provs if provs is not None else [_Prov("zhipu/glm-4v-flash")]

    def _vision_providers(self):
        return list(self._provs)

    def _line_vision_providers(self):
        return list(self._provs)


def _panel(**kw):
    p = W.WebPanel(_Plugin(**kw), PLUGIN)
    return p


# ---- 1. 路由命名与注册 ----
p = _panel()
routes = p.routes()
check(
    "★ 路由都以 /<插件名>/ 开头（AstrBot 规范）",
    all(r[0].startswith(f"/{PLUGIN}/") for r in routes),
    str([r[0] for r in routes]),
)
check(
    "写操作走 POST、读走 GET",
    {r[0].rsplit("/", 1)[1]: r[2] for r in routes}
    == {
        "overview": ["GET"],
        "refresh": ["POST"],
        "pull": ["POST"],
        "crawl": ["POST"],
        "conntest": ["POST"],
        "resources": ["GET"],
        "deps_install": ["POST"],
        "font_download": ["POST"],
        "settings": ["GET"],
        "settings_save": ["POST"],
        "vision_migration_status": ["GET"],
        "vision_migration_prepare": ["POST"],
        "vision_migration_apply": ["POST"],
        "vision_migration_release": ["POST"],
        "vision_prompts": ["GET"],
        "vision_prompt_presets": ["GET"],
        "vision_compare_prepare": ["POST"],
        "vision_compare_run": ["POST"],
        "vision_compare_release": ["POST"],
        "vision_prompt_update": ["POST"],
        "riven_ocr_status": ["GET"],
        "riven_ocr_check": ["POST"],
        "riven_ocr_save": ["POST"],
        "riven_mode_switch": ["POST"],
        "riven_ocr_enable": ["POST"],
        "riven_ocr_try": ["POST"],
        "riven_ocr_install_status": ["GET"],
        "riven_ocr_install": ["POST"],
        "sub_remove": ["POST"],
        "group_push": ["POST"],
        "push_resume": ["POST"],
        "font_import_url": ["POST"],
        "font_upload": ["POST"],
        "font_select": ["POST"],
        "font_delete": ["POST"],
        "font_sample": ["GET"],
        "theme": ["GET"],
        "theme_preview": ["POST"],
        "theme_save": ["POST"],
        "theme_upload_bg": ["POST"],
        "theme_upload_logo": ["POST"],
        "theme_image_delete": ["POST"],
        "about": ["GET"],
    },
)

got = []


class _Ctx:
    def register_web_api(self, *a):
        got.append(a)


check("register 成功返回 True", p.register(_Ctx()) is True and len(got) == len(routes))
check("旧版 AstrBot（无 register_web_api）不抛错", p.register(object()) is False)


class _BadCtx:
    def register_web_api(self, *a):
        raise RuntimeError("x")


check("注册抛错只记日志、不抛", p.register(_BadCtx()) is False)

# ---- 2. 停用后拒绝 ----
p2 = _panel()
p2.close()
for name in ("overview", "refresh", "conntest"):
    r = asyncio.run(getattr(p2, name)())
    check(f"★ 停用后 {name} 一律拒绝", r.get("status") == "error", str(r))
check("停用后刷新没有外呼", p2.p.client.calls == [])

# ---- 3. 新鲜度 ----
tmp = Path(tempfile.mkdtemp(prefix="_tmp_webpanel_"))
try:
    paths.set_run_dir(tmp)
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    rot = json.loads((ROOT / "core/data/rotations.json").read_text(encoding="utf-8"))
    rot["tenet"]["valence_snapshot"] = now.isoformat()
    rot["coda"]["valence_snapshot"] = "2020-01-01T00:00:00+00:00"
    (tmp / "rotations.json").write_text(json.dumps(rot), encoding="utf-8")
    (tmp / "de").mkdir()
    (tmp / "de/acrichis_week.json").write_text(
        json.dumps({"expiry": (now - timedelta(hours=1)).isoformat()}), encoding="utf-8"
    )
    (tmp / "wm_ranks.json").write_text(
        json.dumps({"ts": (now - timedelta(hours=72)).isoformat(), "done": 1, "total": 2}),
        encoding="utf-8",
    )
    # 紫卡实时排行快照：ts 已 20 小时（>12h ⇒ warn）、覆盖率 100%、已收录 3/101
    (tmp / "riven_rt.json").write_text(
        json.dumps(
            {
                "ts": (now - timedelta(hours=20)).isoformat(),
                "rows": {"a": 1, "b": 2, "c": 3},
                "total": 101,
                "coverage": 1.0,
            }
        ),
        encoding="utf-8",
    )
    rows = {r["item"]: r for r in W.freshness(now)}
    check(
        "信条在本轮窗口内 ⇒ ok", rows["信条元素加成"]["status"] == W.OK, str(rows["信条元素加成"])
    )
    check("★ 终幕快照过旧 ⇒ stale", rows["终幕元素加成"]["status"] == W.STALE)
    check("★ 言录使货单过期 ⇒ stale（读运行期覆盖）", rows["言录使本周货单"]["status"] == W.STALE)
    check("WM 排行超过 48h ⇒ warn", rows["WM 价格排行"]["status"] == W.WARN)
    check("紫卡周报没查过 ⇒ info 不报错", rows["紫卡周报（DE 官方）"]["status"] == W.INFO)
    # ★ 2026-10-23 紫卡实时排行（指令 §A.5）：快照的新鲜度行；10-25 起只手动拉 ⇒ 陈旧也只 INFO
    check(
        "★ 紫卡 WM 现价：只手动拉，陈旧不告警（info）",
        rows["紫卡 WM 现价（手动拉取）"]["status"] == W.INFO,
    )
    check(
        "★ 紫卡实时排行：写明已收录数 / 覆盖率",
        "已收录 3/101 把" in rows["紫卡 WM 现价（手动拉取）"]["detail"]
        and "100%" in rows["紫卡 WM 现价（手动拉取）"]["detail"],
        rows["紫卡 WM 现价（手动拉取）"]["detail"],
    )
    check(
        "★ 紫卡实时排行：写明已收录数 / 覆盖率",
        "已收录 3/101 把" in rows["紫卡 WM 现价（手动拉取）"]["detail"]
        and "100%" in rows["紫卡 WM 现价（手动拉取）"]["detail"],
        rows["紫卡 WM 现价（手动拉取）"]["detail"],
    )
    # 快照还没生成（后台首轮未跑完）⇒ info，不报错、不当成过期
    (tmp / "riven_rt.json").unlink()
    _miss = {r["item"]: r for r in W.freshness(now)}["紫卡 WM 现价（手动拉取）"]
    check("紫卡实时排行：快照缺失 ⇒ info 不报错", _miss["status"] == W.INFO, str(_miss))
    check(
        "仲裁评级 / 掉落表 读到包内种子日期",
        rows["仲裁评级（社区实测）"]["snapshot"] != "" and rows["掉落表"]["snapshot"] != "",
    )
    (tmp / "rotations.json").write_text("{坏文件", encoding="utf-8")
    bad = {r["item"]: r for r in W.freshness(now)}
    check("坏文件不让整页挂（按过期报）", bad["信条元素加成"]["status"] == W.STALE)
finally:
    paths.set_run_dir(None)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)

# ---- 4. 订阅打码 ----
check("mask_id 保留首尾两位", W.mask_id("123456789") == "12*****89")
check("短号码全打码", W.mask_id("123") == "***")
check(
    "mask_umo 只打会话号",
    W.mask_umo("aiocqhttp:GroupMessage:987654321") == "aiocqhttp:GroupMessage:98*****21",
)
s1 = Subscription(
    umo="aiocqhttp:GroupMessage:987654321",
    platform="pc",
    event="仲裁",
    rule="高效",
    created_by="11223344",
)
s2 = Subscription(
    umo="aiocqhttp:GroupMessage:987654321",
    platform="pc",
    event="裂缝",
    rule="<b>x</b>",
    until=time.time() - 10,
    created_by="55667788",
)
st = _Store([s1, s2])
masked = W.subscriptions(st)
blob = json.dumps(masked, ensure_ascii=False)
check(
    "★ 默认打码：原始群号 / QQ 号不出现在返回里",
    "987654321" not in blob and "11223344" not in blob,
    blob[:200],
)
check(
    "按会话分组 + 事件计数",
    masked["total"] == 2
    and masked["events"] == {"仲裁": 1, "裂缝": 1}
    and len(masked["sessions"]) == 1,
)
check("过期订阅有标记", any(x["expired"] for x in masked["sessions"][0]["subs"]))
shown = W.subscriptions(st, reveal=True)
check("reveal=1 给原值", shown["sessions"][0]["umo"].endswith("987654321"))


class _BoomStore:
    def all(self):
        raise RuntimeError("disk")


check("订阅读取失败返回 error 字段不抛", "error" in W.subscriptions(_BoomStore()))

# ---- 5. 推送状态 ----
ps = W.push_state(_Push())
check("推送状态：守护未起 ⇒ alive=False", ps["alive"] is False)
check(
    "推送状态字段", ps["pending"] == 1 and ps["failing_sessions"] == 1 and ps["next_delay"] == 90.0
)
check(
    "★ 上次轮询取内存 last_tick_at（零订阅时基线文件不落盘，ts 会停在旧时刻）",
    ps["last_tick"] == 1760000000.0 and ps["idle"] is False,
)
check("最近一轮成败按「本轮」口径命名", ps["last_round_ok"] == 2 and ps["last_round_fail"] == 1)


# PushDaemon 真实对象：零订阅 tick 也要刷新 last_tick_at
async def _idle_tick():
    from core.push import PushDaemon

    class _S:
        def all(self):
            return []

        def expired_list(self, now):
            return []

        def gc(self, now):
            return 0

    d = PushDaemon(None, _S(), None, W.logger, interval=45)
    before = time.time()
    await d.tick()
    return d, before


d_idle, t_before = asyncio.run(_idle_tick())
check(
    "★ 零订阅 tick 也刷新 last_tick_at 并标 idle",
    (d_idle.last_tick_at or 0) >= t_before and d_idle.last_tick_idle is True,
)
check(
    "★ 返回顶层没有 data 键（bridge 会把它当信封剥掉）", "data" not in ps and "data" not in masked
)

# ---- 6. 刷新：只走社区快照，单项失败不影响另一项 ----
p6 = _panel()
r6 = asyncio.run(p6.refresh())
check(
    "★ 刷新两项都带 community_only=True（不走 FlareSolverr）",
    p6.p.client.calls == [("valence", True), ("acrithis", True)],
    str(p6.p.client.calls),
)
check(
    "单项失败写进结果、不抛",
    r6["results"]["valence"] == "fresh" and r6["results"]["acrithis"].startswith("失败"),
)
check("刷新后带回新鲜度", isinstance(r6.get("freshness"), list) and r6["freshness"])


# ---- 6¼. 立即抓排行：面板沿用保护冷却；价格排行在抓时报 running ----
async def _crawl(panel, target):
    async def body():
        return {"target": target}

    panel._body = body
    return await panel.crawl()


p6c = _panel()
r6c = asyncio.run(_crawl(p6c, "riven_rt"))
check(
    "★ 抓紫卡实时排行：不绕过冷却、带回状态与新鲜度",
    p6c.p.client.calls == [("riven_rt", False)]
    and r6c["state"] == "started"
    and (r6c["done"], r6c["total"]) == (5, 101)
    and isinstance(r6c.get("freshness"), list),
    str(r6c)[:200],
)
r6d = asyncio.run(_crawl(_panel(), "wm_ranks"))
check("抓价格排行：已在抓 ⇒ running", r6d["state"] == "running", str(r6d)[:200])
check("未知排行被拒", asyncio.run(_crawl(_panel(), "x")).get("status") == "error")

p6cool = _panel()


def _riven_cooldown(*, force=False):
    p6cool.p.client.calls.append(("riven_rt", force))
    return "cooldown", 5, 101


p6cool.p.client.start_riven_rt_crawl = _riven_cooldown
r6cool = asyncio.run(_crawl(p6cool, "riven_rt"))
check(
    "面板紫卡保护冷却明确拒绝，不误报started或running",
    p6cool.p.client.calls == [("riven_rt", False)]
    and r6cool.get("status") == "error"
    and "冷却" in r6cool.get("message", "")
    and "state" not in r6cool,
    str(r6cool),
)

p6price = _panel()


def _rank_cooldown():
    p6price.p.client.calls.append(("wm_ranks",))
    raise W.WarframeAPIError("价格排行刷新仍在保护冷却中，请稍后再试")


p6price.p.client.start_rank_crawl = _rank_cooldown
r6price = asyncio.run(_crawl(p6price, "wm_ranks"))
check(
    "面板价格排行保护冷却明确拒绝，保留后端原因",
    p6price.p.client.calls == [("wm_ranks",)]
    and r6price.get("status") == "error"
    and "冷却" in r6price.get("message", "")
    and "state" not in r6price,
    str(r6price),
)

# ---- 6½. 从源头重抓（2026-10-18）：两项都 force、不限社区快照，单项失败不连累其他项 ----
#   （紫卡倾向 2026-10 随 DE 取消倾向退出重抓）
p6b = _panel()
r6b = asyncio.run(p6b.pull())
check(
    "★ 从源头重抓：元素加成 / 言录使都带 force，且不限 community_only（不再重抓紫卡倾向）",
    p6b.p.client.calls == [("valence", False, "force"), ("acrithis", False, "force")],
    str(p6b.p.client.calls),
)
check(
    "重抓结果原样带回（含 wiki 未更新），单项失败写进结果",
    r6b["results"]["valence"].startswith("not-updated")
    and r6b["results"]["acrithis"].startswith("失败")
    and "wiki_disp" not in r6b["results"],
    str(r6b["results"]),
)
_html = (ROOT / "pages" / "sdjk" / "index.html").read_text(encoding="utf-8")
check(
    "页面有「从源头重抓」按钮并接到 pull 接口",
    'id="pull"' in _html and 'apiPost("pull"' in _html,
)
# 窄屏与出错提示（浏览器实测在仓外做；这里守住关键结构不被改回）
check(
    "报错浮在视口内且只清加载自己的报错",
    "#err { position: fixed" in _html
    and 'id="errClose"' in _html
    and 'showErr("", "load")' in _html
    and "if (errSrc === src)" in _html,
)
check(
    "多列表格窄屏改卡片（后台任务/社区/账本/登记/依赖/字体/订阅）",
    _html.count('<table class="cards">') == 7
    and 'el("table", { class: "cards" }' in _html
    and "td.dataset.label = heads[i]" in _html,
)
_int_min = re.search(r"const INT_MIN = \{(.*?)\};", _html)
check(
    "设置页前端拦截会让插件出错的数值（每页条数 / 超时 / 推送间隔下限）",
    bool(_int_min)
    and all(
        k in _int_min.group(1) for k in ("page_size: [1", "http_timeout: [1", "push_interval: [15")
    )
    # b17：设置页与识图页各有保存条，按各自范围的校验错误禁用保存按钮
    and 'settings: ["savebar", "saveInfo", "save"], vision: ["vSavebar", "vSaveInfo", "vSave"]'
    in _html
    and "$(btn).disabled = b.length > 0" in _html,
    _int_min.group(0) if _int_min else "缺 INT_MIN",
)
_schema_opts = {
    k: v["options"]
    for k, v in json.loads((ROOT / "_conf_schema.json").read_text("utf-8")).items()
    if v.get("options")
}
_opt_block = re.search(r"const OPT_LABEL = \{(.*?)\n  \};", _html, re.S)
check(
    "下拉选项中文名覆盖 schema 里全部选项",
    bool(_opt_block)
    and all(
        re.search(rf"{k}: \{{[^}}]*\b{re.escape(o)}: ", _opt_block.group(1))
        for k, opts in _schema_opts.items()
        for o in opts
    ),
    str(_schema_opts),
)


# 提示词方案只通过后台接口修改，加载/保存/启用均不触发渠道调用。
async def _vision_prompt_api():
    prov = _Prov("合成/渠道")
    panel = _panel(provs=[prov])
    panel.p.context = type("Ctx", (), {"get_all_providers": lambda self: [prov]})()
    with tempfile.TemporaryDirectory() as folder:
        from core.vision_prompts import VisionPromptStore

        panel.p.vision_prompts = VisionPromptStore(Path(folder) / "vision_prompts.json")
        query = {}
        panel._query = lambda key, default="": query.get(key, default)
        catalog = await panel.vision_prompts()
        tasks = {t["id"] for t in catalog["tasks"]}
        check(
            "提示词目录列出八个可达任务与宿主渠道，隐藏保留的伤害行方法",
            catalog["providers"] == ["合成/渠道"]
            and tasks
            == {
                "riven_json",
                "riven_lines",
                "scan_main",
                "scan_title",
                "scan_panel",
                "scan_mods",
                "scan_arcanes",
                "scan_tiles",
            }
            and all(t["label"] and t["contract"] for t in catalog["tasks"]),
        )
        query.update(provider_id="合成/渠道", task="scan_panel")
        detail = await panel.vision_prompts()
        check(
            "新安装显示生产原文、无草稿和启用副本",
            detail["original"] and detail["draft"] is None and detail["active"] is None,
        )

        async def post(action, rev, **extra):
            async def body():
                return {**query, "action": action, "revision": rev, **extra}

            panel._body = body
            return await panel.vision_prompt_update()

        draft = await post("save", detail["revision"], text="  合成草稿\n保留换行。")
        active = await post("activate", draft["revision"])
        old = await post("save", draft["revision"], text="过期页面的操作")
        inactive = await post("reset", active["revision"])
        after = await panel.vision_prompts()
        check(
            "后台草稿独立、启用指定快照；过期页面写入不得冲掉修改，恢复后保留草稿",
            draft["active"] is None
            and active["active"] == draft["draft"] == "  合成草稿\n保留换行。"
            and old.get("code") == "revision_conflict"
            and inactive["active"] is None
            and after["draft"] == draft["draft"]
            and after["revision"] == inactive["revision"],
        )
        query["task"] = "scan_damage_rows"
        check("拒绝保留任务", (await panel.vision_prompts())["status"] == "error")
        query.update(task="scan_panel", provider_id="曾加载的渠道")
        missing = await post("save", 0, text="不可生效")
        check("未加载的渠道不能保存或误启用", missing["status"] == "error")

        async def closing_body():
            panel.close()
            return {**query, "action": "reset", "revision": 0}

        panel._body = closing_body
        closed = await panel.vision_prompt_update()
        check(
            "读请求体期间停用仍拒绝写，管理过程零渠道调用",
            closed.get("status") == "error"
            and "停用" in closed.get("message", "")
            and prov.calls == 0,
        )


asyncio.run(_vision_prompt_api())


async def _vision_prompt_overview_api():
    from core.vision_prompts import VisionPromptStore

    providers = [_Prov("当前渠道"), _Prov("移除渠道")]
    panel = _panel(provs=providers)
    panel.p.context = type("Ctx", (), {"get_all_providers": lambda self: providers})()
    query = {}
    panel._query = lambda key, default="": query.get(key, default)

    async def catalog():
        query.clear()
        return await panel.vision_prompts()

    async def detail(pid, task):
        query.update(provider_id=pid, task=task)
        return await panel.vision_prompts()

    async def post(pid, task, action, revision, **extra):
        async def body():
            return dict(provider_id=pid, task=task, action=action, revision=revision, **extra)

        panel._body = body
        return await panel.vision_prompt_update()

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "未创建目录" / "prompts.json"
        store = VisionPromptStore(path)
        panel.p.vision_prompts = store
        empty = await catalog()
        check(
            "已有方案：新安装概览为空且读取不创建目录或锁文件",
            empty.get("customized") == []
            and empty.get("customized_error") is None
            and not path.parent.exists(),
        )
        states = {}
        for pid, task in [
            ("移除渠道", "scan_panel"),
            ("当前渠道", "scan_panel"),
            ("当前渠道", "scan_title"),
        ]:
            draft = await post(pid, task, "save", 0, text=f"私有正文：{pid}/{task}")
            states[pid, task] = await post(pid, task, "activate", draft["revision"])
        before = path.read_bytes()
        healthy = await catalog()
        check(
            "已有方案：健康概览只含状态字段且保留逐渠道逐任务修订号",
            healthy.get("customized_error") is None
            and len(healthy.get("customized", [])) == 3
            and all(
                set(row)
                == {
                    "provider_id",
                    "task",
                    "revision",
                    "has_draft",
                    "active",
                    "draft_matches_active",
                    "loaded",
                }
                and row["revision"] == states[row["provider_id"], row["task"]]["revision"]
                and row["has_draft"]
                and row["active"]
                and row["draft_matches_active"]
                and row["loaded"]
                for row in healthy.get("customized", [])
            )
            and path.read_bytes() == before,
        )
        removed = providers.pop()
        lost = await catalog()
        rows = {(row["provider_id"], row["task"]): row for row in lost.get("customized", [])}
        check(
            "已有方案：失联方案仍可发现，providers 只列当前加载渠道",
            lost["providers"] == ["当前渠道"]
            and len(rows) == 3
            and rows.get(("移除渠道", "scan_panel"), {}).get("loaded") is False,
        )
        lost_detail = await detail("移除渠道", "scan_panel")
        rejected_save = await post("移除渠道", "scan_panel", "save", 2, text="拒绝保存")
        rejected_active = await post("移除渠道", "scan_panel", "activate", 2)
        check(
            "已有方案：失联详情可读取，保存和启用拒绝且文件逐字节不变",
            lost_detail.get("status") == "ok"
            and lost_detail.get("loaded") is False
            and lost_detail["active"] == states["移除渠道", "scan_panel"]["active"]
            and rejected_save.get("status") == rejected_active.get("status") == "error"
            and path.read_bytes() == before,
        )
        stale = await post("移除渠道", "scan_panel", "reset", 1)
        check(
            "已有方案：失联方案过期修订号仍拒绝且不改文件",
            stale.get("code") == "revision_conflict" and path.read_bytes() == before,
        )
        reset = await post("移除渠道", "scan_panel", "reset", lost_detail["revision"])
        reset_catalog = await catalog()
        reset_rows = {
            (row["provider_id"], row["task"]): row for row in reset_catalog.get("customized", [])
        }
        row = reset_rows.get(("移除渠道", "scan_panel"), {})
        check(
            "已有方案：失联恢复原版保留草稿，其他渠道与任务不受影响",
            reset.get("status") == "ok"
            and row.get("active") is False
            and row.get("has_draft") is True
            and row.get("draft_matches_active") is False
            and row.get("loaded") is False
            and row.get("revision") == reset["revision"]
            and all(
                store.view(pid, task)
                == {key: state[key] for key in ("revision", "draft", "active")}
                for (pid, task), state in states.items()
                if pid == "当前渠道"
            ),
        )
        providers.append(removed)
        reloaded = await catalog()
        check(
            "已有方案：同ID重新加载后保持原版且管理过程零模型调用",
            store.effective("移除渠道", "scan_panel", "内置原版") == "内置原版"
            and any(
                row["provider_id"] == "移除渠道" and row["loaded"] and not row["active"]
                for row in reloaded.get("customized", [])
            )
            and sum(prov.calls for prov in providers) == 0,
        )
        path.write_bytes(b"{broken-json")
        broken = path.read_bytes()
        failed = await catalog()
        check(
            "已有方案：坏文件不伪装空列表，目录与任务仍可用且原文件不变",
            failed.get("status") == "ok"
            and failed.get("providers") == reloaded["providers"]
            and failed.get("tasks") == reloaded["tasks"]
            and failed.get("customized", "missing") is None
            and isinstance(failed.get("customized_error"), str)
            and "读取失败" in failed["customized_error"]
            and path.read_bytes() == broken,
        )
        del panel.p.vision_prompts
        unavailable = await catalog()
        check(
            "已有方案：缺失存储明确报告未就绪并保留目录",
            unavailable.get("status") == "ok"
            and unavailable.get("providers") == reloaded["providers"]
            and unavailable.get("customized", "missing") is None
            and isinstance(unavailable.get("customized_error"), str)
            and "未就绪" in unavailable["customized_error"],
        )


asyncio.run(_vision_prompt_overview_api())


# ---- 7. 连通性测试 ----
def _ct(panel, target, **extra):
    async def _b():
        return {"target": target, **extra}

    panel._body = _b
    result = asyncio.run(panel.conntest())
    if "attempted" in result:
        _check_call_audit(result)
    return result


def _check_call_audit(result):
    check(
        "渠道调用统计明确单位与 HTTP 次数未知边界",
        result["attempted_unit"] == "provider_text_chat_call"
        and result["sent_semantics"] == "provider_method_invoked"
        and (
            result["actual_http_requests"] is None
            if result["attempted"] > 0
            else result["actual_http_requests"] == 0
        )
        and result["sent"] == (result["attempted"] > 0)
        and "SDK 内部重试" in result["notice"]
        and "不代表实际 HTTP 请求次数或费用硬上限" in result["notice"],
        str(result),
    )


check("未知测试项被拒", _ct(_panel(), "rm -rf").get("status") == "error")
r = _ct(_panel(), "flare")
check("FlareSolverr 可连", r["ok"] and "http://a:8191/v1" in r["detail"], str(r))
r = _ct(_panel(client=_Client(reach=False)), "flare")
check("FlareSolverr 不可连列出候选", not r["ok"] and "http://b:8191/v1" in r["detail"])
r = _ct(_panel(client=_Client(flare=False)), "flare")
check("FlareSolverr 关闭时说明原因", not r["ok"] and "关闭" in r["detail"])
prov = _Prov("siliconflow/Qwen/Qwen3-VL-8B-Instruct")
pv = _panel(provs=[prov])
r = _ct(pv, "vision")
check("★ 只解析渠道：列出 id 且不调用模型", r["ok"] and prov.pid in r["detail"] and prov.calls == 0)
r = _ct(_panel(provs=[]), "vision")
check("没有渠道时给配置提示", not r["ok"] and "vision_provider_id" in r["detail"])
r = _ct(pv, "vision_live")
check("真调一次：调用首选渠道 1 次并回显", r["ok"] and prov.calls == 1 and "OK" in r["detail"])
r = _ct(_panel(provs=[_Prov("x", boom=RuntimeError("401"))]), "vision_live")
check("真调失败给原因", not r["ok"] and "401" in r["detail"])
bad = _Prov("qwen-vl-ocr", boom=RuntimeError("image must be larger than 10"))
good, never = _Prov("glm-4v-flash"), _Prov("third")
r = _ct(_panel(provs=[bad, good, never]), "vision_live")
check(
    "★ 首个渠道拒图 ⇒ 回落下一个、成功即停，并列出先试的失败",
    r["ok"] and good.calls == 1 and never.calls == 0 and "qwen-vl-ocr" in r["detail"],
    str(r),
)
many = [_Prov(f"p{i}", boom=RuntimeError("x")) for i in range(5)]
_ct(_panel(provs=many), "vision_live")
check(
    "真调最多调用 3 次渠道方法（不统计 SDK 内部重试）", [m.calls for m in many] == [1, 1, 1, 0, 0]
)
# 使用生产 selector，fake 仅替代 AstrBot 渠道访问与模型调用。
from core.commands.scan import ScanCommands as _ScanCommands  # noqa: E402
from core.commands.vision import VisionCommands as _VisionCommands  # noqa: E402


class _RouteContext:
    def __init__(self, providers):
        self.providers = providers
        self.lookups = []

    def get_provider_by_id(self, pid):
        self.lookups.append(pid)
        return self.providers.get(pid)

    def get_all_providers(self):
        return list(self.providers.values())


class _RoutePlugin(_VisionCommands, _ScanCommands, _Plugin):
    def __init__(self, config=None, providers=None):
        _Plugin.__init__(self)
        self.cfg = config or {}
        self.context = _RouteContext(providers or {})
        self._VISION_PROVIDER_IDS = tuple(self.context.providers)


def _route_panel(config=None, providers=None):
    return W.WebPanel(_RoutePlugin(config, providers), PLUGIN)


def _route_rows(panel):
    result = _ct(panel, "vision_routes")
    check("任务解析零模型请求标记", result["attempted"] == 0 and not result["sent"])
    return {row["route"]: row for row in result["routes"]}


_route_cfg = {
    "vision_provider_id": "compat-id",
    "vision_provider_id_riven": "riven-id",
    "vision_provider_id_line": "line-id",
    "vision_provider_id_scan": "scan-id",
    "vision_provider_id_scan_equipment": "equipment-id",
    "vision_provider_id_line_recheck": "recheck-id",
}
_route_providers = {pid: _Prov(pid) for pid in _route_cfg.values()}
_route_p = _route_panel(_route_cfg, _route_providers)
_route_info = _route_rows(_route_p)
check(
    "五角色解析沿真实 selector 隔离且不调模型",
    all(
        row["provider_ids"] == [_route_cfg[row["config_key"]]]
        and row["source_mode"] == "explicit"
        and row["available"]
        for row in _route_info.values()
    )
    and all(p.calls == 0 for p in _route_providers.values()),
)
for _route in _route_info:
    _before = {pid: p.calls for pid, p in _route_providers.items()}
    _res = _ct(_route_p, "vision_live", route=_route)
    _selected = _route_cfg[_route_info[_route]["config_key"]]
    check(
        f"真调 {_route} 只向该任务显式渠道发送",
        _res["ok"]
        and _res["route"] == _route
        and _res["attempted"] == 1
        and _res["sent"]
        and _res["test_mode"] == "sequential_connectivity"
        and all(
            p.calls - _before[pid] == (1 if pid == _selected else 0)
            for pid, p in _route_providers.items()
        ),
        str(_res),
    )

# ---- 原子实际生效模式：生产 selector、真实入口判定与零外呼假渠道 ----
from core.commands.riven import RivenCommands as _RivenCommands, riven_read_selection  # noqa: E402


class _SnapshotLocal:
    def __init__(self, status=None, boom=False):
        self.value = status if status is not None else {"ok": True, "enabled": False, "revision": 1}
        self.boom, self.reads, self.recognitions = boom, 0, 0

    def status(self):
        self.reads += 1
        if self.boom:
            raise RuntimeError("/private/secret-model-dir sk-sensitive-endpoint")
        return self.value

    async def recognize(self, image):
        self.recognitions += 1
        return {"ok": False, "state": "fixture_stop"}


class _SnapshotProvider(_Prov):
    def __init__(self, pid):
        super().__init__(pid)
        self.client = self.chat = self.completions = self

    def _forbidden(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("只读快照不得调用模型、OCR 或 SDK")

    get_model = with_options = create = text_chat = _forbidden


def _snapshot_panel(flag=False, local=None, **extra):
    config = {
        "riven_single_ocr": flag,
        "vision_routes_isolated": True,
        "vision_provider_id_riven": "snapshot-riven",
        **extra,
    }
    providers = {
        "snapshot-riven": _SnapshotProvider("snapshot-riven"),
        "next-riven": _SnapshotProvider("next-riven"),
    }
    panel = _route_panel(config, providers)
    if local is not None:
        panel.p.local_ocr = local
    return panel


def _mode_snapshot(panel):
    return _ct(panel, "vision_routes")["riven_effective_mode"]


def _test_riven_mode_switch():
    from core.local_ocr import MODEL_HASHES, LocalRivenOCR

    class Config(dict):
        fail_once = False
        writes = 0

        def save_config(self):
            self.writes += 1
            conf_path.write_text(json.dumps(self), encoding="utf-8")
            if self.fail_once:
                self.fail_once = False
                raise OSError("fixture failure after writing")

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        conf_path = folder / "plugin.json"
        models = folder / "models"
        models.mkdir()
        for name in MODEL_HASHES:
            (models / name).touch()
        local = LocalRivenOCR(folder, ROOT)
        local._write(
            dict(
                enabled=False,
                engine="rapidocr",
                python=sys.executable,
                model_dir=str(models),
                revision=1,
                check_ok=True,
            )
        )
        panel = _snapshot_panel(False, local)
        conf = panel.p._config = Config(panel.p.cfg)
        conf.save_config()

        def request(mode, *, revision=None, confirmed=True):
            async def run():
                async def body():
                    return {"mode": mode, "revision": revision, "confirmed": confirmed}

                panel._body = body
                return await panel.riven_mode_switch()

            if revision is None:
                revision = _mode_snapshot(panel)["revision"]
            return asyncio.run(run())

        def state():
            return dict(panel.p.cfg), dict(conf), conf_path.read_bytes(), local.path.read_bytes()

        before = state()
        check(
            "一步切换需要明确确认",
            not request("single", confirmed=False)["ok"] and state() == before,
        )
        check(
            "一步切换拒绝旧修订",
            not request("single", revision="0" * 64)["ok"] and state() == before,
        )
        check("一步切换拒绝未知模式", not request("other")["ok"] and state() == before)

        conf.fail_once = True
        check("一步切换宿主写入后报错仍恢复配置", not request("single")["ok"] and state() == before)

        reply = request("single")
        check(
            "一步切换双路到单模型即时生效并持久化",
            reply["ok"]
            and _mode_snapshot(panel)["mode"] == "single"
            and json.loads(conf_path.read_text())["riven_single_ocr"] is True,
        )
        writes = conf.writes
        reply = request("local")
        check(
            "一步切换本地模式保留云偏好与渠道",
            reply["ok"]
            and _mode_snapshot(panel)["mode"] == "local"
            and local._read()["enabled"]
            and conf.writes == writes
            and conf["riven_single_ocr"] is True,
        )
        before = state()
        revision = _mode_snapshot(panel)["revision"]
        panel.p.cfg["vision_provider_id_riven"] = "next-riven"
        conf["vision_provider_id_riven"] = "next-riven"
        conf.save_config()
        changed = state()
        check(
            "一步切换本地模式下修改目标渠道也使旧确认失效",
            request("dual", revision=revision).get("code") == "revision_conflict"
            and state() == changed,
        )
        panel.p.cfg["vision_provider_id_riven"] = conf["vision_provider_id_riven"] = (
            "snapshot-riven"
        )
        conf.save_config()
        write = local._write

        def bad_write(data):
            raise OSError("fixture OCR write failure")

        local._write = bad_write
        try:
            check(
                "一步切换第二次落盘失败恢复两份配置",
                not request("dual")["ok"] and state() == before,
            )
        finally:
            local._write = write

        local._busy = True
        check("一步切换拒绝忙碌本地 OCR", not request("dual")["ok"] and state() == before)
        local._busy = False
        conf["vision_provider_id_riven"] = "next-riven"
        conf.save_config()
        changed = state()
        check(
            "一步切换拒绝已保存与运行配置不一致",
            request("dual").get("code") == "revision_conflict" and state() == changed,
        )
        conf["vision_provider_id_riven"] = "snapshot-riven"
        conf.save_config()
        saved_providers = dict(panel.p.context.providers)
        panel.p.context.providers.clear()
        check("一步切换双路无可用渠道则保持本地", not request("dual")["ok"] and state() == before)
        panel.p.context.providers.update(saved_providers)
        reply = request("dual")
        check(
            "一步切换本地到双路同步保存并停用本地",
            reply["ok"]
            and _mode_snapshot(panel)["mode"] == "dual"
            and not local._read()["enabled"]
            and json.loads(conf_path.read_text())["riven_single_ocr"] is False,
        )
        local._write({**local._read(), "check_ok": False})
        before = state()
        check("一步切换拒绝未通过本地配置检查", not request("local")["ok"] and state() == before)
        check("一步切换全程零模型调用", all(p.calls == 0 for p in saved_providers.values()))


_test_riven_mode_switch()


for _flag, _local, _expected in (
    (False, None, "dual"),
    (True, None, "single"),
    (False, _SnapshotLocal(), "dual"),
    (True, _SnapshotLocal(), "single"),
    (False, _SnapshotLocal({"ok": True, "enabled": True, "revision": 2}), "local"),
    (True, _SnapshotLocal({"ok": True, "enabled": True, "revision": 2}), "local"),
    (True, _SnapshotLocal({"ok": False, "enabled": False, "revision": 0}), "local"),
):
    _sp = _snapshot_panel(_flag, _local)
    _ss = _mode_snapshot(_sp)
    check(
        f"模式快照：{_flag}/{_local is not None} 按入口选 {_expected}",
        _ss["mode"] == _expected
        and bool(re.fullmatch(r"[0-9a-f]{64}", _ss["revision"]))
        and _ss["basis"]["local_manager"] == (_local is not None)
        and (_local is None or _local.reads == 1)
        and all(p.calls == 0 for p in _sp.p.context.providers.values()),
        str(_ss),
    )

# 逐值对照原 _use_single 与 extract 的表达式，不把真值判断偷换成严格 bool。
_parity = True
for _flag in (False, True, None, 0, 1, "true"):
    for _ok in (True, False, None, 1):
        for _enabled in (False, True, None, 0, 1, "", "false"):
            _status = {"ok": _ok, "enabled": _enabled}
            _single = _flag is True and _ok is True and _enabled is False
            _expected = (
                "single" if _single else ("local" if _enabled or _ok is not True else "dual")
            )
            _host = type("SnapshotRuntime", (), {})()
            _host.cfg, _host.local_ocr = {"riven_single_ocr": _flag}, _SnapshotLocal(_status)
            _actual = _RivenCommands._use_single_riven_ocr(_host)
            _parity &= (
                _actual == _single
                and riven_read_selection(_flag, _status) == _expected
                and _host.local_ocr.reads == (1 if _flag is True else 0)
            )
check("模式快照：共享纯判定逐值保持真实入口短路与选路", _parity)

# 真实 extract 本地分支只拿本地失败结果返回，禁止落进云端 selector。
_local_stop = _SnapshotLocal({"ok": False, "enabled": False, "revision": 0})
_real_local_host = _snapshot_panel(True, _local_stop).p
_real_local_host._riven_vision_providers = lambda: (_ for _ in ()).throw(
    AssertionError("禁止云端回落")
)
_local_result = asyncio.run(_real_local_host._extract_riven_from_image(W._PING_PNG))
check(
    "模式快照：损坏状态真实extract只本地停止，零模型",
    _local_result.get("_local_ocr", {}).get("state") == "fixture_stop"
    and _local_stop.recognitions == 1
    and _mode_snapshot(_snapshot_panel(True, _SnapshotLocal({"ok": False, "enabled": False})))[
        "blocked"
    ],
)

for _bad in (None, [], "not-a-state", 42):
    _local = _SnapshotLocal()
    _local.value = _bad
    _ss = _mode_snapshot(_snapshot_panel(True, _local))
    check(
        "模式快照：非结构本地状态未知而不猜云端 " + repr(_bad),
        _ss["mode"] == "unknown"
        and _ss["revision"] == ""
        and _ss["blocked"]
        and _ss["basis"]["local_state"] == "unavailable"
        and _local.reads == 1,
    )
_broken_local = _SnapshotLocal(boom=True)
_broken_result = _ct(_snapshot_panel(True, _broken_local), "vision_routes")
check(
    "模式快照：status异常未知，原文路径密钥不泄漏且只读一次",
    _broken_result["riven_effective_mode"]["mode"] == "unknown"
    and _broken_result["riven_effective_mode"]["blocked"]
    and _broken_local.reads == 1
    and "secret-model-dir" not in json.dumps(_broken_result)
    and "sk-sensitive" not in json.dumps(_broken_result)
    and all(
        not row.get("runtime_active", True)
        for row in _broken_result["routes"]
        if row["route"] in {"riven", "line", "line_recheck"}
    ),
)

_stable_local = _SnapshotLocal(
    {
        "ok": True,
        "enabled": False,
        "revision": 7,
        "busy": False,
        "model_dir": "/private/secret-model-dir",
    }
)
_stable_panel = _snapshot_panel(
    True, _stable_local, api_key="sk-hidden", endpoint="https://secret-api"
)
_first = _mode_snapshot(_stable_panel)
_stable_local.value.update(busy=True, checked_at="2099-01-01", message="sk-message")
_stable_panel.p.cfg.update(api_key="sk-another", endpoint="https://new-secret-api")
_stable_panel.p.edits = {"riven_single_ocr": False, "local_enabled": True}
_stable_panel.p.orig = {"riven_single_ocr": False}
_repeat = _mode_snapshot(_stable_panel)
check(
    "模式快照：忙碌/时间/凭据/端点/草稿不改变模式修订且不泄漏",
    _first == _repeat
    and not any(
        token in json.dumps(_repeat)
        for token in ("secret-model-dir", "sk-", "secret-api", "checked_at", "model_dir")
    ),
)
_stable_local.value["revision"] += 1
_local_rev = _mode_snapshot(_stable_panel)
_stable_panel.p.cfg["vision_provider_id_riven"] = "next-riven"
_config_rev = _mode_snapshot(_stable_panel)
_stable_panel.p.cfg["vision_routes_isolated"] = False
_isolated_rev = _mode_snapshot(_stable_panel)
check(
    "模式快照：本地修订、所用渠道配置与隔离标记各自改变修订",
    len(
        {
            _first["revision"],
            _local_rev["revision"],
            _config_rev["revision"],
            _isolated_rev["revision"],
        }
    )
    == 4,
)
_stable_panel.p.context.providers.clear()
_missing_channel = _mode_snapshot(_stable_panel)
check(
    "模式快照：渠道缺失只阻塞、不改选择与修订",
    _missing_channel["mode"] == "single"
    and _missing_channel["blocked"]
    and _missing_channel["revision"] == _isolated_rev["revision"],
)

_empty = _snapshot_panel(True, vision_provider_id_riven="", vision_provider_id="snapshot-riven")
_empty_mode = _mode_snapshot(_empty)
check(
    "模式快照：单模型留空不借兼容渠道，保持single并阻塞",
    _empty_mode["mode"] == "single" and _empty_mode["blocked"],
)
_empty.p.cfg["riven_single_ocr"] = False
_empty_routes = _ct(_empty, "vision_routes")
check(
    "模式快照：隔离双路主读留空不乱继承",
    _empty_routes["riven_effective_mode"]["mode"] == "dual"
    and _empty_routes["riven_effective_mode"]["blocked"]
    and not next(row for row in _empty_routes["routes"] if row["route"] == "riven")["available"],
)

_closed_local = _SnapshotLocal({"ok": True, "enabled": True, "revision": 3, "closed": True})
_closed_mode = _mode_snapshot(_snapshot_panel(True, _closed_local))
check(
    "模式快照：本地服务关闭仍local并标阻塞",
    _closed_mode["mode"] == "local" and _closed_mode["blocked"],
)
_closed_panel = _snapshot_panel(True, _SnapshotLocal())
_closed_panel.close()
_closed_before = _closed_panel.p.local_ocr.reads
_closed_result = _ct(_closed_panel, "vision_routes")
check(
    "模式快照：面板已关闭拒绝读取且零模型",
    _closed_result.get("status") == "error"
    and _closed_panel.p.local_ocr.reads == _closed_before
    and all(p.calls == 0 for p in _closed_panel.p.context.providers.values()),
)

_changing = _snapshot_panel(True, _SnapshotLocal())
_get_provider = _changing.p.context.get_provider_by_id


def _change_during_lookup(pid):
    _changing.p.cfg.update(riven_single_ocr=False, vision_provider_id_riven="next-riven")
    return _get_provider(pid)


_changing.p.context.get_provider_by_id = _change_during_lookup
_changed_response = _ct(_changing, "vision_routes")
_changed_mode = _changed_response["riven_effective_mode"]
check(
    "模式快照：读途中配置改变返回unknown，路由仍来自冻结配置且status仅一次",
    _changed_mode["mode"] == "unknown"
    and _changed_mode["revision"] == ""
    and _changed_mode["blocked"]
    and _changing.p.local_ocr.reads == 1
    and next(row for row in _changed_response["routes"] if row["route"] == "riven")["configured_id"]
    == "snapshot-riven"
    and all(p.calls == 0 for p in _changing.p.context.providers.values()),
)


_invalid_route = _snapshot_panel(
    True, vision_provider_id_riven={"key": "sk-nested-secret", "path": "/private/local-secret"}
)
_invalid_result = _ct(_invalid_route, "vision_routes")
check(
    "模式快照：损坏渠道字段不公开嵌套密钥或路径，不猜其它模式",
    _invalid_result["riven_effective_mode"]["mode"] == "single"
    and _invalid_result["riven_effective_mode"]["blocked"]
    and "sk-nested-secret" not in json.dumps(_invalid_result)
    and "/private/local-secret" not in json.dumps(_invalid_result)
    and all(p.calls == 0 for p in _invalid_route.p.context.providers.values()),
)


_partial_dual = _snapshot_panel(
    False, vision_provider_id_riven="missing-riven", vision_provider_id_line="snapshot-riven"
)
_partial_mode = _mode_snapshot(_partial_dual)
_bad_partial = _snapshot_panel(
    False,
    vision_routes_isolated=False,
    vision_provider_id_riven={"key": "sk-bad-partial"},
    vision_provider_id_line="snapshot-riven",
)
_bad_partial_mode = _mode_snapshot(_bad_partial)
check(
    "模式快照：双路只缺主读而窄读可用时保持dual、不宣称必停止",
    _partial_mode["mode"] == _bad_partial_mode["mode"] == "dual"
    and not _partial_mode["blocked"]
    and not _bad_partial_mode["blocked"],
)
_bad_state = _mode_snapshot(_snapshot_panel(True, _SnapshotLocal({"ok": True, "enabled": None})))
_bad_flag = _mode_snapshot(_snapshot_panel("true"))
check(
    "模式快照：异常本地字段或云端开关未知，不猜能否停止或回退",
    _bad_state["mode"] == _bad_flag["mode"] == "unknown"
    and _bad_state["revision"] == _bad_flag["revision"] == "",
)


class _FlippingLocal(_SnapshotLocal):
    def status(self):
        value = {"ok": True, "enabled": self.reads > 0, "revision": self.reads}
        self.reads += 1
        return value


_flipping = _FlippingLocal()
_flipping_mode = _mode_snapshot(_snapshot_panel(True, _flipping))
check(
    "模式快照：后一次status会变值仍只消费同一次读数",
    _flipping.reads == 1
    and _flipping_mode["mode"] == "single"
    and _flipping_mode["basis"]["local_state"] == "disabled"
    and _flipping_mode["basis"]["local_revision"] == 0,
)


_inherit = _route_rows(_route_panel({"vision_provider_id": "shared"}, {"shared": _Prov("shared")}))
check(
    "专用留空按真实层级继承",
    all(row["provider_ids"] == ["shared"] for key, row in _inherit.items() if key != "line_recheck")
    and _inherit["line_recheck"]["source_mode"] == "disabled"
    and not _inherit["line_recheck"]["provider_ids"]
    and _inherit["riven"]["source_mode"] == "inherit_compat"
    and _inherit["line"]["source_mode"] == "inherit_riven"
    and _inherit["scan"]["source_mode"] == "inherit_compat",
)
for _route, _key in (
    ("compat", "vision_provider_id"),
    ("riven", "vision_provider_id_riven"),
    ("scan", "vision_provider_id_scan"),
    ("line_recheck", "vision_provider_id_line_recheck"),
):
    _other = _Prov("other-vision")
    _p = _route_panel({_key: "missing"}, {"other-vision": _other})
    _rows = _route_rows(_p)
    _res = _ct(_p, "vision_live", route=_route)
    check(
        f"失效专用 {_route} 不向自动候选回落",
        not _rows[_route]["available"]
        and _rows[_route]["source_mode"] == "explicit_unavailable"
        and not _res["ok"]
        and _res["attempted"] == 0
        and not _res["sent"]
        and _res["not_sent_reason"] == "no_candidates"
        and _other.calls == 0,
    )

_line_main = _Prov("main")
_line_p = _route_panel(
    {"vision_provider_id_riven": "main", "vision_provider_id_line": "missing"},
    {"main": _line_main},
)
_line_rows = _route_rows(_line_p)
_line_res = _ct(_line_p, "vision_live", route="line")
check(
    "窄读失效按真实查找分支回落紫卡主读",
    _line_rows["line"]["fallback"]
    and _line_rows["line"]["source_mode"] == "fallback_riven"
    and _line_rows["line"]["provider_ids"] == ["main"]
    and _line_res["ok"]
    and _line_main.calls == 1,
)
_independent = _Prov("line")
_p = _route_panel(
    {"vision_provider_id_riven": "missing", "vision_provider_id_line": "line"},
    {"line": _independent},
)
_rows = _route_rows(_p)
_res = _ct(_p, "vision_live", route="line")
check(
    "紫卡主读空不遮蔽独立窄读可用性",
    not _rows["riven"]["available"]
    and _rows["line"]["available"]
    and _res["ok"]
    and _independent.calls == 1,
)
for _same_object in (True, False):
    _one = _Prov("same-model-id")
    _two = _one if _same_object else _Prov("same-model-id")
    _rows = _route_rows(
        _route_panel(
            {"vision_provider_id_riven": "main-config", "vision_provider_id_line": "line-config"},
            {"main-config": _one, "line-config": _two},
        )
    )
    check(
        f"显式窄读即使模型 ID 相同仍不误判回落（同对象={_same_object}）",
        _rows["line"]["source_mode"] == "explicit" and not _rows["line"]["fallback"],
    )

for _route, _calls in (
    ("compat", [1, 1, 1, 0, 0]),
    ("riven", [1, 0, 0, 0, 0]),
    ("line", [1, 0, 0, 0, 0]),
    ("scan", [1, 1, 1, 0, 0]),
):
    _providers = {f"vision-{i}": _Prov(f"vision-{i}", boom=RuntimeError("401")) for i in range(5)}
    _p = _route_panel(providers=_providers)
    _res = _ct(_p, "vision_live", route=_route)
    check(
        f"{_route} 真调遵循首路与渠道方法调用上限",
        [p.calls for p in _providers.values()] == _calls
        and _res["attempted"] == sum(_calls)
        and len(_res["attempts"]) == sum(_calls),
        str(_res),
    )
    if _route == "scan":
        _limits = _res["route_info"]["limits"]
        check(
            "识卡元数据区分主读4、分区1、标题2及旧伤害行函数",
            _limits["runtime_provider_cap"] == 4
            and _limits["runtime_selected_count"] == 4
            and _limits["focus_provider_cap"] == 1
            and _limits["title_provider_cap"] == 2
            and not _limits["legacy_damage_rows_active"]
            and _limits["live_max_tries"] == 3
            and _limits["live_provider_call_cap"] == 3
            and _limits["live_count_unit"] == "provider_text_chat_call",
        )

for _invalid in ("unknown", "SCAN", [], {}, 0, True):
    _probe = _Prov("vision-probe")
    _p = _route_panel(providers={"vision-probe": _probe})
    _res = _ct(_p, "vision_live", route=_invalid)
    check(
        f"无效 route 发送前拒绝 {_invalid!r}",
        _res.get("status") == "error"
        and _res["attempted"] == 0
        and not _res["sent"]
        and not _p.p.context.lookups
        and _probe.calls == 0,
        str(_res),
    )

_secret = "sk-route-private-key"
_endpoint = "https://private.example/v1/chat/completions"
_p = _route_panel(
    providers={
        "vision-probe": _Prov(
            "vision-probe",
            boom=RuntimeError(f"401 Authorization Bearer {_secret} endpoint={_endpoint}"),
        )
    }
)
_res = _ct(_p, "vision_live")
check(
    "真调异常仅回传错误类型与状态码，不回显密钥或 endpoint",
    not _res["ok"]
    and "401" in _res["detail"]
    and _secret not in json.dumps(_res)
    and _endpoint not in json.dumps(_res),
    str(_res),
)
_p = _route_panel(providers={"vision-probe": _Prov("vision-probe", reply=f"{_secret} {_endpoint}")})
_res = _ct(_p, "vision_live")
check(
    "真调非空响应不泄露模型原文",
    _res["ok"] and _secret not in json.dumps(_res) and _endpoint not in json.dumps(_res),
)


async def _route_cancel_checks():
    class Waiting(_Prov):
        async def text_chat(self, **kw):
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()

    entered = asyncio.Event()
    first, later = Waiting("vision-first"), _Prov("vision-later")
    panel = _route_panel(providers={first.pid: first, later.pid: later})

    async def body():
        return {"target": "vision_live", "route": "scan"}

    panel._body = body
    task = asyncio.create_task(panel.conntest())
    await entered.wait()
    busy = await panel.conntest()
    _check_call_audit(busy)
    check(
        "已有真调时拒绝额外渠道方法调用",
        busy["not_sent_reason"] == "test_busy" and first.calls == 1,
    )

    async def route_body():
        return {"target": "vision_routes"}

    panel._body = route_body
    resolved = await panel.conntest()
    check(
        "真调占锁时五角色解析仍有结果且不追加任何渠道调用",
        len(resolved.get("routes", [])) == 6
        and resolved.get("attempted") == 0
        and first.calls == 1
        and later.calls == 0,
    )
    panel._body = body
    task.cancel()
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    check(
        "取消真调保持取消状态、释放锁且不发后路",
        isinstance(result, asyncio.CancelledError)
        and not panel._test_lock.locked()
        and first.calls == 1
        and later.calls == 0,
    )
    first.boom = RuntimeError("unused")

    # 首路失败期间停用面板：后路仍不得发送。
    class Closing(_Prov):
        async def text_chat(self, **kw):
            self.calls += 1
            panel.close()
            raise RuntimeError("401")

    closer = Closing("vision-first")
    panel = _route_panel(providers={closer.pid: closer, later.pid: later})
    panel._body = body
    result = await panel.conntest()
    _check_call_audit(result)
    check(
        "首路失败后停用不发送后路",
        result["attempted"] == 1
        and result["not_sent_reason"] == "panel_closed"
        and later.calls == 0,
    )


asyncio.run(_route_cancel_checks())

for _boom, _expected in ((asyncio.TimeoutError(), "timeout"), (RuntimeError("401"), "error")):
    _p = _route_panel(providers={"vision-only": _Prov("vision-only", boom=_boom)})
    _res = _ct(_p, "vision_live", route="riven")
    check(
        f"首路异常可审计 {_expected}",
        not _res["ok"] and _res["attempts"][0]["status"] == _expected and _res["attempted"] == 1,
    )


# 解析异常按任务隔离；异常原文可能含凭据，返回类型而不透传。
class _BrokenRoutePlugin(_RoutePlugin):
    def _scan_vision_providers(self):
        raise RuntimeError(f"{_secret} {_endpoint}")


_broken = W.WebPanel(_BrokenRoutePlugin(providers={"vision-good": _Prov("vision-good")}), PLUGIN)
_rows = _route_rows(_broken)
_res = _ct(_broken, "vision_live", route="scan")
check(
    "单任务 selector 异常不影响其余解析且不发模型、不泄露异常原文",
    not _rows["scan"]["available"]
    and all(_rows[route]["available"] for route in ("compat", "riven", "line"))
    and _res["attempted"] == 0
    and not _res["sent"]
    and _secret not in json.dumps(_rows) + json.dumps(_res)
    and _endpoint not in json.dumps(_rows) + json.dumps(_res),
)
_empty, _ok, _unused = _Prov("vision-empty", reply=""), _Prov("vision-ok"), _Prov("vision-unused")
_p = _route_panel(providers={p.pid: p for p in (_empty, _ok, _unused)})
_res = _ct(_p, "vision_live", route="scan")
check(
    "空响应后逐个回落、成功后未发送列表可审计",
    _res["ok"]
    and _res["attempted"] == 2
    and [row["status"] for row in _res["attempts"]] == ["empty", "ok"]
    and _res["not_sent_provider_ids"] == ["vision-unused"]
    and _res["not_sent_reason"] == "success_stop"
    and _unused.calls == 0,
)
_many = {f"vision-{i}": _Prov(f"vision-{i}", boom=RuntimeError("401")) for i in range(5)}
_res = _ct(_route_panel(providers=_many), "vision_live", route="scan")
check(
    "识卡渠道方法调用上限后未调用渠道可审计且不包含运行期前4外的渠道",
    _res["not_sent_provider_ids"] == ["vision-3"]
    and _res["not_sent_reason"] == "budget_limit"
    and _many["vision-4"].calls == 0,
)


# fake 模拟 SDK 在一次方法调用内多次尝试；面板不得把方法次数等同 HTTP 次数。
class _RetryingProvider(_Prov):
    async def text_chat(self, **kw):
        self.calls += 1
        self.sdk_attempts = 3
        return type("R", (), {"completion_text": "OK"})()


_retrying = _RetryingProvider("vision-retrying")
_res = _ct(_route_panel(providers={_retrying.pid: _retrying}), "vision_live")
check(
    "一次渠道调用含 SDK 内部多次尝试时，真实 HTTP 次数仍为未知",
    _res["attempted"] == _retrying.calls == 1
    and _retrying.sdk_attempts == 3
    and _res["actual_http_requests"] is None,
)


_png = _b64.b64decode(W._PING_PNG.split(",", 1)[1])
check(
    "★ 探测图是合法 PNG 且宽高 >10（qwen-vl-ocr 拒收 ≤10 像素）",
    _png[:8] == b"\x89PNG\r\n\x1a\n" and min(_st.unpack(">II", _png[16:24])) > 10,
)

# ---- 8. 页面 / 接线 ----
html = (ROOT / "pages/sdjk/index.html").read_text(encoding="utf-8")
check("★ 页面不用 innerHTML（订阅条件是群友输入的文本）", "innerHTML" not in html)
low = html.lower()
check(
    "★ body / script 结束标签各只出现 1 次（AstrBot 替换第一处 body 结束标签注入接口，"
    "注释里写了就会截断脚本 —— 10-16 线上事故）",
    low.count("</body") == 1 and low.count("</script") == 1 and low.count("</html") == 1,
    f"body={low.count('</body')} script={low.count('</script')}",
)
check("真正的 body 结束标签在 script 结束标签之后", low.index("</script") < low.index("</body"))
check(
    "页面调用的端点都已注册", all(f'"{ep}"' in html for ep in ("overview", "refresh", "conntest"))
)
check("页面目录名不含 ops（开源打包会按 ops 段排除）", "ops" not in Path("pages/sdjk").parts)
i18n = json.loads((ROOT / ".astrbot-plugin/i18n/zh-CN.json").read_text(encoding="utf-8"))
check("i18n 标题键 = 页面目录名", "title" in i18n["pages"]["sdjk"])
main_src = (ROOT / "main.py").read_text(encoding="utf-8")
check(
    "main.py 注册面板并在 terminate 里 close",
    "WebPanel(self, PLUGIN_NAME)" in main_src and "panel.close()" in main_src,
)


# ---- 9. 依赖与资源（2026-10-16 维护者要求：面板里能下载需要的依赖）----
check(
    "版本比较 >=", W._satisfies("12.3.0", ">=", "12.3") and not W._satisfies("11.0.0", ">=", "12.3")
)
_rq = Path(tempfile.mkdtemp(prefix="_tmp_webpanel_req_"))
try:
    (_rq / "requirements.txt").write_text(
        "httpx>=0.27\npillow>=999   # 注释\nno-such-pkg-sdjk\n\n# 整行注释\n", encoding="utf-8"
    )
    rows = {r["name"]: r for r in W.deps_status(_rq / "requirements.txt")}
    check("deps：已装且满足", rows["httpx"]["ok"] and rows["httpx"]["installed"])
    check("deps：版本过低判不满足", rows["pillow"]["installed"] and not rows["pillow"]["ok"])
    check(
        "deps：未安装判缺失",
        rows["no-such-pkg-sdjk"]["installed"] == "" and not rows["no-such-pkg-sdjk"]["ok"],
    )
    check("deps：注释 / 空行不算包", len(rows) == 3)
finally:
    import shutil as _sh

    _sh.rmtree(_rq, ignore_errors=True)
check(
    "deps：真实 requirements.txt 能解析出 httpx 与 pillow",
    {r["name"] for r in W.deps_status()} >= {"httpx", "pillow"},
)

# 安装依赖：走 AstrBot 的 pip_installer（注入假模块），参数是插件自己的 requirements.txt
import types as _types  # noqa: E402
from unittest.mock import patch as _patch  # noqa: E402

_calls = []


class _Pip:
    def __init__(self, boom=None):
        self.boom = boom

    async def install(self, **kw):
        _calls.append(kw)
        if self.boom:
            raise self.boom


_pkg = _types.ModuleType("astrbot")
_core = _types.ModuleType("astrbot.core")
_core.pip_installer = _Pip()
_pkg.core = _core
with _patch.dict(sys.modules, {"astrbot": _pkg, "astrbot.core": _core}):
    r = asyncio.run(_panel().deps_install())
check(
    "★ 安装依赖走 AstrBot pip_installer + 插件 requirements.txt",
    r.get("ok") and _calls == [{"requirements_path": str(W.REQUIREMENTS)}],
    str(r) + str(_calls),
)
check("安装后提示重载插件", "重载插件" in r.get("detail", ""))

_core.pip_installer = _Pip(RuntimeError("隔离安装失败"))
with _patch.dict(sys.modules, {"astrbot": _pkg, "astrbot.core": _core}):
    r = asyncio.run(_panel().deps_install())
check(
    "pip_installer 失败给出说明、不抛",
    r.get("ok") is False
    and "RuntimeError: 隔离安装失败" in r.get("detail", "")
    and _calls == [{"requirements_path": str(W.REQUIREMENTS)}] * 2,
    str(r) + str(_calls),
)

# 显式令导入抛 ModuleNotFoundError（ImportError 子类），不依赖宿主是否安装 AstrBot。
# 不能直接在恢复真实模块后调用，否则真实 AstrBot 环境会实际执行 pip。
with _patch.dict(sys.modules, {"astrbot.core": None}):
    r = asyncio.run(_panel().deps_install())
check(
    "没有 pip_installer 的环境给出说明、不抛",
    r.get("status") == "error"
    and "没有提供依赖安装接口" in r.get("message", "")
    and len(_calls) == 2,
    str(r),
)

# 字库优先级：用户目录里的中文字体 > 随包子集（原先反了，下载的完整字库永远用不上）
from core import render as _R  # noqa: E402

_sub = next((p for p in _R._Fonts.PACKED if "Subset-Regular" in p.name and p.exists()), None)
if _sub is not None:
    _fd = Path(tempfile.mkdtemp(prefix="_tmp_webpanel_font_"))
    try:
        _mine = _fd / "Mine-Regular.otf"
        _mine.write_bytes(_sub.read_bytes())
        _old = _R._Fonts.PACKED
        _R._Fonts.PACKED = [p for p in _old if "Subset" in p.name]  # 模拟市场包：只有子集
        try:
            f = _R._Fonts(user_dirs=[_fd])
        finally:
            _R._Fonts.PACKED = _old
        check("★ 用户字体目录里的中文字体优先于随包子集", f.regular == _mine, str(f.regular))
    finally:
        _sh.rmtree(_fd, ignore_errors=True)

    # 下载流程：MockTransport 模拟「第一个源返回错误页、第二个源返回字体」
    import httpx  # noqa: E402

    _font_bytes = _sub.read_bytes()

    def _handler(req):
        if "cdn.jsdelivr" in str(req.url):
            return httpx.Response(200, content=b"<html>rate limited</html>")
        return httpx.Response(
            200, content=_font_bytes, headers={"Content-Length": str(len(_font_bytes))}
        )

    class _Rnd:
        def __init__(self, d):
            self.user_font_dir = d
            self.reloaded = 0
            self.kind = "subset"

        def reload_fonts(self):
            self.reloaded += 1
            self.kind = "full"

        def font_kind(self):
            return self.kind

    _fd = Path(tempfile.mkdtemp(prefix="_tmp_webpanel_dl_"))
    _old_min = W._MIN_FONT_BYTES
    W._MIN_FONT_BYTES = 1024 * 1024
    try:
        pf = _panel()
        pf.p.renderer = _Rnd(_fd / "fonts")
        pf._http_factory = lambda **kw: httpx.AsyncClient(
            transport=httpx.MockTransport(_handler), **{k: v for k, v in kw.items() if k != "proxy"}
        )

        async def _dl():
            r0 = await pf.font_download()
            r1 = await pf.font_download()  # 进行中再点：不重复起任务
            await pf._font_task
            return r0, r1

        r0, r1 = asyncio.run(_dl())
        names = sorted(p.name for p in (_fd / "fonts").iterdir())
        check("下载任务启动 / 进行中不重复启动", r0["started"] is True and r1["started"] is False)
        check(
            "★ 错误页源被拒（过小）后换源，两个字库都落到用户字体目录",
            names == sorted(W.FULL_FONTS) and pf.font_job["state"] == "done",
            f"{names} {pf.font_job}",
        )
        check("失败的源写进日志", any("cdn.jsdelivr.net 失败" in x for x in pf.font_job["log"]))
        check("没有残留 .part 半截文件", not any(n.endswith(".part") for n in names))
        check("下载完重新挑字体（立即生效）", pf.p.renderer.reloaded == 1)
        st = pf._fonts_state()
        check("资源状态带回已下载文件大小", all(v > 0 for v in st["files"].values()), str(st))
    finally:
        W._MIN_FONT_BYTES = _old_min
        _sh.rmtree(_fd, ignore_errors=True)
else:
    print("[SKIP] 没有随包子集字体，跳过字库相关用例")

p9 = _panel()
p9.close()
check(
    "停用后依赖 / 字库接口也拒绝",
    asyncio.run(p9.font_download()).get("status") == "error"
    and asyncio.run(p9.resources()).get("status") == "error",
)
check(
    "页面调用的新端点都已注册",
    all(f'"{ep}"' in html for ep in ("resources", "deps_install", "font_download")),
)


async def _font_shutdown_checks():
    import ast
    from types import SimpleNamespace
    from unittest.mock import patch

    import httpx

    # 编译真实卸载方法及异常回调，避开 AstrBot 宿主依赖。
    tree = ast.parse(main_src)
    methods = [
        f
        for c in tree.body
        if isinstance(c, ast.ClassDef)
        for f in c.body
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        and f.name in ("terminate", "_log_task_death")
    ]
    cls = ast.ClassDef(name="Host", bases=[], keywords=[], body=methods, decorator_list=[])
    scope = {"asyncio": asyncio, "logger": W.logger}
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
            str(ROOT / "main.py"),
            "exec",
        ),
        scope,
    )

    async def nop():
        pass

    for timeout_case in (False, True):
        with tempfile.TemporaryDirectory(prefix="wf-font-stop-") as directory:
            fdir = Path(directory)
            gates = {
                k: asyncio.Event() for k in ("old_started", "new_started", "cleanup", "finish")
            }
            closed, panels = [], []
            cancelled = asyncio.Event()

            class Stream(httpx.AsyncByteStream):
                def __init__(self, label):
                    self.label = label

                async def __aiter__(self):
                    yield b"x" * (256 * 1024)
                    gates[self.label + "_started"].set()
                    await gates["finish"].wait()

                async def aclose(self):
                    if self.label == "old":
                        await gates["cleanup"].wait()
                    closed.append(self.label)

            def make_panel(label):
                host = scope["Host"]()
                host.cfg = {"allow_private_urls": True}
                host.renderer = SimpleNamespace(user_font_dir=fdir)
                host.client = SimpleNamespace(close=nop)
                host.push = SimpleNamespace(stop=nop)
                panel = W.WebPanel(host, PLUGIN)
                host._panel = panel
                panel._fonts_state = lambda: {}

                async def body():
                    return {"url": "https://fonts.example/font.ttf"}

                async def respond(request):
                    return httpx.Response(200, stream=Stream(label))

                panel._body = body
                panel._http_factory = lambda **kw: httpx.AsyncClient(
                    transport=httpx.MockTransport(respond), **kw
                )
                panels.append(panel)
                return host, panel

            async def background():
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            real_wait = asyncio.wait

            async def bounded_wait(tasks, timeout):
                check("字体与后台任务使用同一5秒收尾边界", timeout == 5)
                return await real_wait(tasks, timeout=0.01 if timeout_case else timeout)

            host, old = make_panel("old")
            stopping = None
            try:
                with (
                    patch.object(W.WebPanel, "_check_cjk_font", return_value=""),
                    patch.object(asyncio, "wait", bounded_wait),
                ):
                    started = await old.font_import_url()
                    await asyncio.wait_for(gates["old_started"].wait(), 1)
                    repeated = await old.font_import_url()
                    check(
                        "导入下载中重复点击不新建任务",
                        started.get("started") and repeated.get("status") == "error",
                    )
                    old_part = set(fdir.glob("*.part"))
                    host._auto_task = asyncio.create_task(background())
                    await asyncio.sleep(0)
                    stopping = asyncio.create_task(host.terminate())
                    await asyncio.wait_for(cancelled.wait(), 1)
                    check("等待字体清理前其它后台任务已取消", host._auto_task.done())
                    old.close()  # 幂等关闭不能二次取消正在等待的连接清理。
                    await asyncio.sleep(0)
                    check("重复关闭不打断慢连接清理", not old._font_task.done())
                    if timeout_case:
                        await asyncio.wait_for(stopping, 1)
                        check(
                            "慢清理达到边界后卸载返回",
                            not old._font_task.done() and "old" not in closed,
                        )
                        _, new = make_panel("new")
                        await new.font_import_url()
                        await asyncio.wait_for(gates["new_started"].wait(), 1)
                        new_part = set(fdir.glob("*.part")) - old_part
                        check(
                            "重载同名字体使用独立临时文件",
                            len(old_part) == 1 and len(new_part) == 1,
                        )
                        gates["cleanup"].set()
                        await asyncio.gather(old._font_task, return_exceptions=True)
                        check(
                            "旧取消清理不删除新下载临时文件",
                            all(path.exists() for path in new_part)
                            and not any(path.exists() for path in old_part),
                        )
                        gates["finish"].set()
                        await new._font_task
                        check(
                            "超时后新实例同名下载仍完成",
                            new.font_job["state"] == "done" and (fdir / "font.ttf").exists(),
                        )
                    else:
                        check(
                            "卸载等待字体连接和临时文件释放",
                            not stopping.done() and old_part and "old" not in closed,
                        )
                        gates["cleanup"].set()
                        await asyncio.wait_for(stopping, 1)
                        check(
                            "卸载返回时正常取消已清理完成",
                            old._font_task.cancelled()
                            and "old" in closed
                            and not list(fdir.glob("*.part")),
                        )
                    check("下载收尾不留下临时文件", not list(fdir.glob("*.part")))
            finally:
                for gate in gates.values():
                    gate.set()
                tasks = [panel._font_task for panel in panels if panel._font_task is not None]
                if stopping is not None:
                    tasks.append(stopping)
                await asyncio.gather(*tasks, return_exceptions=True)


asyncio.run(_font_shutdown_checks())


async def _font_import_close_during_validation():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    for phase in ("body", "dns"):
        for close_while_waiting in (False, True):
            with tempfile.TemporaryDirectory(prefix="wf-font-validate-") as directory:
                panel = W.WebPanel(
                    SimpleNamespace(
                        renderer=SimpleNamespace(user_font_dir=Path(directory)),
                        cfg={"allow_private_urls": phase == "body"},
                    ),
                    PLUGIN,
                )
                entered, release = asyncio.Event(), asyncio.Event()

                async def pause():
                    entered.set()
                    await release.wait()

                async def body():
                    if phase == "body":
                        await pause()
                    return {"url": "https://fonts.example/font.ttf"}

                async def reason(url):
                    await pause()
                    return ""

                panel._body = body
                panel._fonts_state = lambda: {}
                worker = AsyncMock()
                with (
                    patch.object(W, "private_url_reason", reason),
                    patch.object(panel, "_import_worker", worker),
                ):
                    request = asyncio.create_task(panel.font_import_url())
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        if close_while_waiting:
                            panel.close()
                        release.set()
                        response = await request
                        if panel._font_task is not None:
                            await panel._font_task
                        if close_while_waiting:
                            check(
                                f"字体导入等待{phase}期间停用后不启动任务",
                                response.get("status") == "error"
                                and panel._font_task is None
                                and panel.font_job["state"] == "idle"
                                and worker.await_count == 0,
                                str(response),
                            )
                        else:
                            check(
                                f"字体导入等待{phase}后仍启用则正常启动",
                                response.get("started") is True and worker.await_count == 1,
                                str(response),
                            )
                    finally:
                        release.set()
                        await asyncio.gather(request, return_exceptions=True)
                        if panel._font_task is not None:
                            await asyncio.gather(panel._font_task, return_exceptions=True)


asyncio.run(_font_import_close_during_validation())


# ---- 10. 设置 / 订阅操作（2026-10-16 维护者定：面板要能调整，不能比配置页还少）----
_schema = W.load_schema()
check("schema 读得到 17 项以上", len(_schema) >= 17, str(len(_schema)))
check("RELOAD_KEYS 都是 schema 里的键", W.RELOAD_KEYS <= set(_schema))
check(
    "coerce：bool / int / list / options",
    W.coerce_setting({"type": "bool"}, True) is True
    and W.coerce_setting({"type": "int"}, "30") == 30
    and W.coerce_setting({"type": "list"}, "1, 2，3") == ["1", "2", "3"]
    and W.coerce_setting({"type": "string", "options": ["a", "b"]}, "b") == "b",
)
for _bad, _spec in (
    (1, {"type": "bool"}),
    ("x", {"type": "int"}),
    (-1, {"type": "int"}),
    ("c", {"type": "string", "options": ["a", "b"]}),
):
    try:
        W.coerce_setting(_spec, _bad)
        check(f"coerce 拒绝 {_bad!r}", False)
    except ValueError:
        check(f"coerce 拒绝 {_bad!r}", True)


class _Conf(dict):
    saved = 0

    def save_config(self):
        _Conf.saved += 1


class _Groups:
    def __init__(self):
        self.d = {}

    def get(self, umo):
        return {"push": False, "quiet": False, **self.d.get(umo, {})}

    def all(self):
        return self.d

    async def set_switch(self, umo, key, value):
        self.d.setdefault(umo, {})[key] = value

    def pause_of(self, umo):
        p = self.d.get(umo, {}).get("push_pause")
        return dict(p) if isinstance(p, dict) else None

    async def set_pause(self, umo, info):
        rec = self.d.setdefault(umo, {})
        if info is None:
            rec.pop("push_pause", None)
        else:
            rec["push_pause"] = dict(info)


class _SubStore(_Store):
    async def remove(self, pred):
        before = len(self._s)
        self._s = [x for x in self._s if not pred(x)]
        return before - len(self._s)


ps10 = _panel()
ps10.p.cfg = {"render_mode": "image", "page_size": 12, "proxy": ""}
ps10.p._config = _Conf(ps10.p.cfg)
ps10.p.render_mode, ps10.p.page_size = "image", 12
ps10.p.push.interval = 45
ps10.p.context = type(
    "C", (), {"get_all_providers": staticmethod(lambda: [_Prov("a/vl"), _Prov("b/chat")])}
)
st10 = asyncio.run(ps10.settings())
keys10 = [f["key"] for g in st10["groups"] for f in g["fields"]]
check(
    "★ 设置页覆盖 schema 全部配置项（面板自用键除外）、无重复",
    sorted(keys10) == sorted(set(_schema) - W.PANEL_ONLY_KEYS) and len(keys10) == len(set(keys10)),
)
check("面板自用键都在 schema 里", W.PANEL_ONLY_KEYS <= set(_schema))
check("设置页带渠道列表（select_provider 用）", st10["providers"] == ["a/vl", "b/chat"])
check("渠道项标 provider", any(f["provider"] for g in st10["groups"] for f in g["fields"]))
check("没有平台管理器时检测结果为空、不抛", st10["platform_info"]["detected"] == [])
check(
    "平台专属项都在 schema 里、平台名都有显示名",
    set(W.FIELD_PLATFORMS) <= set(_schema)
    and all(x in W.PLATFORM_LABELS for v in W.FIELD_PLATFORMS.values() for x in v),
)
_plat_of = {f["key"]: f["platforms"] for g in st10["groups"] for f in g["fields"]}
check(
    "设置项带适用平台（通用项为空）",
    _plat_of["qq_merge_image_actions"] == ["qq_official"] and _plat_of["render_mode"] == [],
)


class _Inst:
    def __init__(self, name):
        self._name = name

    def meta(self):
        return type("M", (), {"name": self._name})()


ps10.p.context.platform_manager = type(
    "PM",
    (),
    {
        "get_insts": staticmethod(
            lambda: [_Inst("webchat"), _Inst("telegram"), _Inst("qq_official"), _Inst("telegram")]
        )
    },
)
ps10.p.cfg["panel_platforms"] = ["slack", "不存在的平台"]
st10b = asyncio.run(ps10.settings())
check(
    "已启用平台按显示顺序去重、忽略 webchat",
    st10b["platform_info"]["detected"] == ["qq_official", "telegram"],
    str(st10b["platform_info"]["detected"]),
)
check("手动选择只保留认识的平台", st10b["platform_info"]["choice"] == ["slack"])
check(
    "设置页有聊天平台面板（自动 / 手动 / 显示全部）",
    "function platformPanel()" in _html
    and 'mode(false, "自动（按 AstrBot 已启用的平台）")' in _html
    and "showAllPlat" in _html
    # b17：设置页只列非识图键，平台过滤照旧
    and 'g.fields.filter((f) => scopeOf(f.key) === "settings" && fieldShown(f))' in _html,
)


def _save(panel, changes):
    async def _b():
        return {"changes": changes}

    panel._body = _b
    return asyncio.run(panel.settings_save())


r = _save(
    ps10, {"render_mode": "text", "page_size": "20", "push_interval": 15, "proxy": "http://x:1"}
)
check(
    "保存：写回配置对象并落盘",
    _Conf.saved == 1
    and ps10.p._config["render_mode"] == "text"
    and ps10.p._config["page_size"] == 20,
    str(r),
)
check(
    "★ 即时生效：render_mode / page_size / 推送间隔（最小 15）同步到运行中实例",
    ps10.p.render_mode == "text"
    and ps10.p.page_size == 20
    and ps10.p.push.interval == 15
    and ps10.p.cfg["proxy"] == "http://x:1",
)
check("需重载的项单独报出", r["need_reload"] == ["proxy"], str(r))
r = _save(ps10, {"render_mode": "html"})
check(
    "非法取值整批拒绝、不落盘",
    r.get("status") == "error" and _Conf.saved == 1 and ps10.p.render_mode == "text",
)
r = _save(ps10, {"no_such_key": 1})
check("未知配置项被拒", r.get("status") == "error")
ps10.p._config = None
check("拿不到配置对象时给说明", _save(ps10, {"page_size": 10}).get("status") == "error")


def _settings_numeric_boundary_checks():
    """真实保存入口：数值非法时整批拒绝，磁盘、配置对象和运行态均不变。"""
    with tempfile.TemporaryDirectory(prefix="wf-settings-numeric-") as td:
        target = Path(td) / "config.json"

        class Config(dict):
            saved = 0

            def save_config(self):
                target.write_text(json.dumps(self, allow_nan=False), encoding="utf-8")
                self.saved += 1

        panel = _panel()
        original = {
            "render_mode": "image",
            "page_size": 12,
            "http_timeout": 15,
            "push_interval": 45,
            "scan_cooldown": 15,
            "text_chunk_limit": 0,
        }
        panel.p.cfg = dict(original)
        panel.p._config = Config(original)
        panel.p.render_mode, panel.p.page_size, panel.p.push.interval = "image", 12, 45
        target.write_text(json.dumps(original), encoding="utf-8")
        disk = target.read_bytes()
        invalid = [
            (key, value)
            for key in ("page_size", "http_timeout", "push_interval")
            for value in (0, -1, "0", float("nan"), float("inf"), float("-inf"), "NaN", "Inf")
        ] + [("push_interval", 14), ("text_chunk_limit", 199), ("text_chunk_limit", -1)]
        for key, value in invalid:
            result = _save(panel, {"render_mode": "text", key: value})
            check(
                f"数值越界整批拒绝且不写盘/不改运行态：{key}={value!r}",
                result.get("status") == "error"
                and panel.p._config.saved == 0
                and dict(panel.p._config) == original
                and panel.p.cfg == original
                and target.read_bytes() == disk
                and (panel.p.render_mode, panel.p.page_size, panel.p.push.interval)
                == ("image", 12, 45),
                str(result),
            )
        legal = {
            "page_size": 1,
            "http_timeout": 1,
            "push_interval": 15,
            "scan_cooldown": 0,
            "text_chunk_limit": 0,
        }
        result = _save(panel, legal)
        stored = json.loads(target.read_text(encoding="utf-8"))
        check(
            "数值下限和合法 0 可保存并生效",
            result.get("saved") == sorted(legal)
            and panel.p._config.saved == 1
            and all(stored[k] == v and panel.p.cfg[k] == v for k, v in legal.items())
            and panel.p.page_size == 1
            and panel.p.push.interval == 15,
            str(result),
        )
        result = _save(panel, {"text_chunk_limit": 200})
        check(
            "手动文字分段下限 200 可保存",
            result.get("saved") == ["text_chunk_limit"]
            and json.loads(target.read_text(encoding="utf-8"))["text_chunk_limit"] == 200,
        )


_settings_numeric_boundary_checks()


async def _settings_save_failure_checks():
    """真实保存入口：失败改动不能混进下一次无关的成功保存。"""
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory(prefix="wf-settings-rollback-") as td:
        target = Path(td) / "config.json"

        class Config(dict):
            mode = "ok"
            calls = 0

            def save_config(self):
                self.calls += 1
                if self.mode == "before_write":
                    raise OSError("simulated disk error")
                target.write_text(json.dumps(self, ensure_ascii=False), encoding="utf-8")
                if self.mode == "after_write":
                    raise OSError("host raised after writing")

        old = {
            "render_mode": "image",
            "page_size": 12,
            "proxy": "",
            "vision_provider_id": None,
            "untouched": ["keep"],
        }
        conf = Config(old)
        conf.save_config()
        untouched = conf["untouched"]
        host = SimpleNamespace(
            _config=conf,
            cfg=dict(old),
            render_mode="image",
            page_size=12,
            context=SimpleNamespace(get_all_providers=lambda: []),
        )
        panel = W.WebPanel(host, PLUGIN)

        async def post(changes):
            async def body():
                return {"changes": changes}

            panel._body = body
            return await panel.settings_save()

        conf.mode = "before_write"
        failed = await post(
            {
                "proxy": "https://failed-setting.example.com",
                "page_size": 20,
                "vision_provider_id": "failed-channel",
                "wiki_intro": False,
            }
        )
        check(
            "设置写盘失败：返回错误且没有成功字段",
            failed.get("status") == "error" and "saved" not in failed,
        )
        check("设置写盘失败：恢复原有键及原本为None的值", dict(conf) == old)
        check("设置写盘失败：移除本次新增键", "wiki_intro" not in conf)
        check("设置写盘失败：保留非本次字段与其对象", conf["untouched"] is untouched)
        check("设置写盘失败：保留宿主配置对象身份", host._config is conf)
        check("设置写盘失败：运行配置快照不变", host.cfg == old)
        check("设置写盘失败：运行属性不变", host.render_mode == "image" and host.page_size == 12)
        check("设置写盘前失败：磁盘仍是原配置", json.loads(target.read_text()) == old)
        settings_view = await panel.settings()
        shown = {f["key"]: f["value"] for g in settings_view["groups"] for f in g["fields"]}
        check("设置写盘失败：设置页仍显示原值", shown["proxy"] == "" and shown["page_size"] == 12)

        conf.mode = "ok"
        saved = await post({"render_mode": "text"})
        expected = {**old, "render_mode": "text"}
        check(
            "失败后无关保存：只报告本次成功键",
            saved == {"saved": ["render_mode"], "need_reload": []},
        )
        check("失败后无关保存：先前失败改动不会落盘", json.loads(target.read_text()) == expected)
        check("失败后无关保存：持久配置与运行快照一致", dict(conf) == host.cfg == expected)

        saved = await post({"wiki_intro": False, "page_size": 24})
        check(
            "正常混合保存：原有和新增键均成功",
            saved == {"saved": ["page_size", "wiki_intro"], "need_reload": []},
        )
        check(
            "正常混合保存：落盘并即时生效",
            json.loads(target.read_text()) == host.cfg
            and host.page_size == 24
            and conf["wiki_intro"] is False,
        )

        # 插件不能借 save_config 推断宿主的写盘阶段；仅保证本次内存改动可回滚。
        # 若宿主先写盘再报错，不应为「恢复」再调用一次可能继续失败的写盘操作。
        before_partial = dict(conf)
        before_calls = conf.calls
        conf.mode = "after_write"
        partial = await post({"proxy": "https://host-wrote-before-error.example.com"})
        check(
            "宿主写盘后抛错：仍返回错误",
            partial.get("status") == "error" and "saved" not in partial,
        )
        check("宿主写盘后抛错：仍恢复本次内存键", dict(conf) == host.cfg == before_partial)
        check(
            "宿主写盘后抛错：不声称能回滚宿主文件",
            json.loads(target.read_text())["proxy"]
            == "https://host-wrote-before-error.example.com",
        )
        check("设置失败回滚：不额外触发宿主保存", conf.calls == before_calls + 1)


asyncio.run(_settings_save_failure_checks())


async def _settings_private_policy_checks():
    """保存入口与社区消费入口对同一内网开关的解释必须一致；HTTP 只用 Mock。"""
    from types import SimpleNamespace

    import httpx

    class Config(dict):
        fail = False

        def save_config(self):
            if self.fail:
                raise OSError("simulated disk error")

    async def post(panel, changes):
        async def body():
            return {"changes": changes}

        panel._body = body
        return await panel.settings_save()

    async def setup(allowed):
        requests = []

        def transport(request):
            requests.append(str(request.url))
            return httpx.Response(200, json={"synthetic": True})

        client = W.WarframeClient(community_base="http://127.0.0.1/old", allow_private_urls=allowed)
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        conf = Config(allow_private_urls=allowed, community_data_url=client.community_base)
        host = SimpleNamespace(_config=conf, cfg=dict(conf), client=client)
        return W.WebPanel(host, PLUGIN), client, conf, requests

    urls = ("https://unused.example.com/synthetic.json",)
    for old, new in ((True, False), (False, True)):
        panel, client, conf, requests = await setup(old)
        try:
            if old:
                await client._community_json(urls)
                check(
                    "内网开关：合法旧快照已进入缓存",
                    await client.cache.get("community:synthetic.json") is not None,
                )
                requests.clear()
            result = await post(panel, {"allow_private_urls": new})
            check(
                f"内网开关{old}→{new}：保存后无需重载",
                result == {"saved": ["allow_private_urls"], "need_reload": []},
            )
            check(
                f"内网开关{old}→{new}：cfg与社区客户端即时一致",
                panel.p.cfg["allow_private_urls"] is new and client.allow_private_urls is new,
            )
            payload = await client._community_json(urls)
            check(
                f"内网开关{old}→{new}：社区实际采用新策略",
                (payload is not None) is new and len(requests) == int(new),
            )
            if not new:
                check(
                    "关闭内网后：已有缓存仍不能绕过地址禁止",
                    await client.cache.get("community:synthetic.json") is not None
                    and payload is None
                    and requests == [],
                )

            conf.fail = True
            failed = await post(panel, {"allow_private_urls": old})
            check(f"内网开关失败{new}→{old}：返回错误", failed.get("status") == "error")
            check(
                f"内网开关失败{new}→{old}：客户端和配置策略均不变",
                client.allow_private_urls is new
                and panel.p.cfg["allow_private_urls"] is new
                and conf["allow_private_urls"] is new,
            )
        finally:
            await client.close()

    # 同批改来源和策略，JSON键顺序不影响生效；从未外发使用旧策略的请求。
    for new, policy_first in ((False, False), (False, True), (True, False), (True, True)):
        panel, client, _, requests = await setup(not new)
        try:
            url = "http://127.0.0.2/new"
            pairs = [("community_data_url", url), ("allow_private_urls", new)]
            changes = dict(reversed(pairs) if policy_first else pairs)
            result = await post(panel, changes)
            payload = await client._community_json(urls)
            label = f"同批来源与内网开关{new}，策略键在{'前' if policy_first else '后'}"
            check(
                label + "：保存字段及即时状态一致",
                result == {"saved": ["allow_private_urls", "community_data_url"], "need_reload": []}
                and client.community_base == url
                and client.allow_private_urls is new,
            )
            check(
                label + "：只按新来源与新策略请求",
                (payload is not None) is new
                and requests == ([url + "/synthetic.json"] if new else []),
            )
        finally:
            await client.close()


asyncio.run(_settings_private_policy_checks())

# 订阅：groups 用 get 语义（没记录 = 推送关），操作走不透明 key
s_a = Subscription(
    umo="aiocqhttp:GroupMessage:111222333", platform="pc", event="仲裁", rule="", created_by="1"
)
s_b = Subscription(
    umo="aiocqhttp:GroupMessage:444555666", platform="pc", event="裂缝", rule="", created_by="2"
)
pg = _panel()
pg.p.subs = _SubStore([s_a, s_b])
pg.p.groups = _Groups()
ov = W.subscriptions(pg.p.subs, pg.p.groups)
check(
    "★ 群没有设置记录 ⇒ 推送显示为关（与 GroupStore 默认一致）",
    all(x["push"] is False for x in ov["sessions"]),
)
key_a = next(x["key"] for x in ov["sessions"] if x["umo"].endswith("33"))
check("会话 key 不含原始号码", "111222333" not in key_a)


def _post(panel, name, body):
    async def _b():
        return body

    panel._body = _b
    return asyncio.run(getattr(panel, name)())


r = _post(pg, "group_push", {"key": key_a, "push": True})
check(
    "★ 群推送开关按 key 回查到真实 umo 并写入",
    r.get("push") is True and pg.p.groups.d == {"aiocqhttp:GroupMessage:111222333": {"push": True}},
    str(r),
)
check(
    "未知 key 被拒", _post(pg, "group_push", {"key": "nope", "push": True}).get("status") == "error"
)
check(
    "开关值非 bool 被拒",
    _post(pg, "group_push", {"key": key_a, "push": "1"}).get("status") == "error",
)
r = _post(pg, "sub_remove", {"sid": s_b.sid})
check(
    "★ 删除订阅：只删那一条",
    r.get("removed") == 1 and [x.sid for x in pg.p.subs.all()] == [s_a.sid],
)
check("删除不存在的订阅给提示", _post(pg, "sub_remove", {"sid": s_b.sid}).get("status") == "error")
check(
    "非法 sid 被拒（不进 predicate）",
    _post(pg, "sub_remove", {"sid": "../x"}).get("status") == "error",
)

# ★ 2026-10-17：连续失败「暂停推送」在面板上的展示与手动恢复
pg.p.groups.d["aiocqhttp:GroupMessage:111222333"] = {
    "push": True,
    "push_pause": {
        "since": 1760000000.0,
        "next_try": 1760021600.0,
        "reason": "timeout",
        "fails": 5,
    },
}
pg.p.push.groups = pg.p.groups  # 面板的 paused_sessions 回落读 GroupStore
pg.p.subs._s.append(  # 再加一个「未暂停」的会话作对照（444555666 的订阅上面已被删掉）
    Subscription(umo="aiocqhttp:GroupMessage:777888999", platform="pc", event="仲裁", rule="")
)
ov2 = W.subscriptions(pg.p.subs, pg.p.groups)
row_a = next(x for x in ov2["sessions"] if x["umo"].endswith("33"))
row_b = next(x for x in ov2["sessions"] if x["umo"].endswith("99"))
check(
    "★ subscriptions() 每个会话带 paused（since / next_try / reason / fails）",
    isinstance(row_a["paused"], dict)
    and row_a["paused"]["reason"] == "timeout"
    and row_a["paused"]["fails"] == 5
    and row_a["paused"]["since"] == 1760000000.0,
    str(row_a),
)
check("未暂停的会话 paused 为 None", row_b["paused"] is None, str(row_b))
check(
    "paused 的 reason 不含会话号",
    "111222333" not in json.dumps(row_a["paused"], ensure_ascii=False),
)
ps_paused = W.push_state(pg.p.push)
check("★ push_state() 带 paused_sessions", ps_paused.get("paused_sessions") == 1, str(ps_paused))
r = _post(pg, "push_resume", {"key": key_a})
check(
    "★ push_resume 按 key 回查并解除暂停（存档里的 push_pause 清掉）",
    r.get("resumed") is True and pg.p.groups.pause_of("aiocqhttp:GroupMessage:111222333") is None,
    str(r),
)
check(
    "未知 key 的 push_resume 被拒",
    _post(pg, "push_resume", {"key": "nope"}).get("status") == "error",
)
check("push_state 的 paused_sessions 随之归零", W.push_state(pg.p.push)["paused_sessions"] == 0)

pc = _panel()
pc.close()
check(
    "停用后设置 / 订阅操作一律拒绝",
    all(
        asyncio.run(getattr(pc, n)()).get("status") == "error"
        for n in ("settings", "settings_save", "sub_remove", "group_push", "push_resume")
    ),
)

check(
    "★ 页面不用 confirm / alert / prompt（沙箱 iframe 没有 allow-modals，会静默失效）",
    not re.search(r"\b(confirm|alert|prompt)\s*\(", html),
)
check(
    "页面调用的设置 / 订阅端点都已注册",
    all(
        f'"{ep}"' in html
        for ep in ("settings", "settings_save", "sub_remove", "group_push", "push_resume")
    ),
)
check(
    "子菜单的每个视图都有对应区块",
    all(
        f'data-view="{v}"' in html and f'data-go="{v}"' in html
        for v in ("overview", "push", "settings", "vision", "appearance", "data", "maint", "about")
    )
    # b17：「依赖与资源」「连通性」并入「维护」，旧 hash 作为别名仍可打开
    and 'resources: ["maint", "maint-deps"], conn: ["maint", "maint-conn"]' in html
    and 'id="maint-deps"' in html
    and 'id="maint-conn"' in html,
)


# ---- 11. 字库导入 / 选择（2026-10-16 维护者定：字体不限预设一种，支持 URL / 文件导入）----
from core import render as _R2  # noqa: E402

check(
    "safe_font_name：URL 取文件名并清洗",
    W.safe_font_name("https://x.com/a/LXGW%20WenKai.ttf?v=1") == "LXGW_WenKai.ttf",
)
check("safe_font_name：非字体后缀拒绝", W.safe_font_name("https://x.com/a.zip") == "")
check("safe_font_name：路径穿越被剥掉", W.safe_font_name("../../etc/evil.otf") == "evil.otf")
_sub2 = next((p for p in _R2._Fonts.PACKED if "Subset-Regular" in p.name and p.exists()), None)
_dejavu = next(
    (p for p in (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),) if p.exists()), None
)
if _sub2 is not None:
    check("★ 中文字体按字形覆盖判定可用", _R2.pick_cjk_face(_sub2) is not None)
    _old_pref = _R2._PREFERRED_FACE
    _R2._PREFERRED_FACE = ()  # 模拟「族名不认识」的第三方中文字体（霞鹜文楷之类）
    try:
        check(
            "★ 族名不在白名单的中文字体也能被选中（按覆盖）", _R2.pick_cjk_face(_sub2) is not None
        )
    finally:
        _R2._PREFERRED_FACE = _old_pref
    if _dejavu is not None:
        check("★ 不含中文的西文字体被拒（会出方块）", _R2.pick_cjk_face(_dejavu) is None)

    class _Rnd2:
        def __init__(self, d):
            self.cache_dir = d / "cards"
            self.user_font_dir = d / "fonts"
            self.user_font_dir.mkdir(parents=True)
            self.reloads = 0
            self.fonts = None

        appearance_path = property(lambda s: s.cache_dir.parent / "appearance.json")
        load_appearance = _R2.ImageRenderer.load_appearance
        font_kind = lambda s: "subset"  # noqa: E731

        def reload_fonts(self):
            self.reloads += 1
            app = self.load_appearance()
            self.fonts = _R2._Fonts(
                user_dirs=[self.user_font_dir],
                prefer_regular=Path(app["font_regular"]) if app.get("font_regular") else None,
                prefer_bold=Path(app["font_bold"]) if app.get("font_bold") else None,
            )

    _fd2 = Path(tempfile.mkdtemp(prefix="_tmp_webpanel_fontlib_"))
    try:
        pf2 = _panel()
        pf2.p.renderer = _Rnd2(_fd2)
        pf2.p.renderer.reload_fonts()

        # 上传：注入假的上传文件对象
        class _Up:
            def __init__(self, name, data):
                self.filename, self._d = name, data

            async def read(self, n=-1):
                return self._d if n < 0 else self._d[:n]

        def _upload(name, data):
            async def _f():
                return _Up(name, data)

            pf2._upload_file = _f
            return asyncio.run(pf2.font_upload())

        r = _upload("我的字体.otf", _sub2.read_bytes())
        check("上传中文字体成功", r.get("saved") == "我的字体.otf", str(r)[:200])
        r = _upload("我的字体.otf", _sub2.read_bytes())
        check("同名再上传被拒", r.get("status") == "error")
        r = _upload("readme.txt", b"x" * 100000)
        check("非字体后缀被拒", r.get("status") == "error")
        if _dejavu is not None:
            r = _upload("western.ttf", _dejavu.read_bytes())
            check(
                "★ 不含中文的字体上传被拒且不落盘",
                r.get("status") == "error" and not (_fd2 / "fonts" / "western.ttf").exists(),
                str(r),
            )
        check("上传不留 .part", not list((_fd2 / "fonts").glob("*.part")))

        lib = {x["file"]: x for x in asyncio.run(pf2.resources())["fonts"]["library"]}
        mine = lib.get("我的字体.otf")
        check(
            "字体库列出导入的字体（origin=user）", mine and mine["origin"] == "user" and mine["ok"]
        )

        def _sel(slot, fid):
            async def _b():
                return {"slot": slot, "id": fid}

            pf2._body = _b
            return asyncio.run(pf2.font_select())

        r = _sel("both", mine["id"])
        app = json.loads((_fd2 / "appearance.json").read_text(encoding="utf-8"))
        check(
            "★ 设为正文 + 粗体：写 appearance.json 并立即生效",
            app.get("font_regular", "").endswith("我的字体.otf")
            and pf2.p.renderer.fonts.regular.name == "我的字体.otf"
            and pf2.p.renderer.fonts.bold.name == "我的字体.otf",
            str(app),
        )
        lib = {x["file"]: x for x in r["fonts"]["library"]}
        check(
            "字体库标出当前正文 / 粗体",
            lib["我的字体.otf"]["regular"] and lib["我的字体.otf"]["bold"],
        )
        check("未知 id 被拒", _sel("regular", "nope").get("status") == "error")

        s_ = asyncio.run(
            pf2.font_sample.__func__(
                type(
                    "X",
                    (),
                    {
                        "_gone": lambda s: None,
                        "p": pf2.p,
                        "_font_path_of": pf2._font_path_of,
                        "_query": staticmethod(lambda k, d="": mine["id"]),
                    },
                )()
            )
        )
        check(
            "字体样张返回 PNG data URL", str(s_.get("png", "")).startswith("data:image/png;base64,")
        )

        def _del(fid):
            async def _b():
                return {"id": fid}

            pf2._body = _b
            return asyncio.run(pf2.font_delete())

        bundled = next(x for x in lib.values() if x["origin"] == "bundled")
        check("★ 不能删随包字体", _del(bundled["id"]).get("status") == "error" and _sub2.exists())
        r = _del(mine["id"])
        app = json.loads((_fd2 / "appearance.json").read_text(encoding="utf-8"))
        check(
            "★ 删除正在用的字体：清掉选择并回落自动挑选",
            r.get("deleted") == "我的字体.otf"
            and "font_regular" not in app
            and pf2.p.renderer.fonts.regular is not None
            and pf2.p.renderer.fonts.regular.name != "我的字体.otf",
            str(app),
        )

        # URL 导入：MockTransport
        import httpx as _hx  # noqa: E402

        _payload = _sub2.read_bytes()

        def _h(req):
            if req.url.path.endswith("hop.ttf"):  # 公网地址 302 到内网（S4 重定向用例）
                return _hx.Response(302, headers={"Location": "http://127.0.0.1:18702/x.ttf"})
            if req.url.path.endswith("western.ttf") and _dejavu is not None:
                return _hx.Response(200, content=_dejavu.read_bytes())
            return _hx.Response(
                200, content=_payload, headers={"Content-Length": str(len(_payload))}
            )

        pf2._http_factory = lambda **kw: _hx.AsyncClient(
            transport=_hx.MockTransport(_h), **{k: v for k, v in kw.items() if k != "proxy"}
        )

        def _imp(url):
            async def _b():
                return {"url": url}

            async def _run():
                pf2._body = _b
                r0 = await pf2.font_import_url()
                if pf2._font_task:
                    await pf2._font_task
                return r0

            return asyncio.run(_run())

        # 离线也稳定：x.com 视为公网，其余地址走真实内网判定（IP 直写不联网）
        _real_reason = W.private_url_reason

        async def _reason(u):
            return "" if "://x.com/" in u else await _real_reason(u)

        W.private_url_reason = _reason
        check("URL 非 http(s) 被拒", _imp("file:///etc/passwd").get("status") == "error")
        _r_lo = _imp("http://127.0.0.1:18702/fonts/a.ttf")
        check(
            "★ 字体直链指向本机被拒（Astra S4）",
            _r_lo.get("status") == "error" and "内网" in _r_lo.get("message", ""),
            str(_r_lo),
        )
        _imp("https://x.com/fonts/hop.ttf")
        check(
            "★ 公网地址 302 到内网：下载被拦、不落盘",
            pf2.font_job["state"] == "failed" and not (_fd2 / "fonts" / "hop.ttf").exists(),
            str(pf2.font_job),
        )
        check("URL 不是字体后缀被拒", _imp("https://x.com/a.zip").get("status") == "error")
        r = _imp("https://x.com/fonts/Wenkai.ttf")
        check(
            "★ URL 导入中文字体成功",
            pf2.font_job["state"] == "done" and (_fd2 / "fonts" / "Wenkai.ttf").exists(),
            str(pf2.font_job),
        )
        if _dejavu is not None:
            _imp("https://x.com/fonts/western.ttf")
            check(
                "★ URL 导入不含中文的字体：失败且不落盘",
                pf2.font_job["state"] == "failed" and not (_fd2 / "fonts" / "western.ttf").exists(),
                str(pf2.font_job),
            )
    finally:
        if "_real_reason" in dir():
            W.private_url_reason = _real_reason
        _sh.rmtree(_fd2, ignore_errors=True)
check(
    "页面调用的字体端点都已注册",
    all(
        f'"{ep}"' in html
        for ep in ("font_import_url", "font_upload", "font_select", "font_delete", "font_sample")
    ),
)


# ---- 12. 主题（2026-10-16 维护者定：可调颜色 / 背景图 / 角标 + 教程 + 实时预览）----
from PIL import Image as _PI, ImageChops as _IC  # noqa: E402

_R2.apply_theme({})
_td12 = Path(tempfile.mkdtemp(prefix="_tmp_webpanel_theme_"))
try:
    _rr = _R2.ImageRenderer(_td12 / "cards")
    if _rr.available:
        _T, _L, _F = W.PREVIEW_TITLE, W.PREVIEW_LINES, W.PREVIEW_FOOT

        def _img(path):
            im = _PI.open(path).convert("RGB")
            im.load()
            return im

        base = _img(_rr.render(_T, _L, _F))
        _R2.apply_theme({})
        check(
            "★ 默认主题像素级不变（apply_theme({}) 前后同图）",
            _IC.difference(base, _img(_rr.render(_T, _L, _F))).getbbox() is None,
        )
        _amber, _title = _R2.AMBER, list(_R2.TITLE_THEME)
        _R2.apply_theme({"accent": "#5ab0ff", "warn": "#ff0000"})
        check(
            "改强调色：模块常量与派生色一起变",
            _R2.GOLD == (90, 176, 255) and _R2.GOLD_DIM != _R2._THEME_DEFAULTS["GOLD_DIM"],
        )
        check(
            "★ 改强调色：抄进表里的颜色也被替换（TITLE_THEME 等）",
            any(c == (90, 176, 255) for _k, (c, _s) in _R2.TITLE_THEME),
        )
        check(
            "★ 改警示竖条不连带改琥珀（两者默认同值）",
            _R2.WARN_BAR == (255, 0, 0) and _R2.AMBER == _amber,
        )
        themed = _img(_rr.render(_T, _L, _F))
        check("换肤后出图确实不同", _IC.difference(base, themed).getbbox() is not None)
        _R2.apply_theme({})
        check(
            "★ 还原默认后与原图逐像素一致、表也复原",
            _IC.difference(base, _img(_rr.render(_T, _L, _F))).getbbox() is None
            and list(_R2.TITLE_THEME) == _title,
        )
        check(
            "parse_hex_color",
            _R2.parse_hex_color("#A0b1C2") == (160, 177, 194)
            and _R2.parse_hex_color("red") is None,
        )

        # 面板接口
        class _PP12:
            renderer = _rr
            _render_lock = None

        pt = W.WebPanel(_PP12(), "x")

        def _tpost(fn, body):
            async def _b():
                return body

            pt._body = _b
            return asyncio.run(fn())

        meta = asyncio.run(pt.theme())
        check(
            "主题槽位：8 个基础 + 高级类别色",
            sum(1 for x in meta["slots"] if x["group"] == "base") == 8
            and sum(1 for x in meta["slots"] if x["group"] == "adv") >= 10,
        )
        check("每个槽位都写了影响哪里（教程）", all(x["where"] for x in meta["slots"]))
        check("预设含默认", meta["presets"][0]["colors"] == {})

        _legacy_tokens = (
            "[后纪]",
            "[Axi]",
            "Infested",
            "G系",
            "白天",
            "B",
            "3日2时",
            "信500",
            "（3h）",
        )
        _legacy_default = {t: _R2._token_style(t) for t in _legacy_tokens}
        _ivory = next(p["colors"] for p in meta["presets"] if p["key"] == "ivory")
        _R2.apply_theme(_ivory)
        check(
            "象牙：后纪与派系实际 token 跟随深色槽位",
            _R2._token_style("[后纪]")[2] == _R2.RED
            and _R2._token_style("[Axi]")[2] == _R2.RED
            and _R2._token_style("Infested")[3] == _R2.INK_DIM
            and _R2._token_style("G系")[3] == _R2.INK_DIM,
        )
        check(
            "象牙：白天/评级/时长/信用点不再遗留固定亮色",
            _R2._token_style("白天")[3] == _R2.AMBER
            and _R2._token_style("B")[3] == _R2.GREEN
            and _R2._token_style("3日2时")[3] == _R2.BLUE
            and _R2._token_style("信500")[3] == _R2.GOLD
            and _R2._token_style("（3h）")[3] == _R2.TIME_DIM == _R2.GOLD,
        )
        # 浅底上金档与铜档曾只差一点色相，杜卡德 100 与 15/25 档看不出区别。
        _gb = sum((a - b) ** 2 for a, b in zip(_R2.RARITY_GOLD, _R2.RARITY_BRONZE)) ** 0.5
        check("象牙：金档与铜档部件颜色拉开", _gb > 40, f"{_R2.RARITY_GOLD} {_R2.RARITY_BRONZE}")
        # 金档与高亮色互相独立：只改高亮色时金档仍是默认金（面板显示与出图一致），
        # 只改金档时高亮色 / ★ 不动；两个默认值必须不同，换肤按值重建表时才分得开。
        _gold0 = _R2._THEME_DEFAULTS["RARITY_GOLD"]
        check("金档与高亮色默认值不同", _gold0 != _R2._THEME_DEFAULTS["GOLD_BRIGHT"])
        _R2.apply_theme({"highlight": "#c9f1ff"})
        check(
            "只改高亮色：金档部件仍是默认金",
            _R2.RARITY_GOLD == _gold0
            and _R2._SPAN_COLORS["g"] == _gold0
            and _R2._token_style("100杜")[3] == _gold0,
        )
        _R2.apply_theme({"rarity_gold": "#8a6a00"})
        check(
            "只改金档：高亮色不动、金档与古纪芯片跟随",
            _R2.GOLD_BRIGHT == _R2._THEME_DEFAULTS["GOLD_BRIGHT"]
            and _R2._SPAN_COLORS["g"] == (138, 106, 0)
            and _R2.TIER_COLOR["古纪"] == (138, 106, 0),
        )
        _R2.apply_theme({})
        # 连续切换多套预设再恢复默认，防止按值映射或 token 缓存残留上套主题。
        _theme_chain_base = (
            _R2.RARITY_GOLD,
            _R2.GOLD_BRIGHT,
            dict(_R2.TIER_COLOR),
            dict(_R2._SPAN_COLORS),
        )
        for _preset in meta["presets"]:
            _R2.apply_theme(_preset["colors"])
            _expected_gold = (
                _R2.parse_hex_color(_preset["colors"].get("rarity_gold"))
                or _R2._THEME_DEFAULTS["RARITY_GOLD"]
            )
            check(
                "连续换肤：金档及真实价格 token 同步 " + _preset["key"],
                _R2.RARITY_GOLD == _expected_gold
                and _R2._SPAN_COLORS["g"] == _expected_gold
                and _R2._token_style("100杜")[3] == _expected_gold,
            )
        _R2.apply_theme({})
        check(
            "连续换肤后恢复默认：纪元/金档/高亮及span颜色全部还原",
            (_R2.RARITY_GOLD, _R2.GOLD_BRIGHT, dict(_R2.TIER_COLOR), dict(_R2._SPAN_COLORS))
            == _theme_chain_base,
        )
        # 预览样卡的涨跌必须写成真实卡片的「▲12%」：箭头后带空格就不成 token，上下箭头同色。
        _arrows = [t for t in _R2._TOKEN_RE.findall("".join(W.PREVIEW_LINES)) if t[:1] in "▲▼"]
        check("预览样卡：▲/▼ 涨跌按真实写法上色", len(_arrows) == 2, str(_arrows))
        check(
            "象牙切回默认：旧 token 色与图片像素还原",
            {t: _R2._token_style(t) for t in _legacy_tokens} == _legacy_default
            and _IC.difference(base, _img(_rr.render(_T, _L, _F))).getbbox() is None,
        )
        before = _R2.theme_state()["colors"]
        r = _tpost(pt.theme_preview, {"colors": {"panel": "#ff2bd6", "accent": "nothex"}})
        check(
            "★ 预览返回 PNG",
            str(r.get("png", "")).startswith("data:image/png;base64,"),
            str(r)[:120],
        )
        check(
            "★ 预览不改正在生效的主题（套用→渲染→还原）",
            _R2.theme_state()["colors"] == before
            and _R2.PANEL_BG == _R2._THEME_DEFAULTS["PANEL_BG"],
        )
        check(
            "预览图读完即删，预览目录不留文件",
            not list((_td12 / "cards_preview").glob("card_*.png")),
        )
        r = _tpost(
            pt.theme_save,
            {
                "colors": {"accent": "#5ab0ff", "ink": "#ece8de", "x": "#000000"},
                "bg_dim": 5,
                "panel_alpha": 0.1,
            },
        )
        app = json.loads((_td12 / "appearance.json").read_text(encoding="utf-8"))
        check(
            "★ 保存：只存与默认不同的颜色、未知键丢弃、数值夹紧",
            app["theme"]["colors"] == {"accent": "#5ab0ff"}
            and app["theme"]["bg_dim"] == 0.95
            and app["theme"]["panel_alpha"] == 0.3,
            str(app),
        )
        check("★ 保存后立即生效", _R2.GOLD == (90, 176, 255))
        _rr2 = _R2.ImageRenderer(_td12 / "cards")  # 模拟重启：从 appearance.json 读回
        check("重启后从 appearance.json 读回主题", _R2.GOLD == (90, 176, 255))

        # 背景图 / 角标上传
        import io as _io

        def _png(size, mode="RGB"):
            b = _io.BytesIO()
            _PI.new(mode, size, (200, 30, 60) if mode == "RGB" else (200, 30, 60, 128)).save(
                b, "PNG"
            )
            return b.getvalue()

        class _Up2:
            def __init__(self, data):
                self.filename, self._d = "x.png", data

            async def read(self, n=-1):
                return self._d if n < 0 else self._d[:n]

        def _up(fn, data):
            async def _f():
                return _Up2(data)

            pt._upload_file = _f
            return asyncio.run(fn())

        r = _up(pt.theme_upload_bg, _png((3000, 1500)))
        check("★ 背景图上传：缩到最长边 2400", r.get("size") == [2400, 1200], str(r))
        r = _up(pt.theme_upload_logo, _png((800, 800), "RGBA"))
        check(
            "角标上传：缩到 512 并保留透明",
            r.get("size") == [512, 512] and _rr.theme_image("logo").mode == "RGBA",
        )
        check("坏图片被拒", _up(pt.theme_upload_bg, b"not an image" * 100).get("status") == "error")
        _tpost(
            pt.theme_save,
            {"colors": {}, "use_bg": True, "use_logo": True, "panel_alpha": 0.7, "bg_dim": 0.5},
        )
        withbg = _img(_rr.render(_T, _L, _F))
        check(
            "★ 背景图 + 半透明面板 + 角标能出图",
            withbg.size[0] > 0 and _IC.difference(base, withbg).getbbox() is not None,
        )
        _tpost(pt.theme_image_delete, {"kind": "bg"})
        app = json.loads((_td12 / "appearance.json").read_text(encoding="utf-8"))
        check(
            "删除背景图：文件删掉并取消使用",
            not (_td12 / "theme" / "bg.png").exists() and app["theme"]["bg_image"] is False,
        )
        check(
            "非法 kind 被拒",
            _tpost(pt.theme_image_delete, {"kind": "../x"}).get("status") == "error",
        )
finally:
    _R2.apply_theme({})
    _sh.rmtree(_td12, ignore_errors=True)
check(
    "页面调用的主题端点都已注册",
    all(
        f'"{ep}"' in html
        for ep in (
            "theme_preview",
            "theme_save",
            "theme_upload_bg",
            "theme_upload_logo",
            "theme_image_delete",
        )
    ),
)


# ---- 13. 社区数据地址（2026-10-16 维护者定：不想用本项目数据可自填 URL）----
_sch = W.load_schema()
check(
    "schema 有 community_data_url 且归在「数据源」组",
    "community_data_url" in _sch and "community_data_url" in dict(W.SETTINGS_GROUPS)["数据源"],
)
check("社区地址保存即时生效（不在需重载列表）", "community_data_url" not in W.RELOAD_KEYS)


from core.cache import TTLCache as _TTL13  # noqa: E402

_c13 = _TTL13()
asyncio.run(_c13.set("community:valence.json", 1, 600))
asyncio.run(_c13.set("wm:x", 2, 600))


class _Cl13:
    community_base = ""
    cache = _c13


class _P13:
    cfg = {}
    client = _Cl13()


p13 = W.WebPanel(_P13(), PLUGIN)
p13._apply_live({"community_data_url": "https://mirror.example.com/wf/"})
check(
    "★ 保存后客户端立刻换地址（去尾斜杠）",
    _P13.client.community_base == "https://mirror.example.com/wf",
)
check(
    "★ 换地址时丢掉旧地址的社区缓存、不动别的缓存（真 TTLCache）",
    asyncio.run(_c13.get("community:valence.json")) is None and asyncio.run(_c13.get("wm:x")) == 2,
)
p13._apply_live({"community_data_url": "javascript:alert(1)"})
check("非 http(s) 地址等同留空（回落本项目 bot-data）", _P13.client.community_base == "")


async def _community_switch_inflight():
    """真实保存配置→HTTP单飞缓存：切源不能加入旧请求，旧请求也不能重新填缓存。"""
    from types import SimpleNamespace

    import httpx

    from core.api_client import WarframeClient

    class Config(dict):
        def save_config(self):
            pass

    for old_first in (True, False):
        client = WarframeClient(community_base="https://old.example/data", allow_private_urls=True)
        await client._http.aclose()
        started = {h: asyncio.Event() for h in ("old.example", "new.example")}
        release = {h: asyncio.Event() for h in started}
        seen = []

        async def respond(req):
            host = req.url.host
            seen.append(host)
            started[host].set()
            await release[host].wait()
            return httpx.Response(
                200,
                json={
                    "source": host,
                    "_updated": "2026-10-01T00:00:00+00:00",
                    "nodes": {f"probe-{i}": {"n": 10, "tier": "A"} for i in range(50)},
                },
            )

        client._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        panel = W.WebPanel(SimpleNamespace(client=client, _config=Config(), cfg={}), PLUGIN)

        async def body():
            return {"changes": {"community_data_url": "https://new.example/data"}}

        panel._body = body
        urls = ("https://fallback.example/arb_ratings.json",)
        tasks = []
        try:
            await client.cache.set("wm:keep", "unchanged", 600)
            old = asyncio.create_task(client._community_json(urls))
            tasks.append(old)
            await asyncio.wait_for(started["old.example"].wait(), 2)
            saved = await panel.settings_save()
            check("在飞切源通过真实配置保存入口", saved.get("saved") == ["community_data_url"])
            new = asyncio.create_task(client._community_json(urls))
            tasks.append(new)
            try:
                await asyncio.wait_for(started["new.example"].wait(), 0.5)
                independent = True
            except asyncio.TimeoutError:
                independent = False
            check("切源后新请求不加入旧地址的在飞请求", independent, str(seen))
            if not independent:
                release["old.example"].set()
                await asyncio.gather(*tasks)
                continue
            if old_first:
                release["old.example"].set()
                await old
                follower = asyncio.create_task(client._community_json(urls))
                tasks.append(follower)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                check("旧请求退出不移除新请求的单飞登记", seen == ["old.example", "new.example"])
                release["new.example"].set()
                check("新地址等待者收到新数据", (await follower)["source"] == "new.example")
            else:
                release["new.example"].set()
                await new
                release["old.example"].set()
            await asyncio.gather(*tasks)
            latest = await client._community_json(urls)
            check(
                f"切源缓存保留新数据（旧请求先结束={old_first}）",
                latest["source"] == "new.example"
                and seen == ["old.example", "new.example"]
                and await client.cache.get("wm:keep") == "unchanged",
                str((latest, seen)),
            )
        finally:
            for gate in release.values():
                gate.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.close()


asyncio.run(_community_switch_inflight())


# ---- 14. 社区快照通道记录（2026-10-17 批次 B 全链路健康记录·插件侧）----
from core import api_client as _AC14  # noqa: E402
from core import paths as _paths14  # noqa: E402

_rt14 = Path(tempfile.mkdtemp(prefix="_tmp_comm_log_"))
try:
    _paths14.set_run_dir(_rt14)
    _c14 = _AC14.WarframeClient()
    _seed14 = json.loads((ROOT / "core/data/arb_ratings.json").read_text(encoding="utf-8"))
    _serve14 = {}

    async def _ff14(url, ttl=0, headers=None, cache_key=None, retries=2, **kw):
        h = url.split("/")[2]
        name = url.rsplit("/", 1)[-1]
        if "raw.githubusercontent" in h:
            raise _AC14.WarframeAPIError(f"请求失败（{url}）：timeout")
        if name in _serve14:
            return _serve14[name]
        raise _AC14.WarframeAPIError(f"接口不存在或暂未开放：{url}")

    _c14._fetch_json = _ff14
    rep0 = {r["name"]: r for r in _c14.community_report()}
    check(
        "没检查过的快照显示 unchecked，四份齐全",
        len(rep0) == 4 and all(r["result"] == "unchecked" for r in rep0.values()),
    )
    _now14 = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    _newer = dict(_seed14, _updated=_now14)
    _serve14 = {
        "arb_ratings.json": _newer,
        "baro_history.json": {"fetched": _now14, "visits": []},
    }
    asyncio.run(_c14.refresh_community_static())
    rep = {r["name"]: r for r in _c14.community_report()}
    check(
        "★ 采用新快照：记 adopted + 走的通道 + 数据时间",
        rep["arb_ratings.json"]["result"] == "adopted"
        and rep["arb_ratings.json"]["channel"] == "cdn.jsdelivr.net"
        and rep["arb_ratings.json"]["stamp"] == _now14,
        str(rep["arb_ratings.json"]),
    )
    check(
        "★ 校验不过：记 invalid + 原因",
        rep["baro_history.json"]["result"] == "invalid"
        and "到访记录太少" in rep["baro_history.json"]["reason"],
        str(rep["baro_history.json"]),
    )
    _serve14 = {"arb_ratings.json": _newer}
    asyncio.run(_c14.refresh_community_static(("baro_history.json",)))
    rep = {r["name"]: r for r in _c14.community_report()}
    check(
        "★ 所有通道取不到：记 unavailable + 各通道原因（raw 超时 / 镜像没文件）",
        rep["baro_history.json"]["result"] == "unavailable"
        and "没有这个文件" in rep["baro_history.json"]["reason"],
        str(rep["baro_history.json"]),
    )
    asyncio.run(_c14.refresh_community_static(("arb_ratings.json",)))
    check("同一份再拉：fresh", _c14.community_log["arb_ratings.json"]["result"] == "fresh")
    asyncio.run(_c14.close())

    class _P14:
        cfg = {}
        client = _c14
        subs = type("S", (), {"all": lambda self: []})()
        push = _Push()
        groups = None

    _pn14 = W.WebPanel(_P14(), PLUGIN)
    _pn14._query = staticmethod(lambda k, d="": d)  # 测试环境没有 web 请求对象
    ov14 = asyncio.run(_pn14.overview())
    check(
        "面板概览带社区快照记录与地址",
        isinstance(ov14.get("community"), list)
        and len(ov14["community"]) == 4
        and "community_base" in ov14,
        str(ov14.get("community"))[:200],
    )
finally:
    _paths14.set_run_dir(None)
    import shutil as _sh14

    _sh14.rmtree(_rt14, ignore_errors=True)
check("页面渲染社区快照通道表", 'id="comm"' in html and "renderComm(" in html)
# 2026-10-17（V02）：自填社区地址取不到时，通道表上方提示根因 + 一键恢复默认（清空设置后立即重拉）
check(
    "页面：自填社区地址不可用提示区与「恢复默认地址」按钮",
    'id="commCustom"' in html
    and "恢复默认地址" in html
    and 'apiPost("settings_save", { changes: { community_data_url: "" } })' in html,
)
check("页面：自填地址取不到时「需要处理」合并为一条", "customBad" in html)
_sch = W.load_schema()
check(
    "设置：community_data_url 允许清空（恢复默认地址按钮依赖）",
    "community_data_url" in _sch and W.coerce_setting(_sch["community_data_url"], "") == "",
)

# ---- 关于页（2026-10-26）----
_ab = W.about_info(now=W._LOADED_AT + 3700)
check(
    "关于：版本 / 许可 / 公开仓 / 主指令数 / 运行时长",
    _ab["plugin"]["version"]
    and _ab["plugin"]["license"] == "GPL-3.0"
    and _ab["plugin"]["repo"].startswith("https://github.com/")
    and _ab["plugin"]["commands"] > 0
    and _ab["runtime"]["uptime_s"] == 3700
    and _ab["runtime"]["python"],
    str(_ab["plugin"]),
)
check(
    "关于：数据版本读得到伤害数据 wiki 快照、紫卡抽样日期，登记表计数与登记表一致",
    _ab["data_versions"]["damage_wiki_snapshot"].startswith("wiki-modules-")
    and _ab["data_versions"]["riven_survey_date"]
    and sum(_ab["data_versions"]["registry_status"].values())
    == _ab["data_versions"]["registry_total"],
    str(_ab["data_versions"]),
)
check(
    "★ 关于：顶层不得出现 `data` 键（AstrBot 桥会 r.data.data ?? r.data，把整包挤掉）",
    "data" not in _ab and "data_versions" in _ab,
    str(list(_ab.keys())),
)
check(
    "关于：数据来源表含 wiki（CC BY-NC-SA）与字体 OFL，且不含本机路径",
    any("CC BY-NC-SA" in x["license"] for x in _ab["sources"])
    and any("OFL" in x["license"] for x in _ab["sources"])
    and "/root" not in str(_ab)
    and "/home" not in str(_ab),
)
check(
    "页面：导航与视图都有「关于」，进入时才加载",
    'data-go="about"' in html and 'data-view="about"' in html and "loadAbout()" in html,
)


async def _background_overview_checks():
    """实际 Task 与检查记录交叉校正，查询接口不启动/推进后台任务。"""
    from unittest.mock import patch

    panel = _panel()
    panel._query = lambda *args: "0"
    with (
        patch.object(W, "freshness", return_value=[]),
        patch.object(W, "registry_rows", return_value=[]),
        patch.object(W, "ledger_rows", return_value=[]),
    ):
        old = await panel.overview()
        old_rows = {r["key"]: r for r in old["background"]}
        check(
            "旧stub无后台字段仍返回9行且手动任务空闲",
            len(old_rows) == 9
            and all(
                old_rows[k]["state"] == "idle" for k in ("manual_rank", "manual_riven", "font")
            ),
        )
        check(
            "没有任务详情时保持空白，不伪造上游故障",
            all(r["detail"] == "" for r in old_rows.values()),
        )
        check(
            "未知后台时间返回null，不伪造成功或计划",
            all(r["last_success"] is None and r["next_run"] is None for r in old_rows.values()),
        )

        async def wait():
            await asyncio.Event().wait()

        async def complete():
            return None

        async def fail():
            raise RuntimeError("secret-token at http://127.0.0.1:8191/private")

        now = time.time()
        host = panel.p
        host._auto_task = asyncio.create_task(wait())
        host._valence_task = asyncio.create_task(complete())
        host._revision_task = asyncio.create_task(wait())
        host._community_task = asyncio.create_task(fail())
        host._boot_task = asyncio.create_task(complete())
        host._warm_task = asyncio.create_task(wait())
        host.client._rank_crawl_task = asyncio.create_task(wait())
        host.client._riven_rt_crawl_task = asyncio.create_task(complete())
        panel._font_task = asyncio.create_task(complete())
        panel.font_job = {"state": "failed", "log": ["secret-token"]}
        tasks = [
            host._auto_task,
            host._valence_task,
            host._revision_task,
            host._community_task,
            host._boot_task,
            host._warm_task,
            host.client._rank_crawl_task,
            host.client._riven_rt_crawl_task,
            panel._font_task,
        ]
        await asyncio.sleep(0)
        host._warm_task.cancel()
        await asyncio.gather(host._warm_task, return_exceptions=True)
        host._background_status = {
            "rank": {
                "state": "waiting",
                "result": "ok",
                "last_attempt": now - 100,
                "last_finished": now - 90,
                "last_success": now - 90,
                "next_run": now + 30,
            },
            "valence": {"state": "waiting", "result": "ok", "next_run": now + 60},
            "revision": {
                "state": "waiting",
                "result": "degraded",
                "detail": "修订检查失败：言录使",
                "last_attempt": now - 20,
                "next_run": now + 1800,
            },
            "community": {
                "state": "running",
                "result": "error",
                "last_error": "ValueError: secret-token http://127.0.0.1:8191/private",
                "last_error_at": now - 10,
            },
            "boot": {"state": "completed", "result": "ok", "last_success": now - 50},
        }
        before = json.dumps(host._background_status, sort_keys=True)
        try:

            def forbidden(*args, **kwargs):
                raise AssertionError("overview must not start or refresh tasks")

            host.client.start_rank_crawl = forbidden
            host.client.start_riven_rt_crawl = forbidden
            host.client.refresh_valence = forbidden
            host.client.refresh_community_static = forbidden
            host.client.check_rotation_revisions = forbidden
            ov = await panel.overview()
            rows = {r["key"]: r for r in ov["background"]}
            check(
                "常驻任务实际存活且等待，保留最近正常检查及下次计划",
                rows["rank"]["alive"]
                and rows["rank"]["state"] == "waiting"
                and rows["rank"]["last_success"] == now - 90
                and rows["rank"]["next_run"] == now + 30,
            )
            check(
                "文档修订任务独立显示失败退避，不冒充数据更新时间",
                rows["revision"]["alive"]
                and rows["revision"]["state"] == "waiting"
                and rows["revision"]["status"] == W.WARN
                and rows["revision"]["detail"] == "修订检查失败：言录使"
                and rows["revision"]["last_attempt"] == now - 20
                and rows["revision"]["last_success"] is None
                and rows["revision"]["next_run"] == now + 1800,
            )
            host._revision_task.cancel()
            await asyncio.gather(host._revision_task, return_exceptions=True)
            stopped = next(r for r in panel.background_rows() if r["key"] == "revision")
            check(
                "文档修订任务取消后不显示存活或下次计划",
                not stopped["alive"]
                and stopped["state"] == "cancelled"
                and stopped["next_run"] is None,
            )
            check(
                "元数据虽待运行但常驻Task已结束，提示停止且清掉计划展示",
                not rows["valence"]["alive"]
                and rows["valence"]["state"] == "failed"
                and rows["valence"]["status"] == W.STALE
                and rows["valence"]["next_run"] is None,
            )
            check(
                "异常退出只展示实际异常类型",
                rows["community"]["state"] == "failed"
                and rows["community"]["last_error"] == "RuntimeError",
            )
            check(
                "一次性启动预热正常结束不是故障",
                rows["boot"]["state"] == "completed" and rows["boot"]["status"] == W.OK,
            )
            check(
                "取消任务不误标存活",
                rows["warmup"]["state"] == "cancelled" and not rows["warmup"]["alive"],
            )
            check(
                "手动任务运行/正常完成分开显示",
                rows["manual_rank"]["alive"]
                and rows["manual_rank"]["state"] == "running"
                and rows["manual_riven"]["state"] == "completed"
                and rows["manual_riven"]["status"] == W.OK,
            )
            check(
                "字体worker内部已捕获的失败仍可见",
                rows["font"]["state"] == "failed" and rows["font"]["result"] == "error",
            )
            check(
                "后台概览不修改检查记录且不暴露异常连接或凭据",
                before == json.dumps(host._background_status, sort_keys=True)
                and "secret-token" not in json.dumps(ov["background"])
                and "127.0.0.1" not in json.dumps(ov["background"]),
            )
            host._background_status["rank"] = {
                "state": "completed",
                "last_success": float("nan"),
                "next_run": float("inf"),
            }
            corrected = {r["key"]: r for r in panel.background_rows()}["rank"]
            check(
                "实际活动Task校正陈旧completed状态，畸形时间归未知",
                corrected["alive"]
                and corrected["state"] == "running"
                and corrected["last_success"] is None
                and corrected["next_run"] is None,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


asyncio.run(_background_overview_checks())
check(
    "概览后台任务表沿用安全节点渲染并解释成功时间口径",
    'id="background"' in html
    and "renderBackground(ov.background)" in html
    and "最近正常检查" in html
    and "不是数据更新时间" in html,
)


async def _preview_lifecycle_checks():
    """真实预览/保存入口：取消 HTTP 等待后，线程仍须持锁直到主题恢复。"""
    import threading
    from types import SimpleNamespace

    from core import render as render_module

    original = render_module.theme_state()
    try:
        for mode in ("normal", "failure", "cancel", "late_failure"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                renderer = render_module.ImageRenderer(root / "cards")

                class Host:
                    _render_lock = asyncio.Lock()

                host = Host()
                host.renderer = renderer
                panel = W.WebPanel(host, PLUGIN)
                entered, release = threading.Event(), threading.Event()
                save_entered = asyncio.Event()
                png_path = root / "preview.png"

                def preview_render(*args):
                    entered.set()
                    if mode in ("cancel", "late_failure"):
                        if not release.wait(5):
                            raise TimeoutError("测试未释放预览线程")
                    if mode in ("failure", "late_failure"):
                        raise RuntimeError("preview test failure")
                    png_path.write_bytes(_b64.b64decode(W._png_data_url().split(",", 1)[1]))
                    return str(png_path)

                panel._preview_r = SimpleNamespace(render=preview_render)
                body = {"colors": {"accent": "#ff0000"}}

                async def get_body():
                    if body["colors"]["accent"] == "#123456":
                        save_entered.set()
                    return body

                panel._body = get_body
                preview, saving = None, None
                with _patch.object(W.logger, "warning") as warning:
                    try:
                        preview = asyncio.create_task(panel.theme_preview())
                        if mode in ("cancel", "late_failure"):
                            assert await asyncio.to_thread(entered.wait, 3)
                            preview.cancel()
                            try:
                                await asyncio.wait_for(preview, 1)
                            except asyncio.CancelledError:
                                pass
                            check(
                                f"预览{mode}：取消立即返回但线程仍占锁",
                                preview.cancelled()
                                and Host._render_lock.locked()
                                and bool(panel._render_tasks)
                                and not release.is_set(),
                            )
                        else:
                            result = await asyncio.wait_for(preview, 3)
                            check(
                                f"预览{mode}：正常返回或明确失败",
                                bool(result.get("png"))
                                if mode == "normal"
                                else result.get("status") == "error",
                            )
                            check(
                                f"预览{mode}：恢复原主题并删除临时图片",
                                render_module.GOLD == render_module._THEME_DEFAULTS["GOLD"]
                                and not png_path.exists(),
                            )
                        body = {"colors": {"accent": "#123456"}}
                        saving = asyncio.create_task(panel.theme_save())
                        await asyncio.wait_for(save_entered.wait(), 2)
                        if mode in ("cancel", "late_failure"):
                            check(
                                f"预览{mode}：保存等候旧预览恢复主题",
                                not saving.done() and Host._render_lock.locked(),
                            )
                            release.set()
                        result = await asyncio.wait_for(saving, 3)
                        await asyncio.sleep(0)
                        check(
                            f"预览{mode}：后续保存的主题与运行状态一致",
                            "saved" in result
                            and renderer.load_appearance()["theme"]["colors"]["accent"] == "#123456"
                            and render_module.GOLD == (18, 52, 86),
                        )
                        check(
                            f"预览{mode}：线程任务与临时图片均已收尾",
                            not panel._render_tasks and not png_path.exists(),
                        )
                        if mode == "late_failure":
                            check(
                                "取消预览后线程异常已取回并记录类型",
                                warning.call_count == 1
                                and warning.call_args.args[-1] == "RuntimeError",
                            )
                    finally:
                        release.set()
                        pending = list(panel._render_tasks)
                        if saving is not None:
                            pending.append(saving)
                        if preview is not None:
                            pending.append(preview)
                        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        render_module.apply_theme(
            original["colors"],
            bg_image=original["bg_image"],
            bg_dim=original["bg_dim"],
            panel_alpha=original["panel_alpha"],
            logo=original["logo"],
        )


asyncio.run(_preview_lifecycle_checks())


async def _theme_upload_concurrency_checks():
    """真实上传入口：并发各用自己的临时文件，失败只清自己的半成品。"""
    import io
    import threading
    from types import SimpleNamespace

    from PIL import Image

    for kind in ("bg", "logo"):
        with tempfile.TemporaryDirectory(prefix="wf-theme-concurrent-") as directory:
            root = Path(directory)
            panel = W.WebPanel(
                SimpleNamespace(
                    renderer=SimpleNamespace(theme_dir=root, reload_theme=lambda: None)
                ),
                PLUGIN,
            )
            payload = io.BytesIO()
            Image.new("RGB", (32, 32), "red").save(payload, "PNG")

            class Upload:
                async def read(self, size):
                    return payload.getvalue()

            async def upload():
                return Upload()

            panel._upload_file = upload
            endpoint = panel.theme_upload_bg if kind == "bg" else panel.theme_upload_logo
            barrier = threading.Barrier(2)
            original_save = Image.Image.save
            targets = []

            def concurrent_save(image, target, *args, **kwargs):
                targets.append(Path(target))
                result = original_save(image, target, *args, **kwargs)
                barrier.wait(timeout=5)
                return result

            with _patch.object(Image.Image, "save", concurrent_save):
                results = await asyncio.gather(endpoint(), endpoint())
            check(
                f"{kind} 并发上传：两个合法请求都成功",
                all(result.get("kind") == kind for result in results),
                str(results),
            )
            check(f"{kind} 并发上传：临时路径各自独立", len(set(targets)) == 2)
            destination = root / f"{kind}.png"
            with Image.open(destination) as image:
                image.load()
                check(
                    f"{kind} 并发上传：最终 PNG 有效",
                    image.format == "PNG" and image.size == (32, 32),
                )
            check(f"{kind} 并发上传：无半成品残留", not list(root.glob("*.part")))

            existing = destination.read_bytes()
            unrelated = root / "other-request.png.part"
            unrelated.write_bytes(b"another request")
            targets.clear()

            def failed_save(image, target, *args, **kwargs):
                target = Path(target)
                targets.append(target)
                target.write_bytes(b"incomplete PNG")
                raise OSError("simulated write failure")

            with _patch.object(Image.Image, "save", failed_save):
                result = await endpoint()
            check(f"{kind} 上传写入失败：返回错误", result.get("status") == "error")
            check(f"{kind} 上传写入失败：保留原图", destination.read_bytes() == existing)
            check(
                f"{kind} 上传写入失败：清理自己的半成品",
                len(targets) == 1 and not targets[0].exists(),
            )
            check(
                f"{kind} 上传写入失败：保留其它请求文件",
                unrelated.read_bytes() == b"another request",
            )


asyncio.run(_theme_upload_concurrency_checks())


async def _font_exclusive_publish_checks():
    """跨面板、上传与 URL 导入共用同一目标：先发布者胜出，清理仅限本请求。"""
    from types import SimpleNamespace

    import httpx

    regular = (ROOT / "core/data/fonts/NotoSansCJKsc-Subset-Regular.otf").read_bytes()
    bold = (ROOT / "core/data/fonts/NotoSansCJKsc-Subset-Bold.otf").read_bytes()

    class Upload:
        filename = "custom.otf"

        def __init__(self, data, entered=None, release=None):
            self.data, self.entered, self.release = data, entered, release

        async def read(self, count):
            if self.entered is not None:
                self.entered.set()
                await self.release.wait()
            return self.data[:count]

    def panel_for(root, uploaded):
        panel = W.WebPanel(
            SimpleNamespace(renderer=SimpleNamespace(user_font_dir=root), cfg={}), PLUGIN
        )

        async def upload():
            return uploaded

        panel._upload_file = upload
        panel._fonts_state = lambda: {}
        return panel

    for mode in ("upload", "import_late", "import_first"):
        with tempfile.TemporaryDirectory(prefix="wf-font-exclusive-") as directory:
            root = Path(directory)
            destination = root / "custom.otf"
            unrelated = root / "custom.otf.part"
            unrelated.write_bytes(b"other request")
            entered, release = asyncio.Event(), asyncio.Event()
            slow = panel_for(root, Upload(regular, entered, release))
            fast = panel_for(root, Upload(bold))
            pending = None
            try:
                if mode == "upload":
                    pending = asyncio.create_task(slow.font_upload())
                    await entered.wait()
                    winner = await fast.font_upload()
                    release.set()
                    loser = await pending
                    check(
                        "跨面板同名上传：先完成成功、较慢拒绝",
                        winner.get("saved") and loser.get("status") == "error",
                    )
                else:
                    importer = slow if mode == "import_late" else fast
                    importer.p.cfg["allow_private_urls"] = True

                    class Stream(httpx.AsyncByteStream):
                        async def __aiter__(self):
                            if mode == "import_late":
                                entered.set()
                                await release.wait()
                            yield regular if mode == "import_late" else bold

                    async def handler(request):
                        return httpx.Response(200, stream=Stream())

                    importer._http_factory = lambda **kw: httpx.AsyncClient(
                        transport=httpx.MockTransport(handler), **kw
                    )

                    async def body():
                        return {"url": "https://fonts.example/custom.otf"}

                    importer._body = body
                    if mode == "import_first":
                        pending = asyncio.create_task(slow.font_upload())
                        await entered.wait()
                    await importer.font_import_url()
                    if mode == "import_late":
                        await entered.wait()
                        winner = await fast.font_upload()
                        release.set()
                    await importer._font_task
                    if mode == "import_first":
                        release.set()
                        loser = await pending
                        check(
                            "导入先完成：较慢上传拒绝",
                            importer.font_job["state"] == "done" and loser.get("status") == "error",
                        )
                    else:
                        check(
                            "上传先完成：较慢导入失败",
                            winner.get("saved") and importer.font_job["state"] == "failed",
                        )
                check(f"{mode}：先发布字体逐字节保留", destination.read_bytes() == bold)
                check(f"{mode}：无关半成品保留", unrelated.read_bytes() == b"other request")
                check(f"{mode}：自己的半成品已清理", set(root.glob("*.part")) == {unrelated})
            finally:
                release.set()
                tasks = [task for task in (pending, slow._font_task, fast._font_task) if task]
                await asyncio.gather(*tasks, return_exceptions=True)

    for mode in ("download_race", "repair_race", "repair", "repair_failure"):
        with tempfile.TemporaryDirectory(prefix="wf-font-download-") as directory:
            root = Path(directory)
            destination = root / "custom.otf"
            if mode != "download_race":
                destination.write_bytes(b"old small font")
            unrelated = root / "other-request.part"
            unrelated.write_bytes(b"other request")
            entered, release = asyncio.Event(), asyncio.Event()
            downloader = panel_for(root, Upload(regular))
            downloader.p.renderer.reload_fonts = lambda: None
            requested = []

            class DownloadStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    if mode in ("download_race", "repair_race"):
                        entered.set()
                        await release.wait()
                    yield bold

            async def download_handler(request):
                requested.append(str(request.url))
                return httpx.Response(
                    500 if mode == "repair_failure" else 200, stream=DownloadStream()
                )

            downloader._http_factory = lambda **kw: httpx.AsyncClient(
                transport=httpx.MockTransport(download_handler), **kw
            )
            try:
                with (
                    _patch.object(W, "_MIN_FONT_BYTES", len(bold) - 1),
                    _patch.object(
                        W,
                        "FULL_FONTS",
                        {
                            "custom.otf": [
                                "https://fonts.example/a.otf",
                                "https://fonts.example/b.otf",
                            ]
                        },
                    ),
                ):
                    await downloader.font_download()
                    if mode in ("download_race", "repair_race"):
                        await entered.wait()
                        if mode == "repair_race":
                            destination.unlink()  # 模拟管理员删除原小文件后重新上传。
                        winner = await panel_for(root, Upload(regular)).font_upload()
                        check(
                            f"{mode}：完整下载等待时同名上传成功",
                            winner.get("saved") == "custom.otf",
                        )
                        release.set()
                    await downloader._font_task
                expected = (
                    regular
                    if mode in ("download_race", "repair_race")
                    else bold
                    if mode == "repair"
                    else b"old small font"
                )
                check(f"{mode}：目标内容符合预期", destination.read_bytes() == expected)
                check(f"{mode}：自己的半成品已清理", set(root.glob("*.part")) == {unrelated})
                check(f"{mode}：无关文件保留", unrelated.read_bytes() == b"other request")
                if mode in ("download_race", "repair_race"):
                    check(
                        f"{mode}：回退源也拒绝覆盖新字体",
                        len(requested) == 2 and downloader.font_job["state"] == "failed",
                    )
                if mode not in ("download_race", "repair_race"):
                    check(
                        f"{mode}：任务结果符合预期",
                        downloader.font_job["state"] == ("done" if mode == "repair" else "failed"),
                    )
            finally:
                release.set()
                if downloader._font_task is not None:
                    await asyncio.gather(downloader._font_task, return_exceptions=True)

    for mode in ("oversize", "broken", "cancel"):
        with tempfile.TemporaryDirectory(prefix="wf-font-cleanup-") as directory:
            root = Path(directory)
            existing = root / "existing.otf"
            existing.write_bytes(bold)
            unrelated = root / "custom.otf.part"
            unrelated.write_bytes(b"other request")
            entered, release = asyncio.Event(), asyncio.Event()
            uploaded = Upload(
                b"not a font" * W._IMPORT_MIN_BYTES if mode == "broken" else regular,
                entered if mode == "cancel" else None,
                release,
            )
            panel = panel_for(root, uploaded)
            if mode == "cancel":
                task = asyncio.create_task(panel.font_upload())
                await entered.wait()
                task.cancel()
                result = (await asyncio.gather(task, return_exceptions=True))[0]
                check("上传取消：保持取消状态", isinstance(result, asyncio.CancelledError))
            else:
                limit = len(regular) - 1 if mode == "oversize" else W._IMPORT_MAX_BYTES
                with _patch.object(W, "_IMPORT_MAX_BYTES", limit):
                    result = await panel.font_upload()
                check(f"上传{mode}：拒绝无效内容", result.get("status") == "error")
            check(f"上传{mode}：未发布目标", not (root / "custom.otf").exists())
            check(f"上传{mode}：只清自己半成品", set(root.glob("*.part")) == {unrelated})
            check(
                f"上传{mode}：保留原字体与无关文件",
                existing.read_bytes() == bold and unrelated.read_bytes() == b"other request",
            )


asyncio.run(_font_exclusive_publish_checks())


async def _panel_late_request_checks():
    """请求体/上传等待后停用：真实入口拒绝尚未开始的写操作及外呼。"""
    import io
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from PIL import Image

    async def run_request(panel, endpoint, pause, close):
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait():
            entered.set()
            await release.wait()

        pause(wait)
        task = asyncio.create_task(endpoint())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if close:
                panel.close()
            release.set()
            return await asyncio.wait_for(task, 3)
        finally:
            release.set()
            await asyncio.gather(task, *list(panel._render_tasks), return_exceptions=True)

    bodies = {
        "crawl": {"target": "wm_ranks"},
        "conntest": {"target": "vision"},
        "font_select": {"slot": "regular", "id": "auto"},
        "font_delete": {"id": "probe"},
        "settings_save": {"changes": {"render_mode": "text"}},
        "sub_remove": {"sid": "abcd"},
        "group_push": {"key": "probe", "push": False},
        "push_resume": {"key": "probe"},
        "theme_preview": {"colors": {"accent": "#123456"}},
        "theme_save": {"colors": {"accent": "#123456"}},
        "theme_image_delete": {"kind": "logo"},
    }
    for route, body in bodies.items():
        for close in (False, True):
            with tempfile.TemporaryDirectory(prefix="wf-panel-body-") as directory:
                root = Path(directory)
                appearance = root / "appearance.json"
                appearance.write_text('{"font_regular":"old","theme":{"logo":true}}')
                logo, font = root / "logo.png", root / "probe.ttf"
                logo.write_bytes(b"existing logo")
                font.write_bytes(b"existing font")
                renderer = SimpleNamespace(
                    appearance_path=appearance,
                    theme_dir=root,
                    user_font_dir=root,
                    load_appearance=lambda: json.loads(appearance.read_text()),
                    reload_theme=Mock(),
                    reload_fonts=Mock(),
                )

                class Config(dict):
                    def save_config(self):
                        self.saved = True

                conf = Config()
                conf.saved = False
                client = SimpleNamespace(start_rank_crawl=Mock(return_value=(True, 0, 0)))
                subs = SimpleNamespace(remove=AsyncMock(return_value=1))
                groups = SimpleNamespace(set_switch=AsyncMock(), set_pause=AsyncMock())

                class Host:
                    _render_lock = asyncio.Lock()

                host = Host()
                host.renderer, host.client, host.subs = renderer, client, subs
                host.groups, host._config, host.cfg = groups, conf, {}
                panel = W.WebPanel(host, PLUGIN)
                panel._fonts_state = lambda: {}
                panel._font_path_of = lambda *args: font
                panel._umo_of = lambda key: "probe-session"
                panel._test_vision_resolve = Mock(return_value={"ok": True})
                if route == "theme_preview":
                    panel._locked = AsyncMock(return_value="data:image/png;base64,probe")
                before = appearance.read_bytes()

                def pause(wait):
                    async def get_body():
                        await wait()
                        return body

                    panel._body = get_body

                with _patch.object(W, "freshness", return_value=[]):
                    response = await run_request(panel, getattr(panel, route), pause, close)
                effects = {
                    "crawl": client.start_rank_crawl.call_count > 0,
                    "conntest": panel._test_vision_resolve.call_count > 0,
                    "font_select": renderer.reload_fonts.call_count > 0,
                    "font_delete": not font.exists(),
                    "settings_save": conf.saved,
                    "sub_remove": subs.remove.await_count > 0,
                    "group_push": groups.set_switch.await_count > 0,
                    "push_resume": groups.set_pause.await_count > 0,
                    "theme_preview": (
                        panel._locked.await_count > 0 if route == "theme_preview" else False
                    ),
                    "theme_save": appearance.read_bytes() != before,
                    "theme_image_delete": not logo.exists(),
                }
                check(
                    f"请求体等待{route}：{'停用后拒绝' if close else '仍启用正常执行'}",
                    not effects[route] and response.get("status") == "error"
                    if close
                    else effects[route] and response.get("status") != "error",
                    str(response),
                )
                if close:
                    check(
                        f"请求体等待{route}：原外观与图片字体保留",
                        appearance.read_bytes() == before and logo.exists() and font.exists(),
                    )

    for route in ("font_upload", "theme_upload_bg", "theme_upload_logo"):
        for phase in ("multipart", "read"):
            for close in (False, True):
                with tempfile.TemporaryDirectory(prefix="wf-panel-upload-close-") as directory:
                    root = Path(directory)
                    payload = io.BytesIO()
                    Image.new("RGB", (32, 32), "red").save(payload, "PNG")
                    data = (
                        b"probe-font" * W._IMPORT_MIN_BYTES
                        if route == "font_upload"
                        else payload.getvalue()
                    )
                    renderer = SimpleNamespace(
                        theme_dir=root, user_font_dir=root, reload_theme=lambda: None
                    )
                    panel = W.WebPanel(SimpleNamespace(renderer=renderer), PLUGIN)
                    panel._fonts_state = lambda: {}
                    panel._check_cjk_font = lambda path: ""

                    def pause(wait):
                        class Upload:
                            filename = "probe.ttf"

                            async def read(self, size):
                                if phase == "read":
                                    await wait()
                                return data[:size]

                        async def upload():
                            if phase == "multipart":
                                await wait()
                            return Upload()

                        panel._upload_file = upload

                    response = await run_request(panel, getattr(panel, route), pause, close)
                    name = {
                        "font_upload": "probe.ttf",
                        "theme_upload_bg": "bg.png",
                        "theme_upload_logo": "logo.png",
                    }[route]
                    check(
                        f"上传等待{route}/{phase}：{'停用后不发布' if close else '仍启用正常发布'}",
                        not (root / name).exists() and response.get("status") == "error"
                        if close
                        else (root / name).exists() and response.get("status") != "error",
                        str(response),
                    )
                    check(
                        f"上传等待{route}/{phase}/{close}：无本请求半成品",
                        not list(root.glob("*.part")),
                    )


asyncio.run(_panel_late_request_checks())


async def _panel_queued_render_close_checks():
    """关闭前排队的操作不能开工；已开工线程仍执行完整收尾。"""
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock

    for close in (False, True):

        class Host:
            _render_lock = asyncio.Lock()

        panel = W.WebPanel(Host(), PLUGIN)
        fn = Mock(return_value="done")
        await Host._render_lock.acquire()
        task = asyncio.create_task(panel._locked(fn))
        try:
            while not panel._render_tasks:
                await asyncio.sleep(0)
            if close:
                panel.close()
            Host._render_lock.release()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            check(
                f"渲染排队：{'停用后不启动线程' if close else '正常获得锁执行'}",
                isinstance(result, W._PanelClosed) and fn.call_count == 0
                if close
                else result == "done" and fn.call_count == 1,
            )
        finally:
            if Host._render_lock.locked():
                Host._render_lock.release()
            await asyncio.gather(task, return_exceptions=True)

    for route in ("theme_preview", "theme_save", "theme_image_delete"):
        for close in (False, True):
            with tempfile.TemporaryDirectory(prefix="wf-panel-queue-route-") as directory:
                root = Path(directory)
                appearance, logo = root / "appearance.json", root / "logo.png"
                appearance.write_text('{"font_regular":"kept","theme":{"logo":true}}')
                logo.write_bytes(b"original logo bytes")
                before, logo_before = appearance.read_bytes(), logo.read_bytes()

                class Host:
                    _render_lock = asyncio.Lock()

                host = Host()
                host.renderer = SimpleNamespace(
                    theme_dir=root,
                    appearance_path=appearance,
                    load_appearance=lambda: json.loads(appearance.read_text()),
                    reload_theme=Mock(),
                    fonts=None,
                )
                panel = W.WebPanel(host, PLUGIN)
                preview_path = root / "preview.png"

                def preview(*args):
                    preview_path.write_bytes(_b64.b64decode(W._png_data_url().split(",", 1)[1]))
                    return str(preview_path)

                panel._preview_r = SimpleNamespace(render=preview)

                async def body():
                    return {"kind": "logo", "colors": {"accent": "#123456"}}

                panel._body = body
                await Host._render_lock.acquire()
                task = asyncio.create_task(getattr(panel, route)())
                try:
                    while not panel._render_tasks:
                        await asyncio.sleep(0)
                    check(
                        f"渲染排队{route}/{close}：取得锁前配置与图片字节不变",
                        appearance.read_bytes() == before and logo.read_bytes() == logo_before,
                    )
                    if close:
                        panel.close()
                    Host._render_lock.release()
                    response = await asyncio.wait_for(task, 2)
                    if close:
                        check(
                            f"渲染排队{route}：停用后拒绝完整提交且保留原字节",
                            response == panel._gone()
                            and host.renderer.reload_theme.call_count == 0
                            and appearance.read_bytes() == before
                            and logo.read_bytes() == logo_before,
                            str(response),
                        )
                    else:
                        check(
                            f"渲染排队{route}：启用时完整提交正常成功",
                            response.get("status") != "error"
                            and (
                                bool(response.get("png")) and appearance.read_bytes() == before
                                if route == "theme_preview"
                                else host.renderer.reload_theme.call_count == 1
                                and appearance.read_bytes() != before
                                and (
                                    not logo.exists()
                                    if route == "theme_image_delete"
                                    else logo.read_bytes() == logo_before
                                )
                            ),
                            str(response),
                        )
                finally:
                    if Host._render_lock.locked():
                        Host._render_lock.release()
                    await asyncio.gather(task, *list(panel._render_tasks), return_exceptions=True)

    for route in ("theme_save", "theme_image_delete"):
        with tempfile.TemporaryDirectory(prefix="wf-panel-running-commit-") as directory:
            root = Path(directory)
            appearance, logo = root / "appearance.json", root / "logo.png"
            appearance.write_text('{"font_regular":"kept","theme":{"logo":true}}')
            logo.write_bytes(b"original")
            entered, release = threading.Event(), threading.Event()
            applied = []

            def reload_theme():
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("测试未释放主题提交")
                applied.append(json.loads(appearance.read_text())["theme"])

            class Host:
                _render_lock = asyncio.Lock()

            host = Host()
            host.renderer = SimpleNamespace(
                theme_dir=root,
                appearance_path=appearance,
                load_appearance=lambda: json.loads(appearance.read_text()),
                reload_theme=reload_theme,
            )
            panel = W.WebPanel(host, PLUGIN)

            async def body():
                return {"kind": "logo", "colors": {"accent": "#123456"}}

            panel._body = body
            task = asyncio.create_task(getattr(panel, route)())
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                panel.close()
                check(
                    f"主题提交{route}已开工：关闭不拆开提交",
                    Host._render_lock.locked() and not task.done(),
                )
                release.set()
                response = await asyncio.wait_for(task, 2)
                check(
                    f"主题提交{route}已开工：关闭后完整重载并成功返回",
                    response.get("status") != "error"
                    and len(applied) == 1
                    and json.loads(appearance.read_text())["font_regular"] == "kept"
                    and (
                        applied[0]["logo"] is False and not logo.exists()
                        if route == "theme_image_delete"
                        else applied[0]["colors"]["accent"] == "#123456"
                    ),
                    str(response),
                )
            finally:
                release.set()
                await asyncio.gather(task, *list(panel._render_tasks), return_exceptions=True)

    class Host:
        _render_lock = asyncio.Lock()

    panel = W.WebPanel(Host(), PLUGIN)
    entered, release = threading.Event(), threading.Event()
    finished = []

    def running():
        entered.set()
        if not release.wait(3):
            raise TimeoutError("测试未释放线程")
        finished.append("restored")
        return "done"

    task = asyncio.create_task(panel._locked(running))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        panel.close()
        check("渲染已开工：停用不打断线程或释放锁", Host._render_lock.locked() and not task.done())
        release.set()
        result = await asyncio.wait_for(task, 2)
        check("渲染已开工：停用后线程收尾完成", result == "done" and finished == ["restored"])
    finally:
        release.set()
        await asyncio.gather(task, *list(panel._render_tasks), return_exceptions=True)


asyncio.run(_panel_queued_render_close_checks())


async def _appearance_merge_thread_checks():
    """同面板主题线程与字体配置合并写入不丢字段；不承诺跨实例或外部进程协调。"""
    import threading
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory(prefix="wf-panel-appearance-") as directory:
        path = Path(directory) / "appearance.json"
        path.write_text('{"kept":true}')
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        reads = []

        def load():
            app = json.loads(path.read_text())
            reads.append(1)
            if len(reads) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("测试未释放配置写入")
            return app

        panel = W.WebPanel(SimpleNamespace(), PLUGIN)
        renderer = SimpleNamespace(appearance_path=path, load_appearance=load)
        first = asyncio.create_task(
            asyncio.to_thread(panel._save_appearance, renderer, {"theme": {"logo": False}})
        )
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)

            def save_font():
                second_started.set()
                panel._save_appearance(renderer, {"font_regular": "chosen"})

            second = asyncio.create_task(asyncio.to_thread(save_font))
            assert await asyncio.to_thread(second_started.wait, 2)
            await asyncio.sleep(0.03)
            check("同面板外观合并：前一读改写完成前后一个不能读取旧快照", len(reads) == 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            check(
                "同面板外观合并：主题字体和无关字段均保留",
                json.loads(path.read_text())
                == {"kept": True, "theme": {"logo": False}, "font_regular": "chosen"},
            )
        finally:
            release.set()
            await asyncio.gather(
                first, *([second] if second is not None else []), return_exceptions=True
            )


asyncio.run(_appearance_merge_thread_checks())


async def _manual_crawl_result_checks():
    """手动结果绑定当前Task，不把正常返回整数等同于抓取成功。"""
    for kind in ("rank", "riven"):
        for result, want_state, want_status in (
            ("ok", "completed", W.OK),
            ("degraded", "completed", W.WARN),
            ("error", "failed", W.STALE),
        ):
            panel = _panel()
            key = "manual_" + kind
            panel.p._background_status = {key: {"result": "ok", "detail": "其它轮次"}}

            async def finish():
                return 7

            task = asyncio.create_task(finish())
            outcome = {
                "result": result,
                "detail": "本轮结果",
                "last_attempt": 100,
                "last_finished": 101,
            }
            task._wf_crawl_status = outcome
            setattr(
                panel.p.client,
                "_rank_crawl_task" if kind == "rank" else "_riven_rt_crawl_task",
                task,
            )
            await task
            row = next(r for r in panel.background_rows() if r["key"] == key)
            check(
                f"手动{kind}/{result}：正常整数返回仍按本轮结果展示",
                task.result() == 7
                and row["result"] == result
                and row["state"] == want_state
                and row["status"] == want_status
                and row["detail"] == "本轮结果"
                and row["last_attempt"] == 100
                and row["last_finished"] == 101,
                str(row),
            )
            panel.p.client._rank_save_failed = result != "error"
            panel.p.client._rank_source_empty = result != "degraded"
            panel.p._background_status[key] = {"result": "error", "detail": "后续自动抓取"}
            after = next(r for r in panel.background_rows() if r["key"] == key)
            check(f"手动{kind}/{result}：后续共享状态不覆盖已完成结果", after == row)

    for target in ("wm_ranks", "riven_rt"):
        panel = _panel()

        async def body():
            return {"target": target}

        def reject(*args, **kwargs):
            raise W.WarframeAPIError("客户端正在关闭")

        panel._body = body
        panel.p.client.start_rank_crawl = reject
        panel.p.client.start_riven_rt_crawl = reject
        response = await panel.crawl()
        check(
            f"手动{target}：客户端关闭返回明确错误而非started",
            response.get("status") == "error" and "state" not in response,
        )


asyncio.run(_manual_crawl_result_checks())


async def _theme_delete_commit_failure_checks():
    """真实渲染器/删除入口：配置提交或unlink失败时不丢原图，不留内存磁盘分叉。"""
    from unittest.mock import patch

    from PIL import Image
    from core import render as R

    original_state = R.theme_state()
    try:
        for kind in ("bg", "logo"):
            field = "bg_image" if kind == "bg" else "logo"
            for mode in ("normal", "config_fail", "unlink_fail", "rollback_fail"):
                with tempfile.TemporaryDirectory(prefix="wf-theme-delete-fault-") as directory:
                    root = Path(directory)
                    (root / "theme").mkdir()
                    image = root / "theme" / f"{kind}.png"
                    Image.new("RGB", (32, 32), (123, 45, 67)).save(image, "PNG")
                    image_bytes = image.read_bytes()
                    appearance = root / "appearance.json"
                    initial = {
                        "unrelated": "keep",
                        "theme": {field: True, "colors": {"accent": "#123456"}},
                    }
                    appearance.write_text(json.dumps(initial, ensure_ascii=False, indent=1))
                    config_bytes = appearance.read_bytes()
                    renderer = R.ImageRenderer(root / "cards")

                    class Host:
                        _render_lock = asyncio.Lock()

                    host = Host()
                    host.renderer = renderer
                    panel = W.WebPanel(host, PLUGIN)

                    async def body():
                        return {"kind": kind}

                    panel._body = body
                    actual_write, actual_unlink = W.paths.atomic_write_text, Path.unlink
                    writes = []

                    def write(path, *args, **kwargs):
                        if Path(path) == appearance:
                            writes.append(1)
                            if mode == "config_fail" or (
                                mode == "rollback_fail" and len(writes) > 1
                            ):
                                raise OSError("synthetic configuration write failure")
                        return actual_write(path, *args, **kwargs)

                    def unlink(path, *args, **kwargs):
                        if Path(path) == image and mode in ("unlink_fail", "rollback_fail"):
                            raise OSError("synthetic image unlink failure")
                        return actual_unlink(path, *args, **kwargs)

                    error, response = None, None
                    with (
                        patch.object(W.paths, "atomic_write_text", write),
                        patch.object(Path, "unlink", unlink),
                    ):
                        try:
                            response = await panel.theme_image_delete()
                        except OSError as exc:
                            error = exc
                    check(
                        f"主题删除{kind}/{mode}：成功或失败准确返回",
                        response == {"deleted": kind} and error is None
                        if mode == "normal"
                        else response is None and isinstance(error, OSError),
                    )
                    if mode == "normal":
                        check(
                            f"主题删除{kind}/正常：原图已删且配置内存均禁用",
                            not image.exists()
                            and renderer.load_appearance()["theme"][field] is False
                            and R.theme_state()[field] is None,
                        )
                    else:
                        check(
                            f"主题删除{kind}/{mode}：保留原图原字节",
                            image.exists() and image.read_bytes() == image_bytes,
                        )
                        if mode != "rollback_fail":
                            check(
                                f"主题删除{kind}/{mode}：保留原配置与内存主题",
                                appearance.read_bytes() == config_bytes
                                and R.theme_state()[field] is not None,
                            )
                    before_reload = R.theme_state()[field] is not None
                    renderer.reload_theme()
                    check(
                        f"主题删除{kind}/{mode}：重载前后运行状态一致",
                        before_reload == (R.theme_state()[field] is not None),
                    )
                    check(
                        f"主题删除{kind}/{mode}：不覆盖其它配置",
                        renderer.load_appearance()["unrelated"] == "keep",
                    )
                    if mode == "rollback_fail":
                        check(
                            f"主题删除{kind}/回滚失败：报错但按实际磁盘状态重载",
                            len(writes) == 2
                            and renderer.load_appearance()["theme"][field] is False
                            and R.theme_state()[field] is None,
                        )
                    await asyncio.gather(*list(panel._render_tasks), return_exceptions=True)
    finally:
        R.apply_theme(
            original_state["colors"],
            bg_image=original_state["bg_image"],
            bg_dim=original_state["bg_dim"],
            panel_alpha=original_state["panel_alpha"],
            logo=original_state["logo"],
        )


asyncio.run(_theme_delete_commit_failure_checks())


async def _font_delete_commit_failure_checks9():
    """真实选择→删除：失败保留原文件/选择；回滚失败则按真实磁盘重载。"""
    import shutil
    from unittest.mock import patch
    from core import render as R

    source = next(path for path in R._Fonts.PACKED if path.exists())
    cases = (
        ("normal", "both"),
        ("config_fail", "both"),
        ("unlink_fail", "both"),
        ("rollback_fail", "both"),
        ("unlink_fail", "regular"),
        ("normal", "auto"),
        ("unlink_fail", "auto"),
    )
    original_theme = R.theme_state()
    try:
        for mode, selection in cases:
            with tempfile.TemporaryDirectory(prefix="wf-font-delete9-") as directory:
                root = Path(directory)
                (root / "fonts").mkdir()
                target = root / "fonts" / "synthetic-user.ttc"
                shutil.copyfile(source, target)
                original_font = target.read_bytes()
                renderer = R.ImageRenderer(root / "cards")
                panel = W.WebPanel(type("FontHost", (), {"renderer": renderer})(), PLUGIN)
                chosen = {"both": ("regular", "bold"), "regular": ("regular",), "auto": ()}[
                    selection
                ]
                initial = {"unrelated": "keep", "theme": {"colors": {"accent": "#123456"}}}
                renderer.appearance_path.write_text(json.dumps(initial))

                async def select_body():
                    return {
                        "slot": "both" if selection == "both" else "regular",
                        "id": W.font_id(target),
                    }

                panel._body = select_body
                if chosen:
                    await panel.font_select()
                initial = renderer.load_appearance()

                async def delete_body():
                    return {"id": W.font_id(target)}

                panel._body = delete_body
                original_write, original_unlink = W.paths.atomic_write_text, Path.unlink
                writes = []

                def write(path, *args, **kwargs):
                    if Path(path) == renderer.appearance_path:
                        writes.append(1)
                        if mode == "config_fail" or (mode == "rollback_fail" and len(writes) > 1):
                            raise OSError("synthetic config write failure")
                    return original_write(path, *args, **kwargs)

                def unlink(path, *args, **kwargs):
                    if path == target and mode in ("unlink_fail", "rollback_fail"):
                        raise PermissionError("synthetic font unlink denial")
                    return original_unlink(path, *args, **kwargs)

                error, response = None, None
                with (
                    patch.object(W.paths, "atomic_write_text", write),
                    patch.object(Path, "unlink", unlink),
                ):
                    try:
                        response = await panel.font_delete()
                    except OSError as exc:
                        error = exc
                label = f"字体删除 {mode}/{selection}"
                check(
                    f"{label}：操作结果准确",
                    response is not None and response.get("deleted") == target.name
                    if mode == "normal"
                    else isinstance(error, OSError),
                )
                check(
                    f"{label}：仅成功才删除原文件",
                    not target.exists()
                    if mode == "normal"
                    else target.read_bytes() == original_font,
                )
                current = renderer.load_appearance()
                expected = dict(initial)
                if mode in ("normal", "rollback_fail"):
                    for slot in chosen:
                        expected.pop("font_" + slot, None)
                check(
                    f"{label}：配置选择与提交结果一致且无关字段保留",
                    current == expected,
                    str(current),
                )
                before = (renderer.fonts.regular, renderer.fonts.bold)
                renderer.reload_fonts()
                check(
                    f"{label}：即时字体与重载结果一致",
                    before == (renderer.fonts.regular, renderer.fonts.bold),
                    str(before),
                )
                if mode == "rollback_fail":
                    check(
                        f"{label}：回滚失败仍按磁盘重载",
                        len(writes) == 2
                        and all(current.get("font_" + slot) is None for slot in chosen),
                    )
    finally:
        R.apply_theme(**original_theme)


asyncio.run(_font_delete_commit_failure_checks9())


# ---- 2026-11-01：背景图显示方式、关于页来源链接、数据新鲜度「用于」 ----
_pc = _panel()
check(
    "主题候选：显示方式只认 BG_FITS，非法值回退铺满",
    _pc._theme_candidate({"bg_fit": "tile"})["bg_fit"] == "tile"
    and _pc._theme_candidate({"bg_fit": "<x>"})["bg_fit"] == "cover"
    and _pc._theme_candidate({})["bg_fit"] == "cover",
)
_cand = _pc._theme_candidate({"bg_fit": "custom", "bg_pos": "br", "bg_scale": 999})
_cand0 = _pc._theme_candidate({"bg_pos": "<x>", "bg_scale": "abc"})
check(
    "主题候选：位置只认九宫格、缩放夹在 10–200",
    (_cand["bg_pos"], _cand["bg_scale"]) == ("br", 200)
    and (_cand0["bg_pos"], _cand0["bg_scale"]) == ("c", 60),
)
_src_rows = W.about_info()["sources"]
check(
    "关于页每个来源带链接列表，链接都是 http(s)",
    all(isinstance(x.get("links"), list) for x in _src_rows)
    and all(u.startswith(("https://", "http://")) for x in _src_rows for u in x["links"])
    and sum(bool(x["links"]) for x in _src_rows) >= len(_src_rows) - 1,
)
_fr = W.freshness()
check(
    "数据新鲜度每行都写明用于哪些指令",
    all(r.get("used") for r in _fr) and set(W.FRESH_USED) == {r["item"] for r in _fr},
    str([r["item"] for r in _fr if not r.get("used")]),
)
check(
    "页面：新鲜度表有「用于」列、概览过期提示带影响、关于页渲染来源链接、外观有显示方式选择",
    "<th>用于</th>" in _html
    and "影响：${used}" in _html
    and '"data", "去刷新", r.used]' in _html
    and 'class: "srclink"' in _html
    and 'id="bgFit"' in _html
    and "bg_fit: TH.cur.bg_fit, bg_pos: TH.cur.bg_pos, bg_scale: TH.cur.bg_scale" in _html
    and '<option value="custom">' in _html
    and "推荐：PNG / JPG / WebP" in _html,
)
# ---- b12：订阅标识文案、来源链接在宿主 sandbox（无 allow-popups）下改为地址框 + 复制 / 打开回退 ----
check(
    "订阅开关叫「显示完整标识」，页面与面板后端不再把平台标识叫「号码」",
    "显示完整标识" in _html
    and "显示完整号码" not in _html
    and "号码" not in _html
    and "号码" not in (ROOT / "core" / "webpanel.py").read_text(encoding="utf-8")
    and "OpenID" in _html,
)
_about_js = _html[_html.index("async function loadAbout()") : _html.index("// —— 概览 ——")]
check(
    "关于页来源不放点不开的新标签链接，地址框可选中，复制有剪贴板→execCommand→手动三级回退并提示结果",
    'target: "_blank"' not in _about_js
    and 'class: "linkbox"' in _html
    and "navigator.clipboard.writeText(text)" in _html
    and 'document.execCommand("copy")' in _html
    and "请长按或按 Ctrl+C" in _html
    and "浏览器或后台未能打开新窗口，已改为复制。" in _html,
)


# ---- b13：视觉模型设置单独分组、各字段「留空」按实际路由说明、诊断只宣称覆盖兼容主渠道 ----
_vg = dict(W.SETTINGS_GROUPS)
check(
    "六个视觉渠道字段各在视觉模型分组里（紫卡双路和可选复读同组、识卡单独一组），不再落入「其他」",
    _vg.get("视觉模型 · 兼容主渠道") == ["vision_provider_id"]
    and _vg.get("视觉模型 · 紫卡识别")
    == ["vision_provider_id_riven", "vision_provider_id_line", "vision_provider_id_line_recheck"]
    and _vg.get("视觉模型 · 配卡识别（识卡）")
    == ["vision_provider_id_scan", "vision_provider_id_scan_equipment"]
    and not any(
        f["key"].startswith("vision_provider_id")
        for g in st10["groups"]
        if g["title"] == "其他"
        for f in g["fields"]
    ),
)
check(
    "冷却 / 并发 / 详情模式归「识图行为」组",
    set(_vg.get("识图行为", [])) == {"scan_cooldown", "scan_max_concurrent", "scan_detail_mode"},
)
_ve = _html[_html.index("const VISION_EMPTY = {") : _html.index("const VISION_KEYS")]
check(
    "渠道选择器：每个视觉字段有自己的「留空」说明，不再统一写「留空 = 自动挑选」",
    "留空 = 自动挑选" not in _html
    and all(
        k in _ve
        for k in (
            "vision_provider_id:",
            "vision_provider_id_riven:",
            "vision_provider_id_line:",
            "vision_provider_id_scan:",
        )
    )
    and "（留空）沿用兼容主渠道" in _ve
    and "（留空）与紫卡主读用同一渠道" in _ve
    and "当前值，不在已加载的渠道里" in _html,
)
check(
    "诊断按钮不再以「识卡渠道」概称（b14 起改为四路解析 + 按任务真调）",
    "识卡渠道 · " not in _html and "视觉 · 路由解析" in _html and "视觉 · 真调" in _html,
)
check(
    "解析结果文案为「兼容主渠道」",
    "兼容主渠道（按优先级）" in (ROOT / "core" / "webpanel.py").read_text(encoding="utf-8"),
)


# ---- b14：四路已保存路由（vision_routes）与按任务真调（vision_live + route）的页面接线 ----
_js = _html[_html.index('<script type="module">') :]
check(
    "真调只在「视觉 · 真调」按钮里发出，且带所选任务 route；旧的 data-test 视觉按钮已移除",
    _js.count('target: "vision_live"') == 1
    and 'apiPost("conntest", { target: "vision_live", route })' in _js
    and 'data-test="vision' not in _html
    and all(f'<option value="{r}">' in _html for r in ("compat", "riven", "line", "scan")),
)
check(
    "已保存路由走零模型的 vision_routes，在识图页详细自定义的完整路由里展示（b17 起为常驻静态区块）",
    'apiPost("conntest", { target: "vision_routes" })' in _js
    and 'id="visionSaved"' in _html
    and "<b>未保存预览</b>" in _html
    and "<b>已保存路由</b>" in _html,
)
_vs = _js[_js.index("const VSOURCE = {") : _js.index("const vSource")]
check(
    "来源按后端 source_mode 七种取值逐一说明，未登记的值原样标出不推断",
    all(
        f"{m}:" in _vs
        for m in (
            "automatic",
            "explicit",
            "explicit_unavailable",
            "inherit_compat",
            "inherit_riven",
            "fallback_riven",
            "unknown",
        )
    )
    and "后端返回了未识别的来源" in _js,
)
check(
    "调用审计：按渠道方法尝试计数，HTTP 次数为 null 时写「未统计」，未发送原因逐项说明",
    '"provider_text_chat_call"' in _js
    and '"provider_method_invoked"' in _js
    and "HTTP 次数未统计" in _js
    and all(
        f"{k}:" in _js
        for k in (
            "no_candidates",
            "success_stop",
            "budget_limit",
            "panel_closed",
            "test_busy",
            "invalid_route",
        )
    )
    and "不测识别准确率" in _js,
)
check(
    "旧伤害行读取不作为现行步骤展示（只有 legacy_damage_rows_active 为 true 才显示）",
    "L.legacy_damage_rows_active === true" in _js
    and "伤害栏" not in _html
    and "伤害栏" not in (ROOT / "_conf_schema.json").read_text(encoding="utf-8"),
)
check(
    "诊断调用上限写明不是费用上限；无可用渠道时不写「尝试 0 路」",
    "不是费用上限" in _js and "没有可用渠道，本页真调不会发送" in _js,
)


async def _vision_compare_api_checks():
    """纯合成图片与provider验证管理生命周期，不读取真图或GT。"""
    import io
    from hashlib import sha256
    from types import SimpleNamespace
    from unittest.mock import patch

    from PIL import Image

    from core.vision_prompts import VisionPromptStore

    image_bytes = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(image_bytes, format="PNG")
    image = "data:image/png;base64," + _b64.b64encode(image_bytes.getvalue()).decode()
    secret = "password=合成秘密"

    with tempfile.TemporaryDirectory() as folder:
        provider = _Prov("synthetic/compare", reply='{"panel":{"attack_speed":"2/秒"}}')
        panel = _panel(provs=[provider])
        panel.p.context = SimpleNamespace(get_all_providers=lambda: [provider])
        store = VisionPromptStore(Path(folder) / "prompts.json")
        panel.p.vision_prompts = store
        saved = store.apply(provider.pid, "scan_panel", "save", 0, "合成草稿A")
        before = store.path.read_bytes()
        query = {}
        panel._query = lambda key, default="": query.get(key, default)

        async def post(method, body):
            async def read():
                return dict(body)

            panel._body = read
            return await getattr(panel, method)()

        base = {
            "provider_id": provider.pid,
            "task": "scan_panel",
            "image": image,
            "candidate_source": "draft",
            "revision": saved["revision"],
        }

        async def prepare(**changes):
            return await post("vision_compare_prepare", {**base, **changes})

        catalog = await panel.vision_prompt_presets()
        query.update(preset_id="scan_panel_json_v2_draft", task="scan_panel")
        detail = await panel.vision_prompt_presets()
        check(
            "对比方案目录与详情：候选标实验和未评测，摘要不含全文",
            catalog.get("ok") is True
            and len(catalog.get("presets", [])) == 2
            and all("prompt" not in p for p in catalog["presets"])
            and detail.get("ok") is True
            and detail.get("prompt")
            and detail.get("status") == "实验",
        )
        query.clear()
        async with panel._test_lock:
            prepared = await prepare()
            available = await panel.vision_prompt_presets()
        check(
            "持有真调锁时仍可准备快照和读取目录，零模型调用",
            prepared.get("ok") is True
            and available.get("ok") is True
            and provider.calls == 0
            and prepared.get("attempted") == 0
            and prepared.get("actual_http_requests") == 0,
        )
        check(
            "准备结果冻结图片及提示词摘要，不返回图片数据",
            prepared.get("expires_in") == 600
            and prepared.get("provider_call_cap") == 2
            and prepared["image"]["input_sha256"] == sha256(image_bytes.getvalue()).hexdigest()
            and prepared["image"]["input_width"] == 400
            and prepared["image"]["input_height"] == 200
            and prepared["prompts"]["candidate"]["sha256"]
            == sha256("合成草稿A".encode()).hexdigest()
            and image not in json.dumps(prepared, ensure_ascii=False),
        )
        conflict = await prepare(revision=0)
        check(
            "准备草稿拒绝过期修订，不改store",
            conflict.get("ok") is False
            and conflict.get("code") == "revision_conflict"
            and store.path.read_bytes() == before
            and provider.calls == 0,
        )
        preset = await prepare(candidate_source="preset", preset_id="scan_panel_json_v2_draft")
        check(
            "固定方案可直接准备且不保存草稿、不启用",
            preset.get("ok") is True
            and store.path.read_bytes() == before
            and store.view(provider.pid, "scan_panel")["active"] is None,
        )
        invalid_image = await prepare(image="data:image/png;base64," + secret)
        unknown_preset = await prepare(candidate_source="preset", preset_id="missing")
        invalid_source = await prepare(candidate_source="automatic")
        check(
            "准备拒绝无效图片、未知固定方案和来源，不泄露图片/密码",
            all(r.get("ok") is False for r in (invalid_image, unknown_preset, invalid_source))
            and secret not in json.dumps(invalid_image, ensure_ascii=False)
            and provider.calls == 0,
        )
        empty = await prepare(task="scan_mods", revision=0)
        unknown_provider = await prepare(provider_id="missing")
        duplicate = _Prov(provider.pid)
        panel.p.context = SimpleNamespace(get_all_providers=lambda: [provider, duplicate])
        ambiguous = await prepare()
        panel.p.context = SimpleNamespace(get_all_providers=lambda: [provider])
        check(
            "准备拒绝空草稿、缺失渠道和重复渠道ID且零模型",
            all(r.get("ok") is False for r in (empty, unknown_provider, ambiguous))
            and provider.calls == 0,
        )
        no_confirm = await post("vision_compare_run", {"token": prepared["token"]})
        unknown = await post("vision_compare_run", {"token": "unknown", "confirm": True})
        async with panel._test_lock:
            busy = await post("vision_compare_run", {"token": prepared["token"], "confirm": True})
        check(
            "未确认、未知token和共享忙锁均不调用模型",
            all(r.get("ok") is False for r in (no_confirm, unknown, busy)) and provider.calls == 0,
        )

        # run冻结的是准备时的草稿，即使随后修改也不重读。
        store.apply(provider.pid, "scan_panel", "save", saved["revision"], "合成草稿B")
        seen = []
        real_text_chat = provider.text_chat

        async def recording(**kwargs):
            seen.append(kwargs["prompt"])
            return await real_text_chat(**kwargs)

        provider.text_chat = recording
        result = await post("vision_compare_run", {"token": prepared["token"], "confirm": True})
        cached = await post("vision_compare_run", {"token": prepared["token"], "confirm": True})
        check(
            "实际引擎串行两侧且候选使用准备时原文；重复token只读缓存",
            result.get("ok") is True
            and provider.calls == 2
            and len(seen) == 2
            and seen[1] == "合成草稿A"
            and cached.get("cached") is True
            and image not in json.dumps(result),
        )

        panel._compare_jobs[preset["token"]]["expires"] = time.monotonic() - 1
        expired = await post("vision_compare_run", {"token": preset["token"], "confirm": True})
        check(
            "过期token拒绝执行并清理快照，不追加模型调用",
            expired.get("ok") is False
            and provider.calls == 2
            and preset["token"] not in panel._compare_jobs,
        )

        # 注入仅用于生命周期：异常包含图片/密码时不能直出管理响应。
        async def failing(*args, **kwargs):
            raise RuntimeError(image + secret)

        failure_token = (await prepare(revision=2))["token"]
        with patch.object(W, "run_comparison", failing):
            failed = await post("vision_compare_run", {"token": failure_token, "confirm": True})
        serialized = json.dumps(failed, ensure_ascii=False)
        check(
            "对比内部异常脱敏并释放共享锁",
            failed.get("ok") is False
            and failed.get("code") == "comparison_failed"
            and failed.get("attempted") is None
            and failed.get("sent") is None
            and failed.get("actual_http_requests") is None
            and image not in serialized
            and secret not in serialized
            and not panel._test_lock.locked(),
        )

        # 用真实慢provider验证取消传播，第二侧不得发送，token不可重新执行。
        entered = asyncio.Event()

        async def slow(**kwargs):
            provider.calls += 1
            entered.set()
            await asyncio.Event().wait()

        provider.text_chat = slow
        token = (await prepare(revision=2))["token"]
        running = asyncio.create_task(post("vision_compare_run", {"token": token, "confirm": True}))
        await asyncio.wait_for(entered.wait(), 2)
        duplicate_run = await post("vision_compare_run", {"token": token, "confirm": True})
        calls = provider.calls
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        rerun = await post("vision_compare_run", {"token": token, "confirm": True})
        check(
            "运行中重复拒绝；取消释放锁且不发第二侧、token不可重发",
            duplicate_run.get("ok") is False
            and not panel._test_lock.locked()
            and provider.calls == calls
            and rerun.get("ok") is False
            and rerun.get("code") == "cancelled"
            and rerun.get("cached") is True
            and rerun.get("attempted") is None
            and rerun.get("sent") is None
            and rerun.get("actual_http_requests") is None,
        )

        entered.clear()
        token = (await prepare(revision=2))["token"]
        running = asyncio.create_task(post("vision_compare_run", {"token": token, "confirm": True}))
        await asyncio.wait_for(entered.wait(), 2)
        calls = provider.calls
        cleanup = panel.close()
        await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 2)
        if cleanup:
            await cleanup
        closed = await post("vision_compare_run", {"token": token, "confirm": True})
        check(
            "关闭面板取消对比任务，不发送第二侧且拒绝重跑",
            closed.get("ok") is False and provider.calls == calls and not panel._test_lock.locked(),
        )


asyncio.run(_vision_compare_api_checks())


async def _terminate_waits_for_compare_checks():
    """编译真实卸载入口，验证对比清理完成后才关闭连接池。"""
    import ast
    from types import SimpleNamespace

    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    methods = [
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in ("terminate", "_log_task_death")
    ]
    scope = {"asyncio": asyncio, "logger": W.logger}
    cls = ast.ClassDef(name="CompareHost", bases=[], keywords=[], body=methods, decorator_list=[])
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
            str(ROOT / "main.py"),
            "exec",
        ),
        scope,
    )
    host = scope["CompareHost"]()
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed = []

    async def compare():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await release.wait()

    async def nop():
        pass

    async def close_client():
        closed.append(True)

    host.client = SimpleNamespace(close=close_client)
    host.push = SimpleNamespace(stop=nop)
    host._panel = W.WebPanel(host, PLUGIN)
    task = asyncio.create_task(compare())
    host._panel._compare_task = task
    await started.wait()
    stopping = asyncio.create_task(host.terminate())
    await asyncio.wait_for(cancelled.wait(), 1)
    await asyncio.sleep(0)
    check(
        "真实terminate等待对比取消清理，清理未完成前不关闭client",
        not stopping.done() and not closed,
    )
    release.set()
    await asyncio.wait_for(stopping, 1)
    check("真实terminate在对比退出后完成连接池关闭", task.done() and closed == [True])


asyncio.run(_terminate_waits_for_compare_checks())


async def _vision_compare_prepare_capacity_checks():
    import io
    import threading
    from types import SimpleNamespace
    from unittest.mock import patch

    from PIL import Image

    image_file = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(image_file, format="PNG")
    image = "data:image/png;base64," + _b64.b64encode(image_file.getvalue()).decode()
    provider = _Prov("synthetic/capacity")
    panel = _panel(provs=[provider])
    panel.p.context = SimpleNamespace(get_all_providers=lambda: [provider])
    base = {
        "provider_id": provider.pid,
        "task": "scan_panel",
        "image": image,
        "candidate_source": "preset",
        "preset_id": "scan_panel_json_v2_draft",
    }

    async def prepare(**changes):
        # 每个请求体即时返回；两个并发调用各自持有独立body快照。
        async def body():
            return {**base, **changes}

        panel._body = body
        return await panel.vision_compare_prepare()

    legacy = await prepare(preset_id="legacy_v1")
    inherited = await prepare(task="scan_title")
    check(
        "两侧提示词相同的原版/继承方案拒绝准备，零模型且不占槽",
        legacy.get("ok") is False
        and inherited.get("ok") is False
        and provider.calls == 0
        and not panel._compare_jobs,
    )
    initial = [await prepare() for _ in range(3)]
    check(
        "前三个有效准备各占一槽，准备仍零模型",
        all(item.get("ok") is True for item in initial)
        and len(panel._compare_jobs) == 3
        and provider.calls == 0,
    )

    # 强制两次预处理均在容量尚余一槽时进入，再同时释放，验证后置容量护栏。
    entered = asyncio.Event()
    release = threading.Event()
    mutex = threading.Lock()
    count = 0
    loop = asyncio.get_running_loop()
    real_prepare_image = W.prepare_image

    def blocked_prepare(*args, **kwargs):
        nonlocal count
        with mutex:
            count += 1
            if count == 2:
                loop.call_soon_threadsafe(entered.set)
        if not release.wait(3):
            raise ValueError("合成并发准备超时")
        return real_prepare_image(*args, **kwargs)

    with patch.object(W, "prepare_image", blocked_prepare):
        first = asyncio.create_task(prepare())
        second = asyncio.create_task(prepare())
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            release.set()
        results = await asyncio.gather(first, second)
    check(
        "剩一槽时两个并发prepare仅一个成功，任务数量不超过四",
        sum(result.get("ok") is True for result in results) == 1
        and len(panel._compare_jobs) == 4
        and provider.calls == 0,
    )
    full = await prepare()
    check(
        "四槽已满拒绝第五任务，零模型且保留原四槽",
        full.get("ok") is False and len(panel._compare_jobs) == 4 and provider.calls == 0,
    )
    panel._compare_jobs[initial[0]["token"]]["expires"] = time.monotonic() - 1
    replacement = await prepare()
    check(
        "过期释放槽后可准备新任务，仍至多四槽",
        replacement.get("ok") is True
        and len(panel._compare_jobs) == 4
        and initial[0]["token"] not in panel._compare_jobs
        and provider.calls == 0,
    )
    panel.close()


asyncio.run(_vision_compare_prepare_capacity_checks())


# ---- b15：视觉提示词方案（vision_prompts / vision_prompt_update）的页面接线 ----
_vp = _js[_js.index("// —— 视觉提示词方案") : _js.index("// —— 依赖与资源")]
check(
    "提示词方案：目录与详情走 vision_prompts（详情参数交给 bridge 编码），写操作只走 vision_prompt_update",
    'bridge.apiGet("vision_prompts", {})' in _vp
    and 'bridge.apiGet("vision_prompts", { provider_id: pid, task })' in _vp
    and _vp.count('bridge.apiPost("vision_prompt_update", body)') == 1
    and "conntest" not in _vp
    and "vision_live" not in _vp,
)
check(
    "提示词方案：保存才带 text，启用 / 恢复只带修订号；三个动作分开",
    "const body = { provider_id: pid, task, action, revision: v.revision };" in _vp
    and 'if (action === "save") body.text = text;' in _vp
    and all(f'vpUpdate("{a}")' in _vp for a in ("save", "activate", "reset")),
)
check(
    "提示词方案：字数按码点计并取后端上限；迟到的详情按序号丢弃；冲突与失败不重试、保留编辑框",
    "Array.from(" in _vp
    and "max_prompt_length" in _vp
    and "if (seq !== VP.seq) return;" in _vp
    and 'e.code === "revision_conflict"' in _vp
    and "页面不会自动重试或覆盖" in _vp
    and "编辑框里的文字已保留" in _vp,
)
check(
    "提示词方案：有未保存改动时启用不可用、切换前先处理；提示词按纯文本显示",
    "先「保存草稿」再启用" in _vp
    and "VP.pending = { pid, task }" in _vp
    and 'el("pre", { class: "vpText" }, text)' in _vp
    and "innerHTML" not in _vp,
)
check(
    "自动刷新区分操作忙碌与不可选的提示词、模式按钮",
    '"data-vp": ""' in _vp
    and '"data-rm-switch": true' in _js
    and "button:disabled:not([data-vp], [data-rm-switch])" in _js,
)


# ---- b16：固定方案（vision_prompt_presets）与同图对比（vision_compare_prepare / run）的页面接线 ----
_ps = _js[_js.index("// —— 固定方案") : _js.index("// —— 同图对比")]
_vc = _js[_js.index("// —— 同图对比") : _js.index("// —— 依赖与资源")]
check(
    "固定方案：目录与单步原文走 vision_prompt_presets，按 ok 判定；复制只进编辑框，不写方案、不调用对比",
    'bridge.apiGet("vision_prompt_presets", {})' in _ps
    and 'bridge.apiGet("vision_prompt_presets", { preset_id: id, task })' in _ps
    and "d.ok !== true" in _ps
    and 'vpOpenEditor("preset"' in _ps
    and "vision_prompt_update" not in _ps
    and "vision_compare" not in _ps,
)
check(
    "固定方案：状态、评测样本数与验证边界取后端字段；继承原版的步骤写明仍是原版；页面不写「最优」「提升」",
    "p.protocol_boundary" in _ps
    and "p.samples_per_model" in _ps
    and "p.repeats_per_image_and_scheme" in _ps
    and "此步骤仍是内置原版" in _ps
    and not any(k in _ps + _vc for k in ("最优", "准确率提升", "更准")),
)
check(
    "同图对比：准备与执行各只有一处调用；执行固定带 confirm:true 与准备令牌，确认要再点一次",
    _vc.count('bridge.apiPost("vision_compare_prepare", body)') == 1
    and _vc.count('bridge.apiPost("vision_compare_run", { token: p.d.token, confirm: true })') == 1
    and 'vcArm("run", vcRun)' in _vc
    and "vision_live" not in _vc,
)
check(
    "同图对比：草稿候选带当前修订号，未保存改动不参与；输入变化或令牌过期后不能执行；迟到的准备不采用",
    "body.revision = v.revision" in _vc
    and "对比只用已保存草稿" in _vc
    and "vcKey() !== p.key" in _vc
    and "Date.now() > p.expiresAt" in _vc
    and "if (seq !== VC.prepSeq) return;" in _vc,
)
check(
    "同图对比：HTTP 次数为 null 时写「未统计」；失败一侧照常展示；模型原文与解析字段按纯文本显示",
    "HTTP 次数未统计" in _vc
    and 'VC_FAIL = new Set(["调用超时", "渠道调用失败", "未发送"])' in _vc
    and "JSON.stringify(s.parsed, null, 2)" in _vc
    and "vpTextBox(s.raw_text" in _vc
    and "innerHTML" not in _vc,
)
check(
    "同图对比：忙（attempted=0 且无令牌字段）时保留令牌；宿主抛错时允许用同一令牌再请求",
    'd.attempted === 0 && !("token" in d)' in _vc
    and 'p.state = "unknown"' in _vc
    and "用同一令牌再请求结果" in _vc,
)


print()


async def _compare_retained_cache_checks():
    """纯合成验证终态容量、TTL、释放及审计边界。"""
    from types import SimpleNamespace
    from unittest.mock import patch

    provider = _Prov("synthetic/cache")
    panel = _panel(provs=[provider])
    panel.p.context = SimpleNamespace(get_all_providers=lambda: [provider])
    counter = 0

    def seed():
        nonlocal counter
        counter += 1
        token = str(counter)
        panel._compare_jobs[token] = {
            "state": "ready",
            "expires": time.monotonic() + 600,
            "info": {"provider_id": provider.pid, "task": "scan_panel"},
            "provider": provider,
            "prepared": {},
            "original": "a",
            "candidate": "b",
        }
        return token

    async def post(method, token, **extra):
        async def body():
            return {"token": token, "confirm": True, **extra}

        panel._body = body
        return await getattr(panel, method)()

    calls = 0

    async def compare(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {"ok": True, "attempted": 2, "sent": True, "actual_http_requests": None}

    with patch.object(W, "run_comparison", compare):
        first = seed()
        await post("vision_compare_run", first)
        for _ in range(5):
            await post("vision_compare_run", seed())
        cached = await post("vision_compare_run", first)
        check(
            "连续六次对比释放待执行槽，首token缓存不重发",
            calls == 6 and not panel._compare_jobs and cached.get("cached") is True,
        )
        for _ in range(30):
            await post("vision_compare_run", seed())
        evicted = await post("vision_compare_run", first)
        check(
            "终态缓存严格32条，被淘汰token拒绝且不重放",
            len(panel._compare_results) == 32
            and evicted.get("code") == "token_unavailable"
            and calls == 36,
        )
        newest = str(counter)
        panel._compare_results[newest]["expires"] = time.monotonic() - 1
        expired = await post("vision_compare_run", newest)
        check(
            "终态TTL过期删除并拒绝重放", expired.get("code") == "token_unavailable" and calls == 36
        )
        token = seed()
        released = await post("vision_compare_release", token)
        repeated = await post("vision_compare_release", token)
        refused = await post("vision_compare_run", token)
        check(
            "ready释放回收槽且幂等，释放token不能运行",
            released.get("released") is True
            and repeated.get("cached") is True
            and token not in panel._compare_jobs
            and refused.get("code") == "token_unavailable"
            and released.get("attempted") == 0
            and released.get("sent") is False
            and released.get("actual_http_requests") == 0
            and calls == 36,
        )
        token = seed()
        panel._compare_jobs[token]["state"] = "running"
        busy = await post("vision_compare_release", token)
        check(
            "release不取消running，忙响应准确零调用",
            busy.get("code") == "busy"
            and busy.get("attempted") == 0
            and busy.get("sent") is False
            and busy.get("actual_http_requests") == 0,
        )
        panel._compare_jobs.pop(token)
        token = seed()
        panel.p.context = SimpleNamespace(get_all_providers=lambda: [])
        changed = await post("vision_compare_run", token)
        again = await post("vision_compare_run", token)
        check(
            "provider_changed在引擎前拒绝且缓存零调用审计",
            changed.get("code") == "provider_changed"
            and changed.get("attempted") == 0
            and changed.get("sent") is False
            and changed.get("actual_http_requests") == 0
            and again.get("cached") is True
            and calls == 36,
        )
    panel.close()
    check("close清理pending与终态缓存", not panel._compare_jobs and not panel._compare_results)


asyncio.run(_compare_retained_cache_checks())


async def _compare_closed_boundary_checks():
    """关闭前/读取请求期间/图片线程处理期间均返回可穿透宿主的明确code。"""
    from types import SimpleNamespace
    from unittest.mock import patch

    def is_closed(result):
        return (
            result.get("ok") is False
            and result.get("code") == "closed"
            and result.get("attempted") == 0
            and result.get("sent") is False
            and result.get("actual_http_requests") == 0
            and "status" not in result
        )

    panel = _panel()

    async def no_request():
        raise AssertionError("关闭后不应读取request")

    panel._body = no_request
    panel.close()
    prepare = await panel.vision_compare_prepare()
    release = await panel.vision_compare_release()
    check(
        "关闭后prepare/release不读取request并返回closed零调用",
        is_closed(prepare) and is_closed(release),
    )

    for method in ("vision_compare_prepare", "vision_compare_release"):
        panel = _panel()

        async def close_during_body():
            panel.close()
            return {"token": "synthetic"}

        panel._body = close_during_body
        result = await getattr(panel, method)()
        check(f"{method}读取body期间关闭返回closed且无status信封", is_closed(result))

    provider = _Prov("synthetic/close-thread")
    panel = _panel(provs=[provider])
    panel.p.context = SimpleNamespace(get_all_providers=lambda: [provider])

    async def body():
        return {
            "provider_id": provider.pid,
            "task": "scan_panel",
            "candidate_source": "preset",
            "preset_id": "scan_panel_json_v2_draft",
        }

    panel._body = body

    async def close_during_thread(*args, **kwargs):
        panel.close()
        return {"task": "scan_panel"}

    with patch.object(W.asyncio, "to_thread", close_during_thread):
        result = await panel.vision_compare_prepare()
    check(
        "prepare图片预处理等待期间关闭返回closed且不留快照",
        is_closed(result) and not panel._compare_jobs and provider.calls == 0,
    )


asyncio.run(_compare_closed_boundary_checks())

# ---- b17 / b18：识图独立页（紫卡与配卡识卡分区）、详细自定义开关、分页保存与返修 ----
_b17 = _js[_js.index("// —— 识图页") : _js.index("// —— 本地紫卡 OCR")]
_ocr = _js[_js.index("// —— 本地紫卡 OCR") : _js.index("// —— 视觉提示词方案")]
_save_js = _js[_js.index("async function saveScope(") : _js.index("function discardScope(")]
_load_js = _js[_js.index("async function loadSettings(") : _js.index("async function saveScope(")]
check(
    "分页保存：只提交本页范围内的改动；重新读取设置不清空另一页的未保存改动",
    "const changes = scopeEdits(scope);" in _save_js
    and 'bridge.apiPost("settings_save", { changes })' in _save_js
    and "changes: state.edits" not in _js
    and "state.edits = {}" not in _load_js
    and "same(state.edits[k], state.orig[k])" in _load_js,
)
check(
    "保存等待期间的最后输入：先按旧基线取 curVal，再以已提交值为新基线重建差异，不同则保留为新草稿",
    "const want = curVal(k);\n        state.orig[k] = v;\n"
    "        if (same(want, v)) delete state.edits[k]; else state.edits[k] = want;" in _save_js,
)
check(
    "识图设置键按任务归属分区：紫卡（主读 / 窄读）、配卡识卡（渠道 / 详情模式）、两类共用、旧配置兼容与排查在高级区",
    "const VISION_SCOPE = new Set([...VISION_KEYS," in _js
    and 'const V_RIVEN = ["vision_provider_id_riven", "vision_provider_id_line"];' in _b17
    and 'const V_SCAN = ["vision_provider_id_scan", "vision_provider_id_scan_equipment", "scan_detail_mode"];'
    in _b17
    and 'const V_BEHAVIOR = ["scan_cooldown", "scan_max_concurrent"];' in _b17
    and 'const V_ADV = ["vision_provider_id", "vision_provider_id_line_recheck", "scan_debug_save"];'
    in _b17
    and "vpPanel()" not in _js[_js.index("function renderSettings()") : _js.index("const SAVEBAR")],
)
check(
    "详细自定义：默认关闭，偏好只存浏览器本地且读写失败回落；开关撤销真调确认、关闭时释放未执行快照，不写方案或配置",
    "let visionAdv = false;" in _b17
    and 'try { visionAdv = window.localStorage.getItem(ADV_KEY) === "1"; } catch { visionAdv = false; }'
    in _b17
    and 'if (!visionAdv) vcHide("关闭详细自定义");' in _b17
    and 'vcHide("离开识图页")' in _b17
    and "vlRevoke();" in _b17
    and "vision_prompt_update" not in _b17
    and "settings_save" not in _b17,
)
check(
    "识图页常驻节点：提示词方案与同图对比各挂载一次，任务切换节点整体移到高级区顶部，ID 唯一",
    '$("vpMount").replaceWith(vpPanel());' in _b17
    and '$("vcMount").replaceWith(vcPanel());' in _b17
    and '$("vAdvTabs").append($("vpGroups"));' in _b17
    and '$("visionAdvBox").hidden = !visionAdv;' in _b17
    and all(
        _html.count(f'id="{i}"') == 1
        for i in (
            "vpMount",
            "vcMount",
            "visionSaved",
            "visionRoutes",
            "vpState",
            "vrBtn",
            "vlBtn",
            "vlRoute",
            "r-vlive",
            "vSave",
            "save",
            "vRiven",
            "vScan",
            "vOcr",
            "vLegacy",
            "vAdvTabs",
        )
    ),
)
check(
    "任务区生效状态：读不到写「状态未知」；留空继承兼容主渠道注明迁移前兼容模式，严格独立后留空写明停止；已启用自定义写明关闭高级不会停用",
    "状态未知（${vr.err}）" in _b17
    and "状态未知（${VP.cat.customized_error" in _b17
    and "当前沿用旧配置的兼容主渠道（迁移前兼容模式）" in _b17
    and "已严格独立，这一任务会停止" in _b17
    and 'r?.source_mode === "unconfigured"' in _b17
    and "后端调用层尚未与另一类彻底隔离" not in _b17
    and "关闭详细自定义不会停用它们。" in _b17
    and "（渠道未加载）" in _b17,
)
check(
    "视觉真调双确认带上下文：首击记录上下文，二击与发送前再核对；显隐、离页、隐藏、保存、解析都撤销旧确认",
    'const vlCtx = () => [curGroup(), $("vlRoute").value, state.vrGen || 0, visionAdv ? 1 : 0, curView, document.visibilityState]'
    in _js
    and 'if (!b.classList.contains("arm") || vlArmCtx !== vlCtx()) {' in _js
    and "if (tok !== vlTok || vlCtx() !== ctx)" in _js
    and 'document.addEventListener("visibilitychange"' in _js
    and 'if (scope === "vision") vlRevoke();' in _save_js
    and _js.count('apiPost("conntest", { target: "vision_live", route })') == 1,
)
check(
    "迟到 / 未采用快照的释放失败进入待释放清单，可主动重试（只释放原对象）；关高级 / 离页的释放失败同样可重试",
    "async function vcReleaseLate(d)" in _js
    and "VC.orphans.push(o);" in _js
    and "async function vcRetryOrphan(o)" in _js
    and "await vcReleaseLate(d)" in _js
    and "VC.orphans.push({ d: p.d" in _js,
)
_scan_at = _html.index('<section class="panel vtask" id="vScan"')
_scan_html = _html[_scan_at : _html.index('<section class="panel" id="vBehavior"', _scan_at)]
check(
    "本地紫卡 OCR：管理状态以后端为准、摘要取统一快照；未声明 implemented 则暂停；不提交 settings_save、不用浏览器存储；识卡区没有本地 OCR 控件或占位行",
    'const OCR_API = { status: "riven_ocr_status"' in _ocr
    and "d.implemented !== true" in _ocr
    and "settings_save" not in _ocr
    and not any(
        w in _ocr for w in ("localStorage", "sessionStorage", "indexedDB", "caches.", "innerHTML")
    )
    and "尚未接入" not in _ocr
    and "OCR" not in _scan_html
    and "不提供（只为紫卡准备）" not in _js
    and 'const rm = group === "riven" ? rivenMode() : null;' in _js
    and '...(rm ? [["本地 OCR", rmLocalSummary(rm), true]] : [])' in _js
    and "m.basis.local_state" in _js,
)
check(
    "本地 OCR 校验完整状态和检查响应；写入开始作废旧 GET，写入期间不发新 GET",
    "function ocrStatus(d)" in _ocr
    and "Number.isSafeInteger(d.revision)" in _ocr
    and "d.engines.every" in _ocr
    and 'typeof d.check_ok !== "boolean"' in _ocr
    and "d.findings.every" in _ocr
    and "++OCR.seq; // mutation" in _ocr
    and "if (OCR.busy) return;" in _ocr
    and "ocrAdopt(d);" in _ocr,
)
check(
    "本地 OCR 确认绑定已保存配置及修订号，编辑、写入、显隐切换均撤销；保存后再检查启用",
    "OCR.st?.revision, OCR.st?.engine, OCR.st?.python, OCR.st?.model_dir" in _ocr
    and "OCR.armCtx !== now" in _ocr
    and "oninput: (e) => { ocrRevoke();" in _ocr
    and "onchange: (e) => { ocrRevoke();" in _ocr
    and 'OCR.dirty ? "先保存，再检查已保存配置"' in _ocr
    and "保存→检查已保存配置→确认启用" in _ocr
    and "vlRevoke(); ocrRevoke(); renderOcrBar();" in _js
    and "当前插件版本没有接入本地 OCR" not in _ocr,
)

check(
    "本地 OCR 下载部署：两击确认后只发一次 install {confirmed:true,job_id}；部署状态单独校验与计序号，不送进配置校验；只在进行中有界轮询",
    _ocr.count("bridge.apiPost(OCR_API.install, { confirmed: true, job_id: jobId })") == 1
    and _ocr.count("OCR_API.install,") == 1
    and 'ocrArm("install", ocrInstall, ocrInstContext' in _ocr
    and "function ocrInstStatus(d)" in _ocr
    and "ocrInstStatus(await bridge.apiGet(OCR_API.instStatus" in _ocr
    and "ocrStatus(await bridge.apiGet(OCR_API.instStatus" not in _ocr
    and 'typeof d.accepted !== "boolean"' in _ocr
    and "if (!OI.st?.running || OI.err || OI.busy" in _ocr
    and "OI.polls >= OI_POLL_MAX" in _ocr
    and "ocrLoad(); ocrInstLoad();" in _js
    and "clearTimeout(OI.pollT); OI.pollT = 0;  // 离页" in _js
    and "clearTimeout(OI.pollT); OI.pollT = 0;  // 隐藏" in _js,
)
_fill = _ocr[_ocr.index("function ocrFillRecommended()") : _ocr.index("function renderOcrInst()")]
check(
    "部署成功只把推荐路径填进未保存草稿：不调用保存 / 检查 / 启用 / 试识别，并撤销旧确认与旧检查",
    "OCR.form = next; OCR.dirty = !same;" in _fill
    and "ocrRevoke(); OCR.check = null;" in _fill
    and not any(w in _fill for w in ("apiPost", "ocrPost", "ocrTry(", "ocrInstall("))
    and 'engine: "rapidocr", python: s.recommended_python, model_dir: s.model_dir' in _fill,
)
_try = _ocr[_ocr.index("async function ocrTry()") : _ocr.index("let otNode = null;")]
check(
    "本地试识别：两击确认后只发一次 try；读图后、发送前再核对上下文；先按识别结构分支再看 ok，partial/rejected 不被当错误丢弃",
    _ocr.count("bridge.apiPost(OCR_API.try, body)") == 1
    and 'ocrArm("try", ocrTry, ocrTryContext' in _ocr
    and "if (ocrTryContext() !== ctx || gen !== OT.gen)" in _try
    and "image_data_url: `data:${mime};base64," in _try
    and "confirmed: true" in _try
    and "revision: rev" in _try
    and 'd?.code === "revision_conflict"' in _try
    and "await ocrLoad(); // 只重读状态" in _try
    and "Number.isSafeInteger(d.configuration_revision)" in _try
    and "后端配置修订号未确认" in _try
    and "r.rev" not in _try
    and "if (gen !== OT.gen)" in _try
    and "if (known && s) head =" in _try
    and 'state === "accepted" && d.ok !== true' in _try
    and "rejected:" in _ocr
    and 'accept: "image/png,image/jpeg,image/webp"' in _ocr
    and "OT_MAX = 10 * 1024 * 1024" in _ocr
    and "URL.revokeObjectURL(OT.url)" in _ocr
    and not any(w in _try for w in ("riven_value", "market", "vision_compare", "conntest")),
)

_mig = _js[_js.index("// —— 渠道隔离（主动迁移）") : _js.index("function psShortEval(")]
check(
    "渠道隔离迁移：状态只读 GET；预览一次 prepare；确认只发一次 apply（带预览 token 与 revision，发出前作废预览）；成功后重读状态与路由",
    _mig.count("bridge.apiGet(MG_API.status") == 1
    and _mig.count("bridge.apiPost(MG_API.prepare, { operation: op })") == 1
    and _mig.count(
        "bridge.apiPost(MG_API.apply, { confirmed: true, token: pv.d.token, revision: pv.d.revision })"
    )
    == 1
    and "clearTimeout(MG.pvT); MG.pvT = 0; MG.pv = null;" in _mig
    and "const tasks = [mgLoad(), loadVisionSaved()];" in _mig
    and "ocrLoad(); ocrInstLoad(); mgLoad();" in _js,
)
check(
    "迁移确认的阻断与作废：草稿 / 不可确认 / busy 不放行；显隐、离页、隐藏、重新解析、保存、输入改动都作废预览；迟到预览不恢复确认",
    'if (mgEdits() !== "[]") return' in _mig
    and "if (!pv.d.can_apply) return" in _mig
    and "if (seq !== MG.seq || mgCtx() !== ctx)" in _mig
    and "if (mgCtx() !== pv.ctx)" in _mig
    and 'mgRevoke("切换了详细自定义")' in _js
    and 'mgRevoke("离开了识图页")' in _js
    and 'mgRevoke("页面被隐藏")' in _js
    and 'mgRevoke("路由重新解析")' in _js
    and 'mgRevoke("保存设置' in _js
    and "mgObserveEdits();" in _js,
)
check(
    "迁移不触碰本地 OCR、不直接提交隔离键；严格独立的来源文案（unconfigured）与留空说明按后端状态切换",
    not any(
        w in _mig
        for w in (
            "riven_ocr",
            "OCR_API",
            "ocrPost",
            "ocrTry",
            "ocrInstall",
            "settings_save",
            "localStorage",
        )
    )
    and 'unconfigured: "留空：已严格独立，该任务停止（不继承兼容主渠道）"' in _js
    and "const vEmpty = (k) => (vIso() === true ? VISION_EMPTY_ISO : VISION_EMPTY)[k];" in _js
    and "机器总并发预算仍由两类共享" in _html,
)


def _migration_pending_checks():
    """执行页面原函数的异步状态机；合成桥与时钟，不联真实后端或浏览器。"""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        print("[SKIP] 迁移预览异步状态机：未找到 Node.js")
        return
    payload = {
        "migration": _mig[_mig.index("const MG_API =") : _mig.index("function mgValue(")],
        "dirty": _js[_js.index("function markDirty()") : _js.index("async function loadSettings(")],
    }
    program = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const source = JSON.parse(fs.readFileSync(0, 'utf8'));
function fixture() {
  let clock = 1000, serial = 0;
  const timers = new Map(), calls = [], pending = [];
  const current = {vision_provider_id: 'old', vision_provider_id_riven: 'riven',
    vision_provider_id_line: 'line', vision_provider_id_scan: 'scan'};
  const plan = {ok:true, operation:'migrate', phase:'legacy_pending', isolated:false,
    revision:'fixture-revision', current, changes:{vision_routes_isolated:true},
    available:{vision_provider_id_riven:true, vision_provider_id_line:true, vision_provider_id_scan:true},
    requires_selection:[], can_apply:true, already_applied:false, legacy_retained:true};
  const c = vm.createContext({state:{edits:{},vrGen:0}, curView:'vision', visionAdv:false,
    document:{visibilityState:'visible',querySelectorAll:()=>[]}, SAVEBAR:{},
    vpBadge(){}, renderVisionRoutes(){}, renderRivenMode(){}, mgRender(){}, renderVisionSummary(){}, vrRoute(){},
    loadVisionSaved:async()=>{}, loadSettings:async()=>{},
    Date:{now:()=>clock}, setTimeout:(fn,delay)=>{timers.set(++serial,{fn,delay});return serial;},
    clearTimeout:(id)=>timers.delete(id), bridge:{
      apiGet:async()=>plan,
      apiPost:(ep,body)=>{calls.push({ep,body});
        if(ep==='vision_migration_prepare') return new Promise(resolve=>pending.push(resolve));
        return Promise.resolve({ok:true,operation:'migrate',isolated:true,
          saved:['vision_routes_isolated'],legacy_retained:true});
      }
    }});
  vm.runInContext('const RM_SWITCH={busy:false};\n' + source.migration + '\n' + source.dirty +
    '\nglobalThis.m={MG,mgPrepare,mgApply,mgRevoke,markDirty};',c);
  c.m.MG.st=plan; c.m.MG.loaded=true;
  return {c,m:c.m,calls,timers,clock:(v)=>{clock=v;},
    reply:()=>pending.shift()({...plan,token:'fixture-token',expires_in:180}),
    edit:(key,value)=>{if(value===undefined) delete c.state.edits[key];else c.state.edits[key]=value;c.m.markDirty();}};
}
const results=[];
async function test(name,fn) {
  try {await fn();results.push({name,ok:true});}
  catch(e){results.push({name,ok:false,error:e.stack||String(e)});}
}
function expect(ok,message){if(!ok)throw new Error(message);}
(async()=>{
  await test('pending渠道改后改回不恢复旧预览',async()=>{
    const f=fixture(), p=f.m.mgPrepare('migrate');
    f.edit('vision_provider_id_scan','other');f.edit('vision_provider_id_scan',undefined);
    f.reply();await p;
    expect(!f.m.MG.pv,'迟到预览仍可确认');
    expect(f.calls.filter(x=>x.ep==='vision_migration_release').length===1,'迟到令牌须单发回收');
  });
  await test('非视觉草稿不撤销预览且原样保留',async()=>{
    const f=fixture(), p=f.m.mgPrepare('migrate');
    f.edit('push_interval',42);f.reply();await p;
    expect(!!f.m.MG.pv,'非视觉编辑不应扩大阻断范围');
    expect(f.c.state.edits.push_interval===42,'非视觉草稿被改变');
  });
  await test('相同渠道草稿的重复渲染不撤销预览',async()=>{
    const f=fixture(), p=f.m.mgPrepare('migrate');f.m.markDirty();f.m.markDirty();
    f.reply();await p;expect(!!f.m.MG.pv,'未变化的草稿不应撤销预览');
  });
  await test('迟到响应只保留请求起点后的剩余期限',async()=>{
    const f=fixture(), p=f.m.mgPrepare('migrate');f.clock(61000);f.reply();await p;
    expect(f.m.MG.pv?.until===181000,'期限被响应延长');
    expect([...f.timers.values()].some(t=>t.delay===120000),'未按剩余120秒设置计时器');
  });
  await test('返回时已超过期限的预览不被采用',async()=>{
    const f=fixture(), p=f.m.mgPrepare('migrate');f.clock(181001);f.reply();await p;
    expect(!f.m.MG.pv&&!f.m.MG.view,'过期响应恢复了预览');
    expect(f.timers.size===0,'过期响应不应创建新计时器');
    await f.m.mgApply();expect(f.calls.filter(x=>x.ep==='vision_migration_apply').length===0,'过期响应不应发送apply');
  });
  await test('未触发计时器时发送前仍拦截过期确认',async()=>{
    const f=fixture(), p=f.m.mgPrepare('migrate');f.reply();await p;f.clock(181001);
    await f.m.mgApply();expect(!f.m.MG.pv,'发送前未清掉过期确认');
    expect(f.calls.filter(x=>x.ep==='vision_migration_apply').length===0,'过期确认发出了apply');
  });
  await test('取消单发回收且失败仍立即本地作废',async()=>{
    const f=fixture(), p=f.m.mgPrepare('migrate');f.reply();await p;
    f.c.bridge.apiPost=(ep,body)=>{f.calls.push({ep,body});return Promise.reject(new Error('旧后端'));};
    f.m.mgRevoke('取消');f.m.mgRevoke('取消');await Promise.resolve();
    expect(!f.m.MG.pv&&!f.m.MG.view,'失败恢复了本地预览');
    expect(f.calls.filter(x=>x.ep==='vision_migration_release').length===1,'释放重复发送');
  });
  await test('apply在途不会被普通撤销抢走',async()=>{
    const f=fixture(), p=f.m.mgPrepare('migrate');f.reply();await p;
    let finish;f.c.bridge.apiPost=(ep,body)=>{f.calls.push({ep,body});return new Promise(r=>finish=r);};
    const apply=f.m.mgApply();f.m.mgRevoke('离页');
    expect(!f.calls.some(x=>x.ep==='vision_migration_release'),'执行中的令牌被释放');
    finish({ok:false,code:'busy'});await apply;
    expect(f.calls.filter(x=>x.ep==='vision_migration_release').length===1,'busy未回收');
  });
  await test('apply回复未知不释放也不重试',async()=>{
    const f=fixture(), p=f.m.mgPrepare('migrate');f.reply();await p;
    f.c.bridge.apiPost=(ep,body)=>{f.calls.push({ep,body});return Promise.reject(new Error('断线'));};
    await f.m.mgApply();f.m.mgRevoke('离页');await f.m.mgApply();
    expect(f.calls.filter(x=>x.ep==='vision_migration_apply').length===1,'未知结果重试apply');
    expect(!f.calls.some(x=>x.ep==='vision_migration_release'),'未知结果释放执行令牌');
  });
  await test('有效预览单发apply且重复确认不重发',async()=>{
    const f=fixture(), p=f.m.mgPrepare('migrate');f.reply();await p;
    await Promise.all([f.m.mgApply(),f.m.mgApply()]);
    const writes=f.calls.filter(x=>x.ep==='vision_migration_apply');
    expect(writes.length===1,'apply被重复发送');
    expect(writes[0].body.confirmed===true&&writes[0].body.token==='fixture-token'
      &&writes[0].body.revision==='fixture-revision','apply未绑定原预览');
  });
  process.stdout.write(JSON.stringify(results));
})().catch(e=>{process.stderr.write(e.stack||String(e));process.exit(1);});
"""
    try:
        result = subprocess.run(
            [node, "-e", program],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode:
            check("迁移预览异步状态机执行成功", False, result.stderr)
            return
        rows = json.loads(result.stdout)
        check("迁移预览异步状态机没有漏跑", len(rows) == 10)
        for row in rows:
            check("迁移预览异步：" + row["name"], row["ok"], row.get("error", ""))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        check("迁移预览异步状态机执行成功", False, str(exc))


_migration_pending_checks()


async def _install_job_proxy_checks():
    calls = []

    class Installer:
        async def start(self, confirm=False, *, expected_job_id=None):
            calls.append((confirm, expected_job_id))
            return {"accepted": False, "reason": "job_conflict"}

    for body in (
        {"confirmed": True, "job_id": "a" * 32},
        {"confirmed": True},
        {"confirmed": False, "job_id": "bad"},
    ):
        panel = _panel()
        panel.p.local_ocr_installer = Installer()

        async def read():
            return body

        panel._body = read
        result = await panel.riven_ocr_install()
        check(
            "安装接口原样转发确认及job_id",
            calls[-1] == (body["confirmed"], body.get("job_id"))
            and result["reason"] == "job_conflict",
        )
    panel = _panel()
    panel.p.local_ocr_installer = Installer()
    before = len(calls)

    async def closed_read():
        panel.close()
        return {"confirmed": True, "job_id": "a" * 32}

    panel._body = closed_read
    result = await panel.riven_ocr_install()
    check(
        "安装接口读取body期间关闭不调用installer",
        len(calls) == before and result.get("ok") is False,
    )


asyncio.run(_install_job_proxy_checks())


def _install_job_checks():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        print("[SKIP] 安装 job_id 页面状态机：未找到 Node.js")
        return
    source = _js[_js.index("const OI =") : _js.index("// 推荐路径只进表单草稿")]
    source += _js[_js.index("function renderOcrInst()") : _js.index("// —— 单次本地试识别")]
    source += _js[_js.index("function ocrArm(") : _js.index("async function ocrPost(")]
    source = "const RM_SWITCH={busy:false};\n" + source
    program = r"""
const vm=require('node:vm'),fs=require('node:fs'),src=fs.readFileSync(0,'utf8');
const A='a'.repeat(32),B='b'.repeat(32),C='c'.repeat(32);
const status=(id=A,state='idle',extra={})=>({job_id:id,state,running:state==='queued',progress:0,message:'结果',recommended_python:'',model_dir:'',...extra});
function fixture(){const gets=[],posts=[],ocr={arm:'install'},dom={};const c=vm.createContext({OCR:ocr,curView:'vision',visionAdv:true,document:{visibilityState:'visible'},OCR_API:{},ocrFillRecommended(){},$:()=>dom,el:(tag,attrs,...children)=>({tag,attrs,children}),vpPut:(_, ...children)=>{dom.children=children;},setTimeout:()=>0,clearTimeout(){},renderOcr(){},renderOcrInst(){},ocrRevoke(){ocr.arm='';ocr.armCtx='';},bridge:{apiGet:()=>new Promise(r=>gets.push(r)),apiPost:(_,body)=>new Promise(r=>posts.push({body,r}))}});vm.runInContext(src+';globalThis.t={OI,ocrInstStatus,ocrInstContext,ocrInstLoad,ocrInstall,click:()=>ocrArm("install",ocrInstall,ocrInstContext,()=>!!OI.st&&!OI.busy&&!OI.st.running&&!OI.err,6000)};',c);return {...c.t,c,gets,posts,dom};}
const rows=[];async function test(name,fn){try{await fn();rows.push({name,ok:true});}catch(e){rows.push({name,ok:false,error:e.message});}}
function ok(x,m){if(!x)throw Error(m);}
(async()=>{
await test('旧后端缺ID或非法ID拒绝',()=>{const f=fixture();for(const id of [undefined,'bad']){let rejected=false;try{f.ocrInstStatus(status(id===undefined?null:id));}catch{rejected=true;}ok(rejected,'未拒绝无效ID');}});
await test('两窗口同形状态换ID撤销确认并保留历史',async()=>{const f=fixture();f.OI.st=status(A,'failed');const before=f.ocrInstContext();let p=f.ocrInstLoad();f.gets.shift()(status(B,'failed'));await p;ok(f.OI.prev.job_id===A,'同状态历史丢失');ok(JSON.stringify(f.dom).includes('上一次部署（'+A),'历史未显示');ok(f.c.OCR.arm==='','确认未撤销');ok(before!==f.ocrInstContext(),'确认上下文未绑定ID');ok(f.OI.msg.text.includes('变化'),'缺变化提示');});
await test('POST绑定原ID同ID终态保留提交归属',async()=>{const f=fixture();f.OI.st=status();let p=f.ocrInstall();ok(f.posts[0].body.job_id===A,'POST未绑定ID');f.posts.shift().r(status(B,'queued',{accepted:true}));await p;ok(f.OI.ownJobId===B,'未记录本页归属');p=f.ocrInstLoad();f.gets.shift()(status(B,'succeeded'));await p;ok(f.OI.ownJobId===B&&!f.OI.msg,'终态提示或归属错误');p=f.ocrInstLoad();f.gets.shift()(status(C,'queued'));await p;ok(!f.OI.ownJobId&&!f.OI.msg.accepted,'外部任务继承归属');});
await test('接单返回旧ID拒绝',async()=>{const f=fixture();f.OI.st=status();const p=f.ocrInstall();f.posts.shift().r(status(A,'queued',{accepted:true}));await p;ok(f.OI.err&&!f.OI.ownJobId,'旧ID被接单认领');});
await test('拒绝接单新ID不认领',async()=>{const f=fixture();f.OI.st=status();const p=f.ocrInstall();f.posts.shift().r(status(B,'failed',{accepted:false,reason:'job_conflict'}));await p;ok(!f.OI.ownJobId,'拒绝响应被认领');});
await test('未知回复不重试不认领',async()=>{const f=fixture();f.OI.st=status();const p=f.ocrInstall();f.posts.shift().r(status(null,'queued',{accepted:true}));await p;ok(f.OI.err&&!f.OI.ownJobId&&!f.posts.length,'未知回复认领或重试');const q=f.ocrInstLoad();f.gets.shift()(status(B,'queued'));await q;ok(!f.OI.ownJobId,'GET猜测接单归属');});
await test('迟到GET及POST不覆盖新状态',async()=>{const f=fixture();f.OI.st=status();const old=f.ocrInstLoad();const p=f.ocrInstall();f.gets.shift()(status(C));await old;ok(f.OI.st.job_id===A,'迟到GET覆盖');const during=f.ocrInstLoad();await during;ok(!f.gets.length,'POST期间发GET');f.OI.seq++;f.OI.st=status(C);f.posts.shift().r(status(B,'queued',{accepted:true}));await p;ok(f.OI.st.job_id===C&&!f.OI.ownJobId,'迟到POST覆盖');});
await test('实际两击同ID只提交一次并绑定A',async()=>{const f=fixture();f.c.OCR.arm='';f.OI.st=status(A);f.click();ok(!f.posts.length,'首击已提交');const p=f.ocrInstLoad();f.gets.shift()(status(A));await p;f.click();ok(f.posts.length===1&&f.posts[0].body.job_id===A,'二击未绑定A单发');f.click();ok(f.posts.length===1,'在途重复提交');f.posts[0].r(status(B,'queued',{accepted:true}));await Promise.resolve();});
await test('实际两击间同状态换ID只重新确认',async()=>{const f=fixture();f.c.OCR.arm='';f.OI.st=status(A);f.click();const p=f.ocrInstLoad();f.gets.shift()(status(B));await p;ok(!f.OI.prev,'初始idle被显示为历史任务');f.click();ok(!f.posts.length&&f.c.OCR.arm==='install','换ID后第二击仍提交');});
process.stdout.write(JSON.stringify(rows));})().catch(e=>{console.error(e);process.exit(1);});
"""
    result = subprocess.run(
        [node, "-e", program], input=source, text=True, capture_output=True, timeout=15
    )
    check("安装 job_id 页面函数执行", result.returncode == 0, result.stderr)
    if result.returncode == 0:
        rows = json.loads(result.stdout)
        check("安装 job_id 页面状态机没有漏跑", len(rows) == 9)
        for row in rows:
            check("安装 job_id：" + row["name"], row["ok"], row.get("error", ""))


_install_job_checks()


async def _font_pinned_protocol_checks():
    """真实 HTTPcore 协议栈 + 假网络字节流；TLS 握手使用 OpenSSL MemoryBIO。"""
    import importlib.util
    import os
    import socket
    import ssl
    import subprocess
    from unittest.mock import patch

    import httpcore
    import httpx
    from core import api_client as api

    class Wire(httpcore.AsyncNetworkStream):
        def __init__(self, backend):
            self.backend = backend
            self.pending = []
            self.buffer = b""
            self.closed = False

        async def write(self, buffer, timeout=None):
            self.buffer += buffer
            if self.buffer.startswith(b"\x05"):
                payload, self.buffer = self.buffer, b""
                self.backend.socks.append(payload)
                self.pending.append(
                    b"\x05\x00"
                    if len(self.backend.socks) % 2
                    else b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                )
            elif b"\r\n\r\n" in self.buffer:
                payload, self.buffer = self.buffer, b""
                self.backend.requests.append(payload)
                if payload.startswith(b"CONNECT "):
                    self.pending.append(b"HTTP/1.1 200 Connection established\r\n\r\n")
                else:
                    self.pending.append(self.backend.reply(payload))

        async def read(self, max_bytes, timeout=None):
            return self.pending.pop(0) if self.pending else b""

        async def aclose(self):
            self.closed = True

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            self.backend.tls.append(
                (server_hostname, ssl_context.check_hostname, ssl_context.verify_mode)
            )
            if self.backend.server_context is not None:
                # 真正完成客户端与服务端的 TLS 握手，并让证书错误从此处向上传播。
                ci, co, si, so = (ssl.MemoryBIO() for _ in range(4))
                client = ssl_context.wrap_bio(ci, co, server_hostname=server_hostname)
                server = self.backend.server_context.wrap_bio(si, so, server_side=True)
                cd = sd = False
                try:
                    for _ in range(40):
                        if not cd:
                            try:
                                client.do_handshake()
                                cd = True
                            except ssl.SSLWantReadError:
                                pass
                        if co.pending:
                            si.write(co.read())
                        if not sd:
                            try:
                                server.do_handshake()
                                sd = True
                            except ssl.SSLWantReadError:
                                pass
                        if so.pending:
                            ci.write(so.read())
                        if cd and sd:
                            self.backend.handshakes += 1
                            break
                    else:
                        raise AssertionError("TLS MemoryBIO 握手未完成")
                except ssl.SSLError as exc:
                    raise httpcore.ConnectError(str(exc)) from exc
            return self

        def get_extra_info(self, info):
            return None

    class Backend(httpcore.AsyncNetworkBackend):
        def __init__(self, reply=None, server_context=None):
            self.connects, self.requests, self.tls, self.socks = [], [], [], []
            self.reply = reply or (lambda _: b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            self.server_context = server_context
            self.handshakes = 0

        async def connect_tcp(self, host, port, **kwargs):
            self.connects.append((host, port))
            return Wire(self)

        async def sleep(self, seconds):
            pass

    loop = asyncio.get_running_loop()
    dns_calls = []
    answers = {}

    async def dns(host, port, **kwargs):
        dns_calls.append(host)
        rows = answers.get(host, ["8.8.8.8"])
        if isinstance(rows, Exception):
            raise rows
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (ip, port),
            )
            for ip in rows
        ]

    async def get(url, backend, proxy="", context=None):
        transport = api.font_url_transport(proxy, network_backend=backend, ssl_context=context)
        async with httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=True
        ) as client:
            return await client.get(url)

    # 清空环境不会去掉 macOS 系统代理；整个新矩阵模拟该主机条件。
    # 显式直连必须不读取该系统代理；环境代理用例仍取真实环境变量。
    from urllib.request import getproxies_environment

    def system_or_environment_proxy():
        return getproxies_environment() or {"https": "http://127.0.0.1:7897"}

    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(loop, "getaddrinfo", dns),
        patch("urllib.request.getproxies", side_effect=system_or_environment_proxy),
    ):
        for ip in ("8.8.8.8", "2606:4700:4700::1111"):
            answers["font.example"] = [ip]
            backend = Backend()
            response = await get("https://font.example/a.ttf", backend)
            check(
                "字体固定：直连 " + ip,
                backend.connects == [(ip, 443)]
                and response.url.host == "font.example"
                and b"Host: font.example\r\n" in backend.requests[0]
                and backend.tls == [("font.example", True, ssl.CERT_REQUIRED)],
            )
            for scheme in ("http", "https"):
                backend = Backend()
                await get("https://font.example/a.ttf", backend, scheme + "://proxy.example:8443")
                target = f"[{ip}]:443" if ":" in ip else ip + ":443"
                expected_tls = (["proxy.example"] if scheme == "https" else []) + ["font.example"]
                check(
                    "字体固定：" + scheme + " 代理 CONNECT " + ip,
                    backend.connects == [("proxy.example", 8443)]
                    and backend.requests[0].startswith(f"CONNECT {target} HTTP/1.1\r\n".encode())
                    and [x[0] for x in backend.tls] == expected_tls
                    and all(x[1:] == (True, ssl.CERT_REQUIRED) for x in backend.tls),
                )
            backend = Backend()
            await get("http://font.example/a.ttf", backend, "http://proxy.example:8080")
            target = f"[{ip}]" if ":" in ip else ip
            check(
                "字体固定：HTTP代理 absolute-form " + ip,
                backend.requests[0].startswith(f"GET http://{target}/a.ttf HTTP/1.1\r\n".encode()),
            )
            if importlib.util.find_spec("socksio"):
                backend = Backend()
                await get("https://font.example/a.ttf", backend, "socks5h://proxy.example:1080")
                connect = backend.socks[1]
                packed = socket.inet_pton(socket.AF_INET6 if ":" in ip else socket.AF_INET, ip)
                check(
                    "字体固定：SOCKS目标 " + ip,
                    packed in connect
                    and b"font.example" not in connect
                    and backend.tls[0][0] == "font.example",
                )
            else:
                print("[SKIP] 字体固定：SOCKS目标 " + ip + "（可选依赖 socksio 未安装）")
        answers["font.example"] = ["8.8.8.8"]
        for address in ("100.64.0.1", "[::ffff:100.64.0.1]"):
            backend = Backend()
            try:
                await get("https://" + address + "/a.ttf", backend)
                rejected = False
            except httpx.RequestError:
                rejected = True
            check("字体固定：CGNAT/mapped直写拒绝 " + address, rejected and not backend.connects)
        for rows in (
            ["8.8.8.8", "127.0.0.1"],
            ["8.8.8.8", "100.64.0.1"],
            ["8.8.8.8", "::ffff:100.64.0.1"],
            ["not-an-ip"],
            [],
            OSError("DNS失败"),
        ):
            answers["bad.example"] = rows
            backend = Backend()
            try:
                await get("https://bad.example/a.ttf", backend)
                rejected = False
            except httpx.RequestError:
                rejected = True
            check("字体固定：坏/混合DNS拒绝 " + str(rows), rejected and not backend.connects)

        count = 0

        async def rebound(host, port, **kwargs):
            nonlocal count
            count += 1
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("8.8.8.8" if count == 1 else "127.0.0.1", port),
                )
            ]

        with patch.object(loop, "getaddrinfo", rebound):
            backend = Backend()
            await get("https://font.example/a.ttf", backend)
            check(
                "字体固定：校验后不再域名连接",
                count == 1 and backend.connects == [("8.8.8.8", 443)],
            )
            count = 0
            backend = Backend(
                lambda _: b"HTTP/1.1 302 Found\r\nLocation: /next.ttf\r\nContent-Length: 0\r\n\r\n"
            )
            try:
                await get("https://font.example/a.ttf", backend)
                rejected = False
            except httpx.RequestError:
                rejected = True
            check(
                "字体固定：同域下一跳重新解析且拒绝重绑定",
                rejected and count == 2 and len(backend.connects) == 1,
            )

        def redirect_reply(payload):
            if b"GET /first.ttf " in payload:
                return (
                    b"HTTP/1.1 302 Found\r\nLocation: next.ttf\r\n"
                    b"Set-Cookie: font=yes; Path=/\r\nContent-Length: 0\r\n\r\n"
                )
            if b"GET /next.ttf " in payload:
                return (
                    b"HTTP/1.1 302 Found\r\nLocation: https://other.example/final.ttf\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"

        backend = Backend(redirect_reply)
        response = await get("https://font.example/first.ttf", backend)
        check(
            "字体固定：相对/跨域重定向与域名cookie语义",
            response.url.host == "other.example"
            and len(backend.requests) == 3
            and b"Cookie: font=yes\r\n" in backend.requests[1]
            and b"Cookie:" not in backend.requests[2]
            and [x[0] for x in backend.tls] == ["font.example", "font.example", "other.example"],
        )
        backend = Backend(
            lambda _: (
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/x.ttf\r\nContent-Length: 0\r\n\r\n"
            )
        )
        try:
            await get("https://font.example/a.ttf", backend)
            rejected = False
        except httpx.RequestError:
            rejected = True
        check("字体固定：公网跳私网无第二次连接", rejected and len(backend.connects) == 1)
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.example:8080"}):
            backend = Backend()
            await get("https://font.example/a.ttf", backend, proxy=None)
            check(
                "字体固定：环境代理仍经固定传输",
                backend.connects == [("proxy.example", 8080)]
                and backend.requests[0].startswith(b"CONNECT 8.8.8.8:443 "),
            )

        # NO_PROXY 按主机及端口匹配；显式 cfg.proxy 不受该环境旁路规则覆盖。
        with patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://proxy.example:8080", "NO_PROXY": "font.example:8443"},
            clear=True,
        ):
            bypass, other_port, explicit = Backend(), Backend(), Backend()
            await get("https://font.example:8443/a.ttf", bypass, proxy=None)
            await get("https://font.example:443/a.ttf", other_port, proxy=None)
            await get("https://font.example:8443/a.ttf", explicit, "http://configured.example:8181")
            check(
                "字体固定：NO_PROXY含端口旁路且显式代理优先",
                bypass.connects == [("8.8.8.8", 8443)]
                and other_port.connects == [("proxy.example", 8080)]
                and explicit.connects == [("configured.example", 8181)],
            )

        # 在新传输的真实 HTTPcore 响应体读取阶段取消；观察实际 Wire 的关闭。
        reading = asyncio.Event()

        class SlowWire(Wire):
            async def read(self, max_bytes, timeout=None):
                if not self.pending and self.backend.slow_wire is self:
                    reading.set()
                    await asyncio.Event().wait()
                return await super().read(max_bytes, timeout)

            async def write(self, buffer, timeout=None):
                await super().write(buffer, timeout)
                if b"GET /slow.ttf " in buffer:
                    self.backend.slow_wire = self

        class SlowBackend(Backend):
            def __init__(self):
                super().__init__(
                    lambda payload: (
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n"
                        if b"GET /slow.ttf " in payload
                        else b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"
                    )
                )
                self.wires, self.slow_wire = [], None

            async def connect_tcp(self, host, port, **kwargs):
                self.connects.append((host, port))
                wire = SlowWire(self)
                self.wires.append(wire)
                return wire

        backend = SlowBackend()
        transport = api.font_url_transport("", network_backend=backend)
        client = httpx.AsyncClient(transport=transport, trust_env=False)
        closed_pools = []
        try:
            await client.get("https://other.example/a.ttf")

            async def read_slow():
                async with client.stream("GET", "https://font.example/slow.ttf") as response:
                    await response.aread()

            task = asyncio.create_task(read_slow())
            await asyncio.wait_for(reading.wait(), timeout=2)
            # 只包装本实现公开持有的 pool 引用，检查每个 pool.aclose 真正被调用。
            for pool in transport.pools.values():
                original_close = pool.aclose

                async def close_pool(close=original_close, current=pool):
                    closed_pools.append(current)
                    await close()

                pool.aclose = close_pool
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            cancelled_closed = backend.slow_wire.closed
        finally:
            await client.aclose()
        check(
            "字体固定：读取取消关闭实际连接且aclose全部池",
            cancelled_closed
            and len(transport.pools) == 2
            and len(closed_pools) == len(transport.pools)
            and all(wire.closed for wire in backend.wires),
        )
        # 真实面板 worker 默认工厂必须使用此传输；下载的短响应仍按原大小校验拒绝。
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as td:
            renderer = SimpleNamespace(user_font_dir=Path(td))
            panel = W.WebPanel(SimpleNamespace(cfg={"proxy": "http://proxy.example:8080"}), PLUGIN)
            panel.font_job = {"state": "running", "log": [], "got": 0, "total": 0}
            backend = Backend()
            with patch.object(
                W,
                "font_url_transport",
                lambda proxy: api.font_url_transport(proxy or "", network_backend=backend),
            ):
                await panel._import_worker(renderer, "tiny.ttf", "https://font.example/tiny.ttf")
            check(
                "字体固定：真实worker走代理固定传输并保留大小/清理校验",
                backend.requests[0].startswith(b"CONNECT 8.8.8.8:443 ")
                and panel.font_job["state"] == "failed"
                and not list(Path(td).iterdir()),
            )
        # 403 的 HTTPStatusError 自带完整 URL，面板任务和插件日志都不得泄露查询密钥。
        with tempfile.TemporaryDirectory() as td:
            renderer = SimpleNamespace(user_font_dir=Path(td))
            panel = W.WebPanel(SimpleNamespace(cfg={}), PLUGIN)
            panel.font_job = {"state": "running", "log": [], "got": 0, "total": 0}
            backend = Backend(lambda _: b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            logged = []
            with (
                patch.object(
                    W,
                    "font_url_transport",
                    lambda proxy: api.font_url_transport(proxy or "", network_backend=backend),
                ),
                patch.object(W.logger, "info", lambda *args: logged.append(args)),
            ):
                await panel._import_worker(
                    renderer, "signed.ttf", "https://font.example/signed.ttf?token=PRIVATE"
                )
            exposed = str(panel.font_job["log"]) + str(logged)
            check(
                "字体固定：403签名URL不进入任务日志/插件logger",
                panel.font_job["state"] == "failed"
                and "403" in exposed
                and "PRIVATE" not in exposed
                and "token=" not in exposed
                and "https://font.example" not in exposed
                and bool(logged),
            )
        # 直写 IP 不应进入 DNS；IPv6 Host 必须保留括号及非默认端口。
        for address in ("8.8.8.8", "[2606:4700:4700::1111]"):
            before = len(dns_calls)
            backend = Backend()
            await get("https://" + address + ":8443/a.ttf", backend)
            check(
                "字体固定：IP直写/端口/Host " + address,
                len(dns_calls) == before
                and backend.connects == [(address.strip("[]"), 8443)]
                and ("Host: " + address + ":8443\r\n").encode() in backend.requests[0],
            )

        class FailedBackend(Backend):
            async def connect_tcp(self, host, port, **kwargs):
                self.connects.append((host, port))
                raise httpcore.ConnectError("离线模拟连接失败")

        answers["font.example"] = ["8.8.8.8", "1.1.1.1"]
        backend = FailedBackend()
        try:
            await get("https://font.example/a.ttf", backend)
            rejected = False
        except httpx.ConnectError:
            rejected = True
        check(
            "字体固定：连接失败不回落域名重新解析",
            rejected and backend.connects == [("8.8.8.8", 443)],
        )
        answers["font.example"] = ["8.8.8.8"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "cert.conf").write_text(
                "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n"
                "[dn]\nCN=font.example\n[ext]\nbasicConstraints=critical,CA:TRUE\n"
                "subjectAltName=DNS:font.example,DNS:proxy.example\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-keyout",
                    str(root / "key.pem"),
                    "-out",
                    str(root / "cert.pem"),
                    "-config",
                    str(root / "cert.conf"),
                ],
                check=True,
                capture_output=True,
            )
            server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.load_cert_chain(root / "cert.pem", root / "key.pem")
            trusted = ssl.create_default_context(cafile=str(root / "cert.pem"))
            for proxy in ("", "http://proxy.example:8080", "https://proxy.example:8443"):
                # 使用公开 Proxy.ssl_context 只给 HTTPS 代理外层注入测试根。
                backend = Backend(server_context=server)
                real_proxy = httpx.Proxy

                def proxy_with_root(url):
                    return real_proxy(
                        url, ssl_context=trusted if url.startswith("https:") else None
                    )

                with patch.object(httpx, "Proxy", proxy_with_root):
                    await get("https://font.example/a.ttf", backend, proxy, trusted)
                check(
                    "字体固定：真实根证书TLS通过 " + str(proxy),
                    backend.handshakes == (2 if proxy and proxy.startswith("https:") else 1),
                )
            for host, context in (
                ("wrong.example", trusted),
                ("font.example", ssl.create_default_context()),
            ):
                backend = Backend(server_context=server)
                try:
                    await get("https://" + host + "/a.ttf", backend, context=context)
                    rejected = False
                except httpx.ConnectError:
                    rejected = True
                check(
                    "字体固定：真实TLS拒绝错误域名/不可信根 " + host,
                    rejected and not backend.requests,
                )


asyncio.run(_font_pinned_protocol_checks())

_mode = _js[
    _js.index("// —— 紫卡读取方式：本地 OCR / 单模型 / 双路模型") : _js.index(
        "function renderTaskStatus(group)"
    )
]
_actual_mode = _mode[_mode.index("function rivenMode()") : _mode.index("const rmField")]
check(
    "紫卡读取方式：实际生效只消费统一后端快照，严格校验模式/修订/依据/阻塞；缺失或失败待确认",
    "state.vr.d?.riven_effective_mode" in _actual_mode
    and '["local", "single", "dual", "unknown"]' in _actual_mode
    and "/^[a-f0-9]{64}$/" in _actual_mode
    and all(
        field in _actual_mode
        for field in (
            "local_manager",
            "local_state",
            "local_revision",
            "single_ocr",
            "isolated",
            "blocked",
            "reason",
        )
    )
    and "if (!m)" in _actual_mode
    and "if (!valid)" in _actual_mode
    and "state.vr.err" in _actual_mode
    and not any(
        old in _actual_mode for old in ("OCR.st", "state.orig", "state.edits", "riven_read_mode")
    )
    and "state.edits.riven_single_ocr" in _mode,
)

_mode_draft = _mode[_mode.index("function rmSet(") : _mode.index("function rmCard(")]
check(
    "紫卡云端草稿仍独立保存，三模式切换仅由确认入口发出",
    not any(
        w in _mode_draft
        for w in ("apiPost", "apiGet", "ocrPost", "ocrArm", "settings_save", "localStorage")
    )
    and _mode.count('apiPost("riven_mode_switch", { mode, revision, confirmed: true })') == 1
    and '"riven_single_ocr", "scan_cooldown"' in _js
    and "这里只改云端方式，不会启停本地 OCR，也不会改渠道选择。" in _mode,
)
check(
    "单模型与本地 OCR 下紫卡提示词、窄读与分歧复读标为不参与；单模型主读留空即停止，不沿用兼容主渠道",
    "只在双路模式下使用" in _js
    and "不参与：首读与增强图不带文本提示词，位置复读用内置提示词" in _mode
    and "未指定：单模型会停止（不沿用兼容主渠道，也不自动挑选）" in _mode
    and "rmUses(rm.mode, r?.route" in _js
    and 'rmNow.mode === "single" && n.dataset.route === "riven"' in _js,
)
_crawl = _js[_js.index("const CRAWL_ST = {") : _js.index('crawlBtn("crawlWm", "wm_ranks"')]
check(
    "后台立即抓：只有 started / running 显示为已启动或进行中；status=error（重抓保护、额度繁忙）与其它状态写「未启动」，不自动重试",
    'started: "已在后台启动"' in _crawl
    and 'running: "已经在抓了，不另起一份"' in _crawl
    and "if (CRAWL_ST[d.state])" in _crawl
    and "：未启动——" in _crawl
    and "按未启动处理" in _crawl
    and not any(w in _crawl for w in ("setTimeout", "setInterval", "force"))
    and _crawl.count('apiPost("crawl"') == 1
    and "约 30 分钟" not in _js
    and "约 20 分钟以上" not in _js,
)

if fails:
    print(f"✗ {fails} 项失败")
    sys.exit(1)
print("✓ 全部通过")
