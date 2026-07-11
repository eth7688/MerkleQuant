#!/usr/bin/env python3
"""CLI entry point for the AXIOM RJ event-driven backtest."""

import sys

from backtest.cli import main


if __name__ == "__main__":
    if "--csv" in sys.argv:
        raise SystemExit(
            "The legacy squeeze/close-only replay is disabled. "
            "Use `replay_engine.py download` and `replay_engine.py run`."
        )
    raise SystemExit(main())
