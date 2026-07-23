from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path

from backtest.bitget_history import BitgetHistorySource
from backtest.data_store import HistoricalStore
from backtest.engine import PortfolioReplayEngine, ReplayEngine
from backtest.experiment import ExperimentSpec
from btc_stage import classify_btc_stage
from strategy_core import ExitRules
from strategy_core import StrategySnapshot, evaluate_entry


def build_run_id(manifest_hash: str, symbol: str, data_fingerprints: list[str]) -> str:
    raw = "|".join([manifest_hash, symbol.upper(), *data_fingerprints])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _write_result(output: Path, manifest: dict, result, rules: dict, status: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in result.events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    metrics = {**result.metrics, "final_equity": result.final_equity, "status": status}
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    entry_fees = sum(float(event.get("fee", 0) or 0) for event in result.events if event.get("type") == "entry_fill")
    exit_net = sum(float(event.get("net_pnl", 0) or 0) for event in result.events if event.get("type") == "exit_fill")
    initial_equity = float(rules.get("initial_equity", 5000.0))
    ledger_delta = exit_net - entry_fees
    reconciliation = {
        "initial_equity": initial_equity,
        "ledger_delta": ledger_delta,
        "expected_final_equity": initial_equity + ledger_delta,
        "actual_final_equity": result.final_equity,
        "difference": result.final_equity - (initial_equity + ledger_delta),
        "funding_status": "not_modeled",
    }
    (output / "reconciliation.json").write_text(json.dumps(reconciliation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _manifest_fingerprint(store: HistoricalStore, symbol: str, interval: str) -> str:
    path = store._candle_path("bitget", symbol, interval).with_suffix(".manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return str(payload["sha256"])


def _load_experiment(path: Path) -> tuple[ExperimentSpec, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    spec = ExperimentSpec(
        name=payload["name"], rules=payload["rules"], data_version=payload["data_version"],
        train=tuple(payload["train"]), validation=tuple(payload["validation"]),
        test=tuple(payload["test"]), primary_metric=payload.get("primary_metric", "mean_r"),
        minimum_core_trades=int(payload.get("minimum_core_trades", 200)),
        minimum_filter_samples=int(payload.get("minimum_filter_samples", 50)),
    ).validate()
    expected = payload.get("manifest_hash")
    if expected and expected != spec.manifest_hash():
        raise ValueError("experiment_manifest_mutated")
    return spec, payload


def _btc_provider(store: HistoricalStore):
    btc1 = store.read_candles("bitget", "BTCUSDT", "1h")
    btc4 = store.read_candles("bitget", "BTCUSDT", "4h")

    def provider(decision_time: int) -> dict:
        one = btc1[(btc1["ot"] + 3_600_000) <= decision_time].tail(220)
        four = btc4[(btc4["ot"] + 14_400_000) <= decision_time].tail(220)
        return classify_btc_stage(one, four)

    return provider


def _precompute_worker(payload):
    os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
    from trader import SqueezeBreakoutBot, TradeConfig

    symbol, frame, btc1, btc4, rules, start, end = payload
    signal_source = str(rules.get("entry_signal_source", "rj_only") or "rj_only")
    cfg = TradeConfig(mode="paper", enabled=False, exchange="bitget", entry_signal_source=signal_source, scan_interval="30m")
    for key, value in rules.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    bot = SqueezeBreakoutBot(cfg)
    trigger = None
    if bot._entry_signal_source() == "rj_only":
        lines = bot._compute_rj_lines(frame)
        j_line, r_line = lines["j"], lines["r"]
        cross = ((j_line.shift(1) <= r_line.shift(1)) & (j_line > r_line)) | ((j_line.shift(1) >= r_line.shift(1)) & (j_line < r_line))
        level_up, level_down = bot._rj_original_level_triggers(j_line)
        trigger = (cross | level_up | level_down).fillna(False).to_numpy()
    confirm_bars = max(1, int(getattr(cfg, "rj_only_confirm_bars", 6) or 6))
    decisions = {}
    for index in range(max(59, int(rules.get("warmup_bars", 60)) - 1), len(frame)):
        decision_time = int(frame["ot"].iloc[index]) + 1_800_000
        if decision_time < start or decision_time >= end:
            continue
        if trigger is not None and not trigger[max(1, index - confirm_bars):index].any():
            continue
        one = btc1[(btc1["ot"] + 3_600_000) <= decision_time].tail(220)
        four = btc4[(btc4["ot"] + 14_400_000) <= decision_time].tail(220)
        stage = classify_btc_stage(one, four)
        decisions[decision_time] = evaluate_entry(
            bot, StrategySnapshot(symbol, "30m", decision_time, frame.iloc[:index + 1], stage)
        )
    return symbol, decisions


def download(args) -> int:
    store = HistoricalStore(args.root)
    source = BitgetHistorySource()
    frame = source.fetch_candles(args.symbol, args.interval, args.start, args.end)
    if frame.empty:
        raise SystemExit("no historical candles returned")
    path = store.write_candles("bitget", args.symbol, args.interval, frame, args.data_version)
    gaps = store.find_gaps(frame, {"1m": 60_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}[args.interval])
    print(json.dumps({"path": str(path), "rows": len(frame), "gaps": gaps}, ensure_ascii=False))
    return 0


def run(args) -> int:
    os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
    from trader import SqueezeBreakoutBot, TradeConfig

    store = HistoricalStore(args.root)
    spec, manifest = _load_experiment(Path(args.experiment))
    rules = dict(spec.rules)
    cfg = TradeConfig(
        mode="paper", enabled=False, exchange="bitget",
        entry_signal_source=str(rules.get("entry_signal_source", "rj_only") or "rj_only"),
        scan_interval="30m",
    )
    for key, value in rules.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    bot = SqueezeBreakoutBot(cfg)
    frame30 = store.read_candles("bitget", args.symbol, "30m")
    frame1 = store.read_candles("bitget", args.symbol, "1m")
    start, end = spec.test
    frame30 = frame30[frame30["ot"] < end].reset_index(drop=True)
    frame1 = frame1[(frame1["ot"] >= start) & (frame1["ot"] < end)].reset_index(drop=True)
    engine = ReplayEngine(
        bot=bot,
        initial_equity=float(rules.get("initial_equity", 5000.0)),
        risk_usdt=float(rules.get("risk_per_trade", 10.0)),
        fee_rate=float(rules.get("fee_rate", 0.0006)),
        slippage_bps=float(rules.get("slippage_bps", 2.0)),
        exit_rules=ExitRules(
            half_risk_trigger_r=float(rules.get("half_risk_trigger_r", 0.0)),
            early_protect_r=float(rules.get("early_protect_r", 0.8)),
            early_lock_r=float(rules.get("early_protect_lock_r", 0.0)),
            tier1_r=float(rules.get("tier1_defense_r", 1.2)),
            tier2_r=float(rules.get("tier2_partial_r", 1.7)),
        ),
        warmup_bars=int(rules.get("warmup_bars", 60)),
    )
    result = engine.run_symbol(
        args.symbol.upper(), frame30, frame1, _btc_provider(store), entry_start_ms=start
    )
    status = "READY" if result.metrics["trades"] >= spec.minimum_core_trades else "SAMPLE_NOT_READY"
    fingerprints = [
        _manifest_fingerprint(store, args.symbol, "30m"),
        _manifest_fingerprint(store, args.symbol, "1m"),
        _manifest_fingerprint(store, "BTCUSDT", "1h"),
        _manifest_fingerprint(store, "BTCUSDT", "4h"),
    ]
    run_id = build_run_id(spec.manifest_hash(), args.symbol, fingerprints)
    output = Path(args.output or "backtest_runs") / run_id
    output.mkdir(parents=True, exist_ok=True)
    run_manifest = {
        **manifest,
        "run_id": run_id,
        "symbol": args.symbol.upper(),
        "input_fingerprints": fingerprints,
    }
    (output / "manifest.json").write_text(json.dumps(run_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in result.events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    metrics = {**result.metrics, "final_equity": result.final_equity, "status": status}
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    entry_fees = sum(float(event.get("fee", 0) or 0) for event in result.events if event.get("type") == "entry_fill")
    exit_net = sum(float(event.get("net_pnl", 0) or 0) for event in result.events if event.get("type") == "exit_fill")
    initial_equity = float(rules.get("initial_equity", 5000.0))
    ledger_delta = exit_net - entry_fees
    reconciliation = {
        "initial_equity": initial_equity,
        "ledger_delta": ledger_delta,
        "expected_final_equity": initial_equity + ledger_delta,
        "actual_final_equity": result.final_equity,
        "difference": result.final_equity - (initial_equity + ledger_delta),
        "funding_status": "not_modeled",
    }
    (output / "reconciliation.json").write_text(
        json.dumps(reconciliation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"run_id": run_id, "output": str(output), **metrics}, ensure_ascii=False))
    return 0


def run_portfolio(args) -> int:
    os.environ.setdefault("AXIOM_DISABLE_AUTOSTART", "1")
    from trader import SqueezeBreakoutBot, TradeConfig

    store = HistoricalStore(args.root)
    spec, manifest = _load_experiment(Path(args.experiment))
    rules = dict(spec.rules)
    symbols = [line.strip().upper() for line in Path(args.symbols).read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate_symbols")
    cfg = TradeConfig(
        mode="paper", enabled=False, exchange="bitget",
        entry_signal_source=str(rules.get("entry_signal_source", "rj_only") or "rj_only"),
        scan_interval="30m",
    )
    for key, value in rules.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    bot = SqueezeBreakoutBot(cfg)
    start, end = spec.test
    frames30 = {}
    frames1 = {}
    fingerprints = []
    for symbol in symbols:
        frames30[symbol] = store.read_candles("bitget", symbol, "30m")
        frames30[symbol] = frames30[symbol][frames30[symbol]["ot"] < end].reset_index(drop=True)
        frames1[symbol] = store.read_candles("bitget", symbol, "1m")
        frames1[symbol] = frames1[symbol][(frames1[symbol]["ot"] >= start) & (frames1[symbol]["ot"] < end)].reset_index(drop=True)
        fingerprints.extend([
            _manifest_fingerprint(store, symbol, "30m"),
            _manifest_fingerprint(store, symbol, "1m"),
        ])
    fingerprints.extend([
        _manifest_fingerprint(store, "BTCUSDT", "1h"),
        _manifest_fingerprint(store, "BTCUSDT", "4h"),
    ])
    engine = PortfolioReplayEngine(
        bot=bot,
        initial_equity=float(rules.get("initial_equity", 5000.0)),
        risk_usdt=float(rules.get("risk_per_trade", 10.0)),
        fee_rate=float(rules.get("fee_rate", 0.0006)),
        slippage_bps=float(rules.get("slippage_bps", 2.0)),
        exit_rules=ExitRules(
            half_risk_trigger_r=float(rules.get("half_risk_trigger_r", 0.0)),
            early_protect_r=float(rules.get("early_protect_r", 0.8)),
            early_lock_r=float(rules.get("early_protect_lock_r", 0.0)),
            tier1_r=float(rules.get("tier1_defense_r", 1.2)),
            tier2_r=float(rules.get("tier2_partial_r", 1.7)),
        ),
        max_positions=int(rules.get("max_positions", 6)),
        warmup_bars=int(rules.get("warmup_bars", 60)),
    )
    btc1 = store.read_candles("bitget", "BTCUSDT", "1h")
    btc4 = store.read_candles("bitget", "BTCUSDT", "4h")
    precomputed = {}
    payloads = [(symbol, frames30[symbol], btc1, btc4, rules, start, end) for symbol in symbols]
    with ProcessPoolExecutor(max_workers=max(1, int(args.workers))) as pool:
        futures = [pool.submit(_precompute_worker, payload) for payload in payloads]
        for future in as_completed(futures):
            symbol, decisions = future.result()
            precomputed[symbol] = decisions
    result = engine.run(
        frames30, frames1, _btc_provider(store), entry_start_ms=start, precomputed=precomputed
    )
    status = "READY" if result.metrics["trades"] >= spec.minimum_core_trades else "SAMPLE_NOT_READY"
    run_id = build_run_id(spec.manifest_hash(), ",".join(symbols), fingerprints)
    output = Path(args.output or "backtest_runs") / run_id
    run_manifest = {**manifest, "run_id": run_id, "symbols": symbols, "input_fingerprints": fingerprints}
    _write_result(output, run_manifest, result, rules, status)
    print(json.dumps({"run_id": run_id, "output": str(output), **result.metrics,
                      "final_equity": result.final_equity, "status": status}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AXIOM RJ event-driven backtest")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("download")
    fetch.add_argument("--root", default="backtest_data")
    fetch.add_argument("--symbol", required=True)
    fetch.add_argument("--interval", choices=("1m", "30m", "1h", "4h"), required=True)
    fetch.add_argument("--start", type=int, required=True)
    fetch.add_argument("--end", type=int, required=True)
    fetch.add_argument("--data-version", required=True)
    fetch.set_defaults(func=download)
    replay = sub.add_parser("run")
    replay.add_argument("--root", default="backtest_data")
    replay.add_argument("--symbol", required=True)
    replay.add_argument("--experiment", required=True)
    replay.add_argument("--output", default="backtest_runs")
    replay.set_defaults(func=run)
    portfolio = sub.add_parser("portfolio")
    portfolio.add_argument("--root", default="backtest_data")
    portfolio.add_argument("--symbols", required=True)
    portfolio.add_argument("--experiment", required=True)
    portfolio.add_argument("--output", default="backtest_runs")
    portfolio.add_argument("--workers", type=int, default=4)
    portfolio.set_defaults(func=run_portfolio)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
