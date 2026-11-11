# AstrBot Plugin · Warframe SDJKBOT (`astrbot_plugin_warframe_sdjkbot`)

[简体中文](https://github.com/skyti1437/Warframe-SDJK-Bot/blob/main/README.md) | **English** | [Changelog](CHANGELOG.md)

Look up Warframe from your group chat on QQ, Telegram, Discord, Slack, KOOK and other platforms supported by AstrBot: **world state, market prices, relics and parts, damage calculation, build screenshot reading, riven analysis, Kuva / Tenet / Coda weapon auctions, Baro Ki'Teer predictions and event subscriptions**. Replies are rendered as Orokin-style image cards, and the plugin ships with an admin panel inside the AstrBot dashboard.

Replies can be in **Simplified Chinese or English** (international servers only). English mode uses DE's official English names for nodes, missions, items, weapons and factions wherever the plugin has them.

<p align="center">
  <img src="https://cdn.jsdelivr.net/gh/skyti1437/Warframe-SDJK-Bot@main/assets/readme/en-arbitration.png" width="49%" alt="Current Arbitration card in English" />
  <img src="https://cdn.jsdelivr.net/gh/skyti1437/Warframe-SDJK-Bot@main/assets/readme/en-help.png" width="49%" alt="English command list card" />
</p>

> The English cards above show live data from a v1.3.1 release candidate. The Arbitration example used Beijing time. See [Configuration](#configuration) to choose your display time zone.

Commands take **free-form arguments**: `command + content + modifiers`, in any order, separated by spaces. There are **57 main commands**; send `wfhelp` for the full list.

Version history: [CHANGELOG.md](https://github.com/skyti1437/Warframe-SDJK-Bot/blob/main/CHANGELOG.md) (written in Chinese).

---

## Install

1. Have AstrBot (4.x) and a platform adapter running (NapCat / aiocqhttp, QQ official bot, Telegram, Discord, Slack, KOOK, …).
2. Search for **Warframe SDJKBOT** in the AstrBot plugin market and install it. Or place this repository in `data/plugins/astrbot_plugin_warframe_sdjkbot/` and run `pip install -r requirements.txt` (only `httpx` and `pillow`).
3. Reload the plugin and send `wfhelp`.

Image cards work out of the box: the plugin bundles a Chinese font subset (the 6,763 GB2312 characters plus common symbols). For rare characters, open the admin panel → **Appearance** and download the full font (about 40 MB), or import any font you like. Fonts are stored in the plugin data directory and survive plugin updates.

The admin panel needs AstrBot ≥ 4.24.1 and is visible only to dashboard admins.

## Quick start

To get English replies, either add `-en` to a message, or have a group admin run `.language en` once for the chat, or set `default_language` to `en` in the plugin settings (see [Display language](#display-language)).

| Send | You get |
| --- | --- |
| `wfhelp` | The full command list |
| `fissures` · `fissures steel survival` | Void Fissures; filter by Steel Path, mission type or era |
| `arbitration` · `arbitration efficient` | Current and upcoming Arbitrations with community ratings |
| `baro` | Baro Ki'Teer's location and stock |
| `relic Axi A2` · `parts Rubico Prime Barrel` | Relic rewards / where a part drops |
| `wm soma prime set` | warframe.market sell orders |
| `wr glaxion Critical Chance` | Riven auctions for a weapon |
| `damage Soma vs Heavy Gunner 100` | Panel / per-hit / DPS breakdown |
| `subscribe fissure steel axi survival` | A push alert when a matching fissure appears |
| `裂隙 -en` | Any Chinese command, answered in English |

English filter words for `fissures` (`steel`, `survival`, `axi` …) are recognized when the reply is in English; in a Chinese chat use `fissures steel survival -en` or the Chinese words. English words for `arbitration`, `wm` / `wr` and `subscribe` work in either language.

## Common commands

English command words are matched only as **the first word** of a message (so `what is the news` is not a command). Chinese commands keep their usual behaviour. Each line lists the English words; the Chinese word works too.

| Group | English commands | What it does |
| --- | --- | --- |
| Cycles and daily | `timers` `cetus` / `cycle` / `cycles` `bounty` / `bounties` `fissure` / `fissures` `sortie` `archon` / `archonhunt` | Open-world cycles, bounties and rewards, fissure list |
| Missions and rotations | `arbitration` / `arbi` `arbtable` `deeparchimedea` `temporalarchimedea` `descendia` `incursions` `voidstorms` | Arbitration (current and next 14 days), weekly rotations |
| Shops | `baro` / `voidtrader` `darvo` / `deals` `acrithis` `steelpath` `slivers` / `palladino` `varzia` / `primevault` `clanrewards` `flashsales` | Baro Ki'Teer's stock and arrival prediction, Prime Resurgence (Regal Aya / Aya), weekly offers |
| Relics and parts | `relic` / `relics` `parts` `openrelic` `ducats` | Rewards, sources and availability; part lookup; Ducats per Platinum |
| Market and rivens | `wm` `wr` `rm` `trend` `rank` / `ranking` `rivenanalysis` `rivenranges` `splice` `dispo` / `disposition` | warframe.market orders and auctions, price trends, riven stat ranges and grading |
| Weapon auctions | `xh <weapon> [element] [value]` | Kuva / Tenet / Coda weapons (47), filtered by element and value, online sellers first |
| Damage and builds | `damage` / `dmg` `scan` `scandamage` `valence` | Damage calculator; read a build screenshot and recalculate |
| Info | `news` `alerts` `invasions` `events` `nightwave` `calendar` `synthesis` `kuva` `incarnon` `tenet` `coda` `conclave` `construction` `wiki <term>` `translate <term>` | News, limited-time content, weekly weapons, wiki link, Chinese ↔ English names |
| Subscriptions | `subscribe` / `sub` | Push alerts (see [Subscriptions](#subscriptions)) |

`wm` / `wr` accept English modifiers such as `buy`, `max`, `radiant`, `offline`, `madurai` and `neg` (negative riven stat), and English item and part names. Stats can be written in full, abbreviated or run together; `wr glaxion Critical Chance` and `wr glaxion CC` are the same query.

Modifiers work with any command: `-2` / `-3` next page, `-w` plain text, `-t` force an image, `-r` copyable in-game whisper text (market listings), `-pc` / `-ps` / `-xb` / `-sw` platform (all four now share data and show as International).

Group admin commands start with a dot: `.platform`, `.enable push` / `.disable push`, `.status`, `.language`, `.menu`. The Chinese forms (`.默认平台`, `.开启 推送`, `.语言`, `.菜单` …) also work.

## Display language

The reply language is chosen in this order:

1. **Per message**: `-en` or `-cn` at the end of a message (for example `fissures -cn`, `裂隙 -en`).
2. **Per chat**: set by `.language en` / `.language cn`; `.language default` goes back to the global setting. `.language` alone shows the current language. The Chinese forms are `.语言 en` / `.语言 cn` / `.语言 默认`.
3. **Global**: `default_language` in the plugin settings (`cn` by default, or `en`).

Setting the chat language requires a group admin/owner, an AstrBot admin, or a user listed in `super_admins`. This also applies in private chats; anyone can use `-en` / `-cn` on a single message.

The language you type in and the reply language are independent: English command words do not switch the reply to English, and Chinese commands are answered in English when the chat is set to English.

What stays in Chinese in English mode (v1.3.1):

- Part of the wiki card text (descriptions, traits, build materials), some relic source notes, part names and farming hints. For example, a relic card can still show the source line for Baro Ki'Teer in Chinese.

## Subscriptions

`subscribe <event> [filters] [duration]` (Chinese: `蹲`). A group admin must first run `.enable push` in that group.

```text
subscribe fissure steel axi survival     # Steel Path Axi Survival fissures, one alert
subscribe baro forever                    # every Baro Ki'Teer visit
subscribe help                            # events and filter syntax
subscribe cancel                          # cancel your own subscriptions (admins: the whole group)
```

- Events include `fissure`, `arbitration` / `arbi`, `baro`, `alert`, `invasion`, `incursion`, `darvo`, `nightwave`, `bounty`, `varzia`, `cetus`, `orbvallis`, `deimos` and more; `subscribe help` lists them.
- Fissure filters: words separated by spaces must all match (`steel axi` = Steel Path Axi fissures), and commas mean "any of" (`steel,axi`). Era words (`lith` … `omnia`, or `T1`–`T6`) imply a fissure subscription.
- Without a duration a subscription fires once; add `forever`, or a number of hits / a time window as described by `subscribe help`.
- **Each subscription remembers the display language it was created in**, and its alerts are sent in that language. Subscriptions created before v1.3.1 have no language field and are treated as Chinese.

## Platforms

Buttons and menus depend on what each platform and AstrBot adapter supports.

- **Command menus (v1.3.1)**: when AstrBot supports command registration, the plugin registers 19 English commands (`/fissure`, `/sortie`, `/baro`, `/arbi`, `/wm`, `/wr`, `/relic`, `/damage`, `/subscribe`, `/wiki`, `/wfhelp`, …) and AstrBot syncs them to the **Telegram** command menu and **Discord** slash commands. Each command is answered once. For the **QQ official bot**, send `.menu` (`.菜单`) to get a list to enter by hand in the QQ open platform console (Advanced settings, type "command"); tapping an entry fills it into the input box, and in groups the bot must be mentioned.
- **QQ official bot**: buttons and "image + buttons in one message" are Markdown messages; old QQ clients without Markdown support may not show them. Turn off `qq_merge_image_actions` (send images separately) and `qq_markdown_cards` (plain-text cards) if needed. Buttons and Markdown are enabled only for the `qq_official` (WebSocket) adapter; `qq_official_webhook` gets text and images. In groups, buttons fill the input box for the user to send; in private chats, action buttons send directly.
- **Telegram**: to send plain-word commands (Chinese or English) in a group, turn off privacy mode with BotFather `/setprivacy`, or make the bot a group admin. With privacy mode on, the bot only receives some messages, such as commands explicitly addressed to it (`/fissure@your_bot`); see the [Telegram bot FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get). Buttons can run actions, and copy natively when the SDK supports it.
- **Discord**: the AstrBot Discord adapter requests the Message Content and Server Members privileged intents; enable both on the Bot page of the Discord developer portal, or the gateway refuses the connection. The 19 menu commands appear as slash commands; other commands are sent as messages with AstrBot's wake prefix.
- **Slack**: text cards by default (image upload in the AstrBot Slack adapter is not yet reliable: a failed upload drops the whole message); add `-t` to force an image, which needs the `files:write` scope. Buttons need Socket mode (Webhook mode is text only). The app needs at least `chat:write`, and the Socket mode app-level token needs `connections:write`. Slack treats messages starting with `/` as slash commands, so in channels use "@bot command"; in private messages send the command directly.
- **KOOK**: action buttons are supported.
- **Lark / Feishu and other platforms without button callbacks**: text and image output. NapCat keeps plain commands and the `-r` whisper text.
- In-game whisper buttons only copy text for pasting into the game.

The build screenshot reader (`scan`) chooses between on-demand details and two pages based on the platform's button support; `scan_detail_mode` can force a mode. Each scan record belongs to the chat and the original sender and expires after 30 minutes.

## Screenshot reading (optional)

`scan` (build screenshots) and riven screenshots need a vision model channel that you add in AstrBot; the plugin itself has no built-in model and holds no API keys. Riven and build reading can use separate channels (`vision_provider_id_riven`, `vision_provider_id_scan`, …); an explicitly selected channel is used alone and the read stops if it is unavailable.

Rivens can be read in three ways: **local OCR** (on the plugin machine, off by default, engine and models downloaded or provided by the admin), **single model** (`riven_single_ocr`) or **dual model** (main read plus a line-by-line stat read). English client screenshots are supported: local OCR detects Chinese or English card text, and vision models use English prompts in English chats. Only fully accepted reads are priced; the plugin checks the result, but a model returning text does not guarantee every value was read correctly. Details and trade-offs are in the Chinese page sections on vision models and local OCR.

## Configuration

Edit settings on the plugin config page in AstrBot, or on the admin panel **Settings** page. Common ones:

| Key | Meaning |
| --- | --- |
| `default_language` | Display language when the chat has none: `cn` (default) / `en` |
| `display_timezone` | Time zone for replies, cards and the admin panel. Defaults to the AstrBot runtime's system time zone. Fixed offsets such as `UTC+08:00` and `UTC+05:30` are available. Subscription time windows use Beijing time. |
| `worldsource` | World state source: `de` = DE official (default) / `warframestat` = community mirror |
| `default_platform` | `pc` / `ps` / `xb` / `sw` (groups can override with `.platform`) |
| `render_mode` | `image` (default) / `auto` (images for long replies only) / `text` |
| `super_admins` | Sender IDs allowed to use admin commands anywhere |
| `scan_detail_mode` | `auto` / `lazy` / `eager` |
| `flaresolverr_enabled` / `flaresolverr_urls` | Optional FlareSolverr fallback for wiki-based data |
| `community_data_url` | Community data URL; empty uses the `bot-data` branch of this repository |
| `push_interval` / `proxy` / `http_timeout` | Push polling interval and network settings |

**Keeping data fresh**: live data (world state, market, Arbitration schedule) is queried directly. Tenet / Coda valence values and the weekly Acrithis offers are fetched in the background on first start; the plugin first tries the wiki over plain HTTP, then FlareSolverr if enabled, then the community snapshot on the [`bot-data` branch](https://github.com/skyti1437/Warframe-SDJK-Bot/tree/bot-data). Cards say when a value has not been fetched yet. When the plugin runs in a container, point `flaresolverr_urls` at the container name (for example `http://flaresolverr:8191`) or the Docker gateway; `127.0.0.1` usually does not reach the host.

**Market quota**: every warframe.market request made by the plugin shares one budget: under 3 requests per second and at most 3 at a time for normal endpoints; auction search is stricter. Busy requests wait up to about 15 seconds and then report "busy" (which does not mean there are no listings); 429 / `Retry-After` pauses requests. This is the plugin's own conservative pacing based on the [warframe.market API rules](https://docs.warframe.market/docs/rules/overview/), not an official guarantee; the plugin is not an official or endorsed client.

## Data sources

| Data | Source | Notes |
| --- | --- | --- |
| World state | DE official `api.warframe.com` | Live |
| Drop tables | [WFCD/warframe-drop-data](https://github.com/WFCD/warframe-drop-data) + DE official drop page | MIT |
| Items / weapon base stats | [WFCD/warframe-items](https://github.com/WFCD/warframe-items) (MIT) + DE Public Export | Originally from DE |
| Mod ranks and values | DE Public Export (`ExportUpgrades`) | Originally from DE |
| Weapon attacks, Incarnon forms, CO classes, Archgun stats | [wiki.warframe.com](https://wiki.warframe.com) weapon data modules and pages | CC BY-NC-SA 3.0 |
| Mod interactions | English wiki `Module:Mods/data` + DE Public Export + [warframe-public-export-plus](https://github.com/calamity-inc/warframe-public-export-plus) | CC BY-NC-SA 3.0 / MIT |
| Enemy stats | English wiki `Module:Enemies/data` + warframe-public-export-plus | CC BY-NC-SA 3.0 / MIT |
| English and Chinese names | DE Public Export (via warframe-public-export-plus) | Originally from DE |
| Riven stat pools | DE Public Export + warframe-public-export-plus + the project's own warframe.market listing samples | MIT / originally from DE |
| Market / auctions | warframe.market | Shared quota, see above |
| Bounty rotations / Steel Path Incursions | [browse.wf](https://browse.wf) | calamity-inc open source project |
| Arbitration schedule and ratings | [arbi.wf.wiki](https://arbi.wf.wiki) | Community site, unofficial |
| Element bonuses, Baro history, wiki card text (excerpted, translated, rearranged), old riven dispositions | [wiki.warframe.com](https://wiki.warframe.com) | [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/); applies to this data only |
| Font | Noto Sans CJK SC subset (© Adobe) | SIL OFL 1.1, see `core/data/fonts/OFL.txt` |

The damage calculator follows the wiki formulas and is cross-checked against [wfsim](https://github.com/magenie33/wfsim) (no code copied). **Results are theoretical estimates and have not been verified at scale in real gameplay**; always trust the in-game numbers. The Baro Ki'Teer prediction is a statistical guess from wiki visit history, **not an official forecast**. The bounty camp ("tent") calculation follows the public algorithm of [calamity-inc/wf.browse.oracle](https://github.com/calamity-inc/wf.browse.oracle), reimplemented in Python without copying its files.

## Privacy

- The plugin runs only on your own bot and reports nothing to third parties; queries go directly to the public data sources above.
- Runtime data (group settings, subscriptions, appearance, render cache) is written to `data/plugin_data/astrbot_plugin_warframe_sdjkbot/` and never enters the repository.
- With model-based reading, screenshots you send are passed to the model provider configured for that task. With local OCR enabled, riven reading stays on the plugin machine and never falls back to a model.
- Images in chat are fetched with size, timeout and redirect limits; QQ headers are sent only to QQ hosts.
- Failed build reads are **not saved** unless `scan_debug_save` is on (at most 3 images in `scan_debug/`).
- Only dashboard admins can open the admin panel; chat and subscriber IDs are masked by default.

## AI notice

The plugin contains no model and holds no API keys. Screenshot reading uses the multimodal models you configure in AstrBot; Q&A-style questions go through AstrBot's own LLM and knowledge base, not this plugin. Most of the code was written with AI assistance: the author (Sky) provided requirements, screenshots and test feedback, and AI wrote code, checked data and wrote tests and documentation.

## License and fan work notice

- **Code**: GPL-3.0 (see `LICENSE`).
- **Wiki-derived data** listed above: CC BY-NC-SA 3.0. Third-party MIT notices for WFCD/warframe-items and calamity-inc/warframe-public-export-plus are reproduced below.
- **Plugin icon**: taken from the official Warframe Fan Kit (`LotusEmblemIcon`), © Digital Extremes, used in this non-commercial fan work; this does not imply affiliation with or endorsement by DE.
- **Unofficial**: Warframe and related trademarks, game icons, names and translations belong to Digital Extremes. This plugin is a fan work and is not affiliated with Digital Extremes.

### Third-party notices (MIT)

- [WFCD/warframe-items](https://github.com/WFCD/warframe-items): Copyright (c) 2017 Kaptard
- [calamity-inc/warframe-public-export-plus](https://github.com/calamity-inc/warframe-public-export-plus) (pinned commit `513105d`): Copyright (c) 2024-2026 Calamity, Inc.; Copyright (c) 2026 AMelonInsideLemon

```text
MIT License

Copyright (c) 2024-2026 Calamity, Inc.
Copyright (c) 2026 AMelonInsideLemon

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

Development: clone the full repository with `git clone https://github.com/skyti1437/Warframe-SDJK-Bot.git` (market packages and GitHub "Download ZIP" contain runtime files only). Run `python scripts/run_checks.py` before changing formulas or layouts; see the development section of the Chinese page for details.
