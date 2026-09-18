# -*- coding: utf-8 -*-
"""离线渲染帮助卡预览（不导入 main.py，避开 astrbot 依赖）。

用 AST 从 main.py 里取出 HELP_TOPIC，复刻 _h_help 的行构造，渲染成图。
方便在改版式/改文案时先看效果，而不必部署到线上。

用法：
    python scripts/dev_help_preview.py              # 渲染当前版本
    python scripts/dev_help_preview.py --rev HEAD~1  # 渲染某个历史版本
产物：runtime/help_preview[_rev].png
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_help_topic(source: str | None = None) -> dict:
    """从 main.py（或指定源码文本）里 AST 提取 HELP_TOPIC。"""
    if source is None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        target = getattr(node, "target", None)
        if isinstance(target, ast.Name) and target.id == "HELP_TOPIC":
            return ast.literal_eval(node.value)
    raise SystemExit("main.py 里没找到 HELP_TOPIC")


def main():
    rev = None
    args = sys.argv[1:]
    if "--rev" in args:
        rev = args[args.index("--rev") + 1]

    src = None
    if rev:
        src = subprocess.run(
            ["git", "show", f"{rev}:main.py"], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8",
        ).stdout
        if not src:
            raise SystemExit(f"取不到 {rev}:main.py")

    topic = load_help_topic(src)
    lines: list[str] = []
    for t, items in topic.items():
        lines.append(f"◆ {t}")
        lines += [f"· {c}　{d}" for c, d in items]

    from core import render as R

    r = R.ImageRenderer(Path(tempfile.mkdtemp(prefix="help_prev_")))
    if not r.available:
        raise SystemExit("渲染器不可用（缺字体）")
    p = r.render("Warframe 查询助手 指令一览", lines, "平台：国际服")

    out = ROOT / "runtime" / ("help_preview.png" if not rev
                              else f"help_preview_{rev.replace('~', '_').replace('^', '_')}.png")
    out.parent.mkdir(exist_ok=True)
    import shutil
    shutil.copy(p, out)
    total = sum(len(v) for v in topic.values())
    print(f"分类 {len(topic)} 个，条目 {total} 条，行数 {len(lines)}")
    print(f"已保存 {out}")


if __name__ == "__main__":
    main()
