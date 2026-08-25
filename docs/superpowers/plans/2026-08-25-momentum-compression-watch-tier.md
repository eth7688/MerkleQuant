# Momentum Compression Watch Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a geometry-preserving `WATCH` candidate tier with three-closed-candle EMA confirmation while keeping the existing full-window `STRICT` tier unchanged.

**Architecture:** Evaluate `STRICT` first and evaluate `WATCH` only after strict rejection. Both tiers reuse the existing pool, price stream, breakout state machine and compression identity; tier is an orthogonal audit field and never creates a duplicate event by itself.

**Tech Stack:** Python 3.12, pandas, NumPy, Flask, JSON state, native HTML/CSS/JavaScript, `unittest`.

## Global Constraints

- Preserve existing strict-tier rules, thresholds, states and calculations.
- `WATCH` independently selects the maximum anchored structural suffix, then applies the 15–100 bar gate.
- Keep two pivot highs, two pivot lows, HH/HL or LL/LH, three directional wick-touch events, contraction `<= 0.65`, and latest EMA8 distance `<= 1 ATR`.
- `WATCH` checks EMA order and inclusive-band exclusion on exactly the latest three closed candles.
- Keep one pool and one identity; tier changes do not reset `fresh_emitted` or create another event.
- `STRICT` WeCom requires `CONFIRMED`; `WATCH` allows `CONFIRMED` or `UNKNOWN`, never `CONFLICT`.
- Do not modify trading, orders, positions, risk, exchange settings or Webhook configuration.
- Do not deploy without fresh explicit confirmation; after deployment update and commit `PROGRESS.md`.

## File Map

- `momentum_compression.py`: rule profiles, independent suffix selection, tier audit fields.
- `momentum_compression_store.py`: tier validation, legacy normalization, identity-preserving reconciliation.
- `momentum_compression_service.py`, `momentum_compression_monitor.py`: tier counts and split rejection diagnostics.
- `momentum_compression_alerts.py`: tier delivery matrix, deduplication and message copy.
- `web_ui.py`: API aggregates, cards, badges and rejection sections.
- `tests/test_momentum_compression*.py`: focused and integration coverage.

---

### Task 1: Strict-first and watch-second rule evaluation

**Files:**
- Modify: `momentum_compression.py:15-410`
- Test: `tests/test_momentum_compression.py:439-930`

**Interfaces:**
- Produce `STRICT_TIER`, `WATCH_TIER`, and `WATCH_EMA_CONFIRMATION_BARS = 3`.
- Add `candidate_tier`, `strict_rejection_reasons`, `watch_rejection_reasons`, and `ema_confirmation_bars` to every public evaluation.
- Preserve signatures and LONG-then-SHORT ordering of `evaluate_side()` and `evaluate_both_sides()`.

- [ ] **Step 1: Write failing tier tests**

```python
def watch_inputs(latest_valid):
    raw = valid_compression_frame(40)
    indicators = add_compression_indicators(raw)
    indicators.loc[:, "ema8"] = 100.0
    indicators.loc[:, "ema21"] = 99.0
    indicators.loc[:, "c"] = 100.0 + indicators["atr14"] * 0.5
    indicators.iloc[-(latest_valid + 1), indicators.columns.get_loc("ema8")] = 98.0
    return raw, indicators

def test_watch_accepts_only_when_latest_three_ema_candles_pass(self):
    accepted_raw, accepted_indicators = watch_inputs(3)
    rejected_raw, rejected_indicators = watch_inputs(2)
    with patch("momentum_compression.add_compression_indicators", return_value=accepted_indicators):
        accepted = evaluate_side(
            "WATCHUSDT", "LONG", accepted_raw, 100.0,
            evaluated_at_ms=BASE_TIME, htf_alignment="UNKNOWN",
        )
    with patch("momentum_compression.add_compression_indicators", return_value=rejected_indicators):
        rejected = evaluate_side(
            "WATCHUSDT", "LONG", rejected_raw, 100.0,
            evaluated_at_ms=BASE_TIME, htf_alignment="UNKNOWN",
        )
    self.assertEqual(accepted["candidate_tier"], "WATCH")
    self.assertEqual(accepted["ema_confirmation_bars"], 3)
    self.assertTrue(accepted["strict_rejection_reasons"])
    self.assertEqual(accepted["watch_rejection_reasons"], [])
    self.assertIsNone(rejected["candidate_tier"])
    self.assertIn("EMA_DIRECTION", rejected["watch_rejection_reasons"])

def test_watch_keeps_geometry_hard_gates(self):
    indicators = add_compression_indicators(two_touch_frame())
    result = _watch_non_length_rules(indicators, "LONG", CompressionParams())
    self.assertIn("INSUFFICIENT_DIRECTIONAL_TOUCHES", result["rejection_reasons"])

def test_strict_has_priority_over_watch(self):
    result = evaluate_side(
        "STRICTUSDT", "LONG", valid_compression_frame(40), 105.0,
        evaluated_at_ms=BASE_TIME, htf_alignment="CONFIRMED",
    )
    self.assertEqual(result["candidate_tier"], "STRICT")
    self.assertEqual(result["strict_rejection_reasons"], [])
    self.assertEqual(result["watch_rejection_reasons"], [])
```

- [ ] **Step 2: Run RED verification**

Run `python -m unittest tests.test_momentum_compression.CompressionRuleTests -q`.

Expected: new assertions fail because tier fields and watch evaluation do not exist.

- [ ] **Step 3: Implement the watch rule profile**

Keep `_non_length_rules()` as strict behavior. Extract only shared geometry and latest-distance metrics, then add:

```python
STRICT_TIER = "STRICT"
WATCH_TIER = "WATCH"
WATCH_EMA_CONFIRMATION_BARS = 3

def _watch_non_length_rules(frame, side, params, *, common=None):
    metrics = _geometry_rule_metrics(frame, side, params, common=common)
    reasons = list(metrics["rejection_reasons"])
    recent = frame.iloc[-WATCH_EMA_CONFIRMATION_BARS:]
    if len(recent) < WATCH_EMA_CONFIRMATION_BARS:
        reasons.append("EMA_DIRECTION")
    else:
        ordered = recent["ema8"] > recent["ema21"] if side == "LONG" else recent["ema8"] < recent["ema21"]
        if not bool(ordered.all()):
            reasons.append("EMA_DIRECTION")
        ema_low = recent[["ema8", "ema21"]].min(axis=1)
        ema_high = recent[["ema8", "ema21"]].max(axis=1)
        if bool(((recent["c"] >= ema_low) & (recent["c"] <= ema_high)).any()):
            reasons.append("CLOSE_IN_EMA_BAND")
    return {**metrics, "rejection_reasons": list(dict.fromkeys(reasons))}
```

Add `_maximal_watch_structural_suffix()` that searches anchored suffixes using watch non-length rules, returns the first fully passing maximum suffix, and uses truthful best-rejected fallback only for diagnostics.

- [ ] **Step 4: Add strict-first public evaluation**

```python
strict = _evaluate_prepared_side(..., tier=STRICT_TIER)
if strict["state"] != "REJECTED":
    return _with_tier(strict, STRICT_TIER, [], [], strict["compression_bars"])
watch = _evaluate_prepared_side(..., tier=WATCH_TIER)
if watch["state"] != "REJECTED":
    return _with_tier(watch, WATCH_TIER, strict["rejection_reasons"], [], 3)
return _with_tier(
    watch, None, strict["rejection_reasons"], watch["rejection_reasons"], 3,
)
```

Do not add tier to `compression_identity()`.

- [ ] **Step 5: Run GREEN and strict oracle verification**

Run `python -m unittest tests.test_momentum_compression -q`.

Expected: all tests pass; strict calculations still match the independent oracle after excluding only the new audit fields from legacy field equality.

- [ ] **Step 6: Commit**

```powershell
git add momentum_compression.py tests/test_momentum_compression.py
git commit -m "feat: add compression watch tier rules"
```

---

### Task 2: Persist tier facts and preserve one identity

**Files:**
- Modify: `momentum_compression_store.py:96-470`
- Test: `tests/test_momentum_compression_store.py:44-413`

**Interfaces:**
- Consume Task 1 tier fields.
- Normalize old pool, episode and event structures to `STRICT` without changing `STATE_VERSION`.
- Preserve `reconcile_structure_scan()` and `apply_live_prices()` signatures.

- [ ] **Step 1: Write failing migration and transition tests**

```python
def test_legacy_pool_item_without_tier_loads_as_strict(self):
    state = default_state()
    legacy = evaluation(compression_id="legacy", candidate_tier="STRICT")
    state, _ = reconcile_structure_scan(state, [legacy], 1_000)
    for key in ("candidate_tier", "strict_rejection_reasons", "watch_rejection_reasons", "ema_confirmation_bars"):
        state["pool"]["legacy"].pop(key, None)
    with TemporaryDirectory() as folder:
        path = Path(folder) / "state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        loaded = load_compression_state(path)
    self.assertEqual(loaded["pool"]["legacy"]["candidate_tier"], "STRICT")

def test_watch_to_strict_same_identity_never_reemits(self):
    state, _ = reconcile_structure_scan(default_state(), [evaluation(candidate_tier="WATCH")], 1_000)
    state, first = apply_live_prices(state, {"TESTUSDT": 110.6}, 2_000)
    state, result = reconcile_structure_scan(state, [evaluation(candidate_tier="STRICT")], 3_000)
    self.assertEqual(state["pool"]["long-episode-1"]["candidate_tier"], "STRICT")
    self.assertTrue(state["pool"]["long-episode-1"]["fresh_emitted"])
    self.assertEqual(len(first), 1)
    self.assertEqual(result["fresh_events"], [])
```

Also cover strict-to-watch downgrade, changed-window replacement, invalid tier values, and immutable event-time tier.

- [ ] **Step 2: Run RED verification**

Run `python -m unittest tests.test_momentum_compression_store -q`.

Expected: new tests fail on missing normalization/validation.

- [ ] **Step 3: Normalize and validate additive fields**

```python
def _with_tier_defaults(item):
    normalized = copy.deepcopy(item)
    normalized.setdefault("candidate_tier", "STRICT")
    normalized.setdefault("strict_rejection_reasons", [])
    normalized.setdefault("watch_rejection_reasons", [])
    normalized.setdefault("ema_confirmation_bars", normalized.get("compression_bars", 0))
    return normalized
```

Validate tier membership, list-of-string reasons and positive integer confirmation bars. Normalize nested state records before `_validate_state()` without rewriting the file merely by reading it.

- [ ] **Step 4: Preserve refreshed tier and immutable breakout tier**

Same-identity reconciliation updates current tier/audit fields but preserves `first_seen_at`, breakout facts, `fresh_emitted`, state and emitted registry. `_event()` deep-copies the structure so later tier changes cannot alter the event.

- [ ] **Step 5: Run GREEN and commit**

```powershell
python -m unittest tests.test_momentum_compression_store -q
git add momentum_compression_store.py tests/test_momentum_compression_store.py
git commit -m "feat: persist compression candidate tiers"
```

---

### Task 3: Expose split tier and rejection diagnostics

**Files:**
- Modify: `momentum_compression_service.py:149-292`
- Modify: `momentum_compression_monitor.py:41-500`
- Test: `tests/test_momentum_compression_service.py:135-415`
- Test: `tests/test_momentum_compression_monitor.py`

**Interfaces:**
- Produce `_compression_diagnostics(evaluations: list[dict]) -> dict` as the single aggregation implementation used by the scan report.
- Produce `tier_counts`, `strict_rejection_counts`, and `watch_rejection_counts` in scan reports and monitor status.
- Retain `rejection_counts` as a compatibility alias of `strict_rejection_counts` during rollout.

- [ ] **Step 1: Write failing aggregation tests**

```python
def test_scan_reports_tier_counts_and_split_rejections(self):
    rows = [
        {**eligible_evaluation(symbol="STRICTUSDT"), "candidate_tier": "STRICT", "strict_rejection_reasons": [], "watch_rejection_reasons": []},
        {**eligible_evaluation(symbol="WATCHUSDT"), "candidate_tier": "WATCH", "strict_rejection_reasons": ["CLOSE_IN_EMA_BAND"], "watch_rejection_reasons": []},
        {"state": "REJECTED", "candidate_tier": None, "strict_rejection_reasons": ["CLOSE_IN_EMA_BAND"], "watch_rejection_reasons": ["INSUFFICIENT_PIVOTS"]},
    ]
    report = _compression_diagnostics(rows)
    self.assertEqual(report["tier_counts"], {"STRICT": 1, "WATCH": 1})
    self.assertEqual(report["strict_rejection_counts"], {"CLOSE_IN_EMA_BAND": 2})
    self.assertEqual(report["watch_rejection_counts"], {"INSUFFICIENT_PIVOTS": 1})
    self.assertEqual(report["rejection_counts"], report["strict_rejection_counts"])
```

Add monitor tests proving failed scans preserve all last-successful maps and callers receive defensive copies.

- [ ] **Step 2: Run RED verification**

Run `python -m unittest tests.test_momentum_compression_service tests.test_momentum_compression_monitor -q`.

Expected: failures identify missing split diagnostic fields.

- [ ] **Step 3: Aggregate explicit evaluation fields**

```python
tier_counts = {"STRICT": 0, "WATCH": 0}
strict_rejection_counts = {}
watch_rejection_counts = {}
for row in evaluations:
    tier = row.get("candidate_tier")
    if row["state"] in _ELIGIBLE_STATES and tier in tier_counts:
        tier_counts[tier] += 1
    for reason in row.get("strict_rejection_reasons", []):
        strict_rejection_counts[reason] = strict_rejection_counts.get(reason, 0) + 1
    for reason in row.get("watch_rejection_reasons", []):
        watch_rejection_counts[reason] = watch_rejection_counts.get(reason, 0) + 1
```

Return all three maps plus `rejection_counts = strict_rejection_counts`.

- [ ] **Step 4: Snapshot successful diagnostics in the monitor**

Sanitize keys as strings and counts as nonnegative real integers. Replace snapshots only after a successful scan under `_lifecycle_lock`; failed scans keep the previous successful values.

- [ ] **Step 5: Run GREEN and commit**

```powershell
python -m unittest tests.test_momentum_compression_service tests.test_momentum_compression_monitor -q
git add momentum_compression_service.py momentum_compression_monitor.py tests/test_momentum_compression_service.py tests/test_momentum_compression_monitor.py
git commit -m "feat: expose compression tier diagnostics"
```

---

### Task 4: Tier-aware alert delivery and immutable message facts

**Files:**
- Modify: `momentum_compression_alerts.py:106-360`
- Test: `tests/test_momentum_compression_alerts.py:40-250`

**Interfaces:**
- Consume event-time `candidate_tier` and `htf_alignment`.
- Preserve alert IDs, compression-ID idempotency, retry state and indeterminate-send protection.

- [ ] **Step 1: Write failing delivery-matrix tests**

```python
def test_wechat_delivery_matrix_is_tier_aware(self):
    cases = (
        ("STRICT", "CONFIRMED", True),
        ("STRICT", "UNKNOWN", False),
        ("STRICT", "CONFLICT", False),
        ("WATCH", "CONFIRMED", True),
        ("WATCH", "UNKNOWN", True),
        ("WATCH", "CONFLICT", False),
    )
    for tier, alignment, expected in cases:
        with self.subTest(tier=tier, alignment=alignment):
            with TemporaryDirectory() as folder:
                events_path = Path(folder) / "events.jsonl"
                state_path = Path(folder) / "state.json"
                source = fresh(alignment=alignment)
                source["structure"]["candidate_tier"] = tier
                append_compression_alerts(events_path, state_path, [source], 1_000)
                state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(bool(state["delivery_queue"]), expected)

def test_watch_then_strict_same_identity_never_appends_twice(self):
    first = fresh(alignment="UNKNOWN", compression_id="same")
    second = fresh(alignment="CONFIRMED", compression_id="same")
    first["structure"]["candidate_tier"] = "WATCH"
    second["structure"]["candidate_tier"] = "STRICT"
    with TemporaryDirectory() as folder:
        events_path = Path(folder) / "events.jsonl"
        state_path = Path(folder) / "state.json"
        created = append_compression_alerts(events_path, state_path, [first], 1_000)
        created += append_compression_alerts(events_path, state_path, [second], 2_000)
    self.assertEqual(len(created), 1)
```

Add message assertions for the two Chinese titles, confirmation bars, touch count, contraction ratio and HTF label.

- [ ] **Step 2: Run RED verification**

Run `python -m unittest tests.test_momentum_compression_alerts -q`.

Expected: failures show missing tier policy and message fields.

- [ ] **Step 3: Normalize old events and centralize queue eligibility**

```python
def _wechat_eligible(event):
    structure = event["structure"]
    tier = structure.get("candidate_tier", "STRICT")
    alignment = event["htf_alignment"]
    if tier == "STRICT":
        return alignment == "CONFIRMED"
    return alignment in {"CONFIRMED", "UNKNOWN"}
```

Normalize missing tier fields to strict before validation. Use this predicate for initial append, durable-outbox recovery, persisted delivery-queue validation and retry reconciliation; replace the current hard-coded `event["htf_alignment"] == "CONFIRMED"` queue invariant with `_wechat_eligible(event)`. `CONFLICT` never enters the queue.

- [ ] **Step 4: Render tier-aware WeCom markdown**

`format_compression_wechat_markdown()` reads the immutable event structure, emits `动能压缩｜严格级突破` or `动能压缩｜观察级突破`, and includes approved audit facts. Preserve `_safe_error()` secret redaction and payload-size checks.

- [ ] **Step 5: Run GREEN and commit**

```powershell
python -m unittest tests.test_momentum_compression_alerts -q
git add momentum_compression_alerts.py tests/test_momentum_compression_alerts.py
git commit -m "feat: deliver tiered compression alerts"
```

---

### Task 5: Render tiers in authenticated and internal status

**Files:**
- Modify: `web_ui.py:480-510,1880-2020,4482-4570`
- Test: `tests/test_momentum_compression_integration.py:29-410`
- Test: `tests/test_momentum_compression_admin.py:8-100`

**Interfaces:**
- Consume monitor `tier_counts`, `strict_rejection_counts`, and `watch_rejection_counts`.
- Expose aggregates to authenticated users; internal loopback status exposes aggregates only, never pool symbols or alert details.

- [ ] **Step 1: Write failing API and renderer tests**

```python
def test_status_exposes_tier_counts_and_split_rejections(self):
    self._login()
    monitor = {
        "running": True,
        "tier_counts": {"STRICT": 2, "WATCH": 5},
        "strict_rejection_counts": {"CLOSE_IN_EMA_BAND": 9},
        "watch_rejection_counts": {"INSUFFICIENT_PIVOTS": 4},
    }
    with patch.object(web_ui, "load_compression_state", return_value={"pool": {}, "episodes": {}, "last_scan_failures": []}), \
         patch.object(web_ui._compression_monitor, "status", return_value=monitor), \
         patch.object(web_ui, "compression_sound_available_ids", return_value=set()), \
         patch.object(web_ui, "compression_delivery_statuses", return_value={}):
        payload = self.client.get("/api/compression/status").get_json()
    self.assertEqual(payload["tier_counts"], {"STRICT": 2, "WATCH": 5})
    self.assertEqual(payload["strict_rejection_counts"], {"CLOSE_IN_EMA_BAND": 9})
    self.assertEqual(payload["watch_rejection_counts"], {"INSUFFICIENT_PIVOTS": 4})

def test_renderer_labels_strict_and_watch_rows(self):
    result = render_compression_payload({
        "monitor": {"auto_enabled": True, "running": True},
        "scan": {"scanned": 10, "eligible": 2, "errors": 0},
        "tier_counts": {"STRICT": 1, "WATCH": 1},
        "strict_rejection_counts": {"CLOSE_IN_EMA_BAND": 8},
        "watch_rejection_counts": {"INSUFFICIENT_PIVOTS": 3},
        "pool_rows": [
            {"symbol": "STRICTUSDT", "side": "LONG", "state": "PRE_BREAKOUT", "candidate_tier": "STRICT"},
            {"symbol": "WATCHUSDT", "side": "SHORT", "state": "COMPRESSION_ACTIVE_SHORT", "candidate_tier": "WATCH"},
        ],
    })
    html = result["stats"]["innerHTML"] + result["main"]["innerHTML"]
    self.assertIn("严格级", html)
    self.assertIn("观察级", html)
    self.assertIn("严格级拒绝统计", html)
    self.assertIn("观察级拒绝统计", html)
```

Add an internal API test proving aggregate fields are present while `pool_rows`, symbols and Webhook facts remain absent.

- [ ] **Step 2: Run RED verification**

Run `python -m unittest tests.test_momentum_compression_integration tests.test_momentum_compression_admin -q`.

Expected: new API and renderer assertions fail.

- [ ] **Step 3: Extend API payloads without changing authorization**

Update `_run_compression_scan()`, `compression_status()` and `_compression_internal_status()` to copy sanitized aggregate maps. Keep login, admin role, loopback and failure-detail restrictions unchanged.

- [ ] **Step 4: Add cards, tier badges and split rejection sections**

Keep the existing LONG/SHORT tables and add one `等级` column:

```javascript
function compressionTier(row){
  return String((row||{}).candidate_tier||'STRICT')==='WATCH'?'WATCH':'STRICT';
}
function compressionTierLabel(row){
  return compressionTier(row)==='WATCH'?'观察级':'严格级';
}
```

Add strict/watch count cards, two descending-count rejection sections, and event-time tier labels to current/terminal breakout rows. Use existing CSS primitives and no reflow-triggering animation.

- [ ] **Step 5: Run GREEN and commit**

```powershell
python -m unittest tests.test_momentum_compression_integration tests.test_momentum_compression_admin -q
git add web_ui.py tests/test_momentum_compression_integration.py tests/test_momentum_compression_admin.py
git commit -m "feat: show compression candidate tiers"
```

---

### Task 6: Full verification, market replay and deployment handoff

**Files:**
- Verify: all files modified in Tasks 1–5.
- Modify only after actual deployment: `PROGRESS.md`.

**Interfaces:**
- Produce a reviewed local branch and evidence for a separate production confirmation.

- [ ] **Step 1: Run the complete local test suite**

```powershell
$log = Join-Path $env:TEMP 'compression_watch_full_tests.log'
python -m unittest discover -s tests -q *> $log
$code = $LASTEXITCODE
Get-Content -LiteralPath $log -Tail 12
if ($code -ne 0) { exit $code }
```

Expected: zero failures and zero errors.

- [ ] **Step 2: Compile and inspect the complete diff**

```powershell
python -m py_compile momentum_compression.py momentum_compression_store.py momentum_compression_service.py momentum_compression_monitor.py momentum_compression_alerts.py web_ui.py
git diff --check main...HEAD
git status --short --branch
git diff --stat main...HEAD
```

Expected: compilation and diff check exit 0; only the two known runtime lock files may remain untracked.

- [ ] **Step 3: Run read-only live market replay**

Run the production evaluator without saving state or invoking alert callbacks. Print:

```text
symbols, evaluations, fetch_failures,
strict_candidates, watch_candidates,
strict_rejection_counts, watch_rejection_counts,
duplicate_compression_ids
```

Expected: `fetch_failures=0`, `duplicate_compression_ids=0`, and every candidate has 15–100 bars plus complete tier audit fields. Candidate counts are evidence, not fixed assertions.

- [ ] **Step 4: Review against the approved design**

Check every requirement in `docs/superpowers/specs/2026-08-25-momentum-compression-watch-tier-design.md`. Verify no trading file, Webhook setting, database schema or unrelated UI changed. Invoke the requesting-code-review workflow and resolve every actionable finding.

- [ ] **Step 5: Commit verification corrections only when needed**

```powershell
git add momentum_compression.py momentum_compression_store.py momentum_compression_service.py momentum_compression_monitor.py momentum_compression_alerts.py web_ui.py tests
git commit -m "test: verify compression watch tier integration"
```

Skip this step when there are no corrections; never create an empty commit.

- [ ] **Step 6: Stop for explicit production confirmation**

Report commits, full test count, replay counts and review result. Deployment can generate real observation-level WeCom alerts and must not begin without confirmation.

- [ ] **Step 7: After confirmation, back up and deploy**

Verify `<deploy-dir>` and back up at least:

```text
momentum_compression.py
momentum_compression_store.py
momentum_compression_service.py
momentum_compression_monitor.py
momentum_compression_alerts.py
web_ui.py
momentum_compression_state.json
momentum_compression_events.jsonl if present
momentum_compression_alert_state.json if present
```

Upload changed runtime files to a timestamped `/tmp/axiom-compression-watch-tier-*` directory, compare SHA256, compile with `<deploy-dir>/venv/bin/python3`, atomically replace targets and restart only `macd-bot`.

- [ ] **Step 8: Verify production and record deployment**

Require fresh evidence:

```text
macd-bot=active
price_stream_status=connected
last_error=""
one complete scan with scanned>0 and errors=0
strict/watch counts present
state reload succeeds
duplicate compression identities=0
WeCom queue contains only approved tier/alignment combinations
recent journal abnormal-keyword count=0
```

Do not send a fabricated signal. Append change summary, backup path, deployed files/hashes, restart, scan, alert-queue and rollback evidence to `PROGRESS.md`, then commit:

```powershell
git add PROGRESS.md
git commit -m "docs: record compression watch tier deployment"
```
