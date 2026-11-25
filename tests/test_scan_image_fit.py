# -*- coding: utf-8 -*-
"""识卡图片宽度规整 `_fit_scan_image` 回归测试（python3 tests/test_scan_image_fit.py）

覆盖「小图放大 + 大图缩小」：
  · 小图（<1600）：实测 648×242 直接喂模型会大面积幻觉（MOD 名编造/容量数字全错），
    放大到 ~1280 后正常（2026-09-17）。
  · 大图（>1600）：识卡走 provider **直连**（不经 AstrBot agent 的图片压缩），
    传的是原图；vision 耗时随像素近似线性 —— 生产 2326×870 实测 39 s，
    缩到 1600 宽约省一半（2026-09-20 维护者要求补上）。

直接 import main.py 需 astrbot 运行环境 → 导入前注入轻量桩（与 test_admin.py 同法）。
"""

from __future__ import annotations

import base64
import io
import sys
import types
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
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def exception(self, *a, **k):
            pass

    class AstrMessageEvent:
        def __init__(self):
            self.unified_msg_origin = "group://fit_test"

        def get_sender_name(self) -> str:
            return "stub_user"

        def get_sender_id(self) -> str:
            return "stub_id"

    class MessageChain:
        def message(self, text):
            return text

    class _EventMessageType:
        ALL = "ALL"

    class _Filter:
        EventMessageType = _EventMessageType
        event_message_type = staticmethod(lambda spec: lambda fn: fn)

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: lambda fn: fn
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
        def __init__(self, *a, **k):
            pass

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

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


FIT = plugin.WarframeSDJK._fit_scan_image


def data_url(w: int, h: int, fmt: str = "PNG", noisy: bool = False) -> str:
    """构造 data: URL。

    ``noisy=True`` 用高斯噪声图模拟真实截图的信息密度 —— 纯色图的 PNG 压缩率
    与尺寸不成比例（缩完反而可能更大），不能用来验证「体积下降」。
    """
    from PIL import Image as PILImage

    if noisy:
        img = PILImage.effect_noise((w, h), 48).convert("RGB")
    else:
        img = PILImage.new("RGB", (w, h), (30, 40, 60))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return f"data:image/{fmt.lower()};base64,{base64.b64encode(buf.getvalue()).decode()}"


def size_of(url: str) -> tuple:
    from PIL import Image as PILImage

    _, b64 = url.split(",", 1)
    return PILImage.open(io.BytesIO(base64.b64decode(b64))).size


# ---------------------------------------------------------------------------
# 1. 大图缩小（本次新增的能力）
# ---------------------------------------------------------------------------
out = FIT(data_url(2326, 870))
check("★ 大图 2326×870 → 缩到 1600 宽（本次新增）", size_of(out)[0] == 1600, str(size_of(out)))
w, h = size_of(out)
check("  等比保持（1600×598 左右）", abs(h - round(870 * 1600 / 2326)) <= 1, f"{w}x{h}")
check(
    "  ★ 面积降到约一半（提速依据）", (w * h) / (2326 * 870) < 0.52, f"{(w * h) / (2326 * 870):.3f}"
)

out = FIT(data_url(3000, 60))
check("极端宽高比 3000×60 → 宽也收到 1600", size_of(out)[0] == 1600, str(size_of(out)))

out = FIT(data_url(1601, 900))
check("边界：1601 宽 → 收进 1600", size_of(out)[0] == 1600, str(size_of(out)))

# ---------------------------------------------------------------------------
# 2. 小图放大（原有能力的回归）
# ---------------------------------------------------------------------------
out = FIT(data_url(648, 242))
check("小图 648×242 → 放大到 1280 宽", size_of(out)[0] == 1280, str(size_of(out)))
out = FIT(data_url(200, 80))
check("极小图 200×80 → 放大但不超过 4 倍（800 宽）", size_of(out)[0] == 800, str(size_of(out)))
out = FIT(data_url(1599, 900))
check("略低于阈值 1599 → 规整到 1280（实际是缩小）", size_of(out)[0] == 1280, str(size_of(out)))

# ---------------------------------------------------------------------------
# 3. 已在区间内 → 原样返回（不做无谓重编码）
# ---------------------------------------------------------------------------
same = data_url(1600, 900)
check("★ 1600 宽正好在区间内 → 原样返回（不重编码）", FIT(same) == same)
same2 = data_url(1900, 800)
check("1900 宽 > 1600 → 会被缩小（不是原样）", FIT(same2) != same2)

# ---------------------------------------------------------------------------
# 4. 容错：非 data URL / 坏 base64 一律原样返回，不抛异常
# ---------------------------------------------------------------------------
check("非 data: URL（http）→ 原样返回", FIT("https://x/y.png") == "https://x/y.png")
check("空串 → 原样返回", FIT("") == "")
bad = "data:image/png;base64,这不是合法base64!!"
check("★ 坏 base64 → 原样返回且不抛异常", FIT(bad) == bad)

# ---------------------------------------------------------------------------
# 5. 格式与体积
# ---------------------------------------------------------------------------
out = FIT(data_url(2326, 870, "JPEG"))
check("JPEG 输入仍输出 JPEG", out.startswith("data:image/jpeg"), out[:24])
big, small = data_url(2326, 870, noisy=True), FIT(data_url(2326, 870, noisy=True))
check(
    "★ 含噪点的大图缩小后体积明显下降（真实截图同理）",
    len(small) < len(big) * 0.6,
    f"{len(small)} vs {len(big)}",
)

# ---------------------------------------------------------------------------
# 6. 像素上限（2026-10-10 安全审查：防 PNG 解压炸弹）
# ---------------------------------------------------------------------------
OK = plugin.WarframeSDJK._image_pixels_ok


def _b64_png(w: int, h: int) -> str:
    buf = io.BytesIO()
    from PIL import Image as PILImage

    PILImage.new("1", (w, h)).save(buf, "PNG")  # 1-bit 全黑：大尺寸也只有几十 KB
    return base64.b64encode(buf.getvalue()).decode()


check("正常截图尺寸放行（2560×1440）", OK(_b64_png(2560, 1440)))
_bomb = _b64_png(8000, 6000)
check(
    "★ 4800 万像素图拒收（体积远小于 14MB 也拦得住）",
    not OK(_bomb) and len(_bomb) < 14 * 1024 * 1024,
    str(len(_bomb)),
)
check("非图片 base64 拒收且不抛", not OK(base64.b64encode(b"not an image").decode()))


# 7. 真实图片入口的有界读取（全离线，禁止宿主转换）
async def _bounded_image_checks():
    import asyncio
    import tempfile
    from unittest.mock import patch

    import httpx

    host = object.__new__(plugin.WarframeSDJK)
    png = base64.b64decode(data_url(4, 4).split(",", 1)[1])
    host.MAX_SCAN_IMAGE_BYTES = 128
    requests = []
    streams = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, chunks, slow=False):
            self.chunks = chunks
            self.slow = slow
            self.reads = 0
            self.closed = False
            streams.append(self)

        async def __aiter__(self):
            for chunk in self.chunks:
                if self.slow:
                    await asyncio.sleep(1)
                self.reads += 1
                yield chunk

        async def aclose(self):
            self.closed = True

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path == "/redirect":
            return httpx.Response(
                302, headers={"location": "https://gchat.qpic.cn/normal"}, stream=Stream([])
            )
        if path == "/declared":
            return httpx.Response(200, headers={"content-length": "10000"}, stream=Stream([png]))
        if path in {"/lie", "/large"}:
            headers = {"content-length": "1"} if path == "/lie" else {}
            return httpx.Response(200, headers=headers, stream=Stream([b"x" * 65536] * 3))
        if path == "/slow":
            return httpx.Response(200, stream=Stream([png], slow=True))
        if path == "/compressed":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Stream([png]))
        return httpx.Response(200, stream=Stream([png]))

    real_client = httpx.AsyncClient

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    with patch("httpx.AsyncClient", client):
        result = await host._read_scan_image("https://example.test/normal")
        check("远程正常图保持原始字节", base64.b64decode(result) == png)
        for path in ("declared", "lie", "large", "compressed"):
            rejected = False
            try:
                await host._read_scan_image("https://example.test/" + path)
            except ValueError:
                rejected = True
            check("有界读取拒收 " + path, rejected and streams[-1].closed)
            check("超限即停止后续读取 " + path, streams[-1].reads <= 1)
        host.MAX_SCAN_IMAGE_BYTES = 65536 + 100
        rejected = False
        try:
            await host._read_scan_image("https://example.test/large")
        except ValueError:
            rejected = True
        check(
            "累计第二块越界即停止第三块", rejected and streams[-1].reads == 2 and streams[-1].closed
        )
        host.MAX_SCAN_IMAGE_BYTES = 128
        result = await host._read_scan_image("https://example.test/redirect")
        check("重定向后仍通过限量路径", base64.b64decode(result) == png)
        headers = requests[-1].headers
        check(
            "QQ图床重定向保留UA与Referer",
            "Mozilla/5.0" in headers["user-agent"] and headers["referer"] == "https://im.qq.com/",
        )
        check("非图床不携带QQ Referer", "referer" not in requests[0].headers)
        host.SCAN_IMAGE_TIMEOUT = 0.01
        rejected = False
        try:
            await host._read_scan_image("https://example.test/slow")
        except asyncio.TimeoutError:
            rejected = True
        check("滴流总超时且关闭响应", rejected and streams[-1].closed)

    check(
        "严格14MiB编码边界对应原始字节",
        plugin.WarframeSDJK.MAX_SCAN_IMAGE_BYTES == (14 * 1024 * 1024 // 4 - 1) * 3,
    )
    check(
        "假图床域名不带Referer",
        "Referer" not in host._scan_image_headers("https://qpic.cn.evil.test/a"),
    )
    payload = base64.b64encode(png).decode()
    for ref in (
        payload,
        "base64://" + payload,
        "data:image/png;base64," + payload,
        payload[:20] + "\n" + payload[20:],
        "base64://" + payload[:20] + "\n" + payload[20:],
    ):
        check("本地base64来源兼容 " + ref[:12], await host._read_scan_image(ref) == payload)
    for ref, expected in (
        ("file://C:/test%20image.png", "C:/test image.png"),
        ("file:///C:/test%20image.png", "C:/test image.png"),
        ("file://localhost/C:/test%20image.png", "C:/test image.png"),
    ):
        opened = []

        def record_open(path, _flags):
            opened.append(path)
            raise OSError("只验证解析，不读取Windows磁盘")

        with patch("os.open", record_open):
            try:
                await host._read_scan_image(ref)
            except OSError:
                pass
        check("Windows本地drive URI兼容 " + ref, opened == [expected])
    with patch("os.open", side_effect=AssertionError("UNC不能读取")):
        rejected = False
        try:
            await host._read_scan_image("file://remote-host/share/image.png")
        except ValueError:
            rejected = True
        check("远程UNC file URI仍拒收", rejected)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test%20 image.png"
        path.write_bytes(png)
        for ref in (str(path), path.as_uri()):
            check("本地文件限量兼容 " + ref[:8], await host._read_scan_image(ref) == payload)
        path.write_bytes(b"x" * 129)
        rejected = False
        try:
            await host._read_scan_image(str(path))
        except ValueError:
            rejected = True
        check("本地超限拒收", rejected)
        rejected = False
        try:
            await host._read_scan_image(tmp)
        except ValueError:
            rejected = True
        check("本地目录拒收", rejected)
        import os

        if hasattr(os, "mkfifo"):
            fifo = Path(tmp) / "pipe"
            os.mkfifo(fifo)
            rejected = False
            try:
                await host._read_scan_image(str(fifo))
            except ValueError:
                rejected = True
            check("FIFO拒收且不等待写入方", rejected)
            fifo.unlink()

        check("读取未生成持久化缓存", list(Path(tmp).iterdir()) == [path])

    class Image:
        def __init__(self, ref):
            self.file = ref
            self.url = ""

        async def convert_to_base64(self):
            raise AssertionError("不允许调用宿主无界转换")

    event = types.SimpleNamespace(
        message_obj=types.SimpleNamespace(
            message=[
                Image("base64://" + base64.b64encode(b"bad image").decode()),
                Image("base64://" + payload),
                Image("https://must-not-fetch.test/"),
            ]
        )
    )
    with patch("httpx.AsyncClient", side_effect=AssertionError("不应读取第三张")):
        result = await host._image_data_urls(event)
    check("真实入口坏图后取第一张合格图并停止", result == ["data:image/png;base64," + payload])

    host.MAX_SCAN_IMAGE_BYTES = plugin.WarframeSDJK.MAX_SCAN_IMAGE_BYTES
    jpeg = base64.b64decode(data_url(64, 32, "JPEG").split(",")[1])[:-6]
    from PIL import Image as PILImage

    with PILImage.open(io.BytesIO(jpeg)) as truncated:
        truncated.verify()  # 此样本能通过单独的 JPEG 文件头检查。
    for bad in (data_url(32, 32, "BMP"), "base64://" + base64.b64encode(jpeg).decode()):
        event.message_obj.message = [Image(bad), Image("base64://" + payload)]
        assert await host._image_data_urls(event) == ["data:image/png;base64," + payload]
    for fmt in ("JPEG", "WEBP"):
        good = data_url(16, 16, fmt)
        event.message_obj.message = [Image(good)]
        assert await host._image_data_urls(event) == [good]

    # 原图 320×3000 合规，按 4x 放大将达到 1280×12000。只构造 1-bit 原图。
    strip = _b64_png(320, 3000)
    image_url = "data:image/png;base64," + strip
    event.message_obj.message = [Image(image_url), Image("base64://" + payload)]
    with patch("PIL.Image.Image.resize", autospec=True) as resize:
        result = await host._image_data_urls(event)
        check(
            "生产收图跳过派生长边超限图并接受下一张", result == ["data:image/png;base64," + payload]
        )
        check("单独调用规整也避免超预算分配", FIT(image_url) == image_url and not resize.called)
        with patch("core.pips.detect_pips") as detect:
            check(
                "超限图在豆子像素检测前拦截",
                await host._detect_pips(image_url) == [] and not detect.called,
            )
        panel, geometry = host._scan_focus_image(image_url, "panel", with_geometry=True)
        check(
            "聚焦裁剪保留安全尺寸与坐标，跳过超限放大",
            size_of(panel) == (89, 3000)
            and geometry["output_image_size"] == [89, 3000]
            and not resize.called,
        )
    check("派生总像素限制独立于长边", not OK(_b64_png(320, 2400)))
    host.cfg = {"scan_cooldown": 0}
    event.message_obj.message = [Image(image_url)]
    with patch.object(host, "_detect_pips") as detect:
        reply = await host._h_scan(None, event, "pc")
        check("识卡指令在进入豆子检测前结束超限请求", bool(reply.raw_text) and not detect.called)

    # 实际编码并检查送往装备恢复渠道的裁图，再验证返回的坐标映回原图。
    from unittest.mock import AsyncMock

    provider = types.SimpleNamespace(
        text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text="OCR"))
    )
    geometry = {"x": 500, "y": 500, "w": 100, "h": 100}
    with (
        patch("PIL.Image.Image.crop", side_effect=AssertionError("超预算恢复裁图分配了中间图")),
        patch("core.pips.upgrade_slots", return_value=[{"box": [0, 0, 1, 0.775]}]),
        patch.object(host, "_scan_equipment_providers", return_value=[provider]),
        patch(
            "core.loadout_ocr.parse_coordinate_ocr",
            return_value={"weapon": "A", "mods": [{"_geometry": geometry}]},
        ),
    ):
        recovered = await host._read_scan_recovery(
            "data:image/png;base64," + _b64_png(6000, 4000), [1], {}
        )
    w, h = size_of(provider.text_chat.await_args.kwargs["image_urls"][0])
    assert 0 < w * h <= 12_000_000 and abs(h - w * 3200 / 6000) <= 1
    assert recovered["_recognition"]["geometry_transform"]["bounds"] == [0, 0, 1, 0.8]
    assert recovered["mods"][0]["_geometry"] == {**geometry, "y": 400, "h": 80}


import asyncio  # noqa: E402

asyncio.run(_bounded_image_checks())

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 识卡图片规整（放大+缩小）守卫全部通过")
