# -*- coding: utf-8 -*-
"""`main.py` 方法契约检查 —— 防止「编辑时把 @staticmethod 吃掉」。

事故（2026-09-20 真机）：
  给 `_fit_scan_image` 前面插入新的静态方法时，Edit 的替换范围把
  `_fit_scan_image` 原有的 `@staticmethod` 装饰器一起吃掉了，于是它退化成
  实例方法 —— 调用 `self._fit_scan_image(image_url)` 时 `self` 被塞进了
  `image_url` 形参，运行时抛
  `AttributeError: 'WarframeSDJK' object has no attribute 'startswith'`。
  ★ **md5 三处一致性核验查不出这种错**（三处文件完全一致，但代码是坏的），
    所以必须有一道**语义**检查。

本测试用 AST 静态分析（不 import main，因此不需要 AstrBot 环境）：
  ① 普通实例方法：第一个形参必须是 `self`
  ② `@staticmethod`：第一个形参不能是 `self`
  ③ 同一函数不得重复标 `@staticmethod`
  ④ 关键性能敏感/纯函数方法必须仍是静态方法（白名单）
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"

# 这些必须是静态方法（改动会让调用方悄悄错位）
MUST_BE_STATIC = {
    "_detect_pips",        # 豆子像素检测（async 静态）
    "_fit_scan_image",     # 截图宽度规整
}

FAILED = []


def check(name, cond, info=""):
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAILED.append(name)
    print(f"[{mark}] {name}" + (f"  ← {info}" if info and not cond else ""))


def _dec_names(fn):
    out = []
    for d in fn.decorator_list:
        out.append(d.id if isinstance(d, ast.Name) else getattr(d, "attr", "?"))
    return out


def main() -> int:
    src = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    problems, static_names, dup = [], set(), []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decs = _dec_names(fn)
            args = [a.arg for a in fn.args.args]
            first = args[0] if args else ""
            is_static = "staticmethod" in decs
            is_cls = "classmethod" in decs
            if is_static:
                static_names.add(fn.name)
                if decs.count("staticmethod") > 1:
                    dup.append(f"{cls.name}.{fn.name}")
            if not is_static and not is_cls and first != "self":
                problems.append(f"{cls.name}.{fn.name}: 普通方法但首参是 {first!r}")
            if is_static and first == "self":
                problems.append(f"{cls.name}.{fn.name}: staticmethod 却带 self")

    check("所有方法的首参与被装饰类型一致（self ↔ 实例方法）",
          not problems, "; ".join(problems[:4]))
    check("没有重复的 @staticmethod", not dup, "; ".join(dup[:4]))
    for name in sorted(MUST_BE_STATIC):
        check(f"★ {name} 仍是 staticmethod", name in static_names)

    print()
    if FAILED:
        print(f"[FAIL] {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("[OK] main.py 方法契约检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
