# Hermes Direction Veto Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace confidence-based Hermes entry gating with explicit opposite-direction veto while allowing valid neutral analysis.

**Architecture:** Add one pure gate helper to `SqueezeBreakoutBot` and call it after Hermes JSON parsing. The helper separates valid analysis from operational failure, then evaluates only the returned direction.

**Tech Stack:** Python 3.12, `unittest`, existing Hermes CLI integration.

## Global Constraints

- Keep confidence and all analysis metadata for audit only.
- Do not change frontend configuration or RJ strategy parameters.
- Treat incomplete analysis as an operational failure controlled by `hermes_confirm_fail_open`.

---

### Task 1: Direction veto gate

**Files:**
- Create: `tests/test_hermes_direction_gate.py`
- Modify: `trader.py`

**Interfaces:**
- Consumes: AXIOM direction, parsed Hermes response fields, and `fail_open`.
- Produces: `_hermes_direction_gate(direction, parsed, fail_open) -> tuple[bool, str]`.

- [ ] Write tests for same direction, opposite direction, valid neutral, and operational failure.
- [ ] Run `python tests\test_hermes_direction_gate.py` and confirm the missing helper causes failure.
- [ ] Implement the minimal helper and replace confidence-based `allowed` calculation.
- [ ] Run the focused test, complete test discovery, and Python compilation.
- [ ] Deploy `trader.py`, restart `macd-bot`, verify service health, and update `PROGRESS.md`.

