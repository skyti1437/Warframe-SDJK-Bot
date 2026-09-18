# -*- coding: utf-8 -*-
"""端到端实测：对可疑指令直接调用 handler，看**用户实际收到什么**。

重点：插件绝不能对已知失效的功能静默（本项目最高优先级 bug 类型），
必须给出可操作的降级提示。本脚本就是验证这一点的。

用法（容器内）：
    PYTHONPATH=<plugin> python3 scripts/diag_command_output.py 仲裁 赤毒 钢铁之路
"""
from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import parser as P  # noqa: E402


class FakeEvent:
    """最小事件桩：handler 只用到这几项。"""

    def __init__(self):
        self.message_obj = type("M", (), {"message": "", "message_str": ""})()
        self.unified_msg_origin = "test:GroupMessage:0"

    def get_sender_id(self):
        return "0"

    def get_group_id(self):
        return "0"

    def get_platform_name(self):
        return "aiocqhttp"


async def main():
    queries = sys.argv[1:] or ["仲裁", "赤毒", "钢铁之路", "警报", "仲裁表"]
    from main import WarframeSDJK

    plugin = WarframeSDJK(context=None, config={})
    routes = plugin._build_routes()
    print(f"[init] 路由表 {len(routes)} 项，渲染器可用={getattr(plugin.renderer, 'available', '?')}\n")

    for q in queries:
        parsed = P.parse(q)
        handler = routes.get(parsed.command)
        print(f"=== 「{q}」 → cmd={parsed.command} ===")
        if not handler:
            print("  ✗ 路由表里没有该 handler\n")
            continue
        try:
            r = await handler(parsed, FakeEvent(), "pc")
        except Exception as exc:  # noqa: BLE001
            # 复刻 main.chat() 的兜底分支：WarframeAPIError 会被转成
            # 「⚠️ {exc}」文本回复，其它异常走通用内部错误。
            from core.api_client import WarframeAPIError
            if isinstance(exc, WarframeAPIError):
                print("  → 用户实际收到（chat 兜底后）:")
                for line in f"⚠️ {exc}".splitlines():
                    print("      " + line)
            else:
                print(f"  ✗ 未捕获异常 {type(exc).__name__}: {exc}")
                traceback.print_exc()
            print()
            continue

        pages = getattr(r, "pages", None)
        txt = getattr(r, "raw_text", None)
        if pages:
            print(f"  ✓ 多页回复 {len(pages)} 页")
            for i, pg in enumerate(pages, 1):
                t = (getattr(pg, "raw_text", "") or "")
                print(f"     第{i}页: {t[:180]}")
        elif txt:
            print("  ✓ 文本回复:")
            for line in str(txt).splitlines()[:10]:
                print("      " + line)
        else:
            print(f"  ? 其它类型 {type(r).__name__}: {r!r}"[:300])
        print()


asyncio.run(main())
