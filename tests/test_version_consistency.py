# -*- coding: utf-8 -*-
"""版本号一致性守卫（python3 tests/test_version_consistency.py）

抬版本时要同时改 metadata.yaml、main.py 的 ``@register`` 版本、卡片水印三处，
曾经因为漏改导致卡片水印停在旧号上。这里把「单一来源」钉住：

* ``core/__init__.py:__version__`` 是主版本来源；
* ``core/render.WATERMARK_VERSION`` 必须由它派生（主次版本）；
* ``metadata.yaml`` 的 ``version`` 与 ``@register`` 的版本必须一致。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import __version__ as CORE_VERSION          # noqa: E402
from core import __brand__ as BRAND                   # noqa: E402
from core import __brand_card__ as BRAND_CARD         # noqa: E402
from core import render as R                          # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


MM = ".".join(str(CORE_VERSION).split(".")[:2])       # 1.3.0 -> 1.3

check("core.__version__ 形如 X.Y.Z",
      bool(re.fullmatch(r"\d+\.\d+\.\d+", str(CORE_VERSION))), str(CORE_VERSION))
check("卡片水印版本由 core.__version__ 派生",
      R.WATERMARK_VERSION == MM, f"{R.WATERMARK_VERSION} vs {MM}")
# 水印/品牌断言一律用**派生值**比较（不写死品牌）：2026-09-18 换品牌时因为断言
# 写死品牌名，改名连带崩了测试；2026-09-19 改名时改为从 core.__brand__ 派生。
check("水印串含派生版本",
      R.WATERMARK.endswith(R.WATERMARK_VERSION), R.WATERMARK)
check("水印含卡片短品牌（派生自 core.__brand_card__）",
      BRAND_CARD in R.WATERMARK, f"{R.WATERMARK} 不含 {BRAND_CARD}")
check("卡片短品牌与展示名一致（改名只需改 core/__init__.py 两个常量）",
      BRAND_CARD in BRAND.replace(" ", ""), f"{BRAND} / {BRAND_CARD}")
check("展示名与短品牌都是非空字符串",
      isinstance(BRAND, str) and bool(BRAND.strip())
      and isinstance(BRAND_CARD, str) and bool(BRAND_CARD.strip()),
      f"{BRAND!r} / {BRAND_CARD!r}")

meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
m = re.search(r"^version:\s*v?([\d.]+)\s*$", meta, re.M)
check("metadata.yaml 有 version 字段", bool(m))


def _mm(v: str) -> str:
    """取主次版本 —— AstrBot 插件市场规范要求三位 semver（2.0.0），
    而 core.__version__ 也是三位；旧断言写死两位（2.0）会在开源包里误报。"""
    parts = (v or "").lstrip("v").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else (v or "")


check("metadata.yaml 版本 == core 主次版本",
      bool(m) and _mm(m.group(1)) == MM, m.group(1) if m else "?")

# ★ metadata 的 ``name`` 必须是**合法 Python 标识符**：AstrBot 4.x 用它当
#   插件模块名来 import（star_manager._validate_importable_name 会校验
#   `name.isidentifier()`）。2026-09-18 实测：服务端那份写成了显示名
#   「Warframe 查询助手」，带空格 → 从仓库/zip 安装时直接抛
#   「metadata 文件中 name 不是合法的模块名称」而失败。
#   显示名要放 ``display_name`` 字段，别占用 name。
def _meta_name(text: str) -> str:
    """取 metadata 的 name —— 用逐行解析而不是正则。

    用正则写过一版（`^name:` + 空白通配），但经 heredoc 落盘后正则里的
    反斜杠被吞成 `//s`，匹配恒为空 → 断言假失败。逐行解析没有转义问题。
    """
    for line in (text or "").splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return ""


mn = _meta_name(meta)
check("metadata.yaml 的 name 是合法模块名（可被 importlib 加载）",
      bool(mn) and mn.isidentifier(), mn or "缺 name 字段")
check("显示名放 display_name，不占用 name 字段",
      "display_name:" in meta, "缺 display_name")

# 开源包那份（由 dist/package_release.py 生成）同样要合法
oss_meta = ROOT / "dist" / "opensource" / "astrbot_plugin_warframe" / "metadata.yaml"
if oss_meta.exists():
    om = oss_meta.read_text(encoding="utf-8")
    omn = _meta_name(om)
    check("开源包 metadata.yaml 的 name 也是合法模块名",
          bool(omn) and omn.isidentifier(), omn or "缺 name 字段")
    # 开源包的 metadata 是 dist/package_release.py 的 _oss_metadata() **整体生成**的
    # （不是拷贝源文件），所以品牌/版本最容易在这里漏改 —— 钉住。
    check("★ 开源包 metadata.yaml 的 display_name == core.__brand__",
          any(ln.split(":", 1)[1].strip() == BRAND for ln in om.splitlines()
              if ln.startswith("display_name:")),
          "开源包展示名未跟随品牌（改 dist/package_release.py::_oss_metadata）")
    omm = re.search(r"^version:\s*v?([\d.]+)\s*$", om, re.M)
    check("★ 开源包 metadata.yaml 的版本 == core 主次版本",
          bool(omm) and _mm(omm.group(1)) == MM, omm.group(1) if omm else "?")

# ★ CHANGELOG.md 是 AstrBot 面板「更新日志」页与插件市场「更新日志」Tab 的数据源。
#   没有它，市场那片是空的 —— 所以把它变成硬性守卫：发版必须补一条。
changelog = ROOT / "CHANGELOG.md"
check("★ CHANGELOG.md 存在（市场「更新日志」Tab 的数据源）", changelog.exists())
if changelog.exists():
    ctext = changelog.read_text(encoding="utf-8")
    check("★ CHANGELOG.md 含当前版本条目",
          f"## v{CORE_VERSION}" in ctext,
          f"缺「## v{CORE_VERSION}」（发版时忘了写更新日志）")
    check("CHANGELOG.md 最新条目排在最前",
          ctext.index(f"## v{CORE_VERSION}") <
          min([ctext.index(f"## v{v}") for v in ("1.0.2", "1.0.1", "1.0.0")
               if f"## v{v}" in ctext] or [10 ** 9]),
          "版本未按从新到旧排列")
    _first_ver = re.search(r"^## (v[\d.]+)", ctext, re.M)
    check("★ CHANGELOG 首条版本 == 当前版本（发版必补首条，2026-09-21 升级为强等）",
          bool(_first_ver) and _first_ver.group(1) == f"v{CORE_VERSION}",
          f"首条 {_first_ver.group(1) if _first_ver else '无版本标题'} vs v{CORE_VERSION}")

# ★ 版本联动第五处：README 版本标题（2026-09-21 v1.0.4 发版漏改事故后补，
#    cab29df/ccc41c8 修复）。工作树 README 与开源 stage 的 README 源首行都带版本号；
#    漏改会让市场件内 README 停在旧版（用户实测 v1.0.4 市场件里还是 v1.0.3）。
for _rd, _tag in ((ROOT / "README.md", "工作树 README"),
                  (ROOT / "dist" / "OPENSOURCE_README.md", "dist/OPENSOURCE_README")):
    if not _rd.exists():
        check(f"★ {_tag} 存在（版本标题第五处）", False, str(_rd))
        continue
    _first_line = _rd.read_text(encoding="utf-8").splitlines()[:1]
    _first_line = _first_line[0] if _first_line else ""
    check(f"★ {_tag} 首行标题含当前版本",
          f"v{CORE_VERSION}" in _first_line,
          f"{_first_line!r} 不含 v{CORE_VERSION}")

main_src = (ROOT / "main.py").read_text(encoding="utf-8")
# 第 3 个参数现在是 f-string（用品牌常量拼），所以允许可选 f 前缀
m2 = re.search(r'@register\(\s*"[^"]+",\s*"[^"]+",\s*\n\s*f?"[^"]*",\s*\n\s*"([\d.]+)"',
               main_src)
check("@register 版本与 core.__version__ 完全一致（四处联动强断言）",
      bool(m2) and m2.group(1) == str(CORE_VERSION),
      m2.group(1) if m2 else "?")
# 卡片标题已经不写死品牌名（用 f"{BRAND}" 引用常量），所以这两条改成：
#   ① 断言源码里确实用常量拼标题（防止有人又写死）
#   ② 断言 metadata 的展示名与常量一致（改名时最容易漏的一处）
# 2026-09-24 用法速查自查：状态卡标题的**版本号**也从写死改为引用派生常量。
#   原写法 `Reply(f"{BRAND} 1.0"` 靠这里的 MM 字面量比对当「抬版本绊线」，
#   但水印本就派生自 core.__version__ —— 标题再写死一份等于两个版本源，
#   抬到 1.1 时同一张卡上水印 1.1 / 标题 1.0 会打架。现在两处同源。
check("状态卡标题版本引用派生常量（不写死）",
      'Reply(f"{BRAND} {WATERMARK_VERSION}"' in main_src,
      "状态卡标题未引用 core.render.WATERMARK_VERSION")
check("main.py 卡片标题里没有写死的版本号",
      not re.search(r'\{BRAND\}\s*[\d.]+', main_src),
      "发现写死的版本字面量（应引用 WATERMARK_VERSION）")
check("帮助卡标题用品牌常量",
      'Reply(f"{BRAND} 指令一览"' in main_src, "未找到帮助卡标题")
check("★ metadata.yaml 的 display_name == core.__brand__",
      any(ln.split(":", 1)[1].strip() == BRAND for ln in meta.splitlines()
          if ln.startswith("display_name:")),
      f"display_name 与 {BRAND!r} 不一致")
check("★ metadata.yaml 的 desc 以品牌名开头",
      f"{BRAND}：" in meta, "desc 首词未跟随品牌")

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
