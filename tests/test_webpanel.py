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

    async def refresh_valence(self, community_only=False):
        self.calls.append(("valence", community_only))
        return "fresh"

    async def refresh_acrichis_week(self, community_only=False):
        self.calls.append(("acrithis", community_only))
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
        "conntest": ["POST"],
        "resources": ["GET"],
        "deps_install": ["POST"],
        "font_download": ["POST"],
        "settings": ["GET"],
        "settings_save": ["POST"],
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
    rows = {r["item"]: r for r in W.freshness(now)}
    check(
        "信条在本轮窗口内 ⇒ ok", rows["信条元素加成"]["status"] == W.OK, str(rows["信条元素加成"])
    )
    check("★ 终幕快照过旧 ⇒ stale", rows["终幕元素加成"]["status"] == W.STALE)
    check("★ 言录使货单过期 ⇒ stale（读运行期覆盖）", rows["言录使本周货单"]["status"] == W.STALE)
    check("WM 排行超过 48h ⇒ warn", rows["WM 价格排行"]["status"] == W.WARN)
    check("紫卡周报没查过 ⇒ info 不报错", rows["紫卡周报（DE 官方）"]["status"] == W.INFO)
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


# ---- 7. 连通性测试 ----
def _ct(panel, target):
    async def _b():
        return {"target": target}

    panel._body = _b
    return asyncio.run(panel.conntest())


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
check("真调最多试 3 个渠道（控制 token）", [m.calls for m in many] == [1, 1, 1, 0, 0])
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


# ---- 9. 依赖与资源（2026-10-16 用户要求：面板里能下载需要的依赖）----
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

_calls = []


class _Pip:
    async def install(self, **kw):
        _calls.append(kw)


_had = {k: sys.modules.get(k) for k in ("astrbot", "astrbot.core")}
_pkg = _types.ModuleType("astrbot")
_core = _types.ModuleType("astrbot.core")
_core.pip_installer = _Pip()
_pkg.core = _core
sys.modules["astrbot"], sys.modules["astrbot.core"] = _pkg, _core
try:
    r = asyncio.run(_panel().deps_install())
finally:
    for k, v in _had.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
check(
    "★ 安装依赖走 AstrBot pip_installer + 插件 requirements.txt",
    r.get("ok") and _calls == [{"requirements_path": str(W.REQUIREMENTS)}],
    str(r) + str(_calls),
)
check("安装后提示重载插件", "重载插件" in r.get("detail", ""))
r = asyncio.run(_panel().deps_install())
check("没有 pip_installer 的环境给出说明、不抛", r.get("status") == "error", str(r))

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


# ---- 10. 设置 / 订阅操作（2026-10-16 用户：面板要能调整，不能比配置页还少）----
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
    "★ 设置页覆盖 schema 全部配置项、无重复",
    sorted(keys10) == sorted(_schema) and len(keys10) == len(set(keys10)),
)
check("设置页带渠道列表（select_provider 用）", st10["providers"] == ["a/vl", "b/chat"])
check("渠道项标 provider", any(f["provider"] for g in st10["groups"] for f in g["fields"]))


def _save(panel, changes):
    async def _b():
        return {"changes": changes}

    panel._body = _b
    return asyncio.run(panel.settings_save())


r = _save(
    ps10, {"render_mode": "text", "page_size": "20", "push_interval": 5, "proxy": "http://x:1"}
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
        for v in ("overview", "data", "push", "settings", "resources", "conn")
    ),
)


# ---- 11. 字库导入 / 选择（2026-10-16 用户：字体不限预设一种，支持 URL / 文件导入）----
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

        check("URL 非 http(s) 被拒", _imp("file:///etc/passwd").get("status") == "error")
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
        _sh.rmtree(_fd2, ignore_errors=True)
check(
    "页面调用的字体端点都已注册",
    all(
        f'"{ep}"' in html
        for ep in ("font_import_url", "font_upload", "font_select", "font_delete", "font_sample")
    ),
)


# ---- 12. 主题（2026-10-16 用户：可调颜色 / 背景图 / 角标 + 教程 + 实时预览）----
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


# ---- 13. 社区数据地址（2026-10-16 用户：不想用本项目数据可自填 URL）----
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
        "没检查过的快照显示 unchecked，五份齐全",
        len(rep0) == 5 and all(r["result"] == "unchecked" for r in rep0.values()),
    )
    _newer = dict(_seed14, _updated="2099-01-01")
    _serve14 = {
        "arb_ratings.json": _newer,
        "wiki_disp.json": {"snapshot": "2099-01-01T00:00:00+00:00", "disp": {"x": 1}},
    }
    asyncio.run(_c14.refresh_community_static())
    rep = {r["name"]: r for r in _c14.community_report()}
    check(
        "★ 采用新快照：记 adopted + 走的通道 + 数据时间",
        rep["arb_ratings.json"]["result"] == "adopted"
        and rep["arb_ratings.json"]["channel"] == "cdn.jsdelivr.net"
        and rep["arb_ratings.json"]["stamp"] == "2099-01-01",
        str(rep["arb_ratings.json"]),
    )
    check(
        "★ 校验不过：记 invalid + 原因",
        rep["wiki_disp.json"]["result"] == "invalid" and "400" in rep["wiki_disp.json"]["reason"],
        str(rep["wiki_disp.json"]),
    )
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
        and len(ov14["community"]) == 5
        and "community_base" in ov14,
        str(ov14.get("community"))[:200],
    )
finally:
    _paths14.set_run_dir(None)
    import shutil as _sh14

    _sh14.rmtree(_rt14, ignore_errors=True)
check("页面渲染社区快照通道表", 'id="comm"' in html and "renderComm(" in html)

print()
if fails:
    print(f"✗ {fails} 项失败")
    sys.exit(1)
print("✓ 全部通过")
