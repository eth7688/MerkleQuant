# Bitget 动能回流扫描源 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Bitget USDT futures the sole live data source for the momentum-reflow scanner, including approved commodity and FX RWA instruments while excluding equities and unknown RWA instruments.

**Architecture:** `momentum_reflow.py` owns Bitget contract classification and universe discovery. Existing generic Bitget candle helpers in `screener.py` remain the sole candle transport, so hourly and daily confirmation run against the same Bitget `USDT-FUTURES` candles. A source marker in the scan ledger causes the legacy Binance event cursor set to be reset once, without modifying daily displayed signals or trading records.

**Tech Stack:** Python 3.12, requests, pandas, unittest, Flask, Bitget V2 public Mix Market API.

## Global Constraints

- Do not change Predicta/RJ entry conditions, position sizing, stop loss, take profit, or order placement.
- Use only Bitget public `USDT-FUTURES` contracts, tickers, candles and history-candles endpoints for this scanner.
- Non-RWA perpetuals are crypto; RWA instruments enter only through explicit commodity/FX base-asset allowlists.
- Equities, ETFs, stablecoins, leveraged tokens, and unknown RWA instruments fail closed.
- Keep the existing 2,000,000 USDT 24-hour-volume threshold and existing 1H closed-candle first-reflow state machine.
- Preserve `demo_bot_config.json`, positions, trades, account database and displayed historical signals during deployment.

---

### Task 1: Bitget universe classification and source-safe ledger migration

**Files:**
- Modify: `momentum_reflow.py:33-45, 379-428, 542-597`
- Modify: `tests/test_momentum_reflow.py: ReflowScanServiceTests`

**Interfaces:**
- Produces `classify_bitget_contract(row: dict) -> str | None`.
- Produces `fetch_futures_universe() -> tuple[list[str], dict[str, float], dict[str, str]]` sourced only from Bitget.
- Consumes Bitget contract rows with `symbol`, `baseCoin`, `quoteCoin`, `symbolType`, `symbolStatus`, and `isRwa`.
- Persists `ledger["source"] == "bitget_usdt_futures"`; legacy ledgers without this marker are converted to `{version: LEDGER_VERSION, source: ..., symbols: {}}` before symbols are scanned.

- [ ] **Step 1: Write the failing universe/classification tests**

```python
@patch("momentum_reflow.requests.get")
def test_bitget_universe_keeps_crypto_commodity_and_fx_but_rejects_equity_and_unknown_rwa(self, get):
    contracts = Mock(); contracts.raise_for_status.return_value = None
    contracts.json.return_value = {"data": [
        {"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "NO"},
        {"symbol": "XAUUSDT", "baseCoin": "XAU", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "YES"},
        {"symbol": "EURUSDT", "baseCoin": "EUR", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "YES"},
        {"symbol": "TSLAUSDT", "baseCoin": "TSLA", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "YES"},
        {"symbol": "UNKNOWNUSDT", "baseCoin": "UNKNOWN", "quoteCoin": "USDT", "symbolType": "perpetual", "symbolStatus": "normal", "isRwa": "YES"},
    ]}
    tickers = Mock(); tickers.raise_for_status.return_value = None
    tickers.json.return_value = {"data": [{"symbol": s, "usdtVolume": "2000000"} for s in ["BTCUSDT", "XAUUSDT", "EURUSDT", "TSLAUSDT", "UNKNOWNUSDT"]]}
    get.side_effect = [contracts, tickers]

    symbols, _, types = fetch_futures_universe()

    self.assertEqual(symbols, ["BTCUSDT", "XAUUSDT", "EURUSDT"])
    self.assertEqual(types, {"BTCUSDT": "CRYPTO", "XAUUSDT": "COMMODITY", "EURUSDT": "FX"})
    self.assertIn("api.bitget.com/api/v2/mix/market/contracts", get.call_args_list[0].args[0])
```

```python
def test_legacy_binance_ledger_resets_before_bitget_scan(self):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ledger.json"
        save_ledger(path, {"version": 1, "symbols": {"BTCUSDT": make_waiting_state("LONG")}})
        ledger = momentum_reflow.prepare_bitget_ledger(load_ledger(path))
    self.assertEqual(ledger["source"], "bitget_usdt_futures")
    self.assertEqual(ledger["symbols"], {})
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `python -m unittest tests.test_momentum_reflow.ReflowScanServiceTests.test_bitget_universe_keeps_crypto_commodity_and_fx_but_rejects_equity_and_unknown_rwa tests.test_momentum_reflow.ReflowScanServiceTests.test_legacy_binance_ledger_resets_before_bitget_scan -v`

Expected: FAIL because the current Binance universe and ledger contain no Bitget source classifier or source marker.

- [ ] **Step 3: Implement the minimum classifier, Bitget universe and ledger preparation**

```python
BITGET_BASE = "https://api.bitget.com"
BITGET_PRODUCT_TYPE = "USDT-FUTURES"
BITGET_LEDGER_SOURCE = "bitget_usdt_futures"
COMMODITY_BASE_ASSETS = {"XAU", "XAG", "XAUT", "PAXG", "WTI", "BRENT"}
FX_BASE_ASSETS = {"EUR", "GBP", "AUD", "JPY", "CAD", "CHF", "NZD", "TRY", "BRL", "ZAR", "RUB", "UAH", "PLN", "RON", "ARS"}

def classify_bitget_contract(row: dict) -> str | None:
    base = str(row.get("baseCoin", "")).upper()
    if row.get("quoteCoin") != "USDT" or row.get("symbolType") != "perpetual" or row.get("symbolStatus") != "normal":
        return None
    if base in STABLE_BASE_ASSETS or base.endswith(LEVERAGED_MARKERS):
        return None
    if str(row.get("isRwa", "NO")).upper() != "YES":
        return "CRYPTO"
    if base in COMMODITY_BASE_ASSETS:
        return "COMMODITY"
    if base in FX_BASE_ASSETS:
        return "FX"
    return None

def prepare_bitget_ledger(ledger: dict) -> dict:
    if ledger.get("source") == BITGET_LEDGER_SOURCE:
        return ledger
    return {"version": LEDGER_VERSION, "source": BITGET_LEDGER_SOURCE, "symbols": {}}
```

Use `GET /api/v2/mix/market/contracts?productType=USDT-FUTURES` and `GET /api/v2/mix/market/tickers?productType=USDT-FUTURES`. Parse `usdtVolume` first, then `quoteVolume`; include only values greater than or equal to `REFLOW_MIN_VOLUME_USDT`.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python -m unittest tests.test_momentum_reflow.ReflowScanServiceTests -v`

Expected: PASS; the existing inclusive-volume and leveraged-token tests are updated to validate the Bitget payload shape.

- [ ] **Step 5: Commit**

```bash
git add momentum_reflow.py tests/test_momentum_reflow.py
git commit -m "feat: source reflow universe from Bitget"
```

### Task 2: Route initial and incremental candle reads through Bitget

**Files:**
- Modify: `momentum_reflow.py:457-539`
- Modify: `tests/test_momentum_reflow.py: ReflowScanServiceTests`

**Interfaces:**
- Consumes existing `screener.fetch_klines()` and `screener.fetch_klines_range()`.
- `_scan_symbol(symbol: str, old_state: dict, existing: bool)` continues to return `(state, candidate, initialized, worker_error)`.
- Every hourly and daily call sets `exchange="bitget"`, `market_type="futures"`, `testnet=False`, and `closed_only=True` where supported.

- [ ] **Step 1: Write the failing Kline-source test**

```python
@patch("momentum_reflow.fetch_klines_range")
@patch("momentum_reflow.fetch_klines")
@patch("momentum_reflow.fetch_futures_universe")
def test_initial_and_incremental_reflow_reads_use_bitget_usdt_futures(self, universe, latest, ranged):
    universe.return_value = (["NEWUSDT"], {"NEWUSDT": 9_000_000.0}, {"NEWUSDT": "CRYPTO"})
    latest.side_effect = lambda symbol, interval, *args, **kwargs: make_closed_hourly_history(symbol) if interval == "1h" else make_closed_daily_history(symbol)
    ranged.return_value = make_return_window_hourly_history("XAUUSDT")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "ledger.json"
        save_ledger(path, {"version": 1, "source": "bitget_usdt_futures", "symbols": {"XAUUSDT": make_waiting_state("LONG")}})
        scan_momentum_reflow(path, max_workers=1)
    latest.assert_any_call("NEWUSDT", "1h", 1000, exchange="bitget", closed_only=True, market_type="futures", testnet=False)
    latest.assert_any_call("XAUUSDT", "1d", 40, exchange="bitget", closed_only=True, market_type="futures", testnet=False)
    ranged.assert_called_once_with("XAUUSDT", "1h", 0, exchange="bitget", market_type="futures", testnet=False)
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m unittest tests.test_momentum_reflow.ReflowScanServiceTests.test_initial_and_incremental_reflow_reads_use_bitget_usdt_futures -v`

Expected: FAIL because the current scanner passes `exchange="binance"`.

- [ ] **Step 3: Change only the Kline source arguments**

Replace the three `exchange="binance"` arguments in `_scan_symbol()` with `exchange="bitget"`. Do not alter the state machine, indicator calculations, daily confirmation or quantity logic.

- [ ] **Step 4: Run the focused test and full reflow suite**

Run: `python -m unittest tests.test_momentum_reflow -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add momentum_reflow.py tests/test_momentum_reflow.py
git commit -m "feat: fetch reflow candles from Bitget"
```

### Task 3: Regression validation and server deployment

**Files:**
- Modify: `PROGRESS.md`
- Deploy only: `momentum_reflow.py`

**Interfaces:**
- Verifies Flask scanner continues to provide `/api/reflow/automation/status` without changing its schema.
- Preserves all execution and account state files.

- [ ] **Step 1: Run static and full regression verification**

Run:

```powershell
python -m py_compile momentum_reflow.py momentum_reflow_dashboard.py web_ui.py admin_server.py
python -m unittest discover -s tests -q
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 2: Run a public Bitget smoke check without writing server state**

Run a local read-only request to `https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES`; verify `XAUUSDT` classifies as `COMMODITY`, a crypto contract classifies as `CRYPTO`, and a stock RWA such as `TSLAUSDT` classifies as rejected.

- [ ] **Step 3: Back up and deploy the one changed production file**

On the server, create `<deploy-dir>/backups/bitget_reflow_YYYYMMDD_HHMMSS/` containing `momentum_reflow.py`, `demo_bot_config.json`, `positions_<uid>.json`, `trades_<uid>.jsonl`, and `axiom_accounts.db`. Upload only `momentum_reflow.py` after matching SHA256 and compiling it in a remote staging directory. Install the verified file, compile it in `<deploy-dir>`, and restart `macd-bot`. Do not restart `macd-admin` because no admin code changes.

- [ ] **Step 4: Verify server behavior and data preservation**

Run:

```bash
systemctl is-active macd-bot
curl -fsS http://127.0.0.1:5000/api/reflow/automation/status
journalctl -u macd-bot --since '10 minutes ago' --no-pager
sha256sum demo_bot_config.json positions_<uid>.json trades_<uid>.jsonl axiom_accounts.db
```

Expected: `macd-bot` is `active`; status is HTTP 200 with an empty `last_auto_error`; no new import, syntax or traceback errors; state-file hashes match the pre-deployment values. A new `momentum_reflow_ledger.json` source marker is expected scan state and is not trading data.

- [ ] **Step 5: Record deployment and commit**

Append the backup path, exact deployment file, SHA256 check, service result, API result, scanner result and preserved data-file hashes to `PROGRESS.md`.

```bash
git add PROGRESS.md
git commit -m "docs: record Bitget reflow source deployment"
```
