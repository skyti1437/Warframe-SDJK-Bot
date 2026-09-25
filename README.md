# bot-data · 社区快照（只读）

本分支是 [Warframe SDJKBOT](https://github.com/skyti1437/Warframe-SDJK-Bot) 插件的
**数据快照发布位**，与插件源码（`main` 分支）无关，只放下面几个 JSON：

| 文件 | 内容 | 更新时机 |
|---|---|---|
| `valence.json` | 信条 / 终幕武器的当前**元素加成**（每 4 天轮换） | 快照换轮后 |
| `acrithis_week.json` | 言录使**当期货单**（每周一 00:00 UTC 轮换） | 每周刷新后 |
| `index.json` | 发布时间与各文件的时间戳 | 同上 |

## 为什么有这个东西

这两项数据 DE 官方接口不下发（实测 worldState 无对应字段），只能从
[wiki.warframe.com](https://wiki.warframe.com) 取；而 wiki 在 Cloudflare 盾后，
普通 HTTP 客户端会被挑战，需要自建 FlareSolverr 才能抓。为了让**没有部署
FlareSolverr** 的插件用户也能拿到较新的数据，这里把「已经抓到的新鲜快照」发布成
可直接 GET 的 JSON（`raw.githubusercontent.com` 免盾）。

## 数据来源与许可

- 数据来自 **wiki.warframe.com**（CC BY-NC-SA 3.0），由插件在部署了 FlareSolverr
  的机器上抓取后发布；文件内的 `source` 字段记录了具体页面与快照时间。
- Warframe 及相关内容版权归 **Digital Extremes** 所有，本仓库仅供查询用途。
- 本分支只读，且**只发布校验通过的数据**（覆盖不全 / 已过期 / 更旧的一律不发）。
