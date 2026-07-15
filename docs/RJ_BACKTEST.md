# RJ Event-Driven Backtest V1

The replay router also supports `entry_signal_source: predicta_ewo` in the
frozen experiment rules. Predicta replay uses the same closed-candle label,
EWO fast/wait paths, six-bar confirmation window, adaptive choppy filter,
signal-key stop anchor, BTC direction gate, and next-available 1-minute open
fill contract as the live engine. Predicta positions do not use time stops.

V1 reuses the live engine's `_rj_only_signal_from_df` decision for each closed
30-minute window. Entries fill at the next available 1-minute open. Position
management then replays 1-minute OHLCV bars through the shared deterministic
protection and staged-exit state machine.

## Safety Rules

- Strategy code only sees candles closed at the simulated decision timestamp.
- Experiment periods and rules are declared before a run.
- A changed manifest hash creates a different run id.
- Run ids include the symbol and all input candle hashes.
- Results below 200 independent positions are `SAMPLE_NOT_READY`.
- No parameter search or automatic best-result selection exists.
- Hermes is not called during historical replay.

## Download Data

```powershell
python replay_engine.py download --root backtest_data --symbol BTCUSDT --interval 1m --start <ms> --end <ms> --data-version <version>
python replay_engine.py download --root backtest_data --symbol BTCUSDT --interval 30m --start <ms> --end <ms> --data-version <version>
python replay_engine.py download --root backtest_data --symbol BTCUSDT --interval 1h --start <ms> --end <ms> --data-version <version>
python replay_engine.py download --root backtest_data --symbol BTCUSDT --interval 4h --start <ms> --end <ms> --data-version <version>
```

The downloader reports every missing interval. It never fills gaps silently.

## Run A Frozen Experiment

```powershell
python replay_engine.py run --root backtest_data --symbol BTCUSDT --experiment experiment.json --output backtest_runs
```

Each run writes:

- `manifest.json`: rules, periods, symbol, and input hashes;
- `events.jsonl`: immutable signal, fill, stop, and exit events;
- `metrics.json`: independent-position R metrics;
- `reconciliation.json`: ledger-to-equity reconciliation.

## V1 Limits

- The CLI currently runs one symbol at a time. Cross-symbol portfolio slot
  competition is not yet modeled.
- Historical funding is explicitly marked `not_modeled`; it is never described
  as zero.
- Point-in-time universe reconstruction requires archived contract snapshots.
- RJSETUP candidate-pool results must be reported separately from direct RJ
  signals before portfolio conclusions are allowed.
- A short pilot run validates plumbing only; it is not evidence of expectancy.
