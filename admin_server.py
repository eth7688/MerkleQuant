"""
AXIOM QUANT — 独立管理后台
端口 5001 | 开发者专用 | 不依赖交易引擎
"""
import account_manager as accounts, json, math, os, time
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request, session
from functools import wraps
import secrets, os, sys
from momentum_reflow_dashboard import load_reflow_settings, save_reflow_settings
from momentum_reflow_alerts import (
    enable_wechat_alerts,
    load_alert_settings,
    public_alert_settings,
    read_delivery_status,
    save_alert_settings,
    test_wechat_webhook,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'axiom-quant-titanium-2026-secure-key')

def login_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('admin_id'):
            return jsonify({"error": "未登录"}), 401
        return f(*a, **kw)
    return wrap

def admin_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if session.get('role') != 'admin':
            return jsonify({"error": "无权限"}), 403
        return f(*a, **kw)
    return wrap

# ═══ ROUTES ═══
@app.route("/")
def index():
    return render_template_string(ADMIN_HTML, title="AXIOM Admin")

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    username = (data.get("username","") or "").strip()
    password = data.get("password","") or ""
    user = accounts.authenticate(username, password)
    if not user or user['role'] != 'admin':
        diag = accounts.diagnose_auth(username, password)
        msg_map = {
            "user_not_found": "认证失败: 管理员账号不存在",
            "user_disabled": "认证失败: 账号已禁用",
            "password_invalid": "认证失败: 密码不匹配",
            "not_admin": "认证失败: 该账号不是管理员",
        }
        return jsonify({"ok": False, "msg": msg_map.get(diag.get("reason"), "认证失败"), "reason": diag.get("reason")})
    session['admin_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    return jsonify({"ok": True, "user": user['username']})

@app.route("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/stats")
@admin_required
def api_stats():
    return jsonify(accounts.admin_get_stats())

@app.route("/api/users")
@admin_required
def api_users():
    users = accounts.admin_get_all_users()
    result = []
    for u in users:
        valid, plan, days = accounts.check_license_valid(u['id'])
        bal = accounts.get_fuel_balance(u['id'])
        result.append({"id": u['id'], "username": u['username'], "role": u['role'],
                       "active": bool(u['active']), "created_at": u['created_at'],
                       "license_valid": valid, "plan": plan, "days_left": days,
                       "fuel_balance": round(bal, 2)})
    return jsonify(result)

@app.route("/api/users/<int:uid>/toggle", methods=["POST"])
@admin_required
def api_toggle_user(uid):
    data = request.get_json() or {}
    accounts.admin_set_user_active(uid, data.get("active", True))
    return jsonify({"ok": True})

@app.route("/api/license", methods=["POST"])
@admin_required
def api_issue_license():
    data = request.get_json() or {}
    key = accounts.issue_license(int(data.get("user_id",0)), data.get("plan","standard"), int(data.get("days",30)))
    return jsonify({"ok": True, "key": key})

@app.route("/api/fuel/history/<int:uid>")
@admin_required
def api_fuel_history(uid):
    return jsonify(accounts.get_fuel_history(uid, 50))

@app.route("/api/fuel/topup", methods=["POST"])
@admin_required
def api_fuel_topup():
    data = request.get_json() or {}
    uid = int(data.get("user_id", 0))
    amount = float(data.get("amount", 0))
    note = data.get("note", "管理员充值")
    if uid <= 0 or amount <= 0:
        return jsonify({"ok": False, "msg": "参数无效"}), 400
    bal = accounts.fuel_topup(uid, amount, note)
    return jsonify({"ok": True, "balance": round(bal, 2)})

@app.route("/api/fuel/deduct", methods=["POST"])
@admin_required
def api_fuel_deduct():
    data = request.get_json() or {}
    uid = int(data.get("user_id", 0))
    amount = float(data.get("amount", 0))
    note = data.get("note", "管理员扣费")
    if uid <= 0 or amount <= 0:
        return jsonify({"ok": False, "msg": "参数无效"}), 400
    bal = accounts.fuel_deduct(uid, amount, note)
    return jsonify({"ok": True, "balance": round(bal, 2)})

@app.route("/api/users/<int:uid>/password", methods=["POST"])
@admin_required
def api_reset_password(uid):
    data = request.get_json() or {}
    new_pw = data.get("password", "")
    if len(new_pw) < 6:
        return jsonify({"ok": False, "msg": "密码至少6位"}), 400
    success = accounts.reset_password(uid, new_pw)
    return jsonify({"ok": success})

# Proxy to web_ui demo engine APIs
import requests as _requests
_WEB_UI = "http://127.0.0.1:5000"
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_REFLOW_SETTINGS_PATH = Path(_BASE_DIR) / "momentum_reflow_settings.json"
_REFLOW_ALERT_SETTINGS_PATH = Path(_BASE_DIR) / "momentum_reflow_alert_settings.json"
_REFLOW_ALERT_LEDGER_PATH = Path(_BASE_DIR) / "momentum_reflow_alerts.json"

def _normalize_reflow_scheduler_status(scheduler):
    if not isinstance(scheduler, dict):
        raise ValueError("scheduler status must be an object")
    status = {}
    for field in ("last_auto_scan_at", "next_scan_at"):
        value = scheduler.get(field, 0)
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{field} must be a finite nonnegative number")
        status[field] = value
    last_error = scheduler.get("last_auto_error", "")
    if type(last_error) is not str:
        raise ValueError("last_auto_error must be a string")
    scheduler_status = scheduler.get("scheduler_status", "available")
    if type(scheduler_status) is not str:
        raise ValueError("scheduler_status must be a string")
    running = scheduler.get("running", False)
    if type(running) is not bool:
        raise ValueError("running must be a boolean")
    return {
        **status,
        "last_auto_error": last_error,
        "scheduler_status": scheduler_status,
        "running": running,
    }


def _compression_status_facts(payload):
    if not isinstance(payload, dict):
        raise ValueError("compression status must be an object")
    monitor = payload.get("monitor")
    scan = payload.get("scan")
    if not isinstance(monitor, dict) or not isinstance(scan, dict):
        raise ValueError("compression status is incomplete")
    monitor_fields = (
        "running", "auto_enabled", "last_scan_at", "next_scan_at",
        "structure_scanning", "scan_started_at", "scan_duration_ms", "last_error",
    )
    scan_fields = ("scanned", "eligible", "errors")
    return {
        "monitor": {field: monitor.get(field) for field in monitor_fields},
        "scan": {field: scan.get(field) for field in scan_fields},
    }


@app.route("/api/compression/settings", methods=["GET", "POST"])
@admin_required
def api_compression_settings():
    if not session.get("admin_id"):
        return jsonify({"error": "无权限"}), 403
    try:
        if request.method == "POST":
            data = request.get_json(silent=True)
            enabled = data.get("enabled") if isinstance(data, dict) else None
            if type(enabled) is not bool:
                return jsonify({"error": "enabled must be a boolean"}), 400
            response = _requests.post(
                f"{_WEB_UI}/internal/compression/automation",
                json={"enabled": enabled}, timeout=3,
            )
            response.raise_for_status()
            return jsonify(response.json())
        response = _requests.get(
            f"{_WEB_UI}/internal/compression/status", timeout=3
        )
        response.raise_for_status()
        return jsonify(_compression_status_facts(response.json()))
    except Exception:
        return jsonify({"error": "压缩监控暂不可用"}), 503

@app.route("/api/reflow/settings", methods=["GET", "POST"])
@admin_required
def api_reflow_settings():
    if not session.get("admin_id"):
        return jsonify({"error": "无权限"}), 403
    if request.method == "POST":
        data = request.get_json(silent=True)
        enabled = data.get("auto_scan_enabled") if isinstance(data, dict) else None
        if type(enabled) is not bool:
            return jsonify({"error": "auto_scan_enabled must be boolean"}), 400
        saved = save_reflow_settings(
            _REFLOW_SETTINGS_PATH,
            enabled,
            str(session.get("admin_id", "")),
            int(time.time() * 1000),
        )
        return jsonify({"ok": True, **saved})

    settings = load_reflow_settings(_REFLOW_SETTINGS_PATH)
    try:
        response = _requests.get(
            f"{_WEB_UI}/api/reflow/automation/status", timeout=3
        )
        response.raise_for_status()
        scheduler = response.json()
        status = _normalize_reflow_scheduler_status(scheduler)
    except Exception:
        status = {"scheduler_status": "unavailable"}
    return jsonify({**settings, **status})


@app.route("/api/reflow/alert-settings", methods=["GET", "POST"])
@admin_required
def api_reflow_alert_settings():
    if not session.get("admin_id"):
        return jsonify({"error": "无权限"}), 403
    if request.method == "GET":
        try:
            public = public_alert_settings(
                load_alert_settings(_REFLOW_ALERT_SETTINGS_PATH)
            )
        except (TypeError, ValueError):
            return jsonify({"error": "警报设置不可读"}), 500
        try:
            public.update(read_delivery_status(_REFLOW_ALERT_LEDGER_PATH))
        except ValueError:
            public.update(
                last_delivery_at=0,
                last_delivery_status="unavailable",
                last_delivery_alert_id=0,
                last_delivery_error="警报账本不可读，已停止发送",
            )
        return jsonify(public)

    data = request.get_json(silent=True)
    enabled = data.get("wechat_enabled") if isinstance(data, dict) else None
    webhook = data.get("wechat_webhook") if isinstance(data, dict) else None
    if type(enabled) is not bool or (
        webhook is not None and not isinstance(webhook, str)
    ):
        return jsonify({"error": "invalid alert settings"}), 400
    try:
        if enabled:
            saved = enable_wechat_alerts(
                _REFLOW_ALERT_SETTINGS_PATH,
                _REFLOW_ALERT_LEDGER_PATH,
                wechat_webhook=webhook,
                updated_by=str(session["admin_id"]),
                now_ms=int(time.time() * 1000),
            )
        else:
            saved = save_alert_settings(
                _REFLOW_ALERT_SETTINGS_PATH,
                wechat_enabled=False,
                wechat_webhook=webhook,
                updated_by=str(session["admin_id"]),
                now_ms=int(time.time() * 1000),
            )
    except (TypeError, ValueError):
        return jsonify({"error": "invalid alert settings"}), 400
    return jsonify({"ok": True, **public_alert_settings(saved)})


@app.route("/api/reflow/alert-settings/test", methods=["POST"])
@admin_required
def api_reflow_alert_test():
    if not session.get("admin_id"):
        return jsonify({"error": "无权限"}), 403
    try:
        result = test_wechat_webhook(
            _REFLOW_ALERT_SETTINGS_PATH, int(time.time() * 1000)
        )
    except (TypeError, ValueError):
        return jsonify({"ok": False, "last_test_ok": False,
                        "last_test_error": "警报设置不可读"}), 400
    status = 200 if result["last_test_ok"] else 502
    return jsonify({"ok": bool(result["last_test_ok"]), **result}), status

@app.route("/api/admin/demo/status")
@admin_required
def proxy_demo_status():
    r = _requests.get(f"{_WEB_UI}/api/admin/demo/status", timeout=10)
    return jsonify(r.json())

@app.route("/api/admin/demo/start")
@admin_required
def proxy_demo_start():
    r = _requests.get(f"{_WEB_UI}/api/admin/demo/start", timeout=10)
    return jsonify(r.json())

@app.route("/api/admin/demo/stop")
@admin_required
def proxy_demo_stop():
    r = _requests.get(f"{_WEB_UI}/api/admin/demo/stop", timeout=10)
    return jsonify(r.json())

@app.route("/api/admin/demo/config", methods=["GET","POST"])
@admin_required
def proxy_demo_config():
    try:
        if request.method == "POST":
            data = request.get_json(force=True) or {}  # force: 不检查Content-Type
            r = _requests.post(f"{_WEB_UI}/api/admin/demo/config", json=data, timeout=10)
            if r.status_code != 200:
                return jsonify({"ok": False, "msg": f"上游错误 {r.status_code}: {r.text[:200]}"}), 500
        else:
            r = _requests.get(f"{_WEB_UI}/api/admin/demo/config", timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "msg": f"代理请求失败: {str(e)}"}), 500

@app.route("/api/admin/demo/close/<symbol>")
@admin_required
def proxy_demo_close_one(symbol):
    try:
        r = _requests.get(f"{_WEB_UI}/api/admin/demo/close/{symbol}", timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "msg": f"平仓请求失败: {str(e)}"}), 500

@app.route("/api/admin/demo/close_all")
@admin_required
def proxy_demo_close_all():
    try:
        r = _requests.get(f"{_WEB_UI}/api/admin/demo/close_all", timeout=10)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"ok": False, "msg": f"平仓请求失败: {str(e)}"}), 500

# Demo config
_DEMO_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'demo_config.json')
def _load_demo_cfg():
    if os.path.exists(_DEMO_CFG_PATH):
        with open(_DEMO_CFG_PATH) as f: return json.load(f)
    return {"cfg_maxval":5000,"cfg_risk":200,"cfg_atr_mult":3,"cfg_maxpos":10,"cfg_interval":"30m"}

@app.route("/api/demo/config", methods=["GET","POST"])
@admin_required
def api_demo_config():
    if request.method == "POST":
        data = request.get_json() or {}
        cfg = _load_demo_cfg()
        cfg.update({k: data[k] for k in cfg if k in data})
        with open(_DEMO_CFG_PATH, 'w') as f: json.dump(cfg, f)
    return jsonify(_load_demo_cfg())

@app.route("/api/fuel/users")
@admin_required
def api_fuel_users():
    """返回所有用户的燃料余额"""
    users = accounts.admin_get_all_users()
    result = []
    for u in users:
        bal = accounts.get_fuel_balance(u['id'])
        result.append({"id": u['id'], "username": u['username'], "fuel_balance": round(bal, 2)})
    return jsonify(result)

# ═══ HTML ═══
ADMIN_HTML = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AXIOM ADMIN | 管理控制台</title>
<style>
:root{--bg:#0b0c0e;--card:#141518;--border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.1);--text:#e0e2e6;--text2:#8a8d92;--muted:#585b60;--brand:#2dd4bf;--s-green:#34d399;--s-red:#f87171;--s-yellow:#fbbf24;--s-blue:#60a5fa;--s-purple:#a855f7;--font:'Inter','SF Pro Display',system-ui,sans-serif;--font-mono:'JetBrains Mono','SF Mono',monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px;display:flex;min-height:100vh;-webkit-font-smoothing:antialiased}
.sidebar{width:220px;background:var(--card);border-right:1px solid var(--border);padding:24px 0;display:flex;flex-direction:column}
.logo{padding:0 20px 24px;border-bottom:1px solid var(--border);margin-bottom:24px}
.logo h1{font-size:16px;font-weight:800;color:var(--brand);letter-spacing:-0.02em}
.logo span{font-size:10px;color:var(--muted);display:block;margin-top:4px}
.nav-item{padding:10px 20px;cursor:pointer;font-size:13px;color:var(--text2);border-left:2px solid transparent;transition:all 0.15s;font-weight:500}
.nav-item:hover{color:var(--text);background:rgba(255,255,255,0.02)}
.nav-item.active{color:var(--brand);border-left-color:var(--brand);background:rgba(45,212,191,0.04)}
.main{flex:1;padding:32px;overflow-y:auto}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:16px}
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}
.stat-box{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 20px}
.stat-box .label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px}
.stat-box .value{font-size:28px;font-weight:800;font-family:var(--font-mono)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px 14px;border-bottom:1px solid var(--border);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em}
td{padding:10px 14px;border-bottom:1px solid var(--border);font-family:var(--font-mono);font-size:12px}
.btn{padding:8px 18px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;border:1px solid var(--border);background:var(--card);color:var(--text);transition:all 0.15s}
.btn:hover{background:var(--border2)}
.btn-primary{background:var(--brand);color:#000;border-color:var(--brand);font-weight:700}
.btn-danger{color:var(--s-red);border-color:rgba(248,113,113,0.2)}
.btn-danger:hover{background:var(--s-red);color:#fff}
input,select{background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:6px;font-size:13px;width:100%;font-family:var(--font)}
input:focus,select:focus{outline:none;border-color:var(--brand)}
.badge{display:inline-block;padding:3px 8px;border-radius:4px;font-size:10px;font-weight:600;letter-spacing:0.04em}
.badge-green{background:rgba(52,211,153,0.1);color:var(--s-green)}
.badge-red{background:rgba(248,113,113,0.1);color:var(--s-red)}
.badge-blue{background:rgba(96,165,250,0.1);color:var(--s-blue)}
.badge-yellow{background:rgba(251,191,36,0.1);color:var(--s-yellow)}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;z-index:999;display:none}
.modal-overlay.show{display:flex}
.modal{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;width:400px;max-width:90vw}
.modal h3{font-size:16px;margin-bottom:16px;color:var(--brand)}
.modal .field{margin-bottom:12px}
.modal .field label{display:block;font-size:10px;color:var(--muted);text-transform:uppercase;margin-bottom:4px}
.login-box{max-width:400px;margin:120px auto}
.field{margin-bottom:10px}
.field label{display:block;font-size:10px;color:var(--muted);text-transform:uppercase;margin-bottom:4px;letter-spacing:0.03em}
.t-section{border:1px solid var(--border);border-radius:6px;margin-bottom:10px;overflow:hidden;background:linear-gradient(180deg,rgba(20,21,24,0.9) 0%,rgba(20,21,24,0.95) 100%)}
.t-section-header{display:flex;align-items:center;gap:8px;padding:10px 12px;cursor:pointer;user-select:none;font-size:12px;font-weight:520;color:var(--text2);transition:background 0.15s}
.t-section-header:hover{background:rgba(255,255,255,0.015)}
.t-section-header .t-arrow{font-size:7px;color:var(--muted);margin-left:auto;transform:rotate(0deg);transition:transform 0.2s}
.t-section.open .t-section-header .t-arrow{transform:rotate(90deg)}
.t-section-body{max-height:0;overflow:hidden;transition:max-height 0.3s ease;padding:0 12px}
.t-section.open .t-section-body{max-height:600px;padding:0 12px 10px}
.t-section-indicator{width:5px;height:5px;border-radius:50%;flex-shrink:0}
</style></head><body>
<div class="sidebar">
  <div class="logo"><h1>AXIOM ADMIN</h1><span>管理控制台 v1.0</span></div>
  <div class="nav-item active" data-page="dashboard" onclick="switchPage('dashboard')">📊 运营看板</div>
  <div class="nav-item" data-page="users" onclick="switchPage('users')">👥 用户管理</div>
  <div class="nav-item" data-page="license" onclick="switchPage('license')">🔑 许可管理</div>
  <div class="nav-item" data-page="fuel" onclick="switchPage('fuel')">⛽ 燃料管理</div>
  <div class="nav-item" data-page="demo" onclick="switchPage('demo')">📡 回测参数</div>
  <div class="nav-item" data-page="reflow" onclick="switchPage('reflow')">↺ 动能回流</div>
  <div class="nav-item" data-page="compression" onclick="switchPage('compression')">◈ 压缩扫描</div>
  <div class="nav-item" data-page="engine" onclick="switchPage('engine')">⚙ 演示引擎</div>
  <div style="margin-top:auto;padding:20px;border-top:1px solid var(--border)"><span style="font-size:11px;color:var(--muted)" id="loginInfo">未登录</span><br><a href="#" onclick="doLogout()" style="font-size:10px;color:var(--s-red)">退出</a></div>
</div>
<div class="main" id="main">
  <div class="login-box card" id="loginBox" style="padding:32px">
    <h3 style="color:var(--brand);text-align:center;margin-bottom:24px;font-size:20px">AXIOM ADMIN</h3>
    <div class="field"><label>管理员账号</label><input type="text" id="loginUser" placeholder="admin" onkeydown="if(event.key==='Enter')doLogin()"></div>
    <div class="field"><label>密码</label><input type="password" id="loginPwd" onkeydown="if(event.key==='Enter')doLogin()"></div>
    <button class="btn btn-primary" style="width:100%;margin-top:8px" onclick="doLogin()">登录管理后台</button>
    <p id="loginErr" style="color:var(--s-red);font-size:12px;margin-top:8px;display:none;text-align:center"></p>
  </div>
  <div id="content" style="display:none"></div>
</div>
<div class="modal-overlay" id="licenseModal"><div class="modal"><h3>颁发许可证</h3><div class="field"><label>用户ID</label><input type="number" id="licUid"></div><div class="field"><label>套餐</label><select id="licPlan"><option value="trial">试用 (Trial)</option><option value="standard" selected>标准 (Standard)</option><option value="pro">专业 (Pro)</option></select></div><div class="field"><label>天数</label><input type="number" id="licDays" value="30"></div><div style="display:flex;gap:10px;margin-top:16px"><button class="btn btn-primary" onclick="issueLicense()">颁发</button><button class="btn" onclick="closeModal()">取消</button></div></div></div>
<div class="modal-overlay" id="fuelModal"><div class="modal"><h3>燃料充值 - <span id="fuelUname"></span></h3><input type="hidden" id="fuelUid"><div class="field"><label>充值金额 (USDT)</label><input type="number" id="fuelAmt" value="100" min="1"></div><div style="display:flex;gap:10px;margin-top:16px"><button class="btn btn-primary" onclick="doFuelTopup()">确认充值</button><button class="btn" onclick="closeFuelModal()">取消</button></div></div></div>
<div class="modal-overlay" id="deductModal"><div class="modal"><h3>燃料扣费 - <span id="deductUname"></span></h3><input type="hidden" id="deductUid"><div class="field"><label>扣费金额 (USDT)</label><input type="number" id="deductAmt" value="10" min="1"></div><div class="field"><label>扣费说明</label><input type="text" id="deductNote" placeholder="管理员扣费"></div><div style="display:flex;gap:10px;margin-top:16px"><button class="btn btn-primary" style="background:var(--s-red)" onclick="doFuelDeduct()">确认扣费</button><button class="btn" onclick="closeDeductModal()">取消</button></div></div></div>
<div class="modal-overlay" id="pwdModal"><div class="modal"><h3>重置密码 - <span id="pwdUname"></span></h3><input type="hidden" id="pwdUid"><div class="field"><label>新密码 (6位以上)</label><input type="password" id="pwdNew" placeholder="输入新密码"></div><div style="display:flex;gap:10px;margin-top:16px"><button class="btn btn-primary" onclick="doResetPwd()">确认重置</button><button class="btn" onclick="closePwdModal()">取消</button></div></div></div>
<script>
let _currentPage='dashboard';
function toggleSection(hdr){hdr.parentElement.classList.toggle('open')}
function api(url, opts){return fetch(url,{headers:{'Content-Type':'application/json'},...opts}).then(r=>r.json())}
function doLogin(){
  var u=document.getElementById('loginUser').value, p=document.getElementById('loginPwd').value;
  api('/api/login',{method:'POST',body:JSON.stringify({username:u,password:p})}).then(d=>{
    if(d.ok){document.getElementById('loginBox').style.display='none';document.getElementById('content').style.display='block';
      document.getElementById('loginInfo').textContent='admin';switchPage('dashboard')}
    else{var e=document.getElementById('loginErr');e.style.display='block';e.textContent=d.msg}
  })
}
function doLogout(){api('/api/logout').then(()=>location.reload())}
var _refreshTimer=null;
function switchPage(p){
  _currentPage=p;document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('active',n.dataset.page===p));
  if(_refreshTimer){clearInterval(_refreshTimer);_refreshTimer=null;}
  var c=document.getElementById('content');
  if(p==='dashboard') renderDashboard(c);
  else if(p==='users') renderUsers(c);
  else if(p==='license') renderLicense(c);
  else if(p==='fuel') renderFuel(c);
  else if(p==='demo') renderDemoCfg(c);
  else if(p==='reflow') renderReflow(c);
  else if(p==='compression') renderCompression(c);
  else if(p==='engine'){renderEngine(c);_refreshTimer=setInterval(renderDemoPositions,2000);}
}
function renderDashboard(el){
  api('/api/stats').then(s=>{
    el.innerHTML='<div class="stat-grid">'+
    '<div class="stat-box"><div class="label">总用户</div><div class="value" style="color:var(--s-blue)">'+s.total_users+'</div></div>'+
    '<div class="stat-box"><div class="label">活跃用户</div><div class="value" style="color:var(--s-green)">'+s.active_users+'</div></div>'+
    '<div class="stat-box"><div class="label">总交易数</div><div class="value" style="color:#fff">'+s.total_trades+'</div></div>'+
    '<div class="stat-box"><div class="label">燃料收入</div><div class="value" style="color:var(--s-yellow)">$'+s.total_fuel_income.toFixed(2)+'</div></div>'+
    '</div>';
  })
}
function renderUsers(el){
  api('/api/users').then(users=>{
    var h='<div class="card"><h3 style="margin-bottom:16px;font-size:15px;color:var(--brand)">用户列表 ('+users.length+')</h3><table><thead><tr><th>ID</th><th>用户名</th><th>角色</th><th>状态</th><th>许可</th><th>剩余</th><th>燃料</th><th>注册</th><th>操作</th></tr></thead><tbody>';
    users.forEach(u=>{h+='<tr><td>'+u.id+'</td><td>'+u.username+'</td><td><span class="badge '+(u.role==='admin'?'badge-blue':'badge-green')+'">'+u.role+'</span></td><td><span class="badge '+(u.active?'badge-green':'badge-red')+'">'+(u.active?'启用':'禁用')+'</span></td><td><span class="badge '+(u.license_valid?'badge-green':'badge-red')+'">'+u.plan+'</span></td><td style="color:'+(u.days_left>30?'var(--s-green)':u.days_left>0?'var(--s-yellow)':'var(--s-red)')+'">'+u.days_left+'天</td><td>$'+u.fuel_balance+'</td><td>'+u.created_at.slice(0,10)+'</td><td style="display:flex;gap:3px;flex-wrap:wrap"><button class="btn btn-danger" onclick="toggleUser('+u.id+','+!u.active+')">'+(u.active?'禁用':'启用')+'</button><button class="btn" onclick="openLicense('+u.id+')">许可</button><button class="btn" onclick="openFuelTopup('+u.id+',\''+u.username+'\')">充值</button><button class="btn" onclick="openFuelDeduct('+u.id+',\''+u.username+'\')" style="color:var(--s-red);border-color:rgba(248,113,113,0.2)">扣费</button><button class="btn" onclick="openPwdReset('+u.id+',\''+u.username+'\')">密码</button></td></tr>'})
    h+='</tbody></table></div>';el.innerHTML=h;
  })
}
function renderLicense(el){
  el.innerHTML='<div class="card"><h3 style="margin-bottom:16px;font-size:15px;color:var(--brand)">许可管理</h3><p style="color:var(--muted);font-size:13px">在用户管理页面点击"许可"按钮颁发许可证, 或在燃料管理页面为用户充值。</p></div>';
}
function renderFuel(el){
  api('/api/fuel/users').then(users=>{
    var h='<div class="card"><h3 style="margin-bottom:16px;font-size:15px;color:var(--brand)">燃料管理</h3>';
    h+='<table><thead><tr><th>ID</th><th>用户名</th><th>燃料余额</th><th>操作</th></tr></thead><tbody>';
    users.forEach(u=>{
      h+='<tr><td>'+u.id+'</td><td>'+u.username+'</td><td style="color:'+(u.fuel_balance>10?'var(--s-green)':u.fuel_balance>0?'var(--s-yellow)':'var(--s-red)')+';font-weight:700">$'+u.fuel_balance+'</td>';
      h+='<td style="display:flex;gap:4px"><button class="btn" onclick="openFuelTopup('+u.id+',\''+u.username+'\')">充值</button><button class="btn" onclick="openFuelDeduct('+u.id+',\''+u.username+'\')" style="color:var(--s-red)">扣费</button><button class="btn" onclick="viewFuelHistory('+u.id+',\''+u.username+'\')">流水</button></td></tr>';
    });
    h+='</tbody></table></div>';el.innerHTML=h;
  });
}
function formatCompressionTime(value){
  if(!value) return '--';
  if(typeof value==='string') return value.replace('T',' ').replace('+00:00',' UTC');
  return formatReflowTime(value);
}
function renderCompression(el){
  el.innerHTML='<div class="card" style="max-width:760px">'+
    '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:20px">'+
      '<div><h3 style="font-size:15px;color:var(--brand);margin-bottom:6px">压缩扫描自动监控</h3><p style="color:var(--muted);font-size:12px;line-height:1.7">仅控制监控引擎，不修改告警或交易配置。</p></div>'+
      '<label style="display:flex;align-items:center;gap:8px;color:var(--text2);font-size:12px;cursor:pointer"><input type="checkbox" id="compressionAutoEnabled" style="width:auto">自动扫描</label>'+
    '</div><div id="compressionSaveError" role="alert" style="display:none;padding:8px 10px;border-radius:5px;background:rgba(248,113,113,0.1);color:var(--s-red);font-size:12px;margin-bottom:14px"></div>'+
    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px">'+
      '<div class="stat-box"><div class="label">自动状态</div><div class="value" id="compressionEnabledState" style="font-size:18px">--</div></div>'+
      '<div class="stat-box"><div class="label">引擎状态</div><div id="compressionRunning" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">上次扫描</div><div id="compressionLastScan" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">下次扫描</div><div id="compressionNextScan" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">当前扫描</div><div id="compressionCurrentScan" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">扫描耗时</div><div id="compressionDuration" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">已扫描</div><div id="compressionScanned" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">合格候选</div><div id="compressionEligible" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">错误数</div><div id="compressionErrors" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">最近错误</div><div id="compressionLastError" style="font-size:12px;color:var(--s-red)">--</div></div>'+
    '</div></div>';
  var box=document.getElementById('compressionAutoEnabled');
  var error=document.getElementById('compressionSaveError');
  box.disabled=true;
  fetch('/api/compression/settings').then(function(response){return response.json().then(function(data){if(!response.ok) throw new Error(data.error||'读取设置失败');return data;});}).then(function(data){
    if(_currentPage!=='compression'||document.getElementById('compressionAutoEnabled')!==box) return;
    var monitor=data.monitor||{}, scan=data.scan||{};
    box.checked=Boolean(monitor.auto_enabled); box.dataset.savedChecked=String(box.checked); box.disabled=false;
    document.getElementById('compressionEnabledState').textContent=box.checked?'已启用':'已关闭';
    document.getElementById('compressionRunning').textContent=monitor.running?'运行中':'已停止';
    document.getElementById('compressionLastScan').textContent=formatCompressionTime(monitor.last_scan_at);
    document.getElementById('compressionNextScan').textContent=formatCompressionTime(monitor.next_scan_at);
    document.getElementById('compressionCurrentScan').textContent=monitor.structure_scanning?'扫描中':'空闲';
    document.getElementById('compressionDuration').textContent=(monitor.scan_duration_ms||0)+' ms';
    document.getElementById('compressionScanned').textContent=scan.scanned||0;
    document.getElementById('compressionEligible').textContent=scan.eligible||0;
    document.getElementById('compressionErrors').textContent=scan.errors||0;
    document.getElementById('compressionLastError').textContent=monitor.last_error||'无';
    box.onchange=saveCompressionSetting;
  }).catch(function(reason){if(_currentPage!=='compression'||document.getElementById('compressionAutoEnabled')!==box) return;error.style.display='block';error.textContent=reason.message||'读取设置失败';});
}
function saveCompressionSetting(){
  var box=document.getElementById('compressionAutoEnabled');
  var error=document.getElementById('compressionSaveError');
  var previous=box.dataset.savedChecked==='true';
  error.style.display='none'; error.textContent=''; box.disabled=true;
  fetch('/api/compression/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:Boolean(box.checked)})}).then(function(response){return response.json().then(function(data){if(!response.ok) throw new Error(data.error||'保存失败');return data;});}).then(function(){
    if(_currentPage!=='compression'||document.getElementById('compressionAutoEnabled')!==box) return;
    box.dataset.savedChecked=String(box.checked); document.getElementById('compressionEnabledState').textContent=box.checked?'已启用':'已关闭';
  }).catch(function(reason){if(_currentPage!=='compression'||document.getElementById('compressionAutoEnabled')!==box) return;box.checked=previous;error.style.display='block';error.textContent=reason.message||'保存失败';}).finally(function(){if(_currentPage==='compression'&&document.getElementById('compressionAutoEnabled')===box) box.disabled=false;});
}
var _reflowRenderGeneration=0;
function isCurrentReflowRender(generation, box){
  return _currentPage==='reflow' &&
    generation===_reflowRenderGeneration &&
    document.getElementById('reflowAutoEnabled')===box;
}
function refreshCurrentReflowPanel(){
  if(_currentPage!=='reflow') return;
  var box=document.getElementById('reflowAutoEnabled');
  var content=document.getElementById('content');
  if(box&&content) renderReflow(content);
}
function formatReflowTime(value){
  var timestamp=Number(value);
  if(!Number.isFinite(timestamp)||timestamp<=0) return '--';
  return new Intl.DateTimeFormat('zh-CN',{
    timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',
    hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false
  }).format(new Date(timestamp));
}
function renderReflow(el){
  var generation=++_reflowRenderGeneration;
  el.innerHTML='<div class="card" style="max-width:760px">'+
    '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:20px">'+
      '<div><h3 style="font-size:15px;color:var(--brand);margin-bottom:6px">动能回流自动扫描</h3>'+
      '<p style="color:var(--muted);font-size:12px;line-height:1.7">全局扫描开关 · 固定流动性门槛 50万 USDT</p></div>'+
      '<label style="display:flex;align-items:center;gap:8px;color:var(--text2);font-size:12px;cursor:pointer">'+
        '<input type="checkbox" id="reflowAutoEnabled" style="width:auto">自动扫描</label>'+
    '</div>'+
    '<div id="reflowSaveError" style="display:none;padding:8px 10px;border-radius:5px;background:rgba(248,113,113,0.1);color:var(--s-red);font-size:12px;margin-bottom:14px"></div>'+
    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px">'+
      '<div class="stat-box"><div class="label">当前状态</div><div class="value" id="reflowEnabledState" style="font-size:18px">--</div></div>'+
      '<div class="stat-box"><div class="label">最后修改时间</div><div id="reflowUpdatedAt" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">最后修改人</div><div id="reflowUpdatedBy" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">上次自动扫描</div><div id="reflowLastScan" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">下次扫描</div><div id="reflowNextScan" style="font:12px var(--font-mono)">--</div></div>'+
      '<div class="stat-box"><div class="label">最近错误</div><div id="reflowLastError" style="font-size:12px;color:var(--s-red)">--</div></div>'+
    '</div>'+
    '<p style="color:var(--muted);font-size:12px;line-height:1.7;margin-top:18px">关闭自动扫描不会删除历史记录，手动扫描仍可使用。</p>'+
  '</div>'+
  '<div class="card reflow-alert-card" style="max-width:760px">'+
    '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:18px">'+
      '<div><h3 style="font-size:15px;color:var(--brand);margin-bottom:6px">企业微信警报</h3>'+
      '<p style="color:var(--muted);font-size:12px;line-height:1.7">仅发送新出现或升级的高质量信号</p></div>'+
      '<label style="display:flex;align-items:center;gap:8px;color:var(--text2);font-size:12px;cursor:pointer">'+
        '<input id="reflowWechatEnabled" type="checkbox" style="width:auto">企业微信高质量警报</label>'+
    '</div>'+
    '<div id="reflowAlertSaveError" role="alert" style="display:none;padding:8px 10px;border-radius:5px;background:rgba(248,113,113,0.1);color:var(--s-red);font-size:12px;margin-bottom:14px"></div>'+
    '<div class="field"><label>Webhook</label><input id="reflowWebhook" type="password" autocomplete="off" placeholder="粘贴企业微信群机器人 Webhook"></div>'+
    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin:14px 0">'+
      '<div class="stat-box"><div class="label">已保存凭证</div><div id="reflowWebhookMask" style="font:12px var(--font-mono)">未配置</div></div>'+
      '<div class="stat-box"><div class="label">警报状态</div><div id="reflowWechatState" style="font-size:12px">--</div></div>'+
      '<div class="stat-box"><div class="label">最近测试</div><div id="reflowWechatTestState" style="font-size:12px">--</div></div>'+
      '<div class="stat-box"><div class="label">最近正式发送</div><div id="reflowWechatDeliveryState" style="font-size:12px">--</div></div>'+
    '</div>'+
    '<div style="display:flex;gap:8px;flex-wrap:wrap">'+
      '<button type="button" class="btn btn-primary" onclick="saveReflowAlertSetting()">保存警报设置</button>'+
      '<button id="reflowWechatTest" type="button" class="btn" onclick="testReflowWechat()">发送测试消息</button>'+
    '</div>'+
    '<small style="display:block;color:var(--muted);font-size:11px;line-height:1.6;margin-top:14px">网络超时可能造成企业微信已收到、服务器未收到回执，有限重试时存在极低概率重复。</small>'+
  '</div>';
  var box=document.getElementById('reflowAutoEnabled');
  box.dataset.renderGeneration=String(generation);
  box.disabled=true;
  fetch('/api/reflow/settings').then(function(response){
    return response.json().then(function(data){
      if(!response.ok) throw new Error(data.error||'读取设置失败');
      return data;
    });
  }).then(function(data){
    if(!isCurrentReflowRender(generation,box)) return;
    box.checked=Boolean(data.auto_scan_enabled);
    box.dataset.savedChecked=String(box.checked);
    box.disabled=false;
    document.getElementById('reflowEnabledState').textContent=box.checked?'已启用':'已关闭';
    document.getElementById('reflowUpdatedAt').textContent=formatReflowTime(data.updated_at);
    document.getElementById('reflowUpdatedBy').textContent=data.updated_by||'--';
    document.getElementById('reflowLastScan').textContent=formatReflowTime(data.last_auto_scan_at);
    document.getElementById('reflowNextScan').textContent=formatReflowTime(data.next_scan_at);
    document.getElementById('reflowLastError').textContent=data.last_auto_error||
      (data.scheduler_status==='unavailable'?'调度状态不可用':'无');
    box.onchange=saveReflowSetting;
  }).catch(function(reason){
    if(!isCurrentReflowRender(generation,box)) return;
    var error=document.getElementById('reflowSaveError');
    error.style.display='block';
    error.textContent=reason.message||'读取设置失败';
  });
  var wechatBox=document.getElementById('reflowWechatEnabled');
  var testButton=document.getElementById('reflowWechatTest');
  wechatBox.disabled=true;
  testButton.disabled=true;
  fetch('/api/reflow/alert-settings').then(function(response){
    return response.json().then(function(data){
      if(!response.ok) throw new Error(data.error||'读取警报设置失败');
      return data;
    });
  }).then(function(data){
    if(_currentPage!=='reflow'||generation!==_reflowRenderGeneration||
       document.getElementById('reflowWechatEnabled')!==wechatBox) return;
    wechatBox.checked=Boolean(data.wechat_enabled);
    wechatBox.disabled=false;
    testButton.disabled=false;
    document.getElementById('reflowWechatState').textContent=
      wechatBox.checked?'已启用':'已关闭';
    document.getElementById('reflowWebhookMask').textContent=
      data.webhook_mask||'未配置';
    document.getElementById('reflowWechatTestState').textContent=
      data.last_test_at?
        ((data.last_test_ok?'成功 · ':'失败 · ')+formatReflowTime(data.last_test_at)+
         (data.last_test_error?' · '+data.last_test_error:'')):'尚未测试';
    var deliveryLabels={none:'尚无发送记录',pending:'等待发送',
      delivered:'发送成功',failed:'发送失败',unavailable:'状态不可用'};
    var delivery=deliveryLabels[data.last_delivery_status]||'状态不可用';
    if(data.last_delivery_at) delivery+=' · '+formatReflowTime(data.last_delivery_at);
    if(data.last_delivery_error) delivery+=' · '+data.last_delivery_error;
    document.getElementById('reflowWechatDeliveryState').textContent=delivery;
  }).catch(function(reason){
    if(_currentPage!=='reflow'||generation!==_reflowRenderGeneration||
       document.getElementById('reflowWechatEnabled')!==wechatBox) return;
    var error=document.getElementById('reflowAlertSaveError');
    error.style.display='block';
    error.textContent=reason.message||'读取警报设置失败';
  });
}
function saveReflowSetting(){
  var box=document.getElementById('reflowAutoEnabled');
  var error=document.getElementById('reflowSaveError');
  var generation=Number(box.dataset.renderGeneration);
  var previous=box.dataset.savedChecked==='true';
  error.style.display='none';
  error.textContent='';
  box.disabled=true;
  fetch('/api/reflow/settings',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({auto_scan_enabled:Boolean(box.checked)})
  }).then(function(response){
    return response.json().then(function(data){
      if(!response.ok||!data.ok) throw new Error(data.error||'保存失败');
      return data;
    });
  }).then(function(data){
    if(!isCurrentReflowRender(generation,box)){
      refreshCurrentReflowPanel();
      return;
    }
    box.checked=Boolean(data.auto_scan_enabled);
    box.dataset.savedChecked=String(box.checked);
    document.getElementById('reflowEnabledState').textContent=box.checked?'已启用':'已关闭';
    document.getElementById('reflowUpdatedAt').textContent=formatReflowTime(data.updated_at);
    document.getElementById('reflowUpdatedBy').textContent=data.updated_by||'--';
  }).catch(function(reason){
    if(!isCurrentReflowRender(generation,box)) return;
    box.checked=previous;
    error.style.display='block';
    error.textContent=reason.message||'保存失败';
  }).finally(function(){
    if(!isCurrentReflowRender(generation,box)) return;
    box.disabled=false;
  });
}
function saveReflowAlertSetting(){
  var enabled=document.getElementById('reflowWechatEnabled');
  var webhook=document.getElementById('reflowWebhook');
  var error=document.getElementById('reflowAlertSaveError');
  error.style.display='none';
  error.textContent='';
  fetch('/api/reflow/alert-settings',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({wechat_enabled:Boolean(enabled.checked),wechat_webhook:webhook.value})
  }).then(function(response){
    return response.json().then(function(data){
      if(!response.ok||!data.ok) throw new Error(data.error||'保存失败');
      return data;
    });
  }).then(function(data){
    webhook.value='';
    enabled.checked=Boolean(data.wechat_enabled);
    document.getElementById('reflowWechatState').textContent=
      enabled.checked?'已启用':'已关闭';
    document.getElementById('reflowWebhookMask').textContent=
      data.webhook_mask||'未配置';
  }).catch(function(reason){
    error.style.display='block';
    error.textContent=reason.message||'保存失败';
  });
}
function testReflowWechat(){
  var button=document.getElementById('reflowWechatTest');
  var state=document.getElementById('reflowWechatTestState');
  button.disabled=true;
  state.textContent='正在发送测试消息...';
  fetch('/api/reflow/alert-settings/test',{method:'POST'}).then(function(response){
    return response.json().then(function(data){
      if(!response.ok||!data.last_test_ok){
        throw new Error(data.last_test_error||data.error||'测试失败');
      }
      return data;
    });
  }).then(function(data){
    document.getElementById('reflowWebhookMask').textContent=
      data.webhook_mask||'未配置';
    state.textContent='测试消息已发送';
  }).catch(function(reason){
    state.textContent=reason.message||'测试失败';
  }).finally(function(){
    button.disabled=false;
  });
}
function openFuelTopup(uid,uname){
  document.getElementById('fuelUid').value=uid;
  document.getElementById('fuelUname').textContent=uname;
  document.getElementById('fuelModal').classList.add('show');
}
function doFuelTopup(){
  var uid=document.getElementById('fuelUid').value,amt=document.getElementById('fuelAmt').value;
  api('/api/fuel/topup',{method:'POST',body:JSON.stringify({user_id:parseInt(uid),amount:parseFloat(amt),note:'管理员充值'})}).then(d=>{
    if(d.ok){closeFuelModal();renderFuel(document.getElementById('content'));}
  });
}
function closeFuelModal(){document.getElementById('fuelModal').classList.remove('show')}
// fuel deduction
function openFuelDeduct(uid,uname){document.getElementById('deductUid').value=uid;document.getElementById('deductUname').textContent=uname;document.getElementById('deductModal').classList.add('show')}
function closeDeductModal(){document.getElementById('deductModal').classList.remove('show')}
function doFuelDeduct(){var uid=document.getElementById('deductUid').value,amt=document.getElementById('deductAmt').value,note=document.getElementById('deductNote').value||'管理员扣费';api('/api/fuel/deduct',{method:'POST',body:JSON.stringify({user_id:parseInt(uid),amount:parseFloat(amt),note:note})}).then(d=>{if(d.ok){closeDeductModal();switchPage(_currentPage)}})}
// password reset
function openPwdReset(uid,uname){document.getElementById('pwdUid').value=uid;document.getElementById('pwdUname').textContent=uname;document.getElementById('pwdNew').value='';document.getElementById('pwdModal').classList.add('show')}
function closePwdModal(){document.getElementById('pwdModal').classList.remove('show')}
function doResetPwd(){var uid=document.getElementById('pwdUid').value,pw=document.getElementById('pwdNew').value;if(pw.length<6){alert('密码至少6位');return}api('/api/users/'+uid+'/password',{method:'POST',body:JSON.stringify({password:pw})}).then(d=>{if(d.ok){closePwdModal();alert('密码重置成功')}})}
function renderDemoCfg(el){
  api('/api/demo/config').then(cfg=>{
    var h='<div class="card"><h3 style="margin-bottom:16px;font-size:15px;color:var(--brand)">回测参数配置</h3><p style="color:var(--muted);font-size:12px;margin-bottom:16px">这些参数将展示在数据回测面板顶部，用户只读。</p>';
    h+='<div class="field"><label>本金 (USDT)</label><input type="number" id="dcVal" value="'+cfg.cfg_maxval+'"></div>';
    h+='<div class="field"><label>风控/笔 (USDT)</label><input type="text" id="dcRisk" value="'+cfg.cfg_risk+'" placeholder="20 或 15m:10,1h:20" style="font-size:11px"></div>';
    h+='<div class="field"><label>ATR追踪倍数</label><input type="number" id="dcAtr" value="'+cfg.cfg_atr_mult+'" step="0.5"></div>';
    h+='<div class="field"><label>最大持仓</label><input type="number" id="dcMaxpos" value="'+cfg.cfg_maxpos+'"></div>';
    h+='<div class="field"><label>多周期并发</label><input type="text" id="dcInt" value="'+(cfg.cfg_interval||'15m')+'" placeholder="15m,1h,4h,1d" style="font-size:11px"></div>';
    h+='<button class="btn btn-primary" onclick="saveDemoCfg()">保存参数</button></div>';
    el.innerHTML=h;
  });
}
function saveDemoCfg(){
  var cfg={cfg_maxval:parseFloat(document.getElementById('dcVal').value),cfg_risk:document.getElementById('dcRisk').value,cfg_atr_mult:parseFloat(document.getElementById('dcAtr').value),cfg_maxpos:parseInt(document.getElementById('dcMaxpos').value),cfg_interval:document.getElementById('dcInt').value};
  api('/api/demo/config',{method:'POST',body:JSON.stringify(cfg)}).then(d=>{alert('已保存'); renderDemoCfg(document.getElementById('content'));});
}
function saveDemoFullCfg(){
  var cfg={
    testnet: document.getElementById('deTestnet').value==='1',
    market_type: document.getElementById('deMarket').value,
    testnet_api_key: document.getElementById('deTestKey').value,
    testnet_api_secret: document.getElementById('deTestSec').value,
    entry_signal_source: document.getElementById('deSignalSource').value,
    predicta_choppy_filter_mode: document.getElementById('dePredictaChoppy').value==='1'?'hard':'off',
    scan_interval: document.getElementById('deInt').value,
    min_score: parseInt(document.getElementById('deScore').value)||70,
    max_positions: parseInt(document.getElementById('deMaxpos').value)||3,
    rj_kd_ma_type: document.getElementById('deRjKdMa').value,
    rj_slow_line_mode: document.getElementById('deRjSlowMode').value,
    rj_slow_line_scale: parseFloat(document.getElementById('deRjSlowScale').value)||0.88,
    rj_slow_line_offset: parseFloat(document.getElementById('deRjSlowOffset').value)||0,
    rj_slow_line_clamp: document.getElementById('deRjSlowClamp').value==='1',
    rj_only_confirm_bars: parseInt(document.getElementById('deRjConfirm').value)||6,
    rj_only_max_symbols: parseInt(document.getElementById('deRjMaxSymbols').value)||80,
    rj_only_min_volume_usdt: parseFloat(document.getElementById('deRjMinVol').value)||3000000,
    rj_only_volume_filter: document.getElementById('deRjVolFilter').value==='1',
    rj_only_volume_len: parseInt(document.getElementById('deRjVolLen').value)||20,
    rj_only_volume_mult: parseFloat(document.getElementById('deRjVolMult').value)||1.1,
    rj_only_stats_enabled: document.getElementById('deRjStatsOn').value==='1',
    rj_only_stats_lookback_bars: parseInt(document.getElementById('deRjLookback').value)||1000,
    rj_only_stats_horizon_bars: parseInt(document.getElementById('deRjHorizon').value)||12,
    rj_only_stats_target_r: parseFloat(document.getElementById('deRjTargetR').value)||1.0,
    rj_only_stats_min_samples: parseInt(document.getElementById('deRjSamples').value)||8,
    rj_only_stats_min_win_rate: parseFloat(document.getElementById('deRjWin').value)||52,
    rj_only_stats_min_avg_r: parseFloat(document.getElementById('deRjAvgR').value)||0,
    rj_only_watchlist_enabled: document.getElementById('deRjWatchOn').value==='1',
    rj_only_watchlist_refresh_minutes: parseInt(document.getElementById('deRjWatchRefresh').value)||60,
    rj_only_watchlist_size: parseInt(document.getElementById('deRjWatchSize').value)||80,
    rj_only_watchlist_eval_symbols: parseInt(document.getElementById('deRjWatchEval').value)||200,
    rj_only_watchlist_min_samples: parseInt(document.getElementById('deRjWatchSamples').value)||8,
    rj_only_watchlist_min_win_rate: parseFloat(document.getElementById('deRjWatchWin').value)||52,
    rj_only_watchlist_min_avg_r: parseFloat(document.getElementById('deRjWatchAvgR').value)||0,
    rj_only_watchlist_discovery_top_n: parseInt(document.getElementById('deRjWatchDiscover').value)||30,
    rj_only_atr_sl_mult: parseFloat(document.getElementById('deRjAtrSl').value)||0.5,
    rj_only_confirm_atr_buffer: parseFloat(document.getElementById('deRjConfirmBuf').value)||0.08,
    rj_only_invalidate_on_opposite_break: document.getElementById('deRjInvalidate').value==='1',
    rj_only_min_stop_pct: parseFloat(document.getElementById('deRjMinStop').value)||0.003,
    rj_only_max_stop_pct: parseFloat(document.getElementById('deRjMaxStop').value)||0.08,
    rj_only_sr_filter: document.getElementById('deRjSrFilter').value==='1',
    rj_only_sr_pivot_left: parseInt(document.getElementById('deRjSrLeft').value)||5,
    rj_only_sr_pivot_right: parseInt(document.getElementById('deRjSrRight').value)||3,
    rj_only_sr_near_mode: document.getElementById('deRjSrNearMode').value,
    rj_only_sr_near_atr_mult: parseFloat(document.getElementById('deRjSrAtr').value)||0.8,
    rj_only_sr_near_pct: parseFloat(document.getElementById('deRjSrPct').value)||0.8,
    rj_only_require_divergence: document.getElementById('deRjDivReq').value==='1',
    rj_only_div_lookback_bars: parseInt(document.getElementById('deRjDivLookback').value)||80,
    rj_only_use_early_divergence: document.getElementById('deRjEarlyDiv').value==='1',
    rj_only_setup_pool_enabled: document.getElementById('deRjSetupPool').value==='1',
    rj_only_setup_confirm_mode: document.getElementById('deRjSetupMode').value,
    rj_only_setup_close_confirm_sec: parseInt(document.getElementById('deRjCloseSec').value)||45,
    rj_only_setup_trigger_hold_sec: parseInt(document.getElementById('deRjHoldSec').value)||10,
    rj_only_setup_check_interval_sec: parseInt(document.getElementById('deRjCheckSec').value)||10,
    rj_only_setup_near_pct: parseFloat(document.getElementById('deRjNearPct').value)||0.15,
    rj_only_setup_max_pool: parseInt(document.getElementById('deRjSetupMaxPool').value)||40,
    risk_per_trade: document.getElementById('deRisk').value,
    max_position_usdt: parseFloat(document.getElementById('deMaxval').value)||5000,
    account_initial_equity: parseFloat(document.getElementById('deInitialEquity').value)||0,
    leverage: parseInt(document.getElementById('deLeverage').value)||5,
    max_daily_loss: parseFloat(document.getElementById('deMaxDL').value)||0,
    max_consecutive_loss: parseInt(document.getElementById('deMaxCL').value)||5,
    cooldown_minutes: parseInt(document.getElementById('deCooldown').value)||60,
    half_risk_trigger_r: parseFloat(document.getElementById('deHalfRiskR').value)||0,
    enable_early_protect: document.getElementById('deEarlyProtect').value==='1',
    early_protect_r: parseFloat(document.getElementById('deEarlyR').value)||0.8,
    early_protect_lock_r: parseFloat(document.getElementById('deEarlyLock').value)||0,
    tier1_defense_r: parseFloat(document.getElementById('deTier1').value)||1.2,
    tier2_partial_r: parseFloat(document.getElementById('deTier2').value)||2.0,
    enable_time_stop: document.getElementById('deTimeStop').value==='1',
    time_stop_bars: document.getElementById('deTimeBars').value,
    time_stop_min_r: parseFloat(document.getElementById('deTimeMinR').value)||0.6,
    use_atr_trail: document.getElementById('deAtrMode').value==='1',
    ema_ratchet: parseInt(document.getElementById('deEma').value)||20,
    atr_trail_mult: parseFloat(document.getElementById('deAtr').value)||3.5,
    atr_trail_period: parseInt(document.getElementById('deAtrPeriod').value)||14
  };
  fetch('/api/admin/demo/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)}).then(r=>r.json()).then(d=>{
    var m=document.getElementById('deMsg');
    m.style.display='block'; m.style.background='rgba(45,212,191,0.1)'; m.style.color='var(--brand)';
    m.textContent='已保存并生效';
  }).catch(function(e){
    var m=document.getElementById('deMsg');
    m.style.display='block'; m.style.background='rgba(248,113,113,0.1)'; m.style.color='var(--s-red)';
    m.textContent='保存失败: '+e.message;
  });
}
function renderEngine(el){
  el.innerHTML='<div style="text-align:center;padding:40px;color:var(--muted)">加载中...</div>';
  Promise.all([
    fetch('/api/admin/demo/config').then(r=>r.json()),
    fetch('/api/admin/demo/status').then(r=>r.json())
  ]).then(function(res){
    var cfg=res[0], st=res[1];
    var running=st.running;
    var sigSrc=cfg.entry_signal_source||'structure';
    var deIntVal=cfg.scan_interval||(sigSrc==='rj_only'?'30m':'15m');
    var h='<div class="card">';
    // 状态栏
    h+='<div id="deStatusBar" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;padding:12px 16px;border-radius:8px;background:'+(running?'rgba(52,211,153,0.08)':'rgba(248,113,113,0.08)')+';border:1px solid '+(running?'rgba(52,211,153,0.2)':'rgba(248,113,113,0.2)')+'">';
    h+='<div style="display:flex;align-items:center;gap:10px"><span style="width:10px;height:10px;border-radius:50%;background:'+(running?'var(--s-green)':'var(--s-red)')+';'+(running?'box-shadow:0 0 8px var(--s-green);animation:pulse 2s infinite':'')+'"></span><span style="font-weight:700;font-size:16px;color:'+(running?'var(--s-green)':'var(--s-red)')+'">'+(running?'● 运行中':'○ 已停止')+'</span></div>';
    h+='<span style="color:var(--muted);font-size:12px">'+st.status+' · '+(st.testnet?'测试网':'主网')+' · '+(st.mode||'')+' · API '+(st.client_ready?'已连接':'未连接')+'</span></div>';
    h+='<h3 style="margin-bottom:3px;font-size:15px;color:var(--brand)">演示引擎控制</h3>';
    h+='<p style="color:var(--muted);font-size:12px;margin-bottom:14px">配置演示引擎的API凭证和策略参数，数据回测面板将使用此引擎。</p>';
    h+='<div id="deMsg" style="display:none;padding:8px;border-radius:4px;margin-bottom:12px;font-size:12px"></div>';
    // 交易所配置 (折叠)
    h+='<div class="t-section">';
    h+='<div class="t-section-header" onclick="toggleSection(this)"><span class="t-section-indicator" style="background:var(--s-orange);box-shadow:0 0 5px rgba(251,146,60,0.3)"></span><span>交易所配置</span><span class="t-arrow">▶</span></div>';
    h+='<div class="t-section-body">';
    h+='<div class="field"><label>交易模式</label><select id="deTestnet"><option value="1" selected>测试网真实模拟</option></select></div>';
    h+='<div class="field"><label>市场类型</label><select id="deMarket"><option value="spot"'+(cfg.market_type=='spot'?' selected':'')+'>现货</option><option value="futures"'+(cfg.market_type=='futures'?' selected':'')+'>合约</option></select></div>';
    h+='<div class="field"><label>模拟盘API Key</label><input type="text" id="deTestKey" value="'+(cfg.testnet_api_key||'')+'"></div>';
    h+='<div class="field"><label>模拟盘API Secret</label><input type="password" id="deTestSec" value="'+(cfg.testnet_api_secret||'')+'"></div>';
    h+='</div></div>';
    // 策略参数 (折叠)
    h+='<div class="t-section">';
    h+='<div class="t-section-header" onclick="toggleSection(this)"><span class="t-section-indicator" style="background:var(--s-blue);box-shadow:0 0 5px rgba(96,165,250,0.3)"></span><span>策略参数</span><span class="t-arrow">▶</span></div>';
    h+='<div class="t-section-body">';
    h+='<div class="field"><label>信号源</label><select id="deSignalSource"><option value="predicta_ewo"'+(sigSrc==='predicta_ewo'?' selected':'')+'>Predicta + EWO</option><option value="rj_only"'+(sigSrc==='rj_only'?' selected':'')+'>RJ独立模拟</option><option value="structure"'+(sigSrc==='structure'?' selected':'')+'>结构突破</option></select></div>';
    h+='<div class="field"><label>震荡过滤拦截</label><select id="dePredictaChoppy"><option value="1"'+((cfg.predicta_choppy_filter_mode||'off')==='hard'?' selected':'')+'>开启｜震荡信号不进入开仓链路</option><option value="0"'+((cfg.predicta_choppy_filter_mode||'off')!=='hard'?' selected':'')+'>关闭｜不使用震荡过滤拦截</option></select></div>';
    h+='<div class="field"><label>扫描周期</label><input type="text" id="deInt" value="'+deIntVal+'" placeholder="30m" style="font-size:11px"></div>';
    h+='<div class="field"><label>最低评分</label><input type="number" id="deScore" value="'+(cfg.min_score||70)+'"></div>';
    h+='<div class="field"><label>最大持仓</label><input type="number" id="deMaxpos" value="'+(cfg.max_positions||3)+'"></div>';
    h+='<div class="field"><label>RJ K/D均线</label><select id="deRjKdMa"><option value="sma"'+((cfg.rj_kd_ma_type||'sma')==='sma'?' selected':'')+'>SMA(原版)</option><option value="rma"'+((cfg.rj_kd_ma_type||'sma')==='rma'?' selected':'')+'>RMA(旧后端)</option></select></div>';
    h+='<div class="field"><label>RJ紫线口径</label><select id="deRjSlowMode"><option value="k"'+((cfg.rj_slow_line_mode||'k')==='k'?' selected':'')+'>K线(贴近原版)</option><option value="rsi"'+((cfg.rj_slow_line_mode||'k')==='rsi'?' selected':'')+'>普通RSI</option><option value="stoch_rsi"'+((cfg.rj_slow_line_mode||'k')==='stoch_rsi'?' selected':'')+'>随机RSI</option><option value="j_rsi"'+((cfg.rj_slow_line_mode||'k')==='j_rsi'?' selected':'')+'>J线RSI</option></select></div>';
    h+='<div class="field"><label>RJ紫线倍率</label><input type="number" id="deRjSlowScale" value="'+(cfg.rj_slow_line_scale||0.88)+'" min="0.1" max="2" step="0.01"></div>';
    h+='<div class="field"><label>RJ紫线偏移</label><input type="number" id="deRjSlowOffset" value="'+(cfg.rj_slow_line_offset||0)+'" min="-50" max="50" step="0.1"></div>';
    h+='<div class="field"><label>RJ紫线限制</label><select id="deRjSlowClamp"><option value="1"'+(cfg.rj_slow_line_clamp!==false?' selected':'')+'>限制0-100</option><option value="0"'+(cfg.rj_slow_line_clamp===false?' selected':'')+'>不限制</option></select></div>';
    h+='<div class="field"><label>RJ确认K数</label><input type="number" id="deRjConfirm" value="'+(cfg.rj_only_confirm_bars||6)+'" min="1" max="20" step="1"></div>';
    h+='<div class="field"><label>RJ扫描数量</label><input type="number" id="deRjMaxSymbols" value="'+(cfg.rj_only_max_symbols||80)+'" min="10" max="600" step="10"></div>';
    h+='<div class="field"><label>RJ最小成交额</label><input type="number" id="deRjMinVol" value="'+(cfg.rj_only_min_volume_usdt||3000000)+'" step="1000000"></div>';
    h+='<div class="field"><label>RJ量能过滤</label><select id="deRjVolFilter"><option value="1"'+(cfg.rj_only_volume_filter!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_volume_filter===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="field"><label>RJ量能均线K数</label><input type="number" id="deRjVolLen" value="'+(cfg.rj_only_volume_len||20)+'" min="5" max="120" step="1"></div>';
    h+='<div class="field"><label>RJ最低量能倍数</label><input type="number" id="deRjVolMult" value="'+(cfg.rj_only_volume_mult||1.1)+'" min="0" max="5" step="0.05"></div>';
    h+='<div class="field"><label>历史胜率筛选</label><select id="deRjStatsOn"><option value="1"'+(cfg.rj_only_stats_enabled!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_stats_enabled===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="field"><label>历史回看K数</label><input type="number" id="deRjLookback" value="'+(cfg.rj_only_stats_lookback_bars||1000)+'" min="80" max="1000" step="20"></div>';
    h+='<div class="field"><label>统计持有K数</label><input type="number" id="deRjHorizon" value="'+(cfg.rj_only_stats_horizon_bars||12)+'" min="3" max="80" step="1"></div>';
    h+='<div class="field"><label>统计目标R</label><input type="number" id="deRjTargetR" value="'+(cfg.rj_only_stats_target_r||1)+'" min="0.3" max="5" step="0.1"></div>';
    h+='<div class="field"><label>最小样本</label><input type="number" id="deRjSamples" value="'+(cfg.rj_only_stats_min_samples||8)+'" min="3" max="80" step="1"></div>';
    h+='<div class="field"><label>最低胜率%</label><input type="number" id="deRjWin" value="'+(cfg.rj_only_stats_min_win_rate||52)+'" min="30" max="90" step="1"></div>';
    h+='<div class="field"><label>最低平均R</label><input type="number" id="deRjAvgR" value="'+(cfg.rj_only_stats_min_avg_r||0)+'" min="-1" max="2" step="0.05"></div>';
    h+='<div class="field"><label>RJ优选池</label><select id="deRjWatchOn"><option value="1"'+(cfg.rj_only_watchlist_enabled!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_watchlist_enabled===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="field"><label>优选刷新分钟</label><input type="number" id="deRjWatchRefresh" value="'+(cfg.rj_only_watchlist_refresh_minutes||60)+'" min="5" max="720" step="5"></div>';
    h+='<div class="field"><label>优选池数量</label><input type="number" id="deRjWatchSize" value="'+(cfg.rj_only_watchlist_size||80)+'" min="5" max="300" step="5"></div>';
    h+='<div class="field"><label>评估币种数</label><input type="number" id="deRjWatchEval" value="'+(cfg.rj_only_watchlist_eval_symbols||200)+'" min="20" max="600" step="10"></div>';
    h+='<div class="field"><label>优选最小样本</label><input type="number" id="deRjWatchSamples" value="'+(cfg.rj_only_watchlist_min_samples||8)+'" min="3" max="80" step="1"></div>';
    h+='<div class="field"><label>优选最低胜率%</label><input type="number" id="deRjWatchWin" value="'+(cfg.rj_only_watchlist_min_win_rate||52)+'" min="30" max="90" step="1"></div>';
    h+='<div class="field"><label>优选最低平均R</label><input type="number" id="deRjWatchAvgR" value="'+(cfg.rj_only_watchlist_min_avg_r||0)+'" min="-1" max="2" step="0.05"></div>';
    h+='<div class="field"><label>新币发现数量</label><input type="number" id="deRjWatchDiscover" value="'+(cfg.rj_only_watchlist_discovery_top_n||30)+'" min="0" max="200" step="5"></div>';
    h+='<div class="field"><label>RJ止损ATR</label><input type="number" id="deRjAtrSl" value="'+(cfg.rj_only_atr_sl_mult||0.5)+'" min="0" max="3" step="0.1"></div>';
    h+='<div class="field"><label>RJ确认ATR缓冲</label><input type="number" id="deRjConfirmBuf" value="'+(cfg.rj_only_confirm_atr_buffer||0.08)+'" min="0" max="1" step="0.01"></div>';
    h+='<div class="field"><label>RJ反向失效</label><select id="deRjInvalidate"><option value="1"'+(cfg.rj_only_invalidate_on_opposite_break!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_invalidate_on_opposite_break===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="field"><label>最小止损%</label><input type="number" id="deRjMinStop" value="'+(cfg.rj_only_min_stop_pct||0.003)+'" min="0.001" max="0.03" step="0.001"></div>';
    h+='<div class="field"><label>最大止损%</label><input type="number" id="deRjMaxStop" value="'+(cfg.rj_only_max_stop_pct||0.08)+'" min="0.01" max="0.3" step="0.01"></div>';
    h+='<div class="field"><label>支撑压力过滤</label><select id="deRjSrFilter"><option value="1"'+(cfg.rj_only_sr_filter!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_sr_filter===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="field"><label>支压左侧K数</label><input type="number" id="deRjSrLeft" value="'+(cfg.rj_only_sr_pivot_left||5)+'" min="1" max="30" step="1"></div>';
    h+='<div class="field"><label>支压右侧K数</label><input type="number" id="deRjSrRight" value="'+(cfg.rj_only_sr_pivot_right||3)+'" min="1" max="30" step="1"></div>';
    h+='<div class="field"><label>贴近方式</label><select id="deRjSrNearMode"><option value="atr_or_pct"'+((cfg.rj_only_sr_near_mode||'atr_or_pct')==='atr_or_pct'?' selected':'')+'>ATR或百分比</option><option value="atr"'+((cfg.rj_only_sr_near_mode||'atr_or_pct')==='atr'?' selected':'')+'>仅ATR</option><option value="pct"'+((cfg.rj_only_sr_near_mode||'atr_or_pct')==='pct'?' selected':'')+'>仅百分比</option></select></div>';
    h+='<div class="field"><label>贴近ATR倍数</label><input type="number" id="deRjSrAtr" value="'+(cfg.rj_only_sr_near_atr_mult||0.8)+'" min="0" max="5" step="0.1"></div>';
    h+='<div class="field"><label>贴近百分比%</label><input type="number" id="deRjSrPct" value="'+(cfg.rj_only_sr_near_pct||0.8)+'" min="0" max="5" step="0.1"></div>';
    h+='<div class="field"><label>要求J背离</label><select id="deRjDivReq"><option value="1"'+(cfg.rj_only_require_divergence!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_require_divergence===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="field"><label>背离有效K数</label><input type="number" id="deRjDivLookback" value="'+(cfg.rj_only_div_lookback_bars||80)+'" min="5" max="500" step="5"></div>';
    h+='<div class="field"><label>形成中背离</label><select id="deRjEarlyDiv"><option value="1"'+(cfg.rj_only_use_early_divergence!==false?' selected':'')+'>允许</option><option value="0"'+(cfg.rj_only_use_early_divergence===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="field"><label>RJ候选池</label><select id="deRjSetupPool"><option value="1"'+(cfg.rj_only_setup_pool_enabled!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.rj_only_setup_pool_enabled===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="field"><label>候选确认方式</label><select id="deRjSetupMode"><option value="near_close"'+((cfg.rj_only_setup_confirm_mode||'near_close')==='near_close'?' selected':'')+'>临近收盘</option><option value="hold"'+((cfg.rj_only_setup_confirm_mode||'near_close')==='hold'?' selected':'')+'>突破维持</option><option value="touch"'+((cfg.rj_only_setup_confirm_mode||'near_close')==='touch'?' selected':'')+'>触碰即进</option></select></div>';
    h+='<div class="field"><label>收盘前确认秒数</label><input type="number" id="deRjCloseSec" value="'+(cfg.rj_only_setup_close_confirm_sec||45)+'" min="5" max="300" step="5"></div>';
    h+='<div class="field"><label>突破维持秒数</label><input type="number" id="deRjHoldSec" value="'+(cfg.rj_only_setup_trigger_hold_sec||10)+'" min="0" max="120" step="5"></div>';
    h+='<div class="field"><label>候选检查间隔秒</label><input type="number" id="deRjCheckSec" value="'+(cfg.rj_only_setup_check_interval_sec||10)+'" min="3" max="60" step="1"></div>';
    h+='<div class="field"><label>接近触发%</label><input type="number" id="deRjNearPct" value="'+(cfg.rj_only_setup_near_pct||0.15)+'" min="0" max="2" step="0.05"></div>';
    h+='<div class="field"><label>候选池上限</label><input type="number" id="deRjSetupMaxPool" value="'+(cfg.rj_only_setup_max_pool||40)+'" min="5" max="200" step="5"></div>';
    h+='</div></div>';
    // 风控参数 (折叠)
    h+='<div class="t-section">';
    h+='<div class="t-section-header" onclick="toggleSection(this)"><span class="t-section-indicator" style="background:var(--s-red);box-shadow:0 0 5px rgba(248,113,113,0.3)"></span><span>风控参数</span><span class="t-arrow">▶</span></div>';
    h+='<div class="t-section-body">';
    h+='<div class="field"><label>风险/笔 (USDT)</label><input type="text" id="deRisk" value="'+(cfg.risk_per_trade||200)+'" placeholder="200 或 15m:10,1h:20,4h:40" style="font-size:11px"></div>';
    h+='<div class="field"><label>最大名义价值</label><input type="number" id="deMaxval" value="'+(cfg.max_position_usdt||5000)+'"></div>';
    h+='<div class="field"><label>配置本金/初始权益</label><input type="number" id="deInitialEquity" value="'+(cfg.account_initial_equity||0)+'" min="0" step="0.01" placeholder="如 5000"></div>';
    h+='<div class="field"><label>杠杆倍数</label><input type="number" id="deLeverage" value="'+(cfg.leverage||1)+'" min="1" max="125" step="1"></div>';
    h+='<div class="field"><label>单日最大亏损 (0=不限)</label><input type="number" id="deMaxDL" value="'+(cfg.max_daily_loss||0)+'" step="50"></div>';
    h+='<div class="field"><label>连续止损暂停</label><input type="number" id="deMaxCL" value="'+(cfg.max_consecutive_loss||5)+'"></div>';
    h+='<div class="field"><label>止损后冷却(分)</label><input type="number" id="deCooldown" value="'+(cfg.cooldown_minutes||60)+'"></div>';
    h+='</div></div>';
    // 三阶止盈 (折叠)
    h+='<div class="t-section">';
    h+='<div class="t-section-header" onclick="toggleSection(this)"><span class="t-section-indicator" style="background:var(--s-purple);box-shadow:0 0 5px rgba(168,85,247,0.3)"></span><span>三阶止盈</span><span class="t-arrow">▶</span></div>';
    h+='<div class="t-section-body">';
    h+='<div class="field"><label>0.5R半损保护 (0=关闭)</label><input type="number" id="deHalfRiskR" value="'+(cfg.half_risk_trigger_r??0)+'" min="0" max="0.79" step="0.1"></div>';
    h+='<div class="field"><label>提前保护</label><select id="deEarlyProtect"><option value="1"'+(cfg.enable_early_protect!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.enable_early_protect===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="field"><label>提前保护R</label><input type="number" id="deEarlyR" value="'+(cfg.early_protect_r||0.8)+'" min="0.3" max="1.2" step="0.1"></div>';
    h+='<div class="field"><label>提前锁定R</label><input type="number" id="deEarlyLock" value="'+(cfg.early_protect_lock_r||0)+'" min="0" max="0.5" step="0.05"></div>';
    h+='<div class="field"><label>一阶防守 (R倍数)</label><input type="number" id="deTier1" value="'+(cfg.tier1_defense_r||1.2)+'" min="0.5" max="3.0" step="0.1"></div>';
    h+='<div class="field"><label>二阶减仓 (R倍数)</label><input type="number" id="deTier2" value="'+(cfg.tier2_partial_r||2.0)+'" min="1.0" max="5.0" step="0.1"></div>';
    h+='<div class="field"><label>未起爆超时</label><select id="deTimeStop"><option value="1"'+(cfg.enable_time_stop!==false?' selected':'')+'>启用</option><option value="0"'+(cfg.enable_time_stop===false?' selected':'')+'>关闭</option></select></div>';
    h+='<div class="field"><label>超时K数</label><input type="text" id="deTimeBars" value="'+(cfg.time_stop_bars||'15m:6,1h:5,4h:4,1d:3')+'" placeholder="15m:6,1h:5,4h:4,1d:3" style="font-size:11px"></div>';
    h+='<div class="field"><label>最小推进R</label><input type="number" id="deTimeMinR" value="'+(cfg.time_stop_min_r||0.6)+'" min="0.1" max="1.5" step="0.1"></div>';
    h+='<div class="field"><label>三阶追踪模式</label><select id="deAtrMode" onchange="toggleAdminAtrMode()"><option value="1"'+(cfg.use_atr_trail?' selected':'')+'>ATR吊灯</option><option value="0"'+(cfg.use_atr_trail?'':' selected')+'>EMA棘轮</option></select></div>';
    h+='<div id="deEmaFields" style="display:'+(cfg.use_atr_trail?'none':'block')+'"><div class="field"><label>EMA周期</label><input type="number" id="deEma" value="'+(cfg.ema_ratchet||20)+'" min="10" max="200" step="10"></div></div>';
    h+='<div id="deAtrFields" style="display:'+(cfg.use_atr_trail?'block':'none')+'"><div class="field"><label>ATR倍数</label><input type="number" id="deAtr" value="'+(cfg.atr_trail_mult||3.5)+'" min="1.5" max="8.0" step="0.5"></div><div class="field"><label>ATR周期</label><input type="number" id="deAtrPeriod" value="'+(cfg.atr_trail_period||14)+'" min="5" max="50" step="1"></div></div>';
    h+='</div></div>';
    h+='<div style="display:flex;gap:10px;margin:16px 0"><button class="btn btn-primary" id="deSave">保存配置</button><button class="btn btn-primary" id="deStart" style="background:var(--s-green)">启动引擎</button><button class="btn btn-danger" id="deStop">停止引擎</button></div>';
    h+='<div id="dePositions" style="margin-top:20px"></div>';
    h+='</div>';
    el.innerHTML=h;
    document.getElementById('deSave').onclick=saveDemoFullCfg;
    document.getElementById('deStart').onclick=function(){var m=document.getElementById('deMsg');m.style.display='block';m.style.background='rgba(45,212,191,0.1)';m.style.color='var(--brand)';m.textContent='启动中...';fetch('/api/admin/demo/start').then(r=>{if(!r.ok)throw new Error(r.status);return r.json()}).then(()=>{m.textContent='已启动';setTimeout(()=>renderEngine(document.getElementById('content')),800)}).catch(e=>{m.style.background='rgba(248,113,113,0.1)';m.style.color='var(--s-red)';m.textContent='启动失败: '+(e.message||'网络错误')});};
    document.getElementById('deStop').onclick=function(){var m=document.getElementById('deMsg');m.style.display='block';m.style.background='rgba(248,113,113,0.1)';m.style.color='var(--s-red)';m.textContent='停止中...';fetch('/api/admin/demo/stop').then(r=>{if(!r.ok)throw new Error(r.status);return r.json()}).then(()=>{m.textContent='已停止';setTimeout(()=>renderEngine(document.getElementById('content')),800)}).catch(e=>{m.style.background='rgba(248,113,113,0.1)';m.style.color='var(--s-red)';m.textContent='停止失败: '+(e.message||'网络错误')});};
    renderDemoPositions();
  });
}
function toggleAdminAtrMode(){
  var on=document.getElementById('deAtrMode').value==='1';
  document.getElementById('deEmaFields').style.display=on?'none':'block';
  document.getElementById('deAtrFields').style.display=on?'block':'none';
}
function showDeMsg(ok, text){
  var m=document.getElementById('deMsg');
  m.style.display='block';
  m.style.background=ok?'rgba(45,212,191,0.1)':'rgba(248,113,113,0.1)';
  m.style.color=ok?'var(--brand)':'var(--s-red)';
  m.textContent=text;
}
function demoCloseOne(sym){
  if(!confirm('平仓 '+sym+' ？')) return;
  fetch('/api/admin/demo/close/'+sym).then(r=>r.json()).then(d=>{
    showDeMsg(d.ok, d.ok?'已平仓 '+sym:'平仓失败: '+(d.msg||'未知'));
    if(d.ok) setTimeout(renderDemoPositions,500);
  }).catch(e=>{showDeMsg(false,'网络错误')});
}
function demoCloseAll(){
  if(!confirm('确定平掉全部持仓？')) return;
  fetch('/api/admin/demo/close_all').then(r=>r.json()).then(d=>{
    showDeMsg(d.ok, d.ok?'已全部平仓':'平仓失败: '+(d.msg||'未知'));
    if(d.ok) setTimeout(renderDemoPositions,500);
  }).catch(e=>{showDeMsg(false,'网络错误')});
}
function renderDemoPositions(){
  fetch('/api/admin/demo/status').then(r=>r.json()).then(d=>{
    // 更新状态栏
    var sb=document.getElementById('deStatusBar');
    if(sb){
      var running=d.running;
      sb.style.background=running?'rgba(52,211,153,0.08)':'rgba(248,113,113,0.08)';
      sb.style.border='1px solid '+(running?'rgba(52,211,153,0.2)':'rgba(248,113,113,0.2)');
      sb.innerHTML='<div style="display:flex;align-items:center;gap:10px"><span style="width:10px;height:10px;border-radius:50%;background:'+(running?'var(--s-green)':'var(--s-red)')+';'+(running?'box-shadow:0 0 8px var(--s-green);animation:pulse 2s infinite':'')+'"></span><span style="font-weight:700;font-size:16px;color:'+(running?'var(--s-green)':'var(--s-red)')+'">'+(running?'● 运行中':'○ 已停止')+'</span></div><span style="color:var(--muted);font-size:12px">'+d.status+'</span>';
    }
    // 更新持仓
    var el=document.getElementById('dePositions'); if(!el) return;
    var h='<h4 style="color:var(--brand);margin:20px 0 12px;font-size:13px">持仓管理 ('+d.positions+' 个)</h4>';
    if(d.positions>0){
      h+='<button class="btn btn-danger" style="padding:4px 12px;font-size:11px;margin-bottom:12px" onclick="demoCloseAll()">一键平仓</button>';
    }
    var plist=d.position_list||[];
    if(plist.length>0){
      h+='<div style="display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(280px,1fr))">';
      plist.forEach(function(p){
        var ip=p.pnl>=0, isL=p.direction==='LONG';
        var et=(p.entered||''); if(et) et=et.slice(0,16).replace('T',' ');
        h+='<div style="background:rgba(15,17,18,0.9);border:1px solid '+(ip?'rgba(52,211,153,0.3)':'rgba(248,113,113,0.3)')+';border-radius:10px;padding:12px">';
        h+='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><span style="font-weight:700;font-size:14px">'+p.symbol.replace('USDT','')+'</span><span style="color:'+(isL?'var(--s-green)':'var(--s-red)')+';font-size:11px;font-weight:600">'+(isL?'做多 LONG':'做空 SHORT')+'</span></div>';
        if(et) h+='<div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px"><span style="color:var(--muted)">开仓</span><span>'+et+'</span></div>';
        h+='<div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px"><span style="color:var(--muted)">入场价</span><span>'+p.entry+'</span></div>';
        h+='<div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px"><span style="color:var(--muted)">当前价</span><span>'+(p.current_price||p.entry).toFixed(6)+'</span></div>';
        h+='<div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px"><span style="color:var(--muted)">止损价</span><span style="color:var(--s-red)">'+p.sl+'</span></div>';
        h+='<div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px"><span style="color:var(--muted)">持仓价值</span><span>$'+(p.value||0).toFixed(2)+'</span></div>';
        h+='<div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.05)"><span style="font-weight:700;font-size:14px;color:'+(ip?'var(--s-green)':'var(--s-red)')+'">'+(ip?'+':'')+p.pnl.toFixed(2)+' USDT</span><button onclick="demoCloseOne(\''+p.symbol+'\')" style="background:var(--s-red);color:#fff;border:none;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:11px">平仓</button></div>';
        if(p.breakeven) h+='<div style="text-align:center;color:var(--s-yellow);font-size:10px;margin-top:6px">✦ 追踪止盈已开启</div>';
        h+='</div>';
      });
      h+='</div>';
    }else{
      h+='<p style="color:var(--muted);font-size:12px;text-align:center;padding:20px">暂无持仓，引擎监控中...</p>';
    }
    el.innerHTML=h;
  });
}
function viewFuelHistory(uid,uname){
  api('/api/fuel/history/'+uid).then(rows=>{
    var h='<div class="card"><h3 style="margin-bottom:12px;color:var(--brand)">'+uname+' 燃料流水</h3>';
    if(rows.length===0) h+='<p style="color:var(--muted)">暂无记录</p>';
    else{h+='<table><thead><tr><th>时间</th><th>类型</th><th>金额</th><th>余额</th><th>备注</th></tr></thead><tbody>';
      rows.forEach(r=>{h+='<tr><td>'+r.created_at+'</td><td>'+(r.txn_type==='topup'?'充值':r.txn_type==='commission_deduct'?'盈利分成':'退款')+'</td><td style="color:'+(r.amount>0?'var(--s-green)':'var(--s-red)')+'">'+(r.amount>0?'+':'')+r.amount.toFixed(2)+'</td><td>$'+r.balance_after.toFixed(2)+'</td><td>'+r.note+'</td></tr>'});
      h+='</tbody></table>';}
    h+='</div>';document.getElementById('content').innerHTML=h;
  });
}
function toggleUser(uid,active){
  api('/api/users/'+uid+'/toggle',{method:'POST',body:JSON.stringify({active:active})}).then(()=>renderUsers(document.getElementById('content')))
}
function openLicense(uid){
  document.getElementById('licUid').value=uid;document.getElementById('licenseModal').classList.add('show')
}
function closeModal(){document.getElementById('licenseModal').classList.remove('show')}
function issueLicense(){
  var uid=document.getElementById('licUid').value,plan=document.getElementById('licPlan').value,days=document.getElementById('licDays').value;
  api('/api/license',{method:'POST',body:JSON.stringify({user_id:parseInt(uid),plan:plan,days:parseInt(days)})}).then(d=>{
    if(d.ok){closeModal();renderUsers(document.getElementById('content'));alert('许可已颁发: '+d.key)}
  })
}
</script></body></html>"""

if __name__ == "__main__":
    print("\n  >>> AXIOM ADMIN <<<")
    print("  http://127.0.0.1:5001\n")
    app.run(host="0.0.0.0", port=5001, debug=False)
