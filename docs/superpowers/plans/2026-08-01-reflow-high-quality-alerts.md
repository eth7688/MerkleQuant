# 动能回流高质量信号双通道警报 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为首次进入`HIGH + ACTIVE`的动能回流信号提供持久化去重的企业微信提醒和全站“金币落袋三音”浏览器提醒。

**Architecture:** 新增独立`momentum_reflow_alerts.py`，集中管理警报配置、版本化账本、事件观察、企业微信格式与投递状态；`web_ui.py`只在扫描结果成功合并后调用该模块，并以单个守护线程执行外部网络投递。`admin_server.py`负责认证后的Webhook配置与测试发送，普通页面只读取脱敏警报事件并在浏览器本地维护声音游标。

**Tech Stack:** Python 3.12、Flask、`requests`、原生HTML/CSS/JavaScript、Web Audio API、JSON原子持久化、`unittest`、Node.js JavaScript测试。

## Global Constraints

- 仅提醒`quality_label=HIGH`且`status=ACTIVE`的信号。
- 当前高质量信号首次初始化只建立基线，不补发；标准信号后续首次升级为高质量时提醒一次。
- 自动扫描和手动扫描共用同一`signal_key`去重账本；重启、重复扫描和字段更新不得重复提醒。
- 企业微信Webhook只保存在服务器，禁止进入普通页面响应、前端源码、日志、异常文本或警报账本。
- Webhook默认关闭；空字符串保存不得覆盖已配置凭证。
- 浏览器声音在任意AXIOM菜单打开时生效，首次开启或新浏览器只建立当前游标基线。
- 音效由Web Audio API合成约1秒“金币落袋三音”，不复制品牌原始提示音。
- 企业微信和浏览器声音独立失败；通知错误不得使扫描结果失败。
- 最近正式投递状态直接从警报账本派生，不复制到配置文件，避免`macd-bot`与`macd-admin`成为同一配置文件的双写者。
- 不修改扫描条件、交易策略、持仓、订单、交易记录或现有动能回流历史格式。
- 不新增前端框架、npm依赖或独立systemd服务。

---

## File Map

- Create: `momentum_reflow_alerts.py` — 配置/账本校验、事件观察、脱敏读取、微信格式与投递。
- Create: `tests/test_momentum_reflow_alerts.py` — 纯领域逻辑、持久化、微信投递与失败关闭测试。
- Modify: `web_ui.py` — 扫描接入、单工作器生命周期、普通警报API、全站声音和页面按钮。
- Modify: `admin_server.py` — 管理员配置/测试接口和动能回流警报设置UI。
- Modify: `tests/test_momentum_reflow_integration.py` — 自动/手动扫描接入、API、浏览器声音行为测试。
- Modify: `tests/test_momentum_reflow_scheduler.py` — 通知工作器单实例、停止与错误隔离测试。
- Modify: `tests/test_momentum_reflow_admin.py` — Webhook认证、掩码、空值保护、测试发送和管理UI测试。
- Modify: `PROGRESS.md` — 仅在用户批准并完成服务器部署后追加真实部署记录。

---

### Task 1: 持久化警报配置、账本与首次高质量观察

**Files:**
- Create: `momentum_reflow_alerts.py`
- Create: `tests/test_momentum_reflow_alerts.py`

**Interfaces:**
- Produces: `load_alert_settings(path: Path) -> dict`
- Produces: `save_alert_settings(path: Path, *, wechat_enabled: bool, wechat_webhook: str | None, updated_by: str, now_ms: int) -> dict`
- Produces: `public_alert_settings(settings: dict) -> dict`
- Produces: `validate_wechat_webhook(webhook: str) -> None`
- Produces: `observe_reflow_alerts(path: Path, rows: list[dict], now_ms: int) -> list[dict]`
- Produces: `read_public_alerts(path: Path, after_id: int) -> dict`
- Produces: `read_delivery_status(path: Path) -> dict`
- Produces: `baseline_wechat_delivery(path: Path) -> int`

- [ ] **Step 1: 写入失败测试，锁定基线、升级、去重和损坏文件行为**

```python
# tests/test_momentum_reflow_alerts.py
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from momentum_reflow_alerts import (
    baseline_wechat_delivery,
    load_alert_settings,
    observe_reflow_alerts,
    public_alert_settings,
    read_delivery_status,
    read_public_alerts,
    save_alert_settings,
)


def row(key="BTC-LONG-1", quality="HIGH", status="ACTIVE", **overrides):
    value = {
        "signal_key": key, "symbol": "BTCUSDT", "direction": "LONG",
        "quality_label": quality, "status": status, "price": 100.0,
        "ema50": 99.0, "daily_kind": "strong_momentum", "window_index": 1,
        "breakout_volume_ratio": 3.0, "first_seen_at": 100,
    }
    value.update(overrides)
    return value


class ReflowAlertLedgerTests(unittest.TestCase):
    def test_first_observation_baselines_existing_high_without_event(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            self.assertEqual(observe_reflow_alerts(path, [row()], 1_000), [])
            self.assertEqual(read_public_alerts(path, 0), {"latest_alert_id": 0, "events": []})

    def test_standard_upgrade_emits_once_and_snapshot_is_immutable(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            observe_reflow_alerts(path, [row(quality="STANDARD")], 1_000)
            created = observe_reflow_alerts(path, [row(price=101.0)], 2_000)
            duplicate = observe_reflow_alerts(path, [row(price=102.0)], 3_000)
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0]["alert_id"], 1)
            self.assertEqual(created[0]["trigger"], "upgraded_high")
            self.assertEqual(created[0]["snapshot"]["price"], 101.0)
            self.assertEqual(duplicate, [])
            self.assertEqual(read_public_alerts(path, 0)["events"][0]["price"], 101.0)

    def test_only_high_active_rows_emit(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            observe_reflow_alerts(path, [], 1_000)
            created = observe_reflow_alerts(path, [
                row("watch", "WATCH"), row("standard", "STANDARD"),
                row("invalid", "HIGH", "INVALID"), row("active", "HIGH", "ACTIVE"),
            ], 2_000)
            self.assertEqual([event["signal_key"] for event in created], ["active"])
            self.assertEqual(created[0]["trigger"], "new_signal")

    def test_corrupt_ledger_fails_closed_without_overwrite(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            path.write_text("{broken", encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                observe_reflow_alerts(path, [row()], 1_000)
            self.assertEqual(path.read_bytes(), original)

    def test_malformed_nested_event_fails_closed_without_overwrite(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            path.write_text(json.dumps({
                "version": 1, "initialized": True, "next_alert_id": 2,
                "wechat_cursor": 0, "observed": {}, "events": [{"alert_id": "bad"}],
            }), encoding="utf-8")
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                read_public_alerts(path, 0)
            self.assertEqual(path.read_bytes(), original)

    def test_webhook_is_masked_and_blank_save_preserves_secret(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-1234"
            save_alert_settings(path, wechat_enabled=False, wechat_webhook=url,
                                updated_by="7", now_ms=100)
            saved = save_alert_settings(path, wechat_enabled=True, wechat_webhook="",
                                        updated_by="7", now_ms=200)
            public = public_alert_settings(saved)
            self.assertEqual(saved["wechat_webhook"], url)
            self.assertNotIn(url, json.dumps(public))
            self.assertTrue(public["webhook_configured"])
            self.assertEqual(public["webhook_mask"], "****1234")

    def test_non_official_webhook_is_rejected_before_persistence(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            with self.assertRaises(ValueError):
                save_alert_settings(path, wechat_enabled=False,
                    wechat_webhook="https://example.com/hook", updated_by="7", now_ms=100)

    def test_enabling_channel_baselines_latest_event(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "alerts.json"
            observe_reflow_alerts(path, [], 1_000)
            observe_reflow_alerts(path, [row()], 2_000)
            self.assertEqual(baseline_wechat_delivery(path), 1)
```

- [ ] **Step 2: 运行专项测试并确认RED**

Run: `python -m unittest discover -s tests -p 'test_momentum_reflow_alerts.py' -v`
Expected: `ModuleNotFoundError: No module named 'momentum_reflow_alerts'`。

- [ ] **Step 3: 实现最小版本化配置、账本、观察和脱敏读取**

```python
# momentum_reflow_alerts.py
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path

SETTINGS_VERSION = 1
LEDGER_VERSION = 1
WEBHOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
_LOCK = threading.RLock()
DEFAULT_SETTINGS = {
    "version": SETTINGS_VERSION,
    "wechat_enabled": False,
    "wechat_webhook": "",
    "updated_at": 0,
    "updated_by": "",
    "last_test_at": 0,
    "last_test_ok": False,
    "last_test_error": "",
}
DEFAULT_LEDGER = {
    "version": LEDGER_VERSION,
    "initialized": False,
    "next_alert_id": 1,
    "wechat_cursor": 0,
    "observed": {},
    "events": [],
}
SNAPSHOT_FIELDS = (
    "symbol", "direction", "price", "ema50", "daily_kind", "window_index",
    "breakout_volume_ratio", "first_seen_at",
)


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _read_object(path: Path, description: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{description} is unreadable") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description} shape is invalid")
    return value


def _validate_settings(value: dict) -> dict:
    if (value.get("version") != SETTINGS_VERSION
            or type(value.get("wechat_enabled")) is not bool
            or type(value.get("wechat_webhook")) is not str
            or type(value.get("updated_at")) is not int
            or type(value.get("updated_by")) is not str
            or type(value.get("last_test_at")) is not int
            or type(value.get("last_test_ok")) is not bool
            or type(value.get("last_test_error")) is not str):
        raise ValueError("reflow alert settings version or shape is invalid")
    return value


def _validate_ledger(value: dict) -> dict:
    if (value.get("version") != LEDGER_VERSION
            or type(value.get("initialized")) is not bool
            or type(value.get("next_alert_id")) is not int
            or type(value.get("wechat_cursor")) is not int
            or not isinstance(value.get("observed"), dict)
            or not isinstance(value.get("events"), list)):
        raise ValueError("reflow alert ledger version or shape is invalid")
    for key, observed in value["observed"].items():
        if not isinstance(key, str) or not isinstance(observed, dict) \
                or type(observed.get("ever_high")) is not bool \
                or type(observed.get("last_quality_label")) is not str:
            raise ValueError("reflow alert observed entry is invalid")
    for event in value["events"]:
        delivery = event.get("wechat") if isinstance(event, dict) else None
        if (not isinstance(event, dict)
                or type(event.get("alert_id")) is not int
                or type(event.get("signal_key")) is not str
                or event.get("trigger") not in {"new_signal", "upgraded_high"}
                or type(event.get("created_at")) is not int
                or not isinstance(event.get("snapshot"), dict)
                or not isinstance(delivery, dict)
                or delivery.get("status") not in {"pending", "delivered", "failed"}
                or type(delivery.get("attempts")) is not int
                or type(delivery.get("last_attempt_at")) is not int
                or type(delivery.get("next_attempt_at")) is not int
                or type(delivery.get("last_error")) is not str):
            raise ValueError("reflow alert event entry is invalid")
    return value


def load_alert_settings(path: Path) -> dict:
    with _LOCK:
        if not path.exists():
            _atomic_write(path, DEFAULT_SETTINGS.copy())
        return copy.deepcopy(_validate_settings(_read_object(path, "reflow alert settings")))


def validate_wechat_webhook(webhook: str) -> None:
    if not webhook.startswith(WEBHOOK_PREFIX) or not webhook[len(WEBHOOK_PREFIX):]:
        raise ValueError("invalid enterprise wechat webhook")


def save_alert_settings(path: Path, *, wechat_enabled: bool, wechat_webhook: str | None,
                        updated_by: str, now_ms: int) -> dict:
    if type(wechat_enabled) is not bool or type(now_ms) is not int or not isinstance(updated_by, str):
        raise TypeError("invalid reflow alert settings input")
    with _LOCK:
        current = load_alert_settings(path)
        supplied = "" if wechat_webhook is None else str(wechat_webhook).strip()
        if supplied:
            validate_wechat_webhook(supplied)
            current["wechat_webhook"] = supplied
        if wechat_enabled and not current["wechat_webhook"]:
            raise ValueError("wechat webhook is required")
        current.update(wechat_enabled=wechat_enabled, updated_at=now_ms, updated_by=updated_by)
        _atomic_write(path, current)
        return copy.deepcopy(current)


def public_alert_settings(settings: dict) -> dict:
    public = {key: value for key, value in settings.items() if key != "wechat_webhook"}
    webhook = str(settings.get("wechat_webhook", ""))
    public["webhook_configured"] = bool(webhook)
    public["webhook_mask"] = f"****{webhook[-4:]}" if webhook else ""
    return public


def _load_ledger(path: Path) -> dict:
    if not path.exists():
        return copy.deepcopy(DEFAULT_LEDGER)
    return _validate_ledger(_read_object(path, "reflow alert ledger"))


def _is_high_active(row: dict) -> bool:
    return row.get("quality_label") == "HIGH" and row.get("status") == "ACTIVE"


def _snapshot(row: dict) -> dict:
    return {field: copy.deepcopy(row.get(field)) for field in SNAPSHOT_FIELDS}


def observe_reflow_alerts(path: Path, rows: list[dict], now_ms: int) -> list[dict]:
    if not isinstance(rows, list) or type(now_ms) is not int:
        raise TypeError("invalid reflow alert observation")
    with _LOCK:
        ledger = _load_ledger(path)
        created = []
        initializing = not ledger["initialized"]
        for row in rows:
            key = str(row.get("signal_key", ""))
            if not key:
                raise ValueError("signal_key is required")
            previously_seen = key in ledger["observed"]
            observed = ledger["observed"].setdefault(
                key, {"ever_high": False, "last_quality_label": ""},
            )
            qualifies = _is_high_active(row)
            if qualifies and not observed["ever_high"] and not initializing:
                alert_id = ledger["next_alert_id"]
                ledger["next_alert_id"] += 1
                event = {
                    "alert_id": alert_id, "signal_key": key, "created_at": now_ms,
                    "trigger": ("upgraded_high" if previously_seen
                                and observed["last_quality_label"] != "HIGH" else "new_signal"),
                    "snapshot": _snapshot(row),
                    "wechat": {"status": "pending", "attempts": 0,
                               "last_attempt_at": 0, "next_attempt_at": now_ms,
                               "last_error": ""},
                }
                ledger["events"].append(event)
                created.append(copy.deepcopy(event))
            if qualifies:
                observed["ever_high"] = True
            observed["last_quality_label"] = str(row.get("quality_label", ""))
        ledger["initialized"] = True
        _atomic_write(path, ledger)
        return created


def read_public_alerts(path: Path, after_id: int) -> dict:
    if type(after_id) is not int or after_id < 0:
        raise ValueError("after_id must be a nonnegative integer")
    with _LOCK:
        ledger = _load_ledger(path)
        latest = ledger["next_alert_id"] - 1
        events = []
        for event in ledger["events"]:
            if event["alert_id"] > after_id:
                events.append({"alert_id": event["alert_id"], "signal_key": event["signal_key"],
                               "trigger": event["trigger"], "created_at": event["created_at"],
                               **copy.deepcopy(event["snapshot"])})
        return {"latest_alert_id": latest, "events": events[-20:]}


def read_delivery_status(path: Path) -> dict:
    with _LOCK:
        ledger = _load_ledger(path)
        if not ledger["events"]:
            return {"last_delivery_at": 0, "last_delivery_status": "none",
                    "last_delivery_alert_id": 0, "last_delivery_error": ""}
        event = ledger["events"][-1]
        delivery = event["wechat"]
        return {"last_delivery_at": delivery["last_attempt_at"],
                "last_delivery_status": delivery["status"],
                "last_delivery_alert_id": event["alert_id"],
                "last_delivery_error": delivery["last_error"]}


def baseline_wechat_delivery(path: Path) -> int:
    with _LOCK:
        ledger = _load_ledger(path)
        ledger["wechat_cursor"] = ledger["next_alert_id"] - 1
        _atomic_write(path, ledger)
        return ledger["wechat_cursor"]
```

- [ ] **Step 4: 运行专项测试并确认GREEN**

Run: `python -m unittest discover -s tests -p 'test_momentum_reflow_alerts.py' -v`
Expected: 8 tests, all `OK`。

- [ ] **Step 5: 提交领域基础**

```powershell
git add momentum_reflow_alerts.py tests/test_momentum_reflow_alerts.py
git commit -m "feat: persist reflow alert events"
```

---

### Task 2: 企业微信消息格式、脱敏投递与有限重试

**Files:**
- Modify: `momentum_reflow_alerts.py`
- Modify: `tests/test_momentum_reflow_alerts.py`

**Interfaces:**
- Consumes: Task 1 ledger event schema and settings schema.
- Produces: `format_wechat_markdown(event: dict) -> str`
- Produces: `send_wechat_markdown(webhook: str, content: str, *, post=requests.post, timeout: float = 5.0) -> None`
- Produces: `deliver_due_wechat(settings_path: Path, ledger_path: Path, now_ms: int, *, post=requests.post) -> dict`
- Produces: `test_wechat_webhook(settings_path: Path, now_ms: int, *, post=requests.post) -> dict`

- [ ] **Step 1: 添加失败测试，覆盖格式、成功、业务失败、退避和不泄密**

```python
from unittest.mock import Mock
from momentum_reflow_alerts import (
    deliver_due_wechat, format_wechat_markdown, send_wechat_markdown,
    test_wechat_webhook,
)

class ReflowWechatDeliveryTests(unittest.TestCase):
    def test_markdown_uses_frozen_snapshot(self):
        event = {"trigger": "upgraded_high", "snapshot": row(
            price=101.25, direction="SHORT", daily_kind="bearish_engulfing",
            first_seen_at=1_786_118_400_000,
        )}
        text = format_wechat_markdown(event)
        self.assertIn("BTCUSDT · SHORT", text)
        self.assertIn("价格：101.25", text)
        self.assertIn("日线：看跌吞没", text)
        self.assertIn("触发：标准信号升级为高质量", text)
        self.assertIn("首次发现：2026-08-08", text)
        self.assertNotIn("建议", text)

    def test_sender_requires_official_https_webhook_and_success_code(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"errcode": 0, "errmsg": "ok"}
        post = Mock(return_value=response)
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
        send_wechat_markdown(url, "test", post=post)
        post.assert_called_once_with(url, json={"msgtype": "markdown", "markdown": {"content": "test"}}, timeout=5.0)
        with self.assertRaises(ValueError):
            send_wechat_markdown("http://example.com/key=secret", "test", post=post)

    def test_business_failure_is_sanitized_and_scheduled_for_retry(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-1234"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1)
            observe_reflow_alerts(ledger, [row()], 2)
            response = Mock(); response.raise_for_status.return_value = None
            response.json.return_value = {"errcode": 93000, "errmsg": f"bad {url}"}
            result = deliver_due_wechat(settings, ledger, 2, post=Mock(return_value=response))
            raw = ledger.read_text(encoding="utf-8")
            self.assertEqual(result["status"], "retry_pending")
            self.assertNotIn(url, raw)
            self.assertNotIn("secret-1234", raw)

    def test_timeout_retries_then_successfully_delivers_same_event(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1); observe_reflow_alerts(ledger, [row()], 2)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 0}
            post = Mock(side_effect=[TimeoutError("timeout"), response])
            self.assertEqual(deliver_due_wechat(settings, ledger, 2, post=post)["status"], "retry_pending")
            self.assertEqual(deliver_due_wechat(settings, ledger, 60_002, post=post)["status"], "delivered")
            self.assertEqual(post.call_count, 2)

    def test_fifth_failure_marks_event_failed_and_advances_cursor(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1); observe_reflow_alerts(ledger, [row()], 2)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 93000}
            now = 2
            for attempt in range(5):
                result = deliver_due_wechat(settings, ledger, now, post=Mock(return_value=response))
                raw = json.loads(ledger.read_text(encoding="utf-8"))
                now = raw["events"][0]["wechat"]["next_attempt_at"]
            self.assertEqual(result["status"], "failed")
            self.assertEqual(read_delivery_status(ledger)["last_delivery_status"], "failed")

    def test_success_advances_cursor_and_does_not_resend(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1); observe_reflow_alerts(ledger, [row()], 2)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 0}
            post = Mock(return_value=response)
            self.assertEqual(deliver_due_wechat(settings, ledger, 2, post=post)["status"], "delivered")
            self.assertEqual(deliver_due_wechat(settings, ledger, 3, post=post)["status"], "idle")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(read_delivery_status(ledger)["last_delivery_status"], "delivered")

    def test_retry_waiting_event_blocks_later_event(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=True, wechat_webhook=url, updated_by="7", now_ms=1)
            observe_reflow_alerts(ledger, [], 1)
            observe_reflow_alerts(ledger, [row("first")], 2)
            observe_reflow_alerts(ledger, [row("second")], 3)
            response = Mock(); response.raise_for_status.return_value = None
            response.json.return_value = {"errcode": 93000}
            post = Mock(return_value=response)
            deliver_due_wechat(settings, ledger, 3, post=post)
            result = deliver_due_wechat(settings, ledger, 4, post=post)
            self.assertEqual(result["status"], "waiting_retry")
            self.assertEqual(post.call_count, 1)

    def test_test_message_does_not_create_event_or_advance_cursor(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); settings = root / "settings.json"; ledger = root / "ledger.json"
            url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
            save_alert_settings(settings, wechat_enabled=False, wechat_webhook=url, updated_by="7", now_ms=1)
            response = Mock(); response.raise_for_status.return_value = None; response.json.return_value = {"errcode": 0}
            result = test_wechat_webhook(settings, 2, post=Mock(return_value=response))
            self.assertTrue(result["last_test_ok"])
            self.assertFalse(ledger.exists())
```

- [ ] **Step 2: 运行测试并确认RED**

Run: `python -m unittest discover -s tests -p 'test_momentum_reflow_alerts.py' -v`
Expected: imports for delivery functions fail。

- [ ] **Step 3: 实现固定快照格式、官方URL校验与顺序投递**

```python
# append to momentum_reflow_alerts.py
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

RETRY_DELAYS_MS = (60_000, 300_000, 900_000, 3_600_000)
MAX_ATTEMPTS = 5
DAILY_LABELS = {
    "strong_momentum": "强动能日K", "bullish_engulfing": "看涨吞没",
    "bearish_engulfing": "看跌吞没", "hammer": "锤子线",
    "shooting_star": "流星线", "morning_star": "早晨之星",
    "evening_star": "黄昏之星", "bottom_fractal": "底分型", "top_fractal": "顶分型",
}

def _safe_error(error: object) -> str:
    text = str(error)
    marker = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
    if marker in text:
        text = text.split(marker, 1)[0] + marker + "****"
    return text[:120]

def format_wechat_markdown(event: dict) -> str:
    item = event["snapshot"]
    first_seen = datetime.fromtimestamp(item["first_seen_at"] / 1000, ZoneInfo("Asia/Shanghai"))
    trigger = "标准信号升级为高质量" if event["trigger"] == "upgraded_high" else "新高质量信号"
    return "\n".join([
        "【AXIOM 高质量回流警报】", "",
        f"{item['symbol']} · {item['direction']}",
        f"触发：{trigger}", f"价格：{item['price']}", f"EMA50：{item['ema50']}",
        f"日线：{DAILY_LABELS.get(item['daily_kind'], item['daily_kind'])}",
        f"窗口：{item['window_index']}/5", f"量比：{item['breakout_volume_ratio']}x",
        f"首次发现：{first_seen:%Y-%m-%d %H:%M} 北京时间",
    ])

def send_wechat_markdown(webhook: str, content: str, *, post=requests.post, timeout: float = 5.0) -> None:
    validate_wechat_webhook(webhook)
    response = post(webhook, json={"msgtype": "markdown", "markdown": {"content": content}}, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("errcode") != 0:
        raise RuntimeError(f"enterprise wechat rejected request: {payload.get('errcode', 'invalid')}")

def deliver_due_wechat(settings_path: Path, ledger_path: Path, now_ms: int, *, post=requests.post) -> dict:
    settings = load_alert_settings(settings_path)
    if not settings["wechat_enabled"]:
        return {"status": "disabled"}
    with _LOCK:
        ledger = _load_ledger(ledger_path)
        event = next((item for item in ledger["events"]
                      if item["alert_id"] > ledger["wechat_cursor"]), None)
    if event is None:
        return {"status": "idle"}
    if event["wechat"]["next_attempt_at"] > now_ms:
        return {"status": "waiting_retry", "alert_id": event["alert_id"]}
    try:
        send_wechat_markdown(settings["wechat_webhook"], format_wechat_markdown(event), post=post)
        outcome = "delivered"
        error_text = ""
    except Exception as error:
        outcome = "retry_pending"
        error_text = _safe_error(error)
    with _LOCK:
        ledger = _load_ledger(ledger_path)
        current = next(item for item in ledger["events"] if item["alert_id"] == event["alert_id"])
        delivery = current["wechat"]
        delivery["attempts"] += 1
        delivery["last_attempt_at"] = now_ms
        delivery["last_error"] = error_text
        if outcome == "delivered":
            delivery["status"] = "delivered"
            ledger["wechat_cursor"] = current["alert_id"]
        elif delivery["attempts"] >= MAX_ATTEMPTS:
            delivery["status"] = "failed"
            delivery["next_attempt_at"] = 0
            ledger["wechat_cursor"] = current["alert_id"]
            outcome = "failed"
        else:
            delivery["status"] = "pending"
            delay = RETRY_DELAYS_MS[min(delivery["attempts"] - 1, len(RETRY_DELAYS_MS) - 1)]
            delivery["next_attempt_at"] = now_ms + delay
        _atomic_write(ledger_path, ledger)
    return {"status": outcome, "alert_id": event["alert_id"], "error": error_text}

def test_wechat_webhook(settings_path: Path, now_ms: int, *, post=requests.post) -> dict:
    settings = load_alert_settings(settings_path)
    try:
        send_wechat_markdown(settings["wechat_webhook"], "【AXIOM】企业微信警报测试成功", post=post)
        settings.update(last_test_at=now_ms, last_test_ok=True, last_test_error="")
    except Exception as error:
        settings.update(last_test_at=now_ms, last_test_ok=False, last_test_error=_safe_error(error))
    with _LOCK:
        _atomic_write(settings_path, settings)
    return public_alert_settings(settings)
```

- [ ] **Step 4: 运行专项测试并确认GREEN**

Run: `python -m unittest discover -s tests -p 'test_momentum_reflow_alerts.py' -v`
Expected: all tests `OK`。

- [ ] **Step 5: 提交微信投递逻辑**

```powershell
git add momentum_reflow_alerts.py tests/test_momentum_reflow_alerts.py
git commit -m "feat: deliver reflow alerts to wechat"
```

---

### Task 3: 扫描接入、错误隔离、工作器与普通警报API

**Files:**
- Modify: `web_ui.py:15-31, 435-452, 3754-3794, 3894-3930, 4392-4396`
- Modify: `tests/test_momentum_reflow_integration.py`
- Modify: `tests/test_momentum_reflow_scheduler.py`

**Interfaces:**
- Consumes: `observe_reflow_alerts`, `deliver_due_wechat`, `read_public_alerts`。
- Produces: `_process_reflow_alerts(payload: dict, now_ms: int) -> list[dict]`
- Produces: `_start_reflow_alert_worker() -> bool`, `_stop_reflow_alert_worker_for_tests() -> None`
- Produces: `GET /api/reflow/alerts?after=<nonnegative int>` authenticated JSON route。

- [ ] **Step 1: 添加失败测试，覆盖自动/手动统一接入与错误不污染扫描**

```python
# tests/test_momentum_reflow_integration.py
def test_successful_scan_observes_alerts_after_history_merge(self):
    web_ui = importlib.import_module("web_ui")
    payload = {"rows": [{**candidate(), "signal_key": "key", "quality_label": "HIGH", "status": "ACTIVE"}]}
    with patch.object(web_ui, "scan_momentum_reflow", return_value={"rows": []}), \
         patch.object(web_ui, "merge_reflow_signals", return_value=payload), \
         patch.object(web_ui, "observe_reflow_alerts", return_value=[{"alert_id": 1}]) as observe:
        result = web_ui._run_reflow_scan(lambda *_: None)
    self.assertIs(result, payload)
    observe.assert_called_once()

def test_alert_ledger_failure_does_not_fail_scan(self):
    web_ui = importlib.import_module("web_ui")
    payload = {"rows": []}
    with patch.object(web_ui, "scan_momentum_reflow", return_value={"rows": []}), \
         patch.object(web_ui, "merge_reflow_signals", return_value=payload), \
         patch.object(web_ui, "observe_reflow_alerts", side_effect=ValueError("broken ledger")):
        self.assertIs(web_ui._run_reflow_scan(lambda *_: None), payload)
    self.assertIn("broken ledger", web_ui._reflow_alert_status["last_error"])

def test_alert_api_requires_login_and_returns_no_webhook(self):
    web_ui = importlib.import_module("web_ui")
    client = web_ui.app.test_client()
    self.assertEqual(client.get("/api/reflow/alerts?after=0").status_code, 401)
    with client.session_transaction() as user_session:
        user_session["user_id"] = 9
    with patch.object(web_ui, "read_public_alerts", return_value={"latest_alert_id": 3, "events": []}):
        response = client.get("/api/reflow/alerts?after=2")
    self.assertEqual(response.status_code, 200)
    self.assertNotIn("webhook", response.get_data(as_text=True).lower())

def test_alert_api_reports_corrupt_ledger_as_unavailable(self):
    web_ui = importlib.import_module("web_ui")
    client = web_ui.app.test_client()
    with client.session_transaction() as user_session:
        user_session["user_id"] = 9
    with patch.object(web_ui, "read_public_alerts", side_effect=ValueError("broken ledger")):
        response = client.get("/api/reflow/alerts?after=0")
    self.assertEqual(response.status_code, 503)
    self.assertNotIn("broken ledger", response.get_data(as_text=True))
```

```python
# tests/test_momentum_reflow_scheduler.py
class ReflowAlertWorkerLifecycleTests(unittest.TestCase):
    def tearDown(self):
        web_ui._stop_reflow_alert_worker_for_tests()

    def test_start_called_twice_creates_one_alert_thread(self):
        threads = []
        class DeferredThread:
            def __init__(self, target=None, daemon=None): self.target=target; self.alive=False; threads.append(self)
            def start(self): self.alive=True
            def is_alive(self): return self.alive
            def join(self, timeout=None): self.alive=False
        with patch.object(web_ui.threading, "Thread", DeferredThread):
            self.assertTrue(web_ui._start_reflow_alert_worker())
            self.assertFalse(web_ui._start_reflow_alert_worker())
        self.assertEqual(len(threads), 1)
```

- [ ] **Step 2: 运行两个测试文件并确认RED**

Run: `python -m unittest discover -s tests -p 'test_momentum_reflow_integration.py' -v`，随后运行 `python -m unittest discover -s tests -p 'test_momentum_reflow_scheduler.py' -v`
Expected: missing alert imports/functions/routes fail。

- [ ] **Step 3: 接入路径常量、状态、扫描观察和单工作器**

```python
# web_ui.py imports/constants
from momentum_reflow_alerts import deliver_due_wechat, observe_reflow_alerts, read_public_alerts

MOMENTUM_REFLOW_ALERT_SETTINGS = Path(_BASE_DIR) / "momentum_reflow_alert_settings.json"
MOMENTUM_REFLOW_ALERT_LEDGER = Path(_BASE_DIR) / "momentum_reflow_alerts.json"

_reflow_alert_lock = threading.Lock()
_reflow_alert_thread = None
_reflow_alert_stop = threading.Event()
_reflow_alert_wakeup = threading.Event()
_reflow_alert_status = {"running": False, "last_error": "", "last_delivery_at": 0}

def _process_reflow_alerts(payload, now_ms):
    try:
        created = observe_reflow_alerts(MOMENTUM_REFLOW_ALERT_LEDGER, payload.get("rows", []), now_ms)
    except Exception as error:
        with _reflow_alert_lock:
            _reflow_alert_status["last_error"] = str(error)[:80]
        return []
    if created:
        _reflow_alert_wakeup.set()
    return created

def _reflow_alert_worker_loop():
    with _reflow_alert_lock:
        _reflow_alert_status["running"] = True
    try:
        while not _reflow_alert_stop.is_set():
            try:
                result = deliver_due_wechat(
                    MOMENTUM_REFLOW_ALERT_SETTINGS, MOMENTUM_REFLOW_ALERT_LEDGER,
                    int(time.time() * 1000),
                )
                with _reflow_alert_lock:
                    _reflow_alert_status["last_error"] = result.get("error", "")
                    if result.get("status") == "delivered":
                        _reflow_alert_status["last_delivery_at"] = int(time.time() * 1000)
            except Exception as error:
                with _reflow_alert_lock:
                    _reflow_alert_status["last_error"] = str(error)[:80]
            _reflow_alert_wakeup.wait(30)
            _reflow_alert_wakeup.clear()
    finally:
        with _reflow_alert_lock:
            _reflow_alert_status["running"] = False

def _start_reflow_alert_worker():
    global _reflow_alert_thread
    with _reflow_alert_lock:
        if _reflow_alert_thread is not None and _reflow_alert_thread.is_alive():
            return False
        _reflow_alert_stop.clear()
        _reflow_alert_thread = threading.Thread(target=_reflow_alert_worker_loop, daemon=True)
        _reflow_alert_thread.start()
        return True

def _stop_reflow_alert_worker_for_tests():
    global _reflow_alert_thread
    _reflow_alert_stop.set(); _reflow_alert_wakeup.set()
    worker = _reflow_alert_thread
    if worker is not None:
        worker.join(timeout=2)
        if not worker.is_alive():
            _reflow_alert_thread = None
```

Change `_run_reflow_scan` to preserve one timestamp and isolate alerts:

```python
def _run_reflow_scan(progress):
    result = scan_momentum_reflow(MOMENTUM_REFLOW_LEDGER, progress=progress)
    now_ms = int(time.time() * 1000)
    payload = merge_reflow_signals(
        MOMENTUM_REFLOW_HISTORY, MOMENTUM_REFLOW_LEDGER, result, now_ms,
    )
    _process_reflow_alerts(payload, now_ms)
    return payload
```

Add the ordinary route near the other reflow APIs:

```python
@app.route("/api/reflow/alerts")
def reflow_alert_events():
    if not session.get("user_id"):
        return jsonify({"error": "未登录"}), 401
    raw = request.args.get("after", "0")
    try:
        after_id = int(raw)
        if after_id < 0:
            raise ValueError("negative cursor")
    except (TypeError, ValueError):
        return jsonify({"error": "after must be a nonnegative integer"}), 400
    try:
        payload = read_public_alerts(MOMENTUM_REFLOW_ALERT_LEDGER, after_id)
    except ValueError:
        return jsonify({"error": "警报暂不可用"}), 503
    return jsonify(payload)
```

Start both workers only in the executable path:

```python
if __name__ == "__main__":
    _start_reflow_scheduler()
    _start_reflow_alert_worker()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
```

- [ ] **Step 4: 运行接入与生命周期测试并确认GREEN**

Run: `python -m unittest discover -s tests -p 'test_momentum_reflow_integration.py' -v`，随后运行 `python -m unittest discover -s tests -p 'test_momentum_reflow_scheduler.py' -v`
Expected: all tests `OK`，且测试导入不启动真实通知线程。

- [ ] **Step 5: 提交扫描与工作器接入**

```powershell
git add web_ui.py tests/test_momentum_reflow_integration.py tests/test_momentum_reflow_scheduler.py
git commit -m "feat: connect reflow alerts to scan lifecycle"
```

---

### Task 4: 管理员Webhook配置、测试发送和状态面板

**Files:**
- Modify: `admin_server.py:5-10, 135-199, 434-518`
- Modify: `tests/test_momentum_reflow_admin.py`

**Interfaces:**
- Consumes: Task 1 settings/ledger functions and Task 2 `test_wechat_webhook`。
- Produces: `GET/POST /api/reflow/alert-settings`
- Produces: `POST /api/reflow/alert-settings/test`
- Produces: 管理后台Webhook开关、掩码输入、测试按钮和状态显示。

- [ ] **Step 1: 添加失败测试，覆盖认证、凭证保护、启用基线和测试发送**

```python
# tests/test_momentum_reflow_admin.py additions
class MomentumReflowAlertAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(); root = Path(self.temp_dir.name)
        self.settings = root / "alert_settings.json"; self.ledger = root / "alerts.json"
        self.patches = [
            patch.object(admin_server, "_REFLOW_ALERT_SETTINGS_PATH", self.settings, create=True),
            patch.object(admin_server, "_REFLOW_ALERT_LEDGER_PATH", self.ledger, create=True),
        ]
        [item.start() for item in self.patches]
        admin_server.app.config.update(TESTING=True); self.client = admin_server.app.test_client()
    def tearDown(self):
        [item.stop() for item in reversed(self.patches)]; self.temp_dir.cleanup()

    def test_alert_settings_require_admin(self):
        self.assertEqual(self.client.get("/api/reflow/alert-settings").status_code, 403)
        self.assertEqual(self.client.post("/api/reflow/alert-settings", json={}).status_code, 403)

    def test_admin_saves_secret_but_response_only_contains_mask(self):
        login_admin(self.client)
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-1234"
        response = self.client.post("/api/reflow/alert-settings",
            json={"wechat_enabled": False, "wechat_webhook": url})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(url, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["webhook_mask"], "****1234")

    def test_enabling_without_webhook_is_rejected(self):
        login_admin(self.client)
        response = self.client.post("/api/reflow/alert-settings",
            json={"wechat_enabled": True, "wechat_webhook": ""})
        self.assertEqual(response.status_code, 400)

    def test_enable_transition_baselines_existing_events(self):
        login_admin(self.client)
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret"
        def baseline_while_disabled(path):
            self.assertFalse(admin_server.load_alert_settings(self.settings)["wechat_enabled"])
            return 7
        with patch.object(admin_server, "baseline_wechat_delivery", side_effect=baseline_while_disabled) as baseline:
            response = self.client.post("/api/reflow/alert-settings",
                json={"wechat_enabled": True, "wechat_webhook": url})
        self.assertEqual(response.status_code, 200); baseline.assert_called_once_with(self.ledger)

    def test_corrupt_ledger_is_reported_unavailable_without_secret(self):
        login_admin(self.client)
        self.ledger.write_text("{broken", encoding="utf-8")
        response = self.client.get("/api/reflow/alert-settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["last_delivery_status"], "unavailable")
        self.assertNotIn("webhook/send?key=", response.get_data(as_text=True))

    def test_test_route_uses_saved_webhook_without_returning_it(self):
        login_admin(self.client)
        with patch.object(admin_server, "test_wechat_webhook",
                          return_value={"last_test_ok": True, "webhook_mask": "****1234"}) as send:
            response = self.client.post("/api/reflow/alert-settings/test")
        self.assertEqual(response.status_code, 200); self.assertTrue(response.get_json()["last_test_ok"]); send.assert_called_once()
```

Update the Node harness `ids` with:

```javascript
'reflowWechatEnabled','reflowWebhook','reflowWebhookMask','reflowWechatState',
'reflowWechatTest','reflowWechatTestState','reflowWechatDeliveryState','reflowAlertSaveError'
```

Add `value:''` to `newElement()` and capture fetch arguments exactly:

```javascript
global.fetch = function(){
  const request = deferred();
  request.args = Array.prototype.slice.call(arguments);
  requests.push(request);
  return request.promise;
};
```

Add this Node behavior test using the existing `run_admin_javascript` harness:

```python
def test_alert_controls_keep_mask_on_blank_save_and_call_test_route(self):
    run_admin_javascript(textwrap.dedent("""
      renderReflow(content);
      requests[0].resolve(response(true, {
        auto_scan_enabled:true, updated_at:1, updated_by:'admin',
        last_auto_scan_at:0, next_scan_at:2, last_auto_error:'', scheduler_status:'available'
      }));
      requests[1].resolve(response(true, {
        wechat_enabled:false, webhook_configured:true, webhook_mask:'****1234',
        last_test_at:0, last_test_ok:false, last_test_error:''
      }));
      await flush();
      assert.strictEqual(elements.reflowWebhookMask.textContent, '****1234');
      elements.reflowWechatEnabled.checked=true;
      elements.reflowWebhook.value='';
      saveReflowAlertSetting();
      assert.strictEqual(JSON.parse(requests[2].args[1].body).wechat_webhook, '');
      requests[2].resolve(response(true, {
        ok:true, wechat_enabled:true, webhook_configured:true, webhook_mask:'****1234'
      }));
      await flush();
      assert.strictEqual(elements.reflowWebhookMask.textContent, '****1234');
      testReflowWechat();
      assert.strictEqual(requests[3].args[0], '/api/reflow/alert-settings/test');
      requests[3].resolve(response(true, {last_test_ok:true, webhook_mask:'****1234'}));
      await flush();
      assert.strictEqual(elements.reflowWechatTestState.textContent, '测试消息已发送');
    """))
```

Extend the harness `global.fetch` to capture `args` on each deferred request before pushing it into `requests`.

- [ ] **Step 2: 运行管理测试并确认RED**

Run: `python -m unittest discover -s tests -p 'test_momentum_reflow_admin.py' -v`
Expected: missing routes/imports and DOM IDs fail。

- [ ] **Step 3: 实现管理员API，严格区分空值与新Webhook**

```python
# admin_server.py imports/constants
from momentum_reflow_alerts import (
    baseline_wechat_delivery, load_alert_settings, public_alert_settings, read_delivery_status,
    save_alert_settings, test_wechat_webhook, validate_wechat_webhook,
)
_REFLOW_ALERT_SETTINGS_PATH = Path(_BASE_DIR) / "momentum_reflow_alert_settings.json"
_REFLOW_ALERT_LEDGER_PATH = Path(_BASE_DIR) / "momentum_reflow_alerts.json"

@app.route("/api/reflow/alert-settings", methods=["GET", "POST"])
@admin_required
def api_reflow_alert_settings():
    if not session.get("admin_id"):
        return jsonify({"error": "无权限"}), 403
    if request.method == "GET":
        public = public_alert_settings(load_alert_settings(_REFLOW_ALERT_SETTINGS_PATH))
        try:
            public.update(read_delivery_status(_REFLOW_ALERT_LEDGER_PATH))
        except ValueError:
            public.update(last_delivery_at=0, last_delivery_status="unavailable",
                          last_delivery_alert_id=0, last_delivery_error="警报账本不可读，已停止发送")
        return jsonify(public)
    data = request.get_json(silent=True)
    enabled = data.get("wechat_enabled") if isinstance(data, dict) else None
    webhook = data.get("wechat_webhook") if isinstance(data, dict) else None
    if type(enabled) is not bool or webhook is not None and not isinstance(webhook, str):
        return jsonify({"error": "invalid alert settings"}), 400
    previous = load_alert_settings(_REFLOW_ALERT_SETTINGS_PATH)
    try:
        candidate = (webhook or previous["wechat_webhook"]).strip()
        if enabled:
            validate_wechat_webhook(candidate)
        if enabled and not previous["wechat_enabled"]:
            baseline_wechat_delivery(_REFLOW_ALERT_LEDGER_PATH)
        saved = save_alert_settings(
            _REFLOW_ALERT_SETTINGS_PATH, wechat_enabled=enabled,
            wechat_webhook=webhook, updated_by=str(session["admin_id"]),
            now_ms=int(time.time() * 1000),
        )
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"ok": True, **public_alert_settings(saved)})

@app.route("/api/reflow/alert-settings/test", methods=["POST"])
@admin_required
def api_reflow_alert_test():
    if not session.get("admin_id"):
        return jsonify({"error": "无权限"}), 403
    result = test_wechat_webhook(_REFLOW_ALERT_SETTINGS_PATH, int(time.time() * 1000))
    return jsonify({"ok": bool(result["last_test_ok"]), **result}), 200 if result["last_test_ok"] else 502
```

In `renderReflow`, append this alert card after the existing auto-scan settings card (reuse the surrounding card/button classes already present in `ADMIN_HTML`):

```html
<section class="reflow-alert-card">
  <label><input id="reflowWechatEnabled" type="checkbox"> 企业微信高质量警报</label>
  <span id="reflowWechatState"></span>
  <input id="reflowWebhook" type="password" autocomplete="off" placeholder="粘贴企业微信群机器人 Webhook">
  <span id="reflowWebhookMask">未配置</span>
  <button type="button" onclick="saveReflowAlertSetting()">保存警报设置</button>
  <button id="reflowWechatTest" type="button" onclick="testReflowWechat()">发送测试消息</button>
  <span id="reflowWechatTestState"></span>
  <span id="reflowWechatDeliveryState"></span>
  <span id="reflowAlertSaveError" role="alert"></span>
  <small>网络超时可能造成企业微信已收到、服务器未收到回执，有限重试时存在极低概率重复。</small>
</section>
```

Load `/api/reflow/alert-settings` when `renderReflow` loads; map only the masked/public fields, including `last_delivery_at/status/error`, into these IDs. Add:

```javascript
function saveReflowAlertSetting(){
  var enabled=document.getElementById('reflowWechatEnabled');
  var webhook=document.getElementById('reflowWebhook');
  fetch('/api/reflow/alert-settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({wechat_enabled:Boolean(enabled.checked),wechat_webhook:webhook.value})
  }).then(function(response){return response.json().then(function(data){if(!response.ok)throw new Error(data.error||'保存失败');return data;});})
    .then(function(data){webhook.value='';document.getElementById('reflowWebhookMask').textContent=data.webhook_mask||'未配置';})
    .catch(function(error){document.getElementById('reflowAlertSaveError').textContent=error.message;});
}
function testReflowWechat(){
  fetch('/api/reflow/alert-settings/test',{method:'POST'}).then(function(response){return response.json().then(function(data){if(!response.ok)throw new Error(data.last_test_error||data.error||'测试失败');return data;});})
    .then(function(){document.getElementById('reflowWechatTestState').textContent='测试消息已发送';})
    .catch(function(error){document.getElementById('reflowWechatTestState').textContent=error.message;});
}
```

- [ ] **Step 4: 运行管理端Python与Node行为测试并确认GREEN**

Run: `python -m unittest discover -s tests -p 'test_momentum_reflow_admin.py' -v`
Expected: all tests `OK`。

- [ ] **Step 5: 提交管理端配置**

```powershell
git add admin_server.py tests/test_momentum_reflow_admin.py
git commit -m "feat: manage reflow wechat alerts"
```

---

### Task 5: 全站金币落袋声音、浏览器游标和恢复提示

**Files:**
- Modify: `web_ui.py:1378-1382, 1644-1695, closing script initialization`
- Modify: `tests/test_momentum_reflow_integration.py`

**Interfaces:**
- Consumes: `GET /api/reflow/alerts?after=<id>`。
- Produces: `activateReflowAudio() -> Promise<boolean>`
- Produces: `playReflowCoinSound() -> Promise<boolean>`
- Produces: `setReflowSoundEnabled(enabled: boolean) -> Promise<void>`
- Produces: `pollReflowAlerts() -> Promise<void>`
- Produces: `testReflowCoinSound() -> Promise<boolean>`

- [ ] **Step 1: 增加Node行为测试，覆盖基线、多事件、菜单切换、关闭和阻止播放**

```python
# tests/test_momentum_reflow_integration.py
def run_alert_sound_javascript(test_body):
    source = Path("web_ui.py").read_text(encoding="utf-8")
    script = source[source.index("var REFLOW_ALERT_SOUND_KEY="):source.index("// ===== TRADER PANEL =====")]
    harness = """
const assert=require('assert');
let storage={}; global.localStorage={getItem:k=>storage[k]??null,setItem:(k,v)=>storage[k]=String(v)};
let fetchPayload={latest_alert_id:0,events:[]};
global.fetch=()=>Promise.resolve({ok:true,json:()=>Promise.resolve(fetchPayload)});
global.document={getElementById:()=>null}; global.window=global;
global.setInterval=(fn)=>{global.alertPoll=fn;return 9;};
"""
    completed = subprocess.run(["node", "-e", harness + script + test_body],
        check=True, capture_output=True, text=True, encoding="utf-8")
    return completed.stdout

def test_sound_first_enable_baselines_without_playing_and_batch_plays_once(self):
    body = """
let plays=0,activations=0;
activateReflowAudio=()=>{activations++;return Promise.resolve(true);};
playReflowCoinSound=()=>{plays++;return Promise.resolve(true);};
(async()=>{fetchPayload={latest_alert_id:4,events:[]};await setReflowSoundEnabled(true);
assert.equal(activations,1);assert.equal(plays,0);assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'4');
fetchPayload={latest_alert_id:6,events:[{alert_id:5},{alert_id:6}]};await pollReflowAlerts();
assert.equal(plays,1);assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'6');process.stdout.write('ok');})()
"""
    self.assertEqual(run_alert_sound_javascript(body), "ok")

def test_disabled_sound_does_not_poll_or_advance_cursor(self):
    body = """
(async()=>{storage[REFLOW_ALERT_SOUND_KEY]='0';storage[REFLOW_ALERT_CURSOR_KEY]='3';
fetchPayload={latest_alert_id:4,events:[{alert_id:4}]};await pollReflowAlerts();
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'3');process.stdout.write('ok');})()
"""
    self.assertEqual(run_alert_sound_javascript(body), "ok")

def test_coin_sound_starts_three_ascending_metallic_tones(self):
    body = """
let frequencies=[],starts=[],ramps=[];
class FakeAudioContext{
  constructor(){this.currentTime=10;this.destination={};}
  resume(){return Promise.resolve();}
  createOscillator(){return {type:'',frequency:{setValueAtTime:v=>frequencies.push(v)},
    connect:()=>{},start:v=>starts.push(v),stop:()=>{}};}
  createGain(){return {gain:{setValueAtTime:()=>{},exponentialRampToValueAtTime:(v,t)=>ramps.push([v,t])},
    connect:()=>{}};}
}
window.AudioContext=FakeAudioContext;
(async()=>{assert.equal(await playReflowCoinSound(),true);assert.deepEqual(frequencies,[880,1175,1568]);
assert.equal(starts.length,3);assert.equal(ramps.filter(x=>x[0]===0.0001).length,3);
process.stdout.write('ok');})()
"""
    self.assertEqual(run_alert_sound_javascript(body), "ok")

def test_blocked_playback_keeps_cursor_and_requests_user_gesture(self):
    body = """
let state={textContent:''};document.getElementById=id=>id==='reflowSoundState'?state:null;
class BlockedAudioContext{constructor(){this.currentTime=0;}resume(){return Promise.reject(new Error('blocked'));}}
window.AudioContext=BlockedAudioContext;storage[REFLOW_ALERT_SOUND_KEY]='1';storage[REFLOW_ALERT_CURSOR_KEY]='3';
(async()=>{fetchPayload={latest_alert_id:4,events:[{alert_id:4}]};await pollReflowAlerts();
assert.equal(storage[REFLOW_ALERT_CURSOR_KEY],'3');assert.equal(state.textContent,'需要点击恢复声音');
process.stdout.write('ok');})()
"""
    self.assertEqual(run_alert_sound_javascript(body), "ok")
```

- [ ] **Step 2: 运行浏览器测试并确认RED**

Run: `python -m unittest discover -s tests -p 'test_momentum_reflow_integration.py' -v`
Expected: sound constants/functions missing。

- [ ] **Step 3: 实现金币三音、全局轮询和本地游标**

```javascript
var REFLOW_ALERT_SOUND_KEY='axiom_reflow_alert_sound_v1';
var REFLOW_ALERT_CURSOR_KEY='axiom_reflow_alert_cursor_v1';
var _reflowAlertTimer=null,_reflowAudioContext=null,_reflowSoundNeedsGesture=false;

function updateReflowSoundControls(){
  var enabled=localStorage.getItem(REFLOW_ALERT_SOUND_KEY)==='1';
  var state=document.getElementById('reflowSoundState');
  if(state) state.textContent=_reflowSoundNeedsGesture?'需要点击恢复声音':(enabled?'已开启':'已关闭');
}
function activateReflowAudio(){
  var AudioCtor=window.AudioContext||window.webkitAudioContext;
  if(!AudioCtor) return Promise.resolve(false);
  _reflowAudioContext=_reflowAudioContext||new AudioCtor();
  return Promise.resolve(_reflowAudioContext.resume()).then(function(){
    _reflowSoundNeedsGesture=false;updateReflowSoundControls();return true;
  }).catch(function(){_reflowSoundNeedsGesture=true;updateReflowSoundControls();return false;});
}
function playReflowCoinSound(){
  var AudioCtor=window.AudioContext||window.webkitAudioContext;
  if(!AudioCtor) return Promise.resolve(false);
  _reflowAudioContext=_reflowAudioContext||new AudioCtor();
  return Promise.resolve(_reflowAudioContext.resume()).then(function(){
    var now=_reflowAudioContext.currentTime;
    [880,1175,1568].forEach(function(frequency,index){
      var start=now+index*0.18,osc=_reflowAudioContext.createOscillator(),gain=_reflowAudioContext.createGain();
      osc.type='triangle';osc.frequency.setValueAtTime(frequency,start);
      gain.gain.setValueAtTime(0.0001,start);gain.gain.exponentialRampToValueAtTime(0.22,start+0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001,start+0.32);
      osc.connect(gain);gain.connect(_reflowAudioContext.destination);osc.start(start);osc.stop(start+0.34);
    });
    _reflowSoundNeedsGesture=false;updateReflowSoundControls();return true;
  }).catch(function(){_reflowSoundNeedsGesture=true;updateReflowSoundControls();return false;});
}
function setReflowSoundEnabled(enabled){
  localStorage.setItem(REFLOW_ALERT_SOUND_KEY,enabled?'1':'0');
  if(!enabled){updateReflowSoundControls();return Promise.resolve();}
  return activateReflowAudio().then(function(){
    return fetch('/api/reflow/alerts?after=0').then(function(r){return r.json();});
  }).then(function(data){localStorage.setItem(REFLOW_ALERT_CURSOR_KEY,String(data.latest_alert_id||0));updateReflowSoundControls();});
}
function testReflowCoinSound(){return playReflowCoinSound();}
function pollReflowAlerts(){
  if(localStorage.getItem(REFLOW_ALERT_SOUND_KEY)!=='1') return Promise.resolve();
  var cursor=parseInt(localStorage.getItem(REFLOW_ALERT_CURSOR_KEY)||'0',10);
  return fetch('/api/reflow/alerts?after='+cursor).then(function(response){
    if(!response.ok) throw new Error('alert poll failed');return response.json();
  }).then(function(data){
    if(!Array.isArray(data.events)||!data.events.length) return;
    return playReflowCoinSound().then(function(played){
      if(played) localStorage.setItem(REFLOW_ALERT_CURSOR_KEY,String(data.latest_alert_id||cursor));
    });
  }).catch(function(){});
}
function initReflowAlertSound(){
  if(localStorage.getItem(REFLOW_ALERT_CURSOR_KEY)===null){
    fetch('/api/reflow/alerts?after=0').then(function(r){return r.ok?r.json():null;}).then(function(data){
      if(data) localStorage.setItem(REFLOW_ALERT_CURSOR_KEY,String(data.latest_alert_id||0));
    }).catch(function(){});
  }
  if(!_reflowAlertTimer) _reflowAlertTimer=setInterval(pollReflowAlerts,5000);
  updateReflowSoundControls();
}
```

Add this helper and interpolate its output once in the reflow filter area:

```javascript
function reflowSoundControls(){
  var enabled=localStorage.getItem(REFLOW_ALERT_SOUND_KEY)==='1';
  return '<div class="reflow-sound-controls">'
    +'<button type="button" onclick="setReflowSoundEnabled('+(!enabled)+')">'
    +(enabled?'关闭声音提醒':'开启声音提醒')+'</button>'
    +'<button type="button" onclick="testReflowCoinSound()">测试声音</button>'
    +'<span id="reflowSoundState"></span></div>';
}
```

Call `initReflowAlertSound()` once after the existing page initialization; do not stop its timer in `selectTab`. After rerendering the reflow page, call `updateReflowSoundControls()` so the current state is restored.

- [ ] **Step 4: 运行真实Node行为测试并确认GREEN**

Run: `python -m unittest discover -s tests -p 'test_momentum_reflow_integration.py' -v`
Expected: all tests `OK`，包括三音频率递增、批量只响一次和拒绝播放不推进游标。

- [ ] **Step 5: 提交浏览器声音**

```powershell
git add web_ui.py tests/test_momentum_reflow_integration.py
git commit -m "feat: add reflow coin sound alerts"
```

---

### Task 6: 集成复审、完整验证与文档一致性

**Files:**
- Modify if required by verified behavior: `docs/superpowers/specs/2026-08-01-reflow-high-quality-alerts-design.md`
- Modify if required by verified behavior: `docs/superpowers/plans/2026-08-01-reflow-high-quality-alerts.md`
- Test: all `tests/`

**Interfaces:**
- Consumes: Tasks 1-5 complete implementation.
- Produces: zero Critical/Important review findings and a clean verified commit.

- [ ] **Step 1: 运行警报专项测试**

Run:

```powershell
python -m unittest discover -s tests -p 'test_momentum_reflow_alerts.py' -v
python -m unittest discover -s tests -p 'test_momentum_reflow_admin.py' -v
python -m unittest discover -s tests -p 'test_momentum_reflow_integration.py' -v
python -m unittest discover -s tests -p 'test_momentum_reflow_scheduler.py' -v
```

Expected: all alert, admin, integration and lifecycle tests `OK`。

- [ ] **Step 2: 运行完整回归、编译和补丁检查**

Run:

```powershell
python -m unittest discover -s tests -q
python -m py_compile momentum_reflow_alerts.py momentum_reflow_dashboard.py momentum_reflow.py web_ui.py admin_server.py trader.py
git diff --check
git status --short
```

Expected: full suite `OK`；compile exit 0；diff check exit 0；只有本功能批准文件有变更。

- [ ] **Step 3: 完成安全定向检查**

Run:

```powershell
Get-ChildItem momentum_reflow_alerts.py,web_ui.py,admin_server.py,tests\test_momentum_reflow_alerts.py | Select-String -Pattern 'qyapi.weixin.qq.com.*secret|wechat_webhook.*jsonify|console.log.*webhook|print.*webhook'
```

Expected: 只允许测试中的虚拟Webhook与服务端URL前缀常量；生产响应、日志和前端模板无完整Webhook。

- [ ] **Step 4: 请求独立代码审查并修复全部Critical/Important**

Review focus:

- 基线和标准升级是否准确；
- 损坏账本是否失败关闭；
- 扫描是否与外部网络完全解耦；
- 重启和双触发是否去重；
- Webhook是否可能进入响应、日志或浏览器；
- 投递游标是否会跳过未处理事件；
- 浏览器自动播放失败时是否错误推进游标；
- 管理端空凭证是否覆盖旧值。

Expected: reviewer reports no remaining Critical/Important。发现问题后先写回归测试，再最小修复并重跑Steps 1-3。

- [ ] **Step 5: 提交最终审查修复（仅有修复时）**

```powershell
git add momentum_reflow_alerts.py web_ui.py admin_server.py tests
git commit -m "fix: harden reflow alert delivery"
```

---

### Task 7: 服务器部署与真实双通道验收（必须再次获得用户明确部署确认）

**Files:**
- Deploy: `momentum_reflow_alerts.py`, `web_ui.py`, `admin_server.py`
- Do not deploy: tests, specs, plans, memory files.
- Modify after successful deployment: `PROGRESS.md`

**Interfaces:**
- Consumes: verified implementation commit and user-provided Webhook entered through admin UI.
- Produces: active services, one successful test微信消息, verified browser sound, preserved trading state, deployment record.

- [ ] **Step 1: 在任何生产写操作前取得用户明确部署确认**

Expected: user explicitly says deploy/update server. Without it, stop after local handoff。

- [ ] **Step 2: 记录本地哈希并创建服务器备份**

Run local:

```powershell
Get-FileHash momentum_reflow_alerts.py,web_ui.py,admin_server.py -Algorithm SHA256
```

Run server after confirmation:

```bash
cd <deploy-dir>
stamp=$(date +%Y%m%d_%H%M%S)
backup="backups/reflow_alerts_$stamp"
mkdir -p "$backup"
cp -a web_ui.py admin_server.py demo_bot_config.json positions_<uid>.json trades_<uid>.jsonl axiom_accounts.db "$backup"/
test ! -e momentum_reflow_alerts.py || cp -a momentum_reflow_alerts.py "$backup"/
test ! -e momentum_reflow_alert_settings.json || cp -a momentum_reflow_alert_settings.json "$backup"/
test ! -e momentum_reflow_alerts.json || cp -a momentum_reflow_alerts.json "$backup"/
echo "$PWD/$backup"
sha256sum demo_bot_config.json positions_<uid>.json trades_<uid>.jsonl axiom_accounts.db
```

Expected: backup path and protected-state hashes captured before upload。

- [ ] **Step 3: 显式暂存上传、哈希核对和服务器编译**

```powershell
scp -i <repo-root>\<ssh-key> momentum_reflow_alerts.py root@<production-host>:<deploy-dir>/momentum_reflow_alerts.py.codex-new
scp -i <repo-root>\<ssh-key> web_ui.py root@<production-host>:<deploy-dir>/web_ui.py.codex-new
scp -i <repo-root>\<ssh-key> admin_server.py root@<production-host>:<deploy-dir>/admin_server.py.codex-new
```

Server:

```bash
cd <deploy-dir>
sha256sum momentum_reflow_alerts.py.codex-new web_ui.py.codex-new admin_server.py.codex-new
venv/bin/python3 -m py_compile momentum_reflow_alerts.py.codex-new web_ui.py.codex-new admin_server.py.codex-new
```

Expected: staged hashes equal local hashes; compile exit 0 before replacement。

- [ ] **Step 4: 原子替换并重启两个受影响服务**

```bash
cd <deploy-dir>
mv momentum_reflow_alerts.py.codex-new momentum_reflow_alerts.py
mv web_ui.py.codex-new web_ui.py
mv admin_server.py.codex-new admin_server.py
venv/bin/python3 -m py_compile momentum_reflow_alerts.py web_ui.py admin_server.py
systemctl restart macd-bot macd-admin
systemctl is-active macd-bot macd-admin
```

Expected: both services `active`。

- [ ] **Step 5: 通过管理后台保存Webhook并发送测试消息**

User action:

1. 管理后台打开“动能回流自动扫描”。
2. 在警报卡片输入Webhook，先保持正式提醒关闭并保存。
3. 点击“发送测试消息”，确认企业微信群收到`【AXIOM】企业微信警报测试成功`。
4. 开启企业微信正式提醒；系统以当前最新`alert_id`建立基线。

Expected: full Webhook never appears in browser response after save；测试状态成功；无历史警报补发。

- [ ] **Step 6: 验证浏览器声音和线上安全状态**

User action:

1. 在动能回流页点击“开启声音提醒”。
2. 点击“测试声音”，确认听到约1秒金币落袋三音。
3. 切换到其他AXIOM菜单，再次测试仍可听到。

Server checks:

```bash
curl -fsS http://127.0.0.1:5000/demo/status?fast=1 >/dev/null
curl -fsS http://127.0.0.1:5001/ >/dev/null
systemctl is-active macd-bot macd-admin
journalctl -u macd-bot -u macd-admin --since "10 minutes ago" --no-pager | grep -E "Traceback|ERROR|CRITICAL|Exception|ImportError|SyntaxError" || true
sha256sum demo_bot_config.json positions_<uid>.json trades_<uid>.jsonl axiom_accounts.db
stat -c '%a %n' momentum_reflow_alert_settings.json momentum_reflow_alerts.json
```

Expected: endpoints healthy; services active; no new matched errors; protected data unchanged except documented live position movement；两个警报JSON权限均为`600`（若服务账户策略要求组读，则明确记录并限制到该服务组）。真实高质量信号不人为制造，最终端到端正式警报需等待下一次自然信号。

- [ ] **Step 7: 用apply_patch更新PROGRESS.md并提交部署记录**

Record exactly:

- feature commit and test counts;
- server backup path;
- deployed files and service restarts;
- local/server hashes;
- test微信 and browser sound result;
- current alert baseline behavior;
- protected-state before/after comparison;
- journal and endpoint verification;
- any unverified natural-signal condition.

```powershell
git add PROGRESS.md
git commit -m "docs: record reflow alert deployment"
git status --short
```

Expected: clean worktree and committed factual deployment record。
