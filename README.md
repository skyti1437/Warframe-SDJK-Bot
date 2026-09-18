# Warframe 查询助手 · AstrBot 插件 v1.0

给 AstrBot 用的 Warframe 工具集：**世界状态查询 · 市场查价 · 伤害计算与配卡识别 ·
紫卡分析 · 遗物与部件反查 · 玄骸拍卖 · 奸商预测 · 蹲点推送**，
可把结果渲染成 Orokin 风格的图片卡片。

指令为**自由参数式**：`主指令 + 内容 + 附加项`，位置任意、空格分隔。
例如 `伤害 绝路 镀层3`、`遗物 出库 -2`、`仲裁 钢铁`、`xh 赤毒怒雷 辐射` 都能用。

> 发送 `帮助` 查看全部 54 个主指令（分 7 组）；管理指令以 `.` 开头。

---

## 一、能力一览

| 分组 | 代表指令 | 说明 |
| --- | --- | --- |
| 周期与日常 | `夜灵` `赏金` `裂隙` `突击` `执刑官` `时效` | 各周期轮换与剩余时间 |
| 任务与轮换 | `深层科研` `沉沦之地` `侵袭` `仲裁` `仲裁表` `九重天` | 本周任务与排期 |
| 商店与周常 | `奸商` `奸商 预测` `每日特惠` `言录使` `氏族奖励` `出库` | 含**奸商下期预测** |
| 遗物与部件 | `遗物 <名>` `遗物 <部件>` `遗物 出库/入库` `开核桃` `金垃圾` | 中文名、可获取状态、刷取建议 |
| 玄骸拍卖 | `xh <武器> [元素] [数值]` | 赤毒/信条/终幕三类，含在线与信用 |
| 市场与紫卡 | `wm` `wr` `rm` `rank` `trend` `analysis` `disposition` | warframe.market 查价与紫卡分析 |
| 伤害与识卡 | `伤害 <武器> <MOD…>` `识卡` `识卡伤害` `scandamage` | 伤害计算器与配卡截图识别 |
| 其它 | `wiki` `状态` `.默认平台` `.开启/关闭 推送` | 直达维基、运行状态、群管理 |

## 二、几个值得单独说的功能

### 1. 伤害计算器（`伤害`）
582 把武器（含显赫/空战/守护）× 271 MOD × 102 敌人；面板 / 单发 / DPS /
含段合计四个口径，卡面同时给**含暴击期望**与**无暴击**两种结果。
支持灵化形态与进化、多段合击、玄骸回响（`赤毒辐射60`）、空战双部署。

数据底座做过**三源交叉验证**：游戏内面板实测、wfsim.app 逐项对拍
（20 把 × 5 项 = 97/100 一致，余下均为段口径）、overframe MOD 数据比对。

### 2. 识卡（`识卡`）
发一张游戏内「升级」界面截图 + 文字「识卡」：
读出武器（含等级）、每张 MOD 与其**实际等级**、面板数值；
加成由**卡片容量数字反推等级**后重算，再用面板数值交叉校验。
需要你在 AstrBot 里配一个多模态模型（面板 `vision_provider_id`）。

> 为防被刷爆额度与 CPU，识卡有冷却与并发上限（面板可调，设 0 关闭）。

### 3. 玄骸拍卖（`xh`）
覆盖 **赤毒（Kuva）/ 信条（Tenet）/ 终幕（Coda）** 三类共 47 把武器，
中文名支持多种写法（`赤毒怒雷` / `赤毒·布拉玛` / `布拉玛` / 英文名）。
每行给出：**价格 · 元素（中文）· 伤害 · 卖家在线情况 · 信用等级**，在线优先排序。
可用 `xh 武器名 辐射 50` 按元素与伤害下限筛选。

> 部分武器（近战 Tenet 与全部终幕）warframe.market 没有挂单类目，
> 插件会直接说明「市场查不到，只能游戏内交易」，不会报错。

### 4. 奸商预测（`奸商 预测`）
基于 wiki 记录的 **314 次到访 / 466 件商品历史**做统计推测：
按「已静默次数 ÷ 它自己的平均上架间隔」排序，越接近 1 越「该回来了」。
**不是官方预测**（DE 从不公布下期库存），卡片上写明口径与样本量。

### 5. 遗物与部件（`遗物`）
遗物名全中文（`古纪 A12`）；部件反查会标注每把遗物的**当前可获取状态**
（可掉落 / 仅阿耶兑换 / 已入库）并给出**推荐刷取点**与特殊渠道。

## 三、安装与依赖

```bash
# 1) 放进 AstrBot 插件目录
data/plugins/astrbot_plugin_warframe/

# 2) 装依赖
pip install -r requirements.txt      # httpx、pillow

# 3) 下载渲染字体（推荐，一条命令）
python scripts/fetch_font.py
```

字库不随仓库分发（40 MB，超过插件市场 16 MB 上限）；不跑脚本也能用 ——
会回落到系统字体（Windows msyh / macOS PingFang / Linux Noto），
但 Linux 服务器上一个中文字体都没有时会渲染成方块。

## 四、配置（WebUI 插件配置页）

| 配置项 | 说明 |
| --- | --- |
| `worldsource` | 世界状态数据源：`de`=DE 官方直连（默认）/ `warframestat`=社区镜像 |
| `default_platform` | 默认平台 `pc` / `ps` / `xb` / `sw` |
| `render_mode` | `image`（默认图片卡）/ `auto` / `text` |
| `vision_provider_id` | 识卡用的多模态模型 id（留空自动挑选） |
| `scan_cooldown` | 识卡冷却秒数（默认 15，设 0 关闭） |
| `scan_max_concurrent` | 识卡并发上限（默认 3，设 0 不限） |
| `flaresolverr_enabled` / `flaresolverr_urls` | CF 绕过代理开关与地址（自填） |
| `kb_enabled` / `kb_docs_dir` / `kb_id` | 知识库引导开关与文档目录 |
| `push_interval` | 蹲推送轮询间隔（秒，最小 15） |
| `proxy` / `http_timeout` | 出站代理与超时 |

### 可选：FlareSolverr
`wiki.warframe.com` 对非浏览器请求返回 403，取效价轮换与紫卡倾向快照需要它：

```bash
docker run -d --name flaresolverr -p 8191:8191 flaresolverr/flaresolverr
```
然后把地址填进 `flaresolverr_urls`。**插件跑在容器里时 127.0.0.1 通常到不了
宿主机端口**，要填容器名或 docker0 网关（常见 `http://172.17.0.1:8191`）。

## 五、数据来源

| 数据 | 来源 |
| --- | --- |
| 掉落表 | WFCD `warframe-drop-data`（MIT） |
| 武器/MOD 面板 | WFCD `warframe-items` + DE 官方导出 + wfsim 校准 |
| 中文译名 | DE 官方 Public Export（经 `warframe-public-export-plus` 整理） |
| 世界状态 | DE 官方 `api.warframe.com` |
| 市场价 / 玄骸拍卖 | `api.warframe.market`（遵守 3 请求/秒限速） |
| 赏金轮换 / 钢铁侵袭 | `oracle.browse.wf` / `browse.wf` |
| 仲裁排期 | `arbi.wf.wiki` |
| 奸商历史库存 | `wiki.warframe.com` · Module:Baro/data |
| 字体 | Noto Sans CJK（SIL OFL） |

> Warframe 与相关商标归 Digital Extremes 所有。本插件是粉丝作品，与 DE 无关。

## 六、维护

```bash
# 刷新轮换数据（玄骸效价，wiki 每 4 天换批）
python scripts/fetch_valence.py
# 刷新奸商历史（需经 FlareSolverr 抓 wiki，命令见脚本末尾注释）
python scripts/build_baro_history.py --raw <抓下来的源码>
# 重建玄骸武器表（武器库更新后）
python scripts/build_lich_names.py
# 本地预览卡片（改版式必用）
python scripts/dev_relic_preview.py --which 出库
# 排障：直接看某条指令的真实返回
python scripts/diag_command_output.py "遗物 出库"

# 测试（31 个文件，全离线）
python tests/test_help_coverage.py
python tests/audit_damage_calc.py     # 伤害引擎不变量审计
```

## 七、版本

**v1.0**（2026-09-18）：版本号重置为 1.0；品牌统一为「Warframe 查询助手」；
玄骸查询重做（三类武器 + 元素/在线/信用 + 筛选分支）；
奸商新增下期预测；修复失效指令 `.锚点` 并做了一轮安全加固。
