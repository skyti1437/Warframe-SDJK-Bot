# -*- coding: utf-8 -*-
"""识卡图片宽度规整 `_fit_scan_image` 回归测试（python3 tests/test_scan_image_fit.py）

覆盖「小图放大 + 大图缩小」：
  · 小图（<1600）：实测 648×242 直接喂模型会大面积幻觉（MOD 名编造/容量数字全错），
    放大到 ~1280 后正常（2026-09-17）。
  · 大图（>1600）：识卡走 provider **直连**（不经 AstrBot agent 的图片压缩），
    传的是原图；vision 耗时随像素近似线性 —— 生产 2326×870 实测 39 s，
    缩到 1600 宽约省一半（2026-09-20 用户要求补上）。

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
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass

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
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
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
check("  ★ 面积降到约一半（提速依据）",
      (w * h) / (2326 * 870) < 0.52, f"{(w * h)/(2326*870):.3f}")

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
check("极小图 200×80 → 放大但不超过 4 倍（800 宽）",
      size_of(out)[0] == 800, str(size_of(out)))
out = FIT(data_url(1599, 900))
check("略低于阈值 1599 → 规整到 1280（实际是缩小）",
      size_of(out)[0] == 1280, str(size_of(out)))

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
check("★ 含噪点的大图缩小后体积明显下降（真实截图同理）",
      len(small) < len(big) * 0.6, f"{len(small)} vs {len(big)}")

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 识卡图片规整（放大+缩小）守卫全部通过")
