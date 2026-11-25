# Changelog

[简体中文](CHANGELOG.md) | **English**

This file records major version updates for the AstrBot plugin **Warframe SDJKBOT**, newest version first.

> Note: The AstrBot dashboard and plugin marketplace read `CHANGELOG.md` (Simplified Chinese) by default. Both changelogs are kept in sync for every release.

## v1.3.6

**Pure-Rust Standalone Damage Engine & 3-Platform Support (Version Bump Note)**

- Upgraded the full-simulation damage engine to a pure-Rust standalone binary package (`portable-rust-535ed49f5a51149d`) supporting **Linux x86_64, Linux arm64, and macOS arm64**, completely removing the host Python 3.12+ requirement (Python 3.10+ environments can now download and run it directly). Because the plugin adds standalone binary package verification, 3-platform auto-download, and legacy upgrade handling (and the plugin marketplace publishes updates on version changes), this release bumps to `v1.3.6`.
- Significantly improved cold-start time, build search, Riven analysis, trait matrix generation, and damage recalculation speed while reducing resident memory usage; scoring formulas and bundled reference tables remain fully identical, so existing Riven and build caches require no rebuild.
- **In-place legacy upgrade**: Admins running the previous Python standalone engine can click **Upgrade to pure-Rust engine** in the admin panel under **Damage engine** to swap in place (checksum and startup probes run automatically before switching, preserving the previous install if anything fails). Unupgraded legacy installs continue to work normally.

## v1.3.5

**World State, Community Source Parity & DE Multi-Mirror Failover**

- Brought the community source (`warframestat.us`) into full display parity with the official DE worldstate feed: added Plains of Eidolon camp bounties A/B/C, Orb Vallis 8-tier bounties and Cambion Drift Isolation Vault labels, all 12 Conclave challenge names, full 1999 Calendar mission/override/birthday localization, and unified news ordering, Void Storm ordering, Sortie node levels, Invasion construction progress, and card footer source tags.
- Added `oracle.browse.wf` calibration for the Zariman cycle on the community source, restoring both faction display and accurate countdowns (official DE worldstate supplies these directly without extra requests).
- **Official DE worldstate URLs** now accept multiple comma- or newline-separated mirror URLs with ordered failover: if one mirror fails, times out, hits rate limits, or serves stale data, the plugin automatically tries the next mirror, falling back to the community source only when all mirrors fail; the admin panel now supports multi-URL editing and per-mirror probing.

**Query & Calculation Improvements**

- Improved long-magazine reload cycle accuracy and forma/polarity slot solver precision, significantly accelerated trait/weapon lookup and card background rendering, and reduced resident memory usage.
- Added concurrency queueing and timeout budgets for build scanning and build recommendation; added chunked transfer and bilingual prompts for local OCR.

**Bug Fixes**

- **World state & cycles**: Fixed silent fallback to untranslated single endpoints when the community bundle failed; fixed inaccurate Zariman faction/countdown on the community source; fixed Earth, Orb Vallis, and Duviri cycle drift against official state; fixed 1999 Calendar override English fallbacks, empty error cards, and Archimedea footer attribution; fixed slow failover when DE hit HTTP 429 or single-section errors.
- **Command parsing & cards**: Fixed false triggers from English Void terms and casual chat; fixed Requiem mod *Oull*/*Eterna* recognition, `-zoom` negative stat lookup in `wr`, and English/short **Omnia Fissure** filters; fixed price trend chart timezone offsets, oversized fissure images, and truncated Archimedea/Clan reward descriptions.
- **Market & relics**: Fixed a single malformed order breaking an entire market card; fixed stale set-part refreshes, bundle buy recommendations, missing-part hints, and edge-case classification in relics/Prime Vault/Varzia.
- **Subscriptions & push**: Fixed ambiguous `subscribe cancel` deletions and missing English cancel candidates; fixed missed, duplicate, or double-decremented pushes during plugin hot-reload, delivery timeouts, or chunked message failures; fixed occasional shutdown/queue stalls and stale cache hits after runtime data refreshes.
- **Build scan, damage & admin panel**: Fixed corrupt or oversized images blocking subsequent scans and concurrent scan cross-talk; fixed long-magazine reload drift, suboptimal polarity slotting, and unvalidated Arcane rank replay in full simulation (with automatic engine recovery); fixed panel overview crashes on corrupt snapshots, leftover cache after switching data sources, background image cropping, and status icon mismatches.

## v1.3.4

**Unified Benchmark Target & Multi-Mode Build / Trait Recommendations**

- Unified the default benchmark across `loadoutrecommend`, `riventraits`, `damage`, and `scandamage` to **Lv 170 Steel Path Thrax Centurion · No Overguard · No Shields · Body · 30 s**, and added the `sp9999` (`百夫长9999`) preset (Lv 9999 Steel Path · Overguard · No Shields · 100% Headshots · Aiming · 180 s). Commands accept overrides such as `overguard` / `nooverguard`, `shield` / `noshield`, `normal` (non-Steel Path), `weakpoint N%` / `headshot` / `body`, plus Arcane and Exilus options.
- Upgraded bundled reference builds and full trait matrices to 3 tiers (standard / Overguard / shielded, 1,483 scenarios per tier), covering ranged Incarnon cycles, melee Heavy Attacks, and Heavy Slams. Trait recommendations default to `3+1-` with `2+1-` toggle, add same-build single/combined element comparisons, and annotate low-ammo and weak-point conditions.
- Ranged Incarnon weapons now default to **Incarnon Cycle** in `loadoutrecommend` (append base fire or Incarnon form to switch), while `riventraits` continues displaying multiple modes side by side on one card.
- Updated Incarnon cycle simulation to use weak-point charging with body-hit sustained fire; 6 headshot-driven weapons (*Knell*, *Knell Prime*, *Dual Toxocyst*, *Zymos*, *Sporothrix*, *Sporothrix Prime* / *Vesper* family as configured) default to `100%` weak-point hits (still overridable).
- When Burston / Burston Prime Evolution II ties between *2-1* and *2-2*, the recommender prefers *2-1* by default.
- Attaching a Riven screenshot to `loadoutrecommend` now auto-fills Exilus and weapon Arcanes, picks the highest-performing variant automatically when unspecified, and adds local Riven polarity icon detection (if polarity cannot be detected, calculation proceeds with matched-polarity halved drain and marks the slot).

**Admin Panel & Experimental Switches**

- Added the **Command switches** page to the admin panel to globally enable or disable any command immediately across all chats; disabled commands are automatically hidden from the help card.
- Marked **Build scan** and **Build scan damage** as experimental and disabled by default (enable them in **Command switches** if needed); improved Arcane rank detection and duplicate-slot validation.

**Cards & Platform Experience**

- Fixed neutral slots being mislabeled as Universal polarity—only Omni-polarity slots display the official Universal icon; improved help card columns and long-line wrapping.
- QQ Official Bot long-running calculations now send passive-reply progress notices with estimated duration and 5-minute reply window reminders.
- Image-attached plugin commands now claim and stop event propagation cleanly so downstream image captioners do not stall group chats, without affecting normal group images.

**Standalone Engine Update**

- Updated the dual-platform full-simulation package to `portable-535ed49f5a51149d` (adding Incarnon weak-point charging, Burston `2-1` preference, and full trait matrix support).

## v1.3.3

**Weapon Builds & Rivens**

- Added **Build recommendation** (`loadoutrecommend` / `配卡推荐`) and **Riven trait recommendations** (`riventraits` / `词条推荐`). Send `loadoutrecommend Braton Prime` for bundled no-Riven reference builds; with the standalone engine installed, attach a single Riven screenshot to compare eligible weapon variants and builds or specify a firing mode. Cards list weapon stats, physical/elemental damage shares, capacity and polarities, Incarnon evolutions, and per-mod removal DPS deltas.
- Bundled no-Riven builds include mod ranks, capacity, weapon stats, Exilus/Arcane slots, polarized capacity, and per-mod removal DPS; full-simulation evolution rows include Evolution I charge requirements and form effects.
- `riventraits` shows all supported modes for a weapon on one card with separate Positive, Negative, and Spliced trait sections, impact/condition notes on negatives, and community selling references. Existing Riven grading continues showing roll ranges, splice tips, and matching listings on the same card.
- Common shorthand variants (such as Prime abbreviations) work across builds, traits, damage, dispositions, Wiki, and market lookups; Riven auctions still query by weapon family.

**Build Scan & Damage**

- Build scan lists per-slot detections and confirmed rank bonuses, showing full-build DPS and per-mod removal deltas whenever calculable. `scandamage` recalculates the latest scanned build in the chat against a different enemy, level, or hit zone, including confirmed Exilus, Arcanes, and panel-verified Incarnon evolutions.
- Damage commands default to the built-in estimator; admins can download and enable **Full simulation** in the admin panel and switch back at any time.
- Melee damage and build-scan cards display per-hit damage, hit count, and attack speed.

**Market Lookups**

- Standard orders, Riven auctions, and Lich/Sister/Coda auctions use consistent status colors: green for **In-game**, blue for **Online**, and gray for **Offline**; online totals combine in-game and web-online listings.
- Removed deprecated Kuva Siphon queries and fixed Kuva Ayanga matching in Lich auctions.

## v1.3.2

**Relics & Fissures**

- Added **Farm planner** (`farm` / `刷`): send `farm Wisp Prime` for a full-set farming plan or add a part name for one component, listing recommended relics, drop locations, rotations, drop chances, active fissures, and drop/Varzia/vaulted status. Added to help and platform command menus.
- `openrelic` / `relicrun` supports the `endless` filter, placing endless ground fissures ahead of fast fissures within the same era. Railjack Void Storms are listed separately, marking Earth/Saturn Proxima Skirmish as fast and Survival/Defense as endless, with improved table alignment for long node names.

**Commands & Platforms**

- Added 14 English shorthand aliases including `eda` (Deep Archimedea), `eta` (Temporal Archimedea), `sp` (Steel Path Honors), `nw` (Nightwave), `rrange` (Riven stat ranges), and `relicrun`.
- `xh` supports multi-word Lich/Sister/Coda weapon names and `ephemera` filtering (e.g., `xh Tenet Arca Plasmor radiation 50`); `wm` accepts suffix-style Chinese Arcane names.
- Fixed Discord button commands failing to execute when AstrBot 4.29.4 acknowledged interactions first.

**Wiki, World State & Cards**

- `wiki` opens the official English Warframe Wiki by default (Simplified Chinese replies can still choose Huiji Wiki in settings or append `灰机`). Unmatched titles fall back to official Wiki search; locations, game modes, and concept pages match more accurately, and crafted part names reverse-lookup relic drops.
- Enriched English info cards with verified descriptions, abilities, mod effects, recipes, and relic drop locations (including Frost and Frost Prime passives).
- Community source and DE-to-community failover now support timed Events, current Varzia offerings, Weekly Clan Rewards, and Simaris targets.
- Updated 1999 Calendar to use **Omni Forma Blueprint**, verified override descriptions, and season names on push notifications; added **Moonflesh Locus Detected** alert label; prioritized Simplified Chinese news headlines when provided upstream.
- Standardized Tenet shop currency to **Corrupted Holokey**, added English Tenet/Coda notes, and assigned distinct colors to English `Cold` progenitor bonuses and cycle states.

**Damage Calculation**

- Added Incarnon normal-attack calculation for *Innodem*, *Praedos*, and *Thalys*, including +40% attack speed and +3 m range note.

**Subscriptions, Group Admin & Admin Panel**

- Subscriptions (`subscribe` / `蹲`) now parse, display, and trigger in the configured **Display time zone** (`Settings → Output → Display time zone`). **After upgrading, existing subscriptions evaluate their saved weekdays and hours in the current display time zone—if that differs from UTC+08:00, trigger times shift accordingly.**
- Group admins can manage per-group commands with `.disablecmd`, `.enablecmd`, `.disabled`, and custom aliases with `.alias`, and configure daily digests with `.digest on 08:00` (using an independent `digest_timezone`).
- All 9 admin panel pages now support both Simplified Chinese and English, with first-run language setup and independent UI vs. card language settings; Appearance previews follow the saved card language and add slot highlighting and low-contrast warnings.
- Added **Command stats** showing 7-day and 30-day call counts, outcomes, failure rates, and approximate P50/P95 handler latency (aggregated by UTC date).
- Fixed narrow-screen control overflow, rapid-save checkbox drops, retry handling after load failures, preview fallback on errors, and freshness table updates during chained actions.

## v1.3.1

**International English Mode**

- Append `-en` for English replies or `-cn` for Simplified Chinese; group admins can set chat defaults with `.language en` / `.language cn` / `.language default`, and dashboard admins can set the global default language.
- English mode uses official DE English names for nodes, missions, items, weapons, factions, and bounty pools (such as *Gladiator Aegis*), with English cards, notices, and push alerts.
- Added English command aliases (`fissures`, `sortie`, `baro`, `arbitration`, `damage`, `subscribe`, `wfhelp`, etc.) and English market/Riven modifiers (`buy`, `max`, `radiant`, `offline`, `madurai`, `neg`).
- Damage calculator recognizes `heavy attack` or trailing `heavy`; fixed English weapon name *Ruvox* in hints.
- Subscriptions lock in their display language at creation time (legacy subscriptions default to Chinese).
- Riven screenshot recognition supports English game clients across both local OCR and vision models.

**Command Menus**

- Automatically registers 19 menu commands on Telegram and Discord when supported by AstrBot.
- QQ Official Bot admins can send `.menu` (`.菜单`) to print the platform command panel list.

**World State & Data Sources**

- Completed community-source card support for Alerts, Invasions, Darvo Deals, Duviri & Zariman cycles, Conclave, Market Discounts, Descendia, and Deep/Temporal Archimedea, matching official DE formatting across direct and fallback modes.
- Admin panel **Maintenance → Connectivity → World state** now probes the active configured sources, warns if config changes require reload, and explains skipped private URLs.

**Rivens**

- Renamed Chinese display label for Projectile Speed to match in-game terminology while keeping old input aliases; fixed physical damage stat ranges on affected weapons and marked unconfirmed stats with `?`.

**Display & Documentation**

- Timestamps across replies, cards, pushes, and the admin panel now follow the AstrBot host system time zone by default (configurable to a fixed UTC offset).
- Removed global Markdown stripping that affected replies from other plugins.
- Added bilingual Simplified Chinese and English README documentation with localized card galleries.

**Fixes for v1.3.0**

- Fixed unranked Riven faction-damage stats being miscalculated by orders of magnitude with inverted signs.
- Fixed 1999 Calendar showing `?` for all entries when using the community data source.

## v1.3.0

**Admin Panel**

- Added connectivity tests on the **Maintenance** page for World State, Community Data, Wiki source pages, and warframe.market, showing per-route status and latency, distinguishing skipped vs. failed checks, and caching results for 30 seconds.
- Optimized panel rendering to skip redundant DOM redraws when data is unchanged, preserving unsaved settings drafts and focus; fixed auto-refresh stopping after visiting the Appearance page.
- Refreshing community snapshots or refetching from source now immediately updates route status, refresh records, and attention items; added a Reload button to the Data page.
- Added a filter box on the Subscriptions page when more than 3 chats have subscriptions; synced the Settings page when resetting the community data URL to default.
- Restored 1-second updates for "Last poll" and paused font progress polling while the browser tab is hidden.
- Fixed font polling interfering with font selection, stale responses overwriting newer state, and earlier Appearance preview failures overwriting newer previews.

## v1.2.9

**Vision & Local Riven OCR**

- Added one-step mode switching on the **Vision** page between Local OCR, Single-model, and Dual-model Riven reading, validating configuration and displaying the verified active mode.
- Added **Single-model** Riven reading mode (disabled by default): uses only the primary Riven provider with progressive enhanced/positional retries while still prioritizing Local OCR when enabled.
- Added a dedicated **Vision** page in the admin panel separating Riven reading and Build scan channels, status, and presets (with an Advanced toggle for legacy compatibility and diagnostics); merged Dependencies and Connectivity into **Maintenance**.
- Added per-provider/per-task custom vision prompt drafts with explicit enable/reset confirmation and side-by-side same-image comparison (one call per candidate after confirmation).
- Added previewable, opt-in **Channel isolation** migration so Riven reading and Build scan can use strictly separate vision providers (with preview and rollback, never auto-migrated on upgrade).
- Added optional dedicated **Equipment text** channel for Build scan and **Disagreement recheck** channel for Riven reading.
- Improved Riven weapon name extraction (separating Latin Riven suffixes) and mod rank verification combining drain color and rank pips.
- Added opt-in **Local Riven OCR** (disabled by default) with panel-assisted setup or custom external path configuration (`Save → Check → Enable`) and single-image test recognition (third-party engines and models are not bundled).

**Dynamic Data**

- Tenet/Coda progenitor bonuses and Acrithis weekly wares are no longer shipped with stale rotating numbers inside the plugin package; they are fetched and cached at runtime on first start with clear expiry indicators and preserved across upgrades.
- Expanded Chinese location names in drop and acquisition tables.

**Market & Rankings**

- Unified warframe.market rate-limit budgeting across `wm`, `wr`, `xh`, trends, ducats, Riven price references, and ranking crawls (with stricter auction quotas, up to ~15 s queueing when busy, and honoring upstream Retry-After intervals).
- Limited Riven analysis price reference to a single request without automatic retry on failure; restricted `rank refresh` to admins while allowing anyone to check `rank progress`.
- Added 30-minute and 10-minute cooldowns for price ranking and live Riven price refreshes respectively, with clear panel notices during cooldown.

**Multi-Platform & Data Improvements**

- Separated vision provider selectors for Riven screenshots, narrow stat reading, and Build scan; improved Build scan coverage for Stances, Exilus slots, and Arcanes with conflict flags.
- Improved mobile layout for Subscriptions, tables-as-cards on small screens, platform-aware Settings, safe defaults for zero/invalid numeric settings, and QQ Official Bot `@` notifications for subscriptions.
- Added QQ Markdown card support for Arbitration, Coda, Steel Path, Damage, and Help cards; added combined image+button messages on QQ Official Bot; added smart line-preserving text splitting for Telegram, Discord, Slack, and KOOK.
- Standardized `-1` to mean page 1 (use `-w` to force plain text); added interactive help, query, pagination, and market whisper buttons on supported platforms.
- Added a separate **Gold tier** theme color; added direct HTTP rotation fetching before FlareSolverr and GitHub API fallback for community snapshots; added automatic community failover with freshness checks when official DE worldstate is unreachable.

## v1.2.8

**Damage Calculation**

- Fixed *Rifle Elementalist* and *Shotgun Elementalist* erroneously adding +90% base damage (inflating direct hits by ~1.9×); their status damage bonus continues working as intended.
- Scoped *Sacrificial Pressure* and *Sacrificial Steel* +33% faction bonus to Sentients only.
- Fixed *Amalgam Furax Body Count* not adding melee attack speed (its bonus is secondary fire rate) and *Gilded Truth* / magazine flat bonuses not being treated as percentage bonuses.
- Enforced single-choice-per-tier for Incarnon evolutions; clarified Elementalist card descriptions and punch-through annotations.

**Coda Progenitor Bonuses**

- Updated bundled Coda weapon progenitor bonuses for the 2026-10-27 Batch B rotation.

**Privacy**

- Masked raw group IDs across push failure logs and admin panel pause reasons (including IDs embedded in upstream adapter errors).

**Other**

- Updated README data source disclosures (noting the unified 1.2 Riven disposition since 2026-10) and included MIT license notices for `warframe-items` and `warframe-public-export-plus`.

## v1.2.7

**Damage Calculation: New Weapons & Enemies**

- Added 23 weapons including *Daikyu Prime*, *Afentis Prime*, *Sporothrix Prime*, *Vadarya Prime*, *Amprex Prime*, *Athodai Prime*, *Kompressa Prime*, *Kuva Ghoulsaw*, *Coda Bubonico*, *Tenet Quanta*, *Thalys*, and more; cleaned up Archgun and Sentinel weapon stats (corrected *Deconstructor* / *Deconstructor Prime* to pure Impact).
- Corrected base stats for 19 common enemies against the English Wiki (Eidolon Teralyst, Profit-Taker, Nox, Crewman, MOA, Leech Osprey, Demolisher, Bombard, etc.) and fixed headshot/critical-headshot multipliers (including *Ignis*, *Ignis Wraith*, *Cyanex*, and Techrot weak points).

**Damage Calculation: Attack Modes & Fire Rate**

- Rebuilt multi-segment attacks, Incarnon forms, Condition Overload (CO) buckets, and Archgun atmosphere/space stats from the English Wiki (fixing multishot on AoE segments, multiplicative CO on Sentinel weapons/*Zakti*/*Kuva Ayanga*, *Angstrum* rockets, *Tenet Plinx*/*Tenet Diplos*/*Athodai* alt-fires, *Lenz* cold bursts, *Proboscis Cernos*, *Mutalist Cernos*, *Ballistica Prime*, *Nataruk* perfect vs. full charge, *Cinta*, and more).
- Unified alt-fire and charge keywords, separated headshot-only explosions (*Vectis* Incarnon, *Zymos*), added spool-up fire-rate modeling for 14 spool weapons (*Soma*, *Gorgon*, *Tenora*, etc.), and modeled *Phenmor* Incarnon fire-rate decay.

**Damage Calculation: Mods, Incarnon Evolutions & Valence**

- Corrected mod ranks, drains, and conditional triggers (*Ambush Optics*, *Aero Agility*, *Pressure Point*, *Brain Storm*, *Skull Shots*, etc.); added 37 mod mutual-exclusion groups; applied conditional stacks for Galvanized/stacked mods when requested.
- Rebuilt Incarnon evolutions (including 20+ melee Incarnons and fixed *MK1* variants), counting only unconditional stat bonuses and annotating conditional perks.
- Expanded Kuva/Tenet/Coda valence bonus support to 51 weapons; misspelled weapon names now report an explicit not-found error instead of silently matching another weapon.

**Rivens, Build Scan, Market & Security**

- Regenerated **Riven stat ranges** (`rivenranges`) from official DE rules and market surveys (excluding ineligible stats for Sentinel/Tome/hitscan/low-IPS weapons, marking unobserved stats with `?`, adding 9 melee Spliced stats, and removing negative base damage on melee).
- Standardized **Riven splice** names and fixed *Parry Angle* scaling (was 10× too small); relaxed faction-multiplier precision rounding in Riven grading.
- Added fallback title-bar weapon re-read in Build scan; filtered low-volume (<3 sales in 48 h) items out of price rankings; aligned trend percentage math; improved warframe.market timeout/rate-limit notices.
- Restricted vision screenshots strictly to the configured provider, disabled failed-scan image retention by default, masked group IDs in logs, and blocked private/loopback URLs unless `allow_private_urls` is enabled.
- Added the **About** page to the admin panel; migrated damage and Riven pool data generation entirely to official DE exports, `warframe-public-export-plus` (MIT), and English Wiki (CC BY-NC-SA), including full SIL OFL 1.1 license text for bundled fonts.

## v1.2.6

**Rivens: New `splice` Command**

- Added **Riven Splice** (`splice` / `紫卡融合`): lists all 18 Riven Splicer combined traits and their 22 source pairs across Universal, Gun, and Melee categories, filterable by weapon name, weapon class, or stat name, with fusion rules in the footer.

**Riven Rankings & Analysis**

- Restored official DE weekly Riven trade reports as the primary **Riven ranking** view; admins can manually trigger a live warframe.market price comparison snapshot (`WM listings / change`) after major updates.
- Added support for unchosen `???` placeholder lines during Riven splicing in Riven analysis; fixed multi-line wrapped stat parsing (such as *Magazine Reloaded/s when Holstered*).
- Added shorthand `2+1` / `3+1` combinations and color-coded column styling in **Riven stat ranges**.
- Added one-click **Fetch live Riven prices** and **Fetch price rankings** buttons on the admin panel Data page, and stabilized automatic price-ranking refreshes to every ~2 days without overlapping runs.

## v1.2.5

**Rivens: Update 44.1.3 Unified 1.2 Disposition**

- Adapted to DE Hotfix 44.1.3 removing per-weapon Riven dispositions (all Rivens unified to 1.2): **Disposition** now displays unified 1.2 alongside each weapon's pre-change disposition and scaling factor, and **Riven analysis** detects legacy pre-change screenshots.
- Added **Riven stat ranges** (`rivenranges` / `词条范围`) displaying Positive, Spliced, and Negative max-rank ranges side by side across `3+1-`, `3+`, `2+1-`, and `2+` combinations, filtered to stats rollable on the weapon.
- Added **Veiled Riven** lookups in `wr` / `wm` (across all 8 categories); fixed shotgun-pistol base stat pools (*Pyrana*, *Detron*, *Bronco*) and 51 variant categories; added auto-correction for 1-character OCR weapon typos and mixed-language faction stat inputs.

**Trends, Arbitrations & Wiki**

- Upgraded **Price trend** (`trend`) to true line charts showing 48-hour and 90-day high/low/latest prices with timestamps.
- Added community median Vitus Essence/hour numbers to **Arbitration** and **Arbitration schedule**, plus a "Next efficient (S/A+/A)" indicator.
- Localized drop locations in Wiki cards, updated disposition labels to unified 1.2, and merged internal code-name duplicates.
- Handed `/help` cleanly back to AstrBot's built-in help and reported partial DE worldstate parse errors explicitly.

## v1.2.4

**Subscriptions & Rotation Data**

- Added hit-count limits to subscriptions (`subscribe` / `蹲`, e.g., auto-cancel after N alerts, combinable with duration limits) and displayed remaining hits in `.status`.
- Added stale-wiki detection for Tenet, Coda, and Acrithis rotations (checking both previous-rotation value matches and Wiki last-modified timestamps) so un-updated pages are never locked in as fresh.
- Added **Refetch from source** on the admin panel Data page and chat refresh commands (`tenet refresh`, `coda refresh`, `acrithis refresh`) with per-item cooldowns and hourly rate limits.
- Added optional WFHelper fallback setting for progenitor bonuses; separated same-name PvE vs. PvP items (e.g., *Balla*) and cleaned raw Wiki card data.

## v1.2.3

**Wiki Cards, Admin Panel & Stability**

- Fixed Zaw part Wiki cards showing zeroed/garbled weapon stats and trimmed floating-point tails in mod descriptions; prioritized exact item and game-mode pages (`wiki Balla`, `wiki Arbitration`).
- Added clear error diagnostics and a **Reset to default URL** button when a custom community data URL fails; marked runtime-fetched Riven dispositions cleanly in the data registry.
- Stopped background warmup tasks before closing HTTP clients on plugin reload to prevent `client has been closed` log noise.

## v1.2.2

**Subscriptions, Rankings & Admin Panel**

- Instead of immediately deleting a group's subscriptions after temporary delivery failures, the daemon now **pauses** push for that group, retries every 6 hours, and only cleans up after 7 days (still cleaning up immediately if kicked or disbanded); groups can resume via `.enable push` or the admin panel.
- Price-ranking refreshes now require ≥80% item coverage before advancing the timestamp and prioritize missed items on the next pass; sped up domestic fallback mirror switching for community snapshots.
- Added **Community snapshot routes**, **Data registry**, **Active data ledger**, and paused-group management to the admin panel.

## v1.2.1

**New Admin Panel**

- Launched the **SDJKBOT Operations** plugin page in the AstrBot dashboard (AstrBot ≥ 4.24.1, admin-only): data freshness, masked subscription overview, push daemon status, one-click community snapshot refresh, FlareSolverr/vision connectivity checks, and full **Appearance** customization (colors, 4 presets, live preview, backgrounds, logos, and custom font upload/URL import).

**Riven Market, Damage & Baro / Arbitration**

- Improved `wr` stat aliases (`CC`/`CD`/`MS`/`SC`, case-insensitive English stats, strict `3+1` matching without silent relaxation, and fixed weapon names containing `&`).
- Added theoretical-estimate disclaimer to damage cards; fixed *Arbucep* ammo consumption, Steel Path ×2.5 enemy HP/Shield scaling, and *Split Flight* exclusions.
- Grouped Baro Ki'Teer offerings by category with color tags and aligned columns; marked low-sample (<5 runs) Arbitration ratings with `*`; enabled community snapshot updates for Arbitrations, Baro history, and Riven dispositions; switched marketplace category to Entertainment.

## v1.2.0

**Complete Card Redesign**

- Redesigned all image cards (keeping the Orokin gold-on-dark aesthetic) with top-line summaries, aligned zebra-striped tables, unified footers, dedicated warning boxes, better mobile readability, and ~2× faster rendering.
- Reorganized Help, Market (`wm`/`wr`/trends), Damage, Riven analysis, and Fissure cards; fixed `∞` auction icons, decimal price coloring, and ephemera `★` markers.

**World State, Subscriptions, Relics & Arbitrations**

- Fixed Timers, Archon Hunt localized names and levels (130–150), Baro next-visit predictions, Bounty pools, Infested Invasion progress, Railjack fissure mission types, 14-day Arbitration schedules, Nightwave Elite tags, and Prime Vault/Varzia bundles; unified error cards.
- Improved subscription push behavior: no backlog flood on new subscriptions, time-window-scoped `@` mentions, `.disable push` pausing without deleting subscriptions, 30-minute restart catch-up, and space=AND / comma=OR fissure filter semantics.
- Added vaulted relics to `primevault`, Ducat gold/silver/bronze coloring on relic parts, event/Baro acquisition sources, and rebuilt the relic/drop index from official drop tables.
- Refreshed Arbitration community tiers (lowering F-tier threshold to <450 Vitus/hr), localized new Alert reward names, fixed Coda batch display, and added `vision_provider_id_line` for narrow Riven stat reading.

## v1.1.9

**Build Scan & Damage Calculation**

- Fixed `scandamage` parameter parsing (`level`, `headshot`, `heavy`, `status` stacks) so it matches `damage`; included fire-rate mods and post-mod `▸` panel values in Build scan; added auto-correction for swapped Impact/Puncture/Slash rows.
- Applied matching elemental mod bonuses to Heat/Toxin/Electricity DoTs; updated humanoid headshot multiplier to ×3; implemented per-weapon Condition Overload buckets (direct hits only, no longer inflating DoTs or AoE); corrected *Weeping Wounds* formula.
- Switched Incarnon weapons to default to Base Form (add `incarnon` to switch), calculated Incarnon sustained DPS from charge pools, removed double-counted attack segments, fixed 9 AoE weapon crashes (*Komorex*, *Trumna Prime*, *Grattler*, *Kuva Ayanga*, *Mausolon*, etc.), activated stacked fire-rate mods, and calibrated *Epitaph* / *Epitaph Prime* full-charge cadence from frame-by-frame testing.

## v1.1.8

**Riven Splicer Labels & `wr` Auction Improvements**

- Shortened display labels for the 18 Riven Splicer stats while supporting full, medium, and short aliases.
- Included offline listings by default in `wr` (sorted after online listings) alongside listing ages (`s`/`m`/`h`/`>100h`), non-buyout bid listings (`∞`), explicit empty-result messages, and traceable pool logs.

## v1.1.7

**Update 44 Riven Splicer & Market Updates**

- Added full support for all 18 Update 44 Riven Splicer stats across Riven grading and `wr` market search (fixing misbindings with base elements/crit damage/ammo max), prioritized manual weapon names with 1-character typo fallback, removed multi-provider racing, and added Shotgun Zoom base stats (44.0.2).
- Expanded `wm` part keywords (+8) and `wr` stat mappings (32 → 50); refreshed rotation and Acrithis snapshots.

## v1.1.6

**Riven & Build Scan Vision Accuracy**

- Fixed parenthetical multipliers (such as `2x for Heavy Attacks`) hijacking Riven stat values; trimmed narrow-read and scan prompts to remove literal example numbers that caused hallucinated stats; improved JSON recovery; fixed 4 Incarnon reverse-calculation edge cases.

## v1.1.5

**Community Arbitration Ratings & Drop Table Rebuild**

- Integrated community-measured `arbi.wf.wiki` leaderboard medians (9,000+ runs) keyed by canonical node IDs (`SolNodeXXX`), raising rating coverage from 16/88 to 64/88 with sample counts and team-skill footnotes.
- Rebuilt drop tables with mode-aware keys (`planet · node · mode · rotation`, 4,148 → 5,570 entries), fixing mislinked fallback cards and Railjack cache collisions, and added Cryotic Front excavation tables.

## v1.1.4

**Riven Families, 9 New Subscriptions, Translation & Alerts**

- Switched Riven family detection to official DE `parentName` lineages, unified Kitgun dual-mode pistol base stats, fixed recoil sign inversion and multiplier parsing, and repaired automatic price-ranking refreshes.
- Added 9 subscribable events (Bounties, Steel Path Honors, Riven Slivers + new `slivers` command, Incarnon, Tenet, Coda, Varzia, 1999 Calendar, Nightwave), `T1–T6` fissure tier aliases, and initiator `@` mentions on QQ.
- Added the `translate` command, NFKC name normalization, localized Alert rewards (1,386 StoreItems), weapon acquisition sources in knowledge base entries, and missing glyphs in the bundled font subset (53 → 55 main commands).

## v1.1.3

**Market Fixes, Faster Startup & Community Contributions (PR #2 @AoaoMH)**

- Fixed `wr` multi-stat AND filtering, `any negative` filtering, price bounds, and online-first sorting; fixed Riven multiplier-line parsing and exact official weapon matching; moved 2.5 MB market table loading to background warmup; fixed multi-word Arbitration node column shifts; expanded bounty/slang aliases, redesigned bounty cards, and auto-derived full version numbers in card watermarks.

## v1.1.2

**1999 Calendar Redesign & Official Sanctum Names**

- Redesigned `calendar` into a single full-season card with dated To-Do, Prize, and Override blocks; updated Descendia checkpoint floors 7/14/21 to official Sanctum names and added localized Riven reward labels.

## v1.1.1

**Official Localization & Rich-Text Cleanup**

- Replaced 112 residual English Warframe ability names and 433 mod/augment descriptions with official Simplified Chinese strings, aligned 12 hotfix keys (*Alad V*, *Entrati Coin*, Archimedea modifiers), and stripped 92 raw rich-text tags from card output.

## v1.1.0

**Snapshot & Official Hotfix Alignment**

- Refreshed bundled progenitor bonus and Acrithis snapshots, aligned 12 official hotfix localization keys, and reiterated the plugin ID migration to `astrbot_plugin_warframe_sdjkbot`.

## v1.0.9

**Official Chinese Coverage, Layout Polish & Plugin ID Migration**

- Removed the chat-assistant command (54 → 53 main commands).
- Expanded bundled official Chinese keys to cover `CircleOfHell` (Descendia), `1999` (Höllvania Calendar), `Narmer`, and `Weapons`; fixed 5 Descendia mission-type mappings and filtered `[PH]`/`|val|` placeholders.
- Improved card alignment across Bounties, Fissures, Void Storms, Arbitration schedules, and Simaris targets; improved rendering performance by ~10%.
- Fixed Riven polarity-line filtering, Vandal/Wraith family variants, relic package-import crash, duplicate push daemons across reloads, Venus Deepmine Steel Path rewards, and runtime-first progenitor bonus loading.
- Migrated plugin ID from `astrbot_plugin_warframe` to `astrbot_plugin_warframe_sdjkbot` (see AstrBot#10198).

## v1.0.8

**Persistent User Fonts, Community Snapshots & `wm` Filter Fixes**

- Automatically migrated custom user fonts to `data/plugin_data/.../fonts/` so they survive plugin updates; added tiered FlareSolverr warning levels and `bot-data` community snapshot fallback (aligned to `XX:05 UTC` with 30-minute light polling when FlareSolverr is unavailable).
- Fixed `wm` max-rank, relic refinement, and Foil/Atragraph mod filters; expanded Warframe slang/nickname Wiki lookups and added KB document #14.

## v1.0.7

**Bundled Font Subset, Wiki Cards & FlareSolverr Self-Healing**

- Bundled a Noto Sans CJK SC font subset (6,763 GB2312 characters + symbols) and full Wiki intro/mod-effect databases directly in the marketplace/open-source package for out-of-the-box image rendering.
- Rebuilt crashed FlareSolverr Chromium sessions via `destroy → create`, raised `maxTimeout` to 120 s, recycled sessions before each refresh cycle, and updated Coda Batch B bonuses.

## v1.0.6

**Weapon Variants, Wiki Dispositions & Cetus Tent Bounties**

- Added Plains of Eidolon small-tent bounties (A/B/C), clickable Wiki text links paired with intro cards, material/recipe/decoration cards, 1,997 translated mod descriptions, and Chinese shorthand relic lookups (including *Citrine Prime* and updated drop tables).
- Added weapon variant parsing across `dispo`, `wr`, `xh`, and `damage`, switched Riven dispositions to the official Wiki table (418 → 649 entries), and fixed 9 player-reported Riven bugs.

## v1.0.5

**Maintenance Release**

- Internal maintenance update with no user-visible changes.

## v1.0.4

**Faster Build Scan, Pip-Based Mod Rank Detection & Automated Acrithis**

- Auto-downscaled oversized build screenshots to ≤1600 px before sending to vision models (~2× faster), introduced local rank-pip + max-rank-line verification across 1080p/1440p/4K and light/dark UI themes, classified Riven mods separately, and guarded against missed mod rows.
- Fixed Coda progenitor parsing for `Dual ` weapon prefixes, rendered bare `subscribe` help as a card, and automated weekly Acrithis offerings refreshes.

## v1.0.3

**Push Reliability, Command Yielding, `wm` Matching & Rebrand**

- Fixed broken Arbitration and Steel Path Invasion push subscriptions, enabled non-fissure subscription filters, and removed retired Alert subscriptions.
- Rebranded display name to **Warframe SDJKBOT** (no reinstall required); yielded `/` commands occupied by built-in or other plugins; prioritized official names over slang in `wm` and fixed Primed mod matching; fixed Tenet/Coda auto-refresh stalls and Acrithis Monday 00:00 UTC reset cadence.

## v1.0.2

- Initial AstrBot plugin marketplace release (`skyti1437/astrbot_plugin_warframe`).
- Stored all user data and runtime snapshots under `data/plugin_data/<plugin_name>/` with one-time legacy migration.

## v1.0.1

- Fixed `wm` part keyword filtering so single-part blueprint queries no longer return full sets; added missing official Simplified Chinese item names.

## v1.0.0

- Reset versioning to `1.0.0` with unified branding.
- Reworked Kuva/Tenet/Coda weapon auction search, added Baro Ki'Teer next-visit predictions, fixed admin commands, and completed a security hardening pass.
