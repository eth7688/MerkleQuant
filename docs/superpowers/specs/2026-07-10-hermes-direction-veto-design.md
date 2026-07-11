# Hermes Direction Veto Design

Hermes remains the final independent technical-analysis gate, but its uncalibrated confidence value no longer controls entry.

The gate has three outcomes:

- Same explicit direction as AXIOM: pass.
- Opposite explicit direction: block.
- Valid full-mode analysis returning `NEUTRAL`: pass as an abstention.

Timeouts, queue timeouts, parser errors, unavailable skill/data, incomplete full-mode execution, and empty indicator evidence are operational failures rather than neutral opinions. They follow `hermes_confirm_fail_open`; the current production value is false, so they block.

The original decision, direction, confidence, reason, skill metadata, and indicators remain recorded for review. No frontend configuration or strategy parameters change.

