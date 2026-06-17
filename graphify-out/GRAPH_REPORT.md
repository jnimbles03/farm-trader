# Graph Report - .  (2026-06-17)

## Corpus Check
- 102 files · ~303,648 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 638 nodes · 1189 edges · 34 communities (28 shown, 6 thin omitted)
- Extraction: 95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS · INFERRED: 56 edges (avg confidence: 0.82)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Cloudflare Worker API Handlers|Cloudflare Worker API Handlers]]
- [[_COMMUNITY_SMS Broadcast & Test Scripts|SMS Broadcast & Test Scripts]]
- [[_COMMUNITY_Grain Trading Infrastructure|Grain Trading Infrastructure]]
- [[_COMMUNITY_SMS Reply & Confirmation Flow|SMS Reply & Confirmation Flow]]
- [[_COMMUNITY_Advisor Context & Market Data Bundle|Advisor Context & Market Data Bundle]]
- [[_COMMUNITY_Ritchie & Bushel Price Refresh|Ritchie & Bushel Price Refresh]]
- [[_COMMUNITY_Cloudflare SMS Bot & Alert State|Cloudflare SMS Bot & Alert State]]
- [[_COMMUNITY_USDA News & Surprise Alerts|USDA News & Surprise Alerts]]
- [[_COMMUNITY_Elevator Bid Scraping|Elevator Bid Scraping]]
- [[_COMMUNITY_Farm Dashboard HTML Pages|Farm Dashboard HTML Pages]]
- [[_COMMUNITY_Keycloak Auth & Bushel Scraper|Keycloak Auth & Bushel Scraper]]
- [[_COMMUNITY_Signal Evaluation & Contracts|Signal Evaluation & Contracts]]
- [[_COMMUNITY_Price History & News Loading|Price History & News Loading]]
- [[_COMMUNITY_Bushel Settlement Fetching|Bushel Settlement Fetching]]
- [[_COMMUNITY_Sales Log Construction|Sales Log Construction]]
- [[_COMMUNITY_Ledger & Order Processing|Ledger & Order Processing]]
- [[_COMMUNITY_Cloudflare Worker Package Config|Cloudflare Worker Package Config]]
- [[_COMMUNITY_Sales & Freshness Tracking|Sales & Freshness Tracking]]
- [[_COMMUNITY_IB Gateway Implied Volatility|IB Gateway Implied Volatility]]
- [[_COMMUNITY_Grain Marketing Plan & Policy Calendar|Grain Marketing Plan & Policy Calendar]]
- [[_COMMUNITY_Live Position Tracking|Live Position Tracking]]
- [[_COMMUNITY_graphify Knowledge Graph Tools|graphify Knowledge Graph Tools]]
- [[_COMMUNITY_Advisor Bundle Patch & Git State|Advisor Bundle Patch & Git State]]
- [[_COMMUNITY_Freis Farm Brand & Identity|Freis Farm Brand & Identity]]
- [[_COMMUNITY_Order Acceptance Scripts|Order Acceptance Scripts]]
- [[_COMMUNITY_USDA News SMS Formatting|USDA News SMS Formatting]]
- [[_COMMUNITY_graphify Reference Docs|graphify Reference Docs]]
- [[_COMMUNITY_Price History Bootstrap|Price History Bootstrap]]
- [[_COMMUNITY_GitHub Pages Static Deploy|GitHub Pages Static Deploy]]
- [[_COMMUNITY_Pipeline State & Orders|Pipeline State & Orders]]
- [[_COMMUNITY_Whisper Transcription Tools|Whisper Transcription Tools]]
- [[_COMMUNITY_IB IV Shell Wrapper|IB IV Shell Wrapper]]
- [[_COMMUNITY_Non-Bushel Static Sales|Non-Bushel Static Sales]]
- [[_COMMUNITY_Demo Contracts|Demo Contracts]]

## God Nodes (most connected - your core abstractions)
1. `fetch()` - 28 edges
2. `jsonResponse()` - 21 edges
3. `main()` - 20 edges
4. `hmacHex()` - 15 edges
5. `requireAdmin()` - 14 edges
6. `evaluate_signals()` - 14 edges
7. `adminBroadcastStart()` - 12 edges
8. `Cloudflare Worker Entry Point (index.ts)` - 12 edges
9. `TextBelt SMS Gateway Integration` - 12 edges
10. `freis-farm-bot Git Identity` - 12 edges

## Surprising Connections (you probably didn't know these)
- `Bushel Connectivity Check Workflow` --references--> `TextBelt SMS Gateway Integration`  [INFERRED]
  .github/workflows/bushel-connectivity-check.yml → cloudflare-worker/src/index.ts
- `Pulse Confirmation Status Workflow` --references--> `TextBelt SMS Gateway Integration`  [INFERRED]
  .github/workflows/pulse-status.yml → cloudflare-worker/src/index.ts
- `Remind Pending Confirmations Workflow` --references--> `TextBelt SMS Gateway Integration`  [INFERRED]
  .github/workflows/remind-pending.yml → cloudflare-worker/src/index.ts
- `Test SMS Delivery Workflow` --references--> `TextBelt SMS Gateway Integration`  [EXTRACTED]
  .github/workflows/test-sms.yml → cloudflare-worker/src/index.ts
- `Multi-Elevator Bid Comparison with Freight Adjustment` --semantically_similar_to--> `Grain Sale Plan with Seasonal Tranches`  [INFERRED] [semantically similar]
  data/elevator_bids.json → docs/plan.json

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

## Hyperedges (group relationships)
- **Advisor Pipeline: Worker fetches context, calls Claude, SMS replies** — cloudflare_worker_index, concept_advisor_persona, concept_advisor_context_bundle, concept_textbelt_sms [EXTRACTED 1.00]
- **CTA Broadcast Flow: Admin sends SMS, recipients reply, quorum triggers all-clear** — cloudflare_worker_index, concept_cta_quorum, concept_textbelt_sms, data_recipients [EXTRACTED 1.00]
- **Grain Marketing Decision Data: bids + contracts + plan + storage rate** — docs_bushel, contracts, docs_plan, concept_storage_rate [INFERRED 0.85]
- **Signal Evaluation and SMS Alert Pipeline** — evaluate, docs_signals, textbelt_sms_concept, confirmation_flow_concept [EXTRACTED 0.95]
- **Bushel Data Refresh and Advisor Context Pipeline** — scrape_bushel_bids, refresh_ritchie, patch_advisor_bundle, scripts_build_sales_log_from_bushel [EXTRACTED 0.95]
- **Confirmation Lifecycle: Evaluate to Collect to Remind to Pulse** — evaluate, scripts_collect_reply, scripts_remind_pending, scripts_pulse_status [EXTRACTED 0.95]
- **SMS Test Script Variants (with/without webhook, plain)** — scripts_send_realistic_test, scripts_send_realistic_test_nowebhook, scripts_send_realistic_test_plain [EXTRACTED 0.95]
- **Scripts that Write to state/confirmations.json** — scripts_send_realistic_test, scripts_send_realistic_test_plain, scripts_send_farm_test_broadcast [EXTRACTED 1.00]
- **USDA Alert Generation Pipeline (surprise → predict → format)** — usda_alerts_compute_surprise, usda_alerts_dominant_surprise, usda_alerts_predict_move, usda_alerts_format_release_sms [EXTRACTED 1.00]
- **SMS Alert and Y/N Confirmation Pipeline** — workflow_evaluate, workflow_collect_reply, workflow_remind_pending, workflow_pulse_status, concept_yn_reply_loop, data_confirmations_json, concept_textbelt_sms [INFERRED 0.95]
- **Bushel/Ritchie Data Refresh and Advisor Bundle Pipeline** — workflow_refresh_bushel, workflow_build_advisor_context, script_scrape_bushel, script_refresh_ritchie, script_patch_advisor_bundle, data_bushel_json, data_advisor_context_json, data_ritchie_live_json, concept_bushel_portal [INFERRED 0.95]
- **Cloudflare Worker to Git Mirror Pattern** — concept_cloudflare_worker_dispatch, workflow_alerts_config_audit, workflow_recipients_audit, workflow_accept_order, concept_kv_source_of_truth, concept_git_as_audit_trail [INFERRED 0.90]
- **SMS Confirmation Pipeline: evaluate.py → TextBelt → Cloudflare Worker → GitHub Actions → collect_reply.py** — evaluate_py, textbelt_integration, cloudflare_worker_concept, repository_dispatch_concept, collect_reply_py [EXTRACTED 1.00]
- **IB IV Data Flow: IB Gateway → refresh_ib_iv.py → ib_iv.json → hedge.html** — ib_gateway_concept, refresh_ib_iv_py, ib_iv_json, docs_hedge_html [EXTRACTED 1.00]
- **Unified Dashboard Shell: index, hedge, home, admin, books, farm-ops all share the same CSS design system** — unified_shell_design, docs_index_html, docs_hedge_html, docs_home_html, docs_admin_html, docs_books_html, docs_farm_ops_html [EXTRACTED 1.00]

## Communities (34 total, 6 thin omitted)

### Community 0 - "Cloudflare Worker API Handlers"
Cohesion: 0.07
Nodes (80): adminAlertsConfig(), adminBroadcastStart(), adminBroadcastSubmit(), adminBroadcastTest(), adminGroups(), adminRecipients(), AdminRun, adminRunDetail() (+72 more)

### Community 1 - "SMS Broadcast & Test Scripts"
Cohesion: 0.07
Nodes (48): Confirmation Signal Key Pattern, _load(), main(), datetime, _save(), _send(), _short_id(), _build_message() (+40 more)

### Community 2 - "Grain Trading Infrastructure"
Cohesion: 0.08
Nodes (49): Bushel/Akron Grain Portal, Cloudflare Worker repository_dispatch Trigger, CME Grain Trading Schedule (Market Hours), freis-farm-bot Git Identity, Git Commits as Audit Trail, Cloudflare KV as Live Source-of-Truth, SMS Quiet Hours Configuration, SMS OTP Order Submission Flow (+41 more)

### Community 3 - "SMS Reply & Confirmation Flow"
Cohesion: 0.08
Nodes (40): Y/N Confirmation Flow, _describe_pending(), find_pending_for_phone(), load_confirmations(), main(), parse_reply(), pending_for_phone(), Return (vote, hint) where hint is a dict that may carry one of:         {"sid": (+32 more)

### Community 4 - "Advisor Context & Market Data Bundle"
Cohesion: 0.07
Nodes (39): Advisor Context Bundle (Farm P&L, Storage, Bids), Ritchie Live Bids & Storage Snapshot, USDA Market Calendar with DCA Weights, Claude Code Settings (PreToolUse Hooks), Cloudflare Worker Entry Point (index.ts), compilerOptions, allowSyntheticDefaultImports, esModuleInterop (+31 more)

### Community 5 - "Ritchie & Bushel Price Refresh"
Cohesion: 0.10
Nodes (33): Bushel Portal-Mediated Auth, _api_headers(), _centre_headers(), fetch_avg_pricing(), fetch_commodity_balances(), fetch_session_meta(), main(), _now_iso() (+25 more)

### Community 6 - "Cloudflare SMS Bot & Alert State"
Cohesion: 0.15
Nodes (28): state/alerts_state.json — Per-Signal Cooldown State, BOT_PROMPT.md — SMS Bot Coding Agent Prompt, Cloudflare Worker (freis-farm-sms-reply), Cloudflare Worker README — freis-farm-sms-reply Setup, scripts/collect_reply.py — Reply State Updater, CONFIRMATIONS.md — SMS Confirmation Flow, contracts.json — Open Bushel Contracts, docs/confirmations.json — Sanitized Confirmation State (Public) (+20 more)

### Community 7 - "USDA News & Surprise Alerts"
Cohesion: 0.12
Nodes (26): News Alerts Sent State (news_alerts_sent.json), compute_surprise(), _confidence(), _direction_word(), _dominant_surprise(), _find_report(), _fmt_band(), format_preview_sms() (+18 more)

### Community 8 - "Elevator Bid Scraping"
Cohesion: 0.14
Nodes (23): build_payload(), current_month_code(), fetch_fs_grain_html(), fetch_futures_prices(), _find_data_dir(), _find_docs_dir(), load_ritchie_baseline(), main() (+15 more)

### Community 9 - "Farm Dashboard HTML Pages"
Cohesion: 0.14
Nodes (21): Auth Gate (client-side PIN lock on dashboard pages), build_pages.py — Shared CSS Sync Tool, docs/admin.html — Freis Farm Admin Trade Orchestration, docs/books.html — Freis Farm Books, docs/devices.html — Freis Farm Device Preview, docs/farm-ops.html — Freis Farm Farm Ops, docs/hedge.html — Freis Farm Trading/Hedge Page, docs/home.html — Freis Farm Home (+13 more)

### Community 10 - "Keycloak Auth & Bushel Scraper"
Cohesion: 0.14
Nodes (18): Keycloak OIDC Split-Step Auth Flow, fetch_data(), _find_form_action(), login(), main(), make_session(), Session, Bushel (akronservices.com) scraper.  Bushel powers The Akron App / akronservices (+10 more)

### Community 11 - "Signal Evaluation & Contracts"
Cohesion: 0.18
Nodes (19): Positions JSON, Prices JSON, Signals JSON, Contract, evaluate_signals(), _load_confirmations(), load_contracts(), model() (+11 more)

### Community 12 - "Price History & News Loading"
Cohesion: 0.16
Nodes (19): _append_history(), append_news(), build_prices_snapshot(), load_history(), _load_news(), _load_news_alerts_sent(), _price_detail(), Any (+11 more)

### Community 13 - "Bushel Settlement Fetching"
Cohesion: 0.20
Nodes (17): _decode_body(), _dump_csv(), fetch_settlements_har(), fetch_settlements_live(), _hdrs(), main(), _money(), Any (+9 more)

### Community 14 - "Sales Log Construction"
Cohesion: 0.22
Nodes (16): extract_bushel_sales(), first_delivery_period(), load_live_storage(), load_static_sales(), main(), marketing_year_window(), normalize_crop(), parse_first_date() (+8 more)

### Community 15 - "Ledger & Order Processing"
Cohesion: 0.13
Nodes (16): build_ledger_snapshot(), load_orders(), load_state(), main(), process_live_orders(), Resolve a list of recipients for an alert of the given kind.      `kind` is one, Dispatch via TextBelt to every recipient for the given kind.      `kind` selects, Pass-through of sales_ledger.json with lightweight derived totals:     YTD bushe (+8 more)

### Community 16 - "Cloudflare Worker Package Config"
Cohesion: 0.14
Nodes (13): description, devDependencies, @cloudflare/workers-types, typescript, wrangler, name, private, scripts (+5 more)

### Community 17 - "Sales & Freshness Tracking"
Cohesion: 0.24
Nodes (13): Sales Log JSON, Sales Ledger JSON, feed_age(), format_alert(), hours_old(), main(), parse_iso(), datetime (+5 more)

### Community 18 - "IB Gateway Implied Volatility"
Cohesion: 0.22
Nodes (12): _find_docs_dir(), main(), _pick_atm_strike(), _pick_chain(), _pick_chain_expiry(), Path, refresh_ib_iv.py — pulls the at-the-money implied vol for the actively-modeled g, Match the chain whose tradingClass aligns with the futures contract. (+4 more)

### Community 19 - "Grain Marketing Plan & Policy Calendar"
Cohesion: 0.17
Nodes (12): big_move_news_for(), load_plan(), policy_calendar_news_for_today(), DEPRECATED as a news source — kept for reference.      Check each seasonal tranc, If a commodity moved more than the plan's big_move_pct today, emit     an item., Read docs/plan.json. Falls back to hardcoded defaults if the file     is absent, Emit a news item for any USDA report whose release date is today.     Calendar c, Emit a news item for any curated policy/geopolitical event scheduled     for tod (+4 more)

### Community 20 - "Live Position Tracking"
Cohesion: 0.20
Nodes (11): build_positions_snapshot(), _fetch_raw(), get_price(), get_price_with_date(), Open-position view over contracts.json. Pulls a live price for each     contract, Return the yfinance ticker for a commodity, optionally a specific contract., Fetch last price via yfinance. Returns (raw_price, date_str).      yfinance retu, Dollar-denominated last price, None on failure. (+3 more)

### Community 21 - "graphify Knowledge Graph Tools"
Cohesion: 0.29
Nodes (10): CLAUDE.md - graphify Rules, BFS Graph Traversal Mode, graphify build_merge Function, graphify Cluster-Only Mode (--cluster-only), DFS Graph Traversal Mode, graphify Incremental Update (--update), graphify save-result Feedback Loop, Constrained Query Vocab Expansion (+2 more)

### Community 22 - "Advisor Bundle Patch & Git State"
Cohesion: 0.25
Nodes (8): Git Commits as State Persistence, main(), merge_storage_state(), patch_advisor_bundle.py — overlay live Ritchie sidecar onto the published adviso, Return (new_storage_state, changed?). Same overlay logic as     context_builder., Refresh IB Implied Volatility, Refresh IB IV Shell Wrapper, Tranche Sell Model

### Community 23 - "Freis Farm Brand & Identity"
Cohesion: 0.62
Nodes (7): Board of Trade (Commodity Exchange), Farm Commodities (Corn/Agriculture), Freis Farm Brand Identity, Illinois State (Geographic Identity), Freis Farm Logo (Dark Theme), Freis Farm Logo (Light Theme), Freis Farm Board of Trade Logo (SVG)

### Community 24 - "Order Acceptance Scripts"
Cohesion: 0.62
Nodes (6): load_orders(), main(), parse_payload(), Any, save_orders(), validate_payload()

### Community 25 - "USDA News SMS Formatting"
Cohesion: 0.33
Nodes (6): _direction_hint(), _layman_headline(), _news_alert_message(), Rewrite the trader-shaped `title` into plain English for SMS.      The dashboard, One plain-language sentence telling a non-trader which way this     is pushing p, Build the SMS body for one news item.      If the item carries `format: "usda-pa

### Community 26 - "graphify Reference Docs"
Cohesion: 0.33
Nodes (6): Graphify Add/Watch Reference, Graphify Exports Reference, Graphify Extraction Spec Reference, Graphify GitHub and Merge Reference, Graphify Hooks Reference, Graphify Skill Definition (SKILL.md)

### Community 27 - "Price History Bootstrap"
Cohesion: 0.67
Nodes (3): fetch(), main(), Yahoo Finance Price Feed

## Knowledge Gaps
- **92 isolated node(s):** `name`, `version`, `private`, `description`, `deploy` (+87 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **6 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Sales Log JSON` connect `Sales & Freshness Tracking` to `Sales Log Construction`?**
  _High betweenness centrality (0.058) - this node is a cross-community bridge._
- **Why does `Sales Ledger JSON` connect `Sales & Freshness Tracking` to `Signal Evaluation & Contracts`?**
  _High betweenness centrality (0.058) - this node is a cross-community bridge._
- **Why does `Git Commits as State Persistence` connect `Advisor Bundle Patch & Git State` to `Signal Evaluation & Contracts`?**
  _High betweenness centrality (0.037) - this node is a cross-community bridge._
- **What connects `name`, `version`, `private` to the rest of the system?**
  _199 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Cloudflare Worker API Handlers` be split into smaller, more focused modules?**
  _Cohesion score 0.07222222222222222 - nodes in this community are weakly interconnected._
- **Should `SMS Broadcast & Test Scripts` be split into smaller, more focused modules?**
  _Cohesion score 0.06848357791754019 - nodes in this community are weakly interconnected._
- **Should `Grain Trading Infrastructure` be split into smaller, more focused modules?**
  _Cohesion score 0.07568027210884354 - nodes in this community are weakly interconnected._