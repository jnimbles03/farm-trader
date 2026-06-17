# Graph Report - farm-trader  (2026-06-17)

## Corpus Check
- 70 files · ~303,648 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 581 nodes · 974 edges · 42 communities (35 shown, 7 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS · INFERRED: 1 edges (avg confidence: 0.8)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `22e8d114`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]

## God Nodes (most connected - your core abstractions)
1. `fetch()` - 28 edges
2. `jsonResponse()` - 21 edges
3. `main()` - 20 edges
4. `hmacHex()` - 15 edges
5. `requireAdmin()` - 14 edges
6. `evaluate_signals()` - 14 edges
7. `adminBroadcastStart()` - 12 edges
8. `ordersStart()` - 11 edges
9. `compilerOptions` - 11 edges
10. `load_plan()` - 11 edges

## Surprising Connections (you probably didn't know these)
- `main()` --calls--> `usda_pair_news_for_today()`  [EXTRACTED]
  evaluate.py → usda_alerts.py
- `main()` --calls--> `fetch_bids_raw()`  [EXTRACTED]
  refresh_ritchie.py → scrape_bushel_bids.py
- `main()` --calls--> `get_token_portal()`  [EXTRACTED]
  refresh_ritchie.py → scrape_bushel_bids.py
- `main()` --calls--> `get_token_ropc()`  [EXTRACTED]
  refresh_ritchie.py → scrape_bushel_bids.py
- `main()` --calls--> `shape_ritchie()`  [EXTRACTED]
  refresh_ritchie.py → scrape_bushel_bids.py

## Import Cycles
- 1-file cycle: `evaluate.py -> evaluate.py`
- 1-file cycle: `usda_alerts.py -> usda_alerts.py`
- 1-file cycle: `scripts/freshness_check.py -> scripts/freshness_check.py`
- 1-file cycle: `scripts/pulse_status.py -> scripts/pulse_status.py`
- 1-file cycle: `scripts/remind_pending.py -> scripts/remind_pending.py`
- 1-file cycle: `scripts/send_farm_test_broadcast.py -> scripts/send_farm_test_broadcast.py`
- 1-file cycle: `scripts/send_realistic_test.py -> scripts/send_realistic_test.py`
- 1-file cycle: `scripts/send_realistic_test_plain.py -> scripts/send_realistic_test_plain.py`
- 1-file cycle: `scripts/who_replied.py -> scripts/who_replied.py`

## Communities (42 total, 7 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.07
Nodes (80): adminAlertsConfig(), adminBroadcastStart(), adminBroadcastSubmit(), adminBroadcastTest(), adminGroups(), adminRecipients(), AdminRun, adminRunDetail() (+72 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (79): _append_history(), append_news(), big_move_news_for(), build_ledger_snapshot(), build_positions_snapshot(), build_prices_snapshot(), Contract, _direction_hint() (+71 more)

### Community 2 - "Community 2"
Cohesion: 0.11
Nodes (32): _api_headers(), _centre_headers(), fetch_avg_pricing(), fetch_commodity_balances(), fetch_session_meta(), main(), _now_iso(), Session (+24 more)

### Community 3 - "Community 3"
Cohesion: 0.14
Nodes (23): build_payload(), current_month_code(), fetch_fs_grain_html(), fetch_futures_prices(), _find_data_dir(), _find_docs_dir(), load_ritchie_baseline(), main() (+15 more)

### Community 4 - "Community 4"
Cohesion: 0.13
Nodes (24): compute_surprise(), _confidence(), _direction_word(), _dominant_surprise(), _find_report(), _fmt_band(), format_preview_sms(), format_release_sms() (+16 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (23): For /graphify add and --watch, For /graphify query, For the commit hook and native CLAUDE.md integration, For --update and --cluster-only, /graphify, Honesty Rules, Interpreter guard for subcommands, Part A - Structural extraction for code files (+15 more)

### Community 6 - "Community 6"
Cohesion: 0.20
Nodes (17): _decode_body(), _dump_csv(), fetch_settlements_har(), fetch_settlements_live(), _hdrs(), main(), _money(), Any (+9 more)

### Community 7 - "Community 7"
Cohesion: 0.12
Nodes (16): 1. Create a new GitHub repo and push, 2. Add secrets for SMS, 3. Enable GitHub Pages, 4. Kick off the first Action run, 5. Patch the React site, Add a new contract, Change signal logic, Check if alerts are wired (+8 more)

### Community 8 - "Community 8"
Cohesion: 0.22
Nodes (16): extract_bushel_sales(), first_delivery_period(), load_live_storage(), load_static_sales(), main(), marketing_year_window(), normalize_crop(), parse_first_date() (+8 more)

### Community 9 - "Community 9"
Cohesion: 0.19
Nodes (15): _describe_pending(), find_pending_for_phone(), load_confirmations(), main(), parse_reply(), pending_for_phone(), Return (vote, hint) where hint is a dict that may carry one of:         {"sid":, All pending (sid, sent_at) where this phone hasn't voted yet,     newest first. (+7 more)

### Community 10 - "Community 10"
Cohesion: 0.14
Nodes (13): description, devDependencies, @cloudflare/workers-types, typescript, wrangler, name, private, scripts (+5 more)

### Community 11 - "Community 11"
Cohesion: 0.22
Nodes (12): fetch_data(), _find_form_action(), login(), main(), make_session(), Session, Bushel (akronservices.com) scraper.  Bushel powers The Akron App / akronservices, First try a real <form> tag (in case Keycloak ever serves the        traditional (+4 more)

### Community 12 - "Community 12"
Cohesion: 0.15
Nodes (12): compilerOptions, allowSyntheticDefaultImports, esModuleInterop, lib, module, moduleResolution, noEmit, skipLibCheck (+4 more)

### Community 13 - "Community 13"
Cohesion: 0.22
Nodes (12): _find_docs_dir(), main(), _pick_atm_strike(), _pick_chain(), _pick_chain_expiry(), Path, refresh_ib_iv.py — pulls the at-the-money implied vol for the actively-modeled g, Match the chain whose tradingClass aligns with the futures contract. (+4 more)

### Community 14 - "Community 14"
Cohesion: 0.26
Nodes (12): _build_message(), _drop_stale_sim_entries(), _load(), main(), datetime, Keep the state file from accumulating old sim/smoke pending rows.      Leaves an, Send via Textbelt using only phone + message + key (no webhook/URL fields)., Build the SMS body.      NOTE: We deliberately do NOT include the 6-char hex slu (+4 more)

### Community 15 - "Community 15"
Cohesion: 0.17
Nodes (11): 1. Deploy the Cloudflare Worker, 2. Add `REPLY_WEBHOOK_URL` to repo secrets, 3. That's it, Disabling confirmations, File map, One-time setup (~20 min), Reminders for non-responders, Reply parsing (+3 more)

### Community 16 - "Community 16"
Cohesion: 0.30
Nodes (11): feed_age(), format_alert(), hours_old(), main(), parse_iso(), datetime, Path, Build the SMS body. Stays inside one ~160-char segment when possible     but we (+3 more)

### Community 17 - "Community 17"
Cohesion: 0.32
Nodes (11): _format_pulse(), _last4(), load_confirmations(), main(), _parse_iso(), datetime, A compact fingerprint of vote state. If this hasn't changed     since the last p, Single SMS body summarizing one pending alert. (+3 more)

### Community 18 - "Community 18"
Cohesion: 0.30
Nodes (11): _due(), _last4(), load_confirmations(), main(), _parse_iso(), datetime, Last 4 digits of a phone number, used to identify holdouts in the     group remi, Context-carrying group-reminder text.      Sent to ALL recipients on the alert ( (+3 more)

### Community 19 - "Community 19"
Cohesion: 0.36
Nodes (11): _age(), _last4(), main(), _parse_iso(), _print_alert(), _print_orphans(), datetime, Reduce a +E.164 phone to its last 4 digits for terse display. (+3 more)

### Community 20 - "Community 20"
Cohesion: 0.33
Nodes (10): _build_message(), _drop_stale_sim_entries(), _load(), main(), datetime, Keep the state file from accumulating old sim/smoke pending rows.      Leaves an, _recipients(), _save() (+2 more)

### Community 21 - "Community 21"
Cohesion: 0.20
Nodes (9): 1. IB Gateway, 2. IBC (auto-restart, handle the daily 2FA), 3. Python env, 4. Git push from cron, 5. Install the launchd job, IB Gateway → AgDCA live IV, One-time setup (Mac mini), Tuning (+1 more)

### Community 22 - "Community 22"
Cohesion: 0.22
Nodes (8): Acceptance criteria, Code structure, Coding-agent prompt — add an SMS bot route to the Freis Farm Worker, Decisions already made — do not ask the user, Files to change, Task, What NOT to do, What to do when you're done

### Community 23 - "Community 23"
Cohesion: 0.22
Nodes (8): graphify reference: extra exports and benchmark, Step 6b - Wiki (only if --wiki flag), Step 7 - Neo4j export (only if --neo4j or --neo4j-push flag), Step 7a - FalkorDB export (only if --falkordb or --falkordb-push flag), Step 7b - SVG export (only if --svg flag), Step 7c - GraphML export (only if --graphml flag), Step 7d - MCP server (only if --mcp flag), Step 8 - Token reduction benchmark (only if total_words > 5000)

### Community 24 - "Community 24"
Cohesion: 0.25
Nodes (7): About the response, Diff view, Fix, Patch to `freis-farm-v5.jsx`, Staleness caveat — read this, Verify after the first Action run, What's broken today

### Community 25 - "Community 25"
Cohesion: 0.43
Nodes (6): _load(), main(), datetime, _save(), _send(), _short_id()

### Community 26 - "Community 26"
Cohesion: 0.62
Nodes (6): load_orders(), main(), parse_payload(), Any, save_orders(), validate_payload()

### Community 27 - "Community 27"
Cohesion: 0.33
Nodes (5): freis-farm-sms-reply (Cloudflare Worker), One-time setup (~15 min), Rotating the GitHub PAT, Smoke test, Updating

### Community 28 - "Community 28"
Cohesion: 0.33
Nodes (5): For /graphify explain, For /graphify path, graphify reference: query, path, explain, Step 0 — Constrained query expansion (REQUIRED before traversal), Step 1 — Traversal

### Community 29 - "Community 29"
Cohesion: 0.50
Nodes (4): main(), merge_storage_state(), patch_advisor_bundle.py — overlay live Ritchie sidecar onto the published adviso, Return (new_storage_state, changed?). Same overlay logic as     context_builder.

### Community 30 - "Community 30"
Cohesion: 0.70
Nodes (4): _build_message(), main(), _recipients(), _send()

### Community 31 - "Community 31"
Cohesion: 0.50
Nodes (3): For /graphify add, For --watch, graphify reference: add a URL and watch a folder

### Community 32 - "Community 32"
Cohesion: 0.50
Nodes (3): For git commit hook, For native CLAUDE.md integration, graphify reference: commit hook and native CLAUDE.md integration

### Community 33 - "Community 33"
Cohesion: 0.50
Nodes (3): For --cluster-only, For --update (incremental re-extraction), graphify reference: incremental update and cluster-only

### Community 34 - "Community 34"
Cohesion: 0.83
Nodes (3): main(), sanitize_for_sms(), send_sms()

## Knowledge Gaps
- **130 isolated node(s):** `name`, `version`, `private`, `description`, `deploy` (+125 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `usda_pair_news_for_today()` connect `Community 4` to `Community 1`?**
  _High betweenness centrality (0.003) - this node is a cross-community bridge._
- **Why does `main()` connect `Community 1` to `Community 4`?**
  _High betweenness centrality (0.002) - this node is a cross-community bridge._
- **What connects `name`, `version`, `private` to the rest of the system?**
  _235 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.07222222222222222 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.05506329113924051 - nodes in this community are weakly interconnected._
- **Should `Community 2` be split into smaller, more focused modules?**
  _Cohesion score 0.10695187165775401 - nodes in this community are weakly interconnected._
- **Should `Community 3` be split into smaller, more focused modules?**
  _Cohesion score 0.14333333333333334 - nodes in this community are weakly interconnected._