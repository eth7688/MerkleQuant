# 动能压缩扫描可靠性修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 15M 动能压缩全市场扫描每轮只获取一次全量价格，并由管理员后台安全控制和查看自动扫描。

**Architecture:** 服务层在扫描开始时获取一次 Binance Futures 全量价格快照，逐币 15M K 线评估复用该快照。监控器保存扫描时序并暴露运行/超时状态；`web_ui` 提供仅限 `127.0.0.1` 的内部状态/开关接口，管理员后台只代理这些内部接口。

**Tech Stack:** Python 3.12、Flask、requests、unittest、原生 JavaScript。

## Global Constraints

- 保持 Binance U 本位 USDT 永续、24h USDT 成交额 `>= 1,000,000` 的全量范围。
- 不改候选池判定、突破判定、交易执行、企业微信设置或 Webhook。
- 价格快照或单币数据缺失时，不得以空结果删除已有池条目。
- 后台入口必须经过现有管理员认证；内部 `web_ui` 接口只允许 `127.0.0.1`，且不返回凭证。
- 线上部署后必须更新本地 `PROGRESS.md`。

---

### Task 1: 扫描价格快照与运行状态

**Files:**
- Modify: `momentum_compression_service.py`
- Modify: `momentum_compression_monitor.py`
- Test: `tests/test_momentum_compression_service.py`
- Test: `tests/test_momentum_compression_monitor.py`

**Interfaces:** `fetch_live_prices(*, get=requests.get) -> dict[str, float]`; `_scan_symbol(symbol, *, live_price: float, evaluated_at_ms: int)`; report fields `started_at`, `finished_at`, `duration_ms`; monitor fields `scan_started_at`, `scan_duration_ms`, `scan_overdue`.

- [ ] **Step 1: Write the failing service tests.** Add one test with `AUSDT` and `BUSDT` that mocks `fetch_live_prices` as `{AUSDT: 10.0, BUSDT: 20.0}`, asserts it runs once, and asserts `_scan_symbol` receives those two `live_price` keyword values. Add a second test where `BUSDT` lacks a price and its existing pool item remains with `data_status == "unavailable"`.
- [ ] **Step 2: Verify RED.** Run `python -m unittest tests.test_momentum_compression_service.CompressionScanFailureIsolationTests -v`; it must fail because the bulk-price interface and keyword do not exist.
- [ ] **Step 3: Implement minimally.** Add `fetch_live_prices` using one `GET /fapi/v1/ticker/price`, retaining only finite positive prices. Fetch once before the executor. Submit `_scan_symbol(symbol, live_price=prices[symbol], evaluated_at_ms=now_ms)` only for valid price symbols; add price-missing symbols to `failed_symbols`. Record `started_at` before network work and `finished_at`/`duration_ms` before return. Preserve the successful-symbol rule so an unscanned item cannot be removed from the pool.
- [ ] **Step 4: Write failing monitor tests.** Inject a scan return `{evaluated_at: 1000, started_at: 900, finished_at: 1000, duration_ms: 100, events: []}` and assert start/duration fields. Hold the scan lock and assert `scan_overdue` changes only after injected time reaches start plus 900,000 ms.
- [ ] **Step 5: Verify RED.** Run `python -m unittest tests.test_momentum_compression_monitor -v`; it must fail because the fields are absent.
- [ ] **Step 6: Implement monitor state.** Set start before invoking the service, retain finish/duration after completion, expose an overdue flag only when the existing non-blocking scan lock is held for at least 15 minutes. Do not allow a second scheduled scan while the lock is held.
- [ ] **Step 7: Verify and commit.** Run `python -m unittest tests.test_momentum_compression_service tests.test_momentum_compression_monitor -v`; expect all passing. Commit service, monitor and their tests as `fix: speed compression market scans`.

### Task 2: 管理员后台压缩扫描开关

**Files:**
- Modify: `admin_server.py`
- Test: `tests/test_momentum_reflow_admin.py`

**Interfaces:** administrator-only `/api/compression/settings` GET/POST; `web_ui` adds loopback-only `/internal/compression/status` GET and `/internal/compression/automation` POST; the latter accepts only `{"enabled": bool}`.

- [ ] **Step 1: Write failing admin and loopback tests.** Assert unauthenticated clients cannot access `/api/compression/settings`; authenticated GET returns a whitelisted safe monitor payload; authenticated POST with `{"enabled": true}` calls local `POST /internal/compression/automation` with that exact JSON and timeout 3. Add `web_ui` tests proving non-loopback requests receive 403 and loopback requests can read status or set the Boolean switch. Assert HTML contains `data-page="compression"` and `renderCompression`.
- [ ] **Step 2: Verify RED.** Run `python -m unittest tests.test_momentum_reflow_admin -v`; it must fail because the route and renderer are absent.
- [ ] **Step 3: Implement loopback and protected proxy routes.** In `web_ui`, add loopback-only GET/POST internal routes that call the same monitor status/switch methods as the external routes, but reject any `request.remote_addr` other than `127.0.0.1` or `::1`. In `admin_server`, add `@admin_required` GET/POST `/api/compression/settings`; GET obtains internal local state, whitelists monitor/scan fields only, and returns 503 when unavailable. POST requires exactly Boolean `enabled`, forwards it to the internal endpoint, and returns the safe response. Do not read alert settings or any Webhook.
- [ ] **Step 4: Implement the admin panel.** Add a sidebar page and `renderCompression(el)`. Show auto state, engine state, last/next scan, current scan, duration, scanned, eligible, errors and last error. Disable the checkbox during save and restore its previous value on failure. Reuse the `renderReflow` stale-render-generation pattern without dependencies.
- [ ] **Step 5: Verify and commit.** Run `python -m unittest tests.test_momentum_reflow_admin -v`; expect all passing. Commit admin and test files as `feat: add compression controls to admin`.

### Task 3: Verify and deploy

**Files:**
- Modify: `PROGRESS.md`

- [ ] **Step 1: Local verification.** Run Python compilation for service, monitor, admin and web UI; run `python -m unittest discover -s tests -q`; run `git diff --check`. Every command must exit 0.
- [ ] **Step 2: Independent review.** Review the diff against this plan for full-universe coverage, pool preservation after partial failure, admin authorization, no credential exposure and no trading change. Resolve blocking findings, then repeat local verification.
- [ ] **Step 3: Confirm deployment.** State modified files, backup target and restart scope. Wait for explicit confirmation before server writes.
- [ ] **Step 4: Deploy and record.** Back up server files; upload only `momentum_compression_service.py`, `momentum_compression_monitor.py`, `web_ui.py` and `admin_server.py`; stage compilation; atomically replace; restart `macd-bot` and `macd-admin`; verify both services and protected endpoints; append backup, files, restart and verification evidence to `PROGRESS.md`.
