# AstrBot 插件 · Warframe SDJKBOT v1.2.7（`astrbot_plugin_warframe_sdjkbot`）

在 QQ 群里查 Warframe 的一切：**世界状态、市场价、遗物与部件、伤害计算、配卡截图识别、紫卡分析、玄骸拍卖、奸商预测、蹲点推送**——
回复渲染成 Orokin 风格的图片卡片，后台还带一个可视化的运维面板。

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/skyti1437/Warframe-SDJK-Bot@main/assets/readme/arbitration.png" width="49%" alt="当前仲裁卡片" />
  <img src="https://cdn.jsdelivr.net/gh/skyti1437/Warframe-SDJK-Bot@main/assets/readme/relic.png" width="49%" alt="遗物卡片" />
</p>

指令是**自由参数式**的：`主指令 + 内容 + 附加项`，顺序随意、空格分隔。比如 `伤害 绝路 镀层3`、`遗物 出库`、`仲裁 生存 高效` 都行。
发送 `帮助` 查看完整指令表——共 **57 个主指令**，分 9 组。

---

## 卡片一览

| 虚空裂隙（按纪元排序、钢铁 / 九重天标记） | 伤害计算（实战 / 爆发 / 持续 DPS、分段明细） |
| :---: | :---: |
| <img src="https://cdn.jsdelivr.net/gh/skyti1437/Warframe-SDJK-Bot@main/assets/readme/fissures.png" alt="虚空裂隙卡片" /> | <img src="https://cdn.jsdelivr.net/gh/skyti1437/Warframe-SDJK-Bot@main/assets/readme/damage.png" alt="伤害计算卡片" /> |
| **Prime 出库清单（出库遗物按杜卡德金 / 银 / 铜着色）** | **指令一览（`帮助`）** |
| <img src="https://cdn.jsdelivr.net/gh/skyti1437/Warframe-SDJK-Bot@main/assets/readme/prime-vault.png" alt="Prime 出库卡片" /> | <img src="https://cdn.jsdelivr.net/gh/skyti1437/Warframe-SDJK-Bot@main/assets/readme/help.png" alt="指令一览卡片" /> |

> 截图均由插件的真实渲染器用实时数据生成，配色是默认的「Orokin」主题，可以在运维面板里换。

## 功能

| 分组 | 代表指令 | 能做什么 |
| --- | --- | --- |
| 周期与日常 | `夜灵` `赏金` `裂隙` `突击` `执刑官` `时效` | 各开放世界昼夜、赏金与奖励、裂隙列表（可按钢铁 / 类型 / 纪元筛） |
| 任务与轮换 | `仲裁` `仲裁表` `深层科研` `沉沦之地` `侵袭` `九重天` | 当前与未来 14 天仲裁（带社区实测评级）、每周轮换 |
| 商店 | `奸商` `奸商 预测` `每日特惠` `言录使` `钢铁` `出库` | 虚空商人当期货单（按武器 / MOD / 外观等分组）与历史统计预测、Prime 出库与阿耶精华兑换 |
| 遗物与部件 | `遗物 <名>` `遗物 <部件>` `开核桃` `金垃圾` | 奖励、出处与当前可获取状态，部件反查推荐刷取点，杜卡德 / 白金性价比 |
| 市场与紫卡 | `wm` `wr` `rm` `趋势` `排行` `紫卡分析` `倾向` | warframe.market 在售 / 收购、紫卡拍卖、价格趋势、紫卡词条区间与评分 |
| 玄骸拍卖 | `xh <武器> [元素] [数值]` | 赤毒 / 信条 / 终幕 47 把武器，元素与数值筛选、卖家在线优先 |
| 伤害与配卡 | `伤害 <武器> <MOD…>` `识卡` `识卡伤害` | 按 wiki 口径算 DPS；发配卡截图自动识别并复算 |
| 推送 | `蹲 <事件> [条件]` | 裂隙、仲裁、警报、夜灵等事件出现时在群里提醒 |

几个值得单独说的：

- **玄骸拍卖**：中文名支持 `赤毒怒雷` / `赤毒·布拉玛` / `布拉玛` 等写法。warframe.market 没有挂单类目的武器（近战信条、全部终幕）会明确说明「只能游戏内交易」，不会报错。
- **紫卡拍卖（`wr`）**：词条可以写全称、简称或连写，`wr 冰凇 基础 多重 异常伤害 负i伤`、`wr 冰凇 基多异 负i` 查到的是同一组；英文写法不分大小写、全称不用加引号（`wr glaxion Critical Chance`、`CC` / `CD` / `MS`）。卡片顶部回显实际用了哪些词条。指定 `3+1`（或三正一负）时只给完全匹配的挂单，没有就直说；其他查询在没有完全匹配时给最接近的选项。
- **奸商预测**：基于 wiki 记录的 300 多次到访、460 多件商品做统计推测——按「已缺席次数 ÷ 它自己的平均上架间隔」排序。**这不是官方预测**（DE 从不公布下期库存），卡片上写明了口径和样本量。
- **伤害计算 / 识卡伤害**：按 wiki 公式计算，数据取自 DE 官方导出、warframe-public-export-plus 与英文 wiki，并以开源模拟器 [wfsim](https://github.com/magenie33/wfsim) 交叉核对。**这是理论推算，没有经过大规模实战检验**，可能和游戏面板或实战有出入，请以游戏内为准；卡片上也有同样的提醒。发现对不上的欢迎提 issue。
- **仲裁评级**：官方 tierlist 优先，没评级的节点用社区实测生息中位数补；样本少于 5 条的标 `*`。实测值来自高水平队伍，普通队伍未必打得到，卡片上也写了。

## 运维面板

在 AstrBot 后台的插件详情页打开「SDJKBOT 运维面板」（需要 AstrBot ≥ 4.24.1；只有登录后台的管理员能看到）。

| 概览 | 外观（配色、字体、实时预览） |
| :---: | :---: |
| <img src="https://cdn.jsdelivr.net/gh/skyti1437/Warframe-SDJK-Bot@main/assets/readme/panel-overview.png" alt="运维面板概览" /> | <img src="https://cdn.jsdelivr.net/gh/skyti1437/Warframe-SDJK-Bot@main/assets/readme/panel-appearance.png" alt="运维面板外观页" /> |

- **概览**：推送守护、订阅、数据、依赖、字体一眼看完，「需要处理」列出过期数据和该做的事，点一下就跳过去。
- **数据**：每份数据快照的时间和状态；一键从社区快照拉新数据。
- **推送与订阅**：按群查看「蹲」订阅（号码默认打码），可删订阅、开关某个群的推送。
- **设置**：插件全部配置项，每项标注「即时生效」还是「需要重载」。
- **外观**：
  - 卡片配色有 8 个基础色和 12 个类别色、4 套预设，每项都写明改的是卡片哪部分；
  - 「定位」按钮会把这一项在预览里临时染成品红，一眼看出它管哪里；
  - 支持背景图（可调压暗程度和面板透明度）和角标；
  - 字体可以从链接导入或上传，正文和粗体分别选；
  - 预览用的是真实出图程序，保存后新卡片立即生效。
- **依赖与资源 / 连通性**：一键安装依赖（走 AstrBot 自己的 pip 设置）；测试 FlareSolverr 和识卡模型渠道是否可用。

## 安装

1. 准备好 AstrBot（≥ 4.x）和消息平台适配器（NapCat / aiocqhttp 等）。
2. 在 AstrBot 插件市场搜索 **Warframe SDJKBOT** 安装；或者把本仓库放到 `data/plugins/astrbot_plugin_warframe_sdjkbot/`，再执行 `pip install -r requirements.txt`（只有 httpx 和 pillow 两个依赖）。
3. 重载插件，在群里发 `帮助` 试试。

**不需要额外操作就能出图**：插件自带常用 6763 字的中文字体。想让生僻字也能显示，到运维面板「外观」页点「下载完整字库」（约 40 MB），或导入任意你喜欢的中文字体。字体存在插件数据目录，插件更新不会丢。

## 配置

在 AstrBot 后台的插件配置页（或运维面板「设置」页）修改。常用的几项：

| 配置项 | 说明 |
| --- | --- |
| `worldsource` | 世界状态数据源：`de` = DE 官方直连（默认）/ `warframestat` = 社区镜像 |
| `default_platform` | 默认平台 `pc` / `ps` / `xb` / `sw`（群里可用 `.默认平台` 覆盖） |
| `render_mode` | `image`（默认图片卡片）/ `auto`（长内容才出图）/ `text` |
| `vision_provider_id` | 识卡用的多模态模型（留空自动挑选） |
| `flaresolverr_enabled` / `flaresolverr_urls` | FlareSolverr 开关与地址（可选，见下） |
| `community_data_url` | 社区数据地址，留空用本项目公开仓的 bot-data（见下） |
| `push_interval` / `proxy` / `http_timeout` | 推送轮询间隔与网络参数 |

### 数据怎么保持新鲜

- **实时数据**直接查官方或社区接口：世界状态、市场价、仲裁排期等。
- **wiki 上的数据**（元素加成、言录使货单、奸商到访历史）在 Cloudflare 后面，普通请求拿不到。插件有两条路：
  - **社区快照（默认，零配置）**：本项目在一台装了 FlareSolverr 的服务器上定时抓取，发布到本仓库的 [`bot-data` 分支](https://github.com/skyti1437/Warframe-SDJK-Bot/tree/bot-data)。插件每 30 分钟到 3 小时检查一次，**内容校验通过而且比本地新**才采用。仲裁评级也从这里更新。
  - **自建 FlareSolverr（可选）**：自己直连 wiki。
    ```bash
    docker run -d --name flaresolverr -p 8191:8191 flaresolverr/flaresolverr
    ```
    然后把地址填进 `flaresolverr_urls`。插件跑在容器里时 `127.0.0.1` 通常连不到宿主机，要填容器名（`http://flaresolverr:8191`）或 Docker 网关（常见 `http://172.17.0.1:8191`）。
- 不想用本项目的数据，可以在 `community_data_url` 填自己的地址：在那个地址下放同名文件（`valence.json`、`arb_ratings.json` 等），格式与 bot-data 分支一致。
- 其余随包数据（武器 / MOD 数值、掉落表、中文译名等）随插件版本更新。

## AI 相关说明

**插件本身不内置任何模型，也不代持任何 API Key。**

| 场景 | 用 AI 做什么 | 由谁提供 |
| --- | --- | --- |
| 识卡 / 紫卡截图识别 | 把配卡截图读成结构化数据（MOD、等级、面板数值） | 你在 AstrBot 里配置的多模态模型 |
| 推荐 / 攻略类问答 | 走 AstrBot 平台的 LLM + 知识库 | AstrBot 平台，插件不参与 |

识卡必须自己配一个多模态渠道；没配会直接提示，不会静默失败。插件内部会核对识别结果和游戏面板。在配置里**指定了** `vision_provider_id` 时，截图只发给这一个渠道；留空时才在你已添加的视觉渠道里自动挑选，并会同时交给其中几个渠道比对结果。

**开发过程**：这个项目的绝大部分代码是在 AI 辅助下完成的。由作者（Sky）提需求、提供截图和实测反馈，AI 负责写代码、核对数据、写测试和文档。所以代码里有大量「为什么这么写」的注释和防回归测试，用来弥补 AI 改动容易引入静默回归的问题。

## 隐私

- 插件只在你自己的机器人上运行，不向任何第三方上报数据，所有查询都直接发给下表的公开数据源。
- 运行数据（群配置、订阅、外观设置、渲染缓存）写在 `data/plugin_data/astrbot_plugin_warframe_sdjkbot/`，不会进仓库。
- 识卡会把**你发的截图**交给你自己配置的模型服务商。介意的话不用这个功能，或在 `vision_provider_id` 里**指定**本地 / 自建的视觉模型（指定后只发给它一个；留空自动挑选时可能同时发给多个已添加的视觉渠道）。
- 识卡对齐失败时**默认不留存截图**；只有在配置里开启 `scan_debug_save` 才会把截图存到运行目录的 `scan_debug/`（最多 3 张，可直接删除）。
- 运维面板只有登录 AstrBot 后台的管理员能打开；订阅里的群号、QQ 号默认打码。

## 数据来源与许可

| 数据 | 来源 | 说明 |
| --- | --- | --- |
| 世界状态 | DE 官方 `api.warframe.com` | 实时接口 |
| 掉落表 | [WFCD/warframe-drop-data](https://github.com/WFCD/warframe-drop-data) + DE 官方掉落页 | MIT |
| 物品 / 武器基础面板 | [WFCD/warframe-items](https://github.com/WFCD/warframe-items)（MIT）+ DE 官方 Public Export | 数据源头为 DE |
| MOD 等级与逐级数值 | DE 官方 Public Export（`ExportUpgrades`） | 数据源头为 DE |
| 武器攻击形态、灵化形态与进化、CO 分类、回响加成、Archgun 面板 | [wiki.warframe.com](https://wiki.warframe.com) 的武器数据模块、各武器灵化页与「Condition Overload (Mechanic)」攻击目录 | CC BY-NC-SA 3.0；各文件 `_meta` 写明 wiki 快照 |
| MOD 互斥 / 条件叠层 / 不兼容武器 | 英文 wiki `Module:Mods/data` + DE 官方 Public Export + [warframe-public-export-plus](https://github.com/calamity-inc/warframe-public-export-plus) | CC BY-NC-SA 3.0 / MIT |
| 常用敌人数值 | 英文 wiki `Module:Enemies/data` + [warframe-public-export-plus](https://github.com/calamity-inc/warframe-public-export-plus)（wiki 未写的护盾 / 护甲） | CC BY-NC-SA 3.0 / MIT |
| 中文译名 | DE 官方 Public Export（经 [warframe-public-export-plus](https://github.com/calamity-inc/warframe-public-export-plus) 整理） | 数据源头为 DE |
| 紫卡词条池（「词条范围」可出词条） | DE 官方 Public Export + [warframe-public-export-plus](https://github.com/calamity-inc/warframe-public-export-plus)（词条正负与限制）+ 自有 warframe.market 挂单抽样 | MIT / 数据源头为 DE |
| 市场价 / 拍卖 | warframe.market | 按接口分别限速：普通查询约 3 次/秒；拍卖搜索更严，只在管理员手动拉取时按约 15 秒一次进行，遇 429 退避 |
| 赏金轮换 / 钢铁侵袭 | [browse.wf](https://browse.wf) | calamity-inc 开源项目 |
| 仲裁排期与社区实测 | [arbi.wf.wiki](https://arbi.wf.wiki) | 社区站点，非官方 |
| 元素加成、奸商历史、资料卡文本（简介 / 效果 / 获取，经摘录、翻译与重排）、紫卡取消前的旧倾向（只用于识别旧截图） | [wiki.warframe.com](https://wiki.warframe.com) | [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/)；仅这些数据适用，代码另按 GPL-3.0 |
| 字体 | Noto Sans CJK SC 子集（© Adobe） | SIL OFL 1.1，全文与子集化说明见 `core/data/fonts/OFL.txt` |

- **算法参考**：赏金「小帐篷」推算参考了 [calamity-inc/wf.browse.oracle](https://github.com/calamity-inc/wf.browse.oracle) 公开的算法（种子与洗牌步骤与其一致）。该仓库没有附带许可证；本插件用 Python 另行实现，没有拷贝其源代码文件。
- **伤害计算**：以 [magenie33/wfsim](https://github.com/magenie33/wfsim)（AGPL-3.0-or-later）为对照基准交叉核对公式，未复制其源代码。早期版本的部分派生数据表取自其整理的数据，**v1.2.7 起已全部改为从上表所列来源重新生成**（生成器见 `scripts/build_*.py`），wfsim 只作交叉核对。
- **插件图标**：按作者记录取自 Warframe 官方 Fan Kit 素材（`LotusEmblemIcon`），版权归 Digital Extremes，用于本非商业粉丝作品，不表示与 DE 有关联或经其认可。
- **代码许可**：GPL-3.0（见 `LICENSE`）。
- **非官方声明**：Warframe 与相关商标、游戏图标、名称与译文归 Digital Extremes 所有。本插件是粉丝作品，与 Digital Extremes 无关。

---

<details>
<summary><b>开发与维护</b></summary>

改任何公式或版式之前，先跑统一检查入口（本地和 CI 是同一条命令）：

```bash
pip install -r requirements.txt -r scripts/requirements-dev.txt
python scripts/run_checks.py          # regression（默认）：ruff + 全部 tests/test_*.py + 伤害审计
python scripts/run_checks.py health   # 随包时效数据是否过期（只读、不联网）
```

- regression 红 = 代码回归；health 红 = 随包时效数据过期，需要刷新数据，不代表算法出错。CI 在 push / PR 上跑 regression（Python 3.10 与 3.12），health 在 Actions 页手动触发。
- 测试按 `tests/test_*.py` 自动发现、每个脚本独立运行；缺能力（例如不带完整字库、没有作者私有的运维脚本）时会明确打印跳过原因，不会静默。
- `scripts/` 下是数据构建、排障和版式开发工具，插件运行不需要它们：
  - 数据构建：`scripts/build_*.py`（从 WFCD / DE 导出 / warframe-public-export-plus / 英文 wiki 快照重新生成 `core/data/`，只用标准库；输入快照路径作参数传入，多数带 `--check` 可复现校验）；
  - 排障：`scripts/diag_command_output.py "遗物 出库"`（在 AstrBot 容器里直接打印指令的真实返回）、`scripts/smoke_all.py`；
  - 版式：`scripts/dev_relic_preview.py`、`scripts/dev_help_preview.py` 等，本地预览卡面。
- 版本历史见 [`CHANGELOG.md`](CHANGELOG.md)（也是插件面板与市场「更新日志」页的数据源）。

</details>
