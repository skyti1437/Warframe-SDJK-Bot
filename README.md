# AstrBot 插件 · Warframe SDJK v1.0（`astrbot_plugin_warframe_sdjk`）

一个给 AstrBot 用的 Warframe 工具集：**世界状态 / 市场查价 / 伤害计算与配卡识别 /
紫卡分析 / 遗物与部件反查 / 玄骸拍卖 / 奸商预测 / 蹲点推送**，
并支持渲染成 Orokin 风格图片卡片。

指令为**自由参数式**：`主指令 + 内容 + 附加项`，三者位置任意、空格分隔。
例如 `伤害 绝路 镀层3`、`遗物 出库 -2`、`仲裁 钢铁` 都可以。

> 本插件以 GPL-3.0 开源。数据与人格相关的私有集成**已移除**
> （见「开源版与自用版的差异」）。

---

## 一、功能一览

| 分组 | 代表指令 |
| --- | --- |
| 周期与日常 | `夜灵` `赏金` `裂隙` `突击` `执刑官` `时效` |
| 任务与轮换 | `深层科研` `沉沦之地` `侵袭` `仲裁` `仲裁表` `九重天` |
| 商店与周常 | `奸商` `奸商 预测` `每日特惠` `言录使` `氏族奖励` `出库` |
| 玄骸拍卖 | `xh <武器> [元素] [数值]`（赤毒/信条/终幕三类） |
| 遗物与部件 | `遗物 <名>`（奖励与出处）`遗物 <部件>`（反查）`遗物 出库/入库` `开核桃` `金垃圾` |
| 市场与紫卡 | `wm <物品>` `wr` `rm` `rank` `trend` `analysis` `disposition` |
| 伤害计算 | `伤害 <武器> <MOD…>` `识卡`（配卡截图识别）`识卡伤害` `scandamage` |
| 其他 | `wiki <词>` `状态` `蹲 <事件>` `帮助` … 共 **54 个主指令** |

`帮助` 指令会输出完整指令表（分 7 组），由 `tests/test_help_coverage.py` 锁定覆盖率。

## 一点五、三个值得单独说的功能

**① 玄骸拍卖（`xh`）** —— 覆盖赤毒（Kuva）/ 信条（Tenet）/ 终幕（Coda）
三类共 47 把武器，中文名支持 `赤毒怒雷` / `赤毒·布拉玛` / `布拉玛` 等多种写法。
每行给：价格 · **元素（中文）** · 伤害 · **卖家在线情况** · **信用等级**，在线优先。
支持筛选：`xh 赤毒怒雷 辐射 50`（元素 + 伤害下限）。
部分武器（近战 Tenet、全部终幕）warframe.market 没有挂单类目，插件会明确说明
「市场查不到，只能游戏内交易」，不会报错。

**② 奸商预测（`奸商 预测`）** —— 基于 wiki 记录的 **314 次到访 / 466 件商品**
历史做统计推测：按「已静默次数 ÷ 它自己的平均上架间隔」排序，越接近 1
越「该回来了」。**不是官方预测**（DE 从不公布下期库存），卡片上写明口径与样本量。

**③ 遗物与部件（`遗物`）** —— 遗物名全中文（`古纪 A12`）；部件反查会标注每把
遗物的**当前可获取状态**（可掉落 / 仅阿耶兑换 / 已入库），并给出**推荐刷取点**
与「只在特定位置能获取」的特殊渠道。

---

## 二、安装

1. 安装 AstrBot（≥ 4.x），并准备好消息平台适配器（NapCat / aiocqhttp 等）。
   **Python 要求 ≥ 3.10**（代码使用了 3.10+ 的联合类型语法；AstrBot 4.x 自带的
   运行环境一般已满足，自建环境时留意）。
2. 把本仓库放到 AstrBot 插件目录（或打包成 zip 后在 WebUI 里上传）：
   ```
   data/plugins/astrbot_plugin_warframe_sdjk/
   ```
3. 安装依赖：
   ```bash
   pip install -r requirements.txt      # httpx、pillow
   ```
4. **下载渲染字体**（推荐，一条命令）：
   ```bash
   python scripts/fetch_font.py
   ```
   验证当前用的是哪个字体：`python scripts/fetch_font.py --check`
5. 重载插件，发送 `帮助` 验证。

> **关于字体**：卡片渲染用 Noto Sans CJK（Regular + Bold 约 40 MB）。
> 它**不随仓库分发** —— 一是体积比插件本体还大，二是会让 zip 超过 AstrBot
> 插件市场的 16 MB 上限。不跑第 4 步也能用：`core/render.py` 的候选里带了
> 各平台常见中文字体（Windows `msyh.ttc`、macOS `PingFang`、Linux Noto/WQY），
> 只是字形可能与作者出图略有差异；**Linux 服务器若一个中文字体都没装，
> 卡片会渲染成方块**，务必跑一次脚本。

## 三、配置（WebUI 插件配置页）

常用项：

| 配置项 | 说明 |
| --- | --- |
| `worldsource` | 世界状态数据源：`de`=DE 官方直连（默认，无 CF 风险）/ `warframestat`=社区镜像 |
| `default_platform` | 默认平台 `pc`/`ps`/`xb`/`sw` |
| `render_mode` | `image`（默认图片卡片）/ `auto` / `text` |
| `flaresolverr_enabled` | 是否启用 CF 绕过代理（见下） |
| `flaresolverr_urls` | FlareSolverr 地址，多个用逗号或换行分隔，**自己填** |
| `vision_provider_id` | 截图识别用的多模态模型 id（见「AI 相关说明」） |
| `kb_enabled` / `kb_docs_dir` / `kb_id` | 知识库引导（见下） |
| `proxy` / `http_timeout` / `push_interval` | 网络与推送参数 |

### FlareSolverr（可选）

`wiki.warframe.com` 对非浏览器请求直接 403，取效价轮换、紫卡倾向快照需要它：

```bash
docker run -d --name flaresolverr -p 8191:8191 flaresolverr/flaresolverr
```

然后在面板把地址填进 `flaresolverr_urls`。**插件跑在容器里时 `127.0.0.1` 通常到不了
宿主机端口**，要填容器名（`http://flaresolverr:8191`）或 Docker 网桥网关
（常见 `http://172.17.0.1:8191`）。不需要这个功能就把 `flaresolverr_enabled` 关掉，
相关指令会给出降级提示而不是卡住。

### 知识库（可选）

插件**不直连**知识库：检索由 AstrBot 平台的 RAG 提供。这里只是把 `kb/` 目录下的
文档（机制术语 / 新手入门 / 玄骸刷取）交给你，自行上传到 AstrBot 知识库后，
「推荐类」问题的回答质量会明显更好。`kb_enabled=false` 可以关掉相关提示。

## 四、AI 相关说明

### 1. 运行时会用到 AI 的地方

| 场景 | 用 AI 做什么 | 由谁提供 |
| --- | --- | --- |
| **识卡 / 紫卡截图识别** | 把配卡截图读成结构化数据（MOD 名、等级、面板数值） | 你在 AstrBot 里配置的**多模态模型**，用 `vision_provider_id` 指定 |
| **推荐/攻略类问答** | 走 AstrBot 平台的 LLM + 知识库（RAG） | AstrBot 平台自带，插件不参与 |

也就是说：**插件本身不内置任何模型、不代持任何 API Key**。识卡功能必须你自己
配一个多模态渠道；没配时识卡会直接给出「未配置视觉模型」的提示，不会静默失败。

实测可用的视觉模型（2026-09，仅供参考）：
`Qwen/Qwen3-VL-32B-Instruct`（伤害行识别最稳，但一次调用较慢）、
`glm-4.1v-thinking-flash`（快，复杂面板容易读串）。插件内部有**面板校验 + 换渠道重试**
的闭环，识别结果对不上游戏面板时会自动换下一个渠道再试。

### 2. 开发过程中 AI 的参与

诚实说明：**这个项目的绝大部分代码是在 AI 辅助下完成的**。

- 主要开发方式：由人类（Sky）提出需求、提供截图与实测反馈，AI 负责
  代码编写、数据核对、测试与文档。
- AI 也用于：伤害公式的交叉验证（对拍 wiki 与游戏面板）、译名查证
  （DE 官方简中导出表）、卡片版式的像素级调整。
- 因此代码里保留了大量「为什么这么写」的注释和防回归测试 —— 那正是
  为了弥补 AI 改动容易引入静默回归的问题。

如果你要接手维护，建议：**改任何公式/版式前先跑 `tests/`**（共 29 个测试文件），
尤其是 `tests/audit_damage_calc.py`（伤害引擎不变量审计）。

## 五、数据来源与许可

| 数据 | 来源 | 许可 / 说明 |
| --- | --- | --- |
| 掉落表（`core/data/drops.json`） | [WFCD/warframe-drop-data](https://github.com/WFCD/warframe-drop-data) | MIT |
| 物品/武器/MOD 面板 | [WFCD/warframe-items](https://github.com/WFCD/warframe-items) + DE 官方导出 + wfsim 校准 | 数据源头为 DE |
| 中文译名（`core/data/de/`） | DE 官方 Public Export（经 [calamity-inc/warframe-public-export-plus](https://github.com/calamity-inc/warframe-public-export-plus) 整理） | 数据源头为 DE，见下方声明 |
| 世界状态 | DE 官方 `api.warframe.com` | 实时接口 |
| 市场价 / 拍卖 | warframe.market v2 / v1 | 遵守其 3 req/s 限速 |
| 赏金轮换 / 钢铁侵袭 | [oracle.browse.wf](https://oracle.browse.wf)、[browse.wf](https://browse.wf) | calamity-inc 开源项目 |
| 仲裁排期 | [arbi.wf.wiki](https://arbi.wf.wiki) | 社区站点 |
| wiki 抓取（效价、紫卡倾向） | `wiki.warframe.com` | 需 FlareSolverr |
| 字体 | Noto Sans CJK | SIL OFL |

> **非官方声明**：Warframe 与相关商标归 Digital Extremes 所有。本插件是粉丝作品，
> 与 Digital Extremes 无关；所含游戏数据按 DE 的粉丝内容政策使用，仅用于查询展示。

## 六、隐私说明

- 插件**只在你自己的机器人上运行**，不向任何第三方上报数据；所有查询都直接
  打给上表列出的公开数据源。
- 运行时数据（群配置、推送订阅、渲染缓存、价格排行/倾向表快照）按 AstrBot 插件
  规范写在 `data/plugin_data/astrbot_plugin_warframe_sdjk/` 下，**不落在插件目录**
  （插件目录对安装用户只读，升级还会整包覆盖）。早期版本写在插件目录 `runtime/`
  里的用户数据会在首次启动时自动搬到新位置，之后不再往插件目录写任何东西。
- 识卡功能会把**你发的截图**交给你自己配置的多模态模型渠道处理 —— 也就是说
  图片会离开你的服务器、进入你选择的模型服务商。介意的话请不要使用该功能，
  或改用一个本地/自建的视觉模型。
- 开源版本已移除自用版里依赖外部「好感度」插件的功能（它会读取用户维度的
  关系数据），并清理了相关数据。

## 七、开源版与自用版的差异

| 项目 | 开源版 | 自用版 |
| --- | --- | --- |
| 好感度 → 人格温度联动（读取外部好感度插件的数据） | **移除** | 保留 |
| 品牌标识 | 通用插件名（Warframe SDJK） | 原氏族品牌 |
| 内部文档、运维脚本、第三方插件打包脚本 | 不带 | 保留 |

技术上是**同一份源码**，开源包由 `dist/package_release.py --opensource` 构建时剥离，
自用包照旧打包，两边互不干扰。

## 八、附带的脚本（`scripts/` 与根目录）

平时用不到；只在「改数据 / 调版式 / 排障」时需要。按用途分组：

| 分组 | 脚本 | 说明 |
| --- | --- | --- |
| **安装必需** | `scripts/fetch_font.py` | 下载渲染字体（见第二节第 4 步） |
| **数据刷新** | `scripts/fetch_valence.py` | 刷新玄骸效价轮换（wiki 每 4 天换批，**只展示当前批**，要定期重跑） |
| **数据构建** | `build_damage_data.py`、`build_de_data.py`、`build_stances.py`、`scripts/build_*.py`、`scripts/enrich_mods_from_wfsim.py`、`scripts/merge_wfsim_enemies.py`、`scripts/sync_mods_from_wfsim.py`、`scripts/repair_panel_basis.py` | 从 WFCD / DE 导出 / wfsim 重新生成 `core/data/*.json`。**需要额外装 PyYAML**：`pip install pyyaml`（插件运行本身不需要它） |
| **排障** | `scripts/diag_command_output.py`、`scripts/diag_worldstate_keys.py`、`scripts/smoke_all.py` | 在容器里直接跑指令看输出 / 查 DE 原始键 / 冒烟全指令 |
| **版式开发** | `scripts/dev_relic_preview.py`、`scripts/dev_help_preview.py`、`scripts/bench_supersample*.py`、`scripts/diag_col_starts.py`、`scripts/diag_help_layout.py`、`scripts/diag_row_edges.py` | 本地预览卡面、渲染性能基准、列位像素诊断（改版式必用） |
| **工具** | `scripts/lookup_zh.py` | 反查 DE 官方简中译名（确认某个词条官方到底有没有） |

> 排查线上问题最常用的一条：
> `python scripts/diag_command_output.py "遗物 出库"`（在 AstrBot 容器内跑，
> 直接打印 handler 的真实返回，绕过 QQ 只看结果）。

## 九、许可

- 代码：**GPL-3.0**（见 `LICENSE`）
- 数据：见上表各来源的许可；游戏内容归 Digital Extremes
- 字体：SIL Open Font License
