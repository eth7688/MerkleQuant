"""
AXIOM QUANT — 多用户账户管理与许可系统
SQLite 数据库, 支持: 注册/登录/权限 · 许可证 · 燃料费 · 数据隔离
"""
import os, sqlite3, hashlib, secrets, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DB_PATH = Path(__file__).parent / "axiom_accounts.db"

def _now():
    """北京时间显示"""
    return (_utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

def _utcnow():
    """内部UTC时间(用于计算和存储)"""
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _hash_pw(password: str) -> str:
    salt = secrets.token_hex(8)
    return salt + ":" + hashlib.sha256((salt + password).encode()).hexdigest()

def _verify_pw(password: str, stored: str) -> bool:
    parts = stored.split(":", 1)
    if len(parts) != 2:
        return stored == hashlib.sha256(password.encode()).hexdigest()
    salt, h = parts
    return h == hashlib.sha256((salt + password).encode()).hexdigest()

# ═══════════════════════════════════════════
# Database init
# ═══════════════════════════════════════════
def _get_db():
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def _migrate(conn):
    """安全的增量迁移: 只添加不删除, 永不丢失数据"""
    # v1 → 添加邮箱和重置字段
    migrations = [
        ("email", "TEXT DEFAULT ''"),
        ("reset_token", "TEXT DEFAULT ''"),
        ("reset_expires", "TEXT DEFAULT ''"),
    ]
    existing = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for col, col_def in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {col_def}")

def init_db():
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',       -- 'admin' / 'user'
            active INTEGER DEFAULT 1,
            email TEXT DEFAULT '',
            reset_token TEXT DEFAULT '',
            reset_expires TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS user_configs (
            user_id INTEGER PRIMARY KEY,
            config_json TEXT DEFAULT '{}',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            license_key TEXT UNIQUE NOT NULL,
            plan TEXT DEFAULT 'standard',   -- 'trial' / 'standard' / 'pro'
            issued_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS fuel_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            txn_type TEXT NOT NULL,          -- 'topup' / 'commission_deduct' / 'refund'
            balance_after REAL,
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS trade_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT, direction TEXT,
            entry REAL, exit_price REAL, pnl REAL,
            reason TEXT, created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    # 安全增量迁移: 只加不删, 永不丢数据
    _migrate(conn)
    conn.commit()
    # 确保有默认管理员
    row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if not row:
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                     ("admin", _hash_pw("axiom2026"), "admin"))
        conn.execute("INSERT INTO licenses (user_id, license_key, plan, expires_at) VALUES (?,?,?,?)",
                     (1, "AXM-ADMIN-0000-0001", "pro", "2099-12-31 23:59:59"))
    else:
        conn.execute("UPDATE users SET role='admin', active=1 WHERE username='admin'")
    conn.commit()
    conn.close()

# ═══════════════════════════════════════════
# Auth
# ═══════════════════════════════════════════
def register_user(username: str, password: str, email: str = "", role: str = "user") -> tuple:
    """返回 (success:bool, message:str)"""
    if len(username) < 3: return False, "用户名至少3个字符"
    if len(password) < 6: return False, "密码至少6个字符"
    if role == "user" and not email:
        return False, "请填写邮箱用于找回密码"
    conn = _get_db()
    try:
        conn.execute("INSERT INTO users (username, password_hash, role, email) VALUES (?,?,?,?)",
                     (username, _hash_pw(password), role, email))
        conn.commit()
        return True, "注册成功"
    except sqlite3.IntegrityError:
        return False, "用户名已存在"
    finally:
        conn.close()

def request_password_reset(email: str) -> tuple:
    """请求密码重置, 返回 (success, code, message)"""
    conn = _get_db()
    row = conn.execute("SELECT id FROM users WHERE email=? AND active=1", (email,)).fetchone()
    if not row:
        conn.close()
        return False, "", "该邮箱未注册"
    code = secrets.token_hex(3).upper()  # 6位验证码
    exp = (_utcnow() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE users SET reset_token=?, reset_expires=? WHERE id=?", (code, exp, row["id"]))
    conn.commit()
    conn.close()
    return True, code, "验证码已生成"

def reset_password_with_code(email: str, code: str, new_password: str) -> tuple:
    """用验证码重置密码"""
    if len(new_password) < 6: return False, "新密码至少6位"
    conn = _get_db()
    row = conn.execute("SELECT id, reset_token, reset_expires FROM users WHERE email=? AND active=1", (email,)).fetchone()
    if not row or row["reset_token"] != code.upper():
        conn.close()
        return False, "验证码错误"
    exp = datetime.strptime(row["reset_expires"], "%Y-%m-%d %H:%M:%S")
    if _utcnow() > exp:
        conn.close()
        return False, "验证码已过期"
    conn.execute("UPDATE users SET password_hash=?, reset_token='', reset_expires='' WHERE id=?",
                 (_hash_pw(new_password), row["id"]))
    conn.commit()
    conn.close()
    return True, "密码重置成功"

def authenticate(username: str, password: str) -> dict | None:
    """验证成功返回用户dict, 失败返回None"""
    username = (username or "").strip()
    conn = _get_db()
    row = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
    conn.close()
    if not row: return None
    if not _verify_pw(password, row["password_hash"]): return None
    return dict(row)

def diagnose_auth(username: str, password: str) -> dict:
    """Return a non-secret login diagnosis for admin troubleshooting."""
    username = (username or "").strip()
    conn = _get_db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "reason": "user_not_found"}
    if not int(row["active"] or 0):
        return {"ok": False, "reason": "user_disabled", "role": row["role"]}
    if not _verify_pw(password, row["password_hash"]):
        return {"ok": False, "reason": "password_invalid", "role": row["role"]}
    if row["role"] != "admin":
        return {"ok": False, "reason": "not_admin", "role": row["role"]}
    return {"ok": True, "reason": "pass", "role": row["role"]}

def get_user(user_id: int) -> dict | None:
    conn = _get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

# ═══════════════════════════════════════════
# License
# ═══════════════════════════════════════════
def check_license_valid(user_id: int) -> tuple:
    """返回 (valid:bool, plan:str, days_left:int)"""
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM licenses WHERE user_id=? AND active=1 ORDER BY expires_at DESC LIMIT 1",
        (user_id,)).fetchone()
    conn.close()
    if not row: return False, "none", 0
    exp = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
    days = (exp - _utcnow()).days
    if days < 0: return False, row["plan"], days
    return True, row["plan"], days

def issue_license(user_id: int, plan: str, days: int, key: str = None) -> str:
    """颁发许可证, 返回key"""
    if not key:
        key = f"AXM-{plan.upper()[:4]}-{secrets.token_hex(4).upper()}-{secrets.token_hex(2).upper()}"
    exp = (_utcnow() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_db()
    conn.execute("INSERT INTO licenses (user_id, license_key, plan, expires_at) VALUES (?,?,?,?)",
                 (user_id, key, plan, exp))
    conn.commit()
    conn.close()
    return key

def get_user_licenses(user_id: int) -> list:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM licenses WHERE user_id=? ORDER BY issued_at DESC", (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ═══════════════════════════════════════════
# Fuel
# ═══════════════════════════════════════════
def get_fuel_balance(user_id: int) -> float:
    conn = _get_db()
    row = conn.execute(
        "SELECT balance_after FROM fuel_ledger WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,)).fetchone()
    conn.close()
    return row["balance_after"] if row else 0.0

def fuel_topup(user_id: int, amount: float, note: str = "充值") -> float:
    bal = get_fuel_balance(user_id) + amount
    conn = _get_db()
    conn.execute("INSERT INTO fuel_ledger (user_id, amount, txn_type, balance_after, note) VALUES (?,?,?,?,?)",
                 (user_id, amount, "topup", bal, note))
    conn.commit()
    conn.close()
    return bal

def fuel_deduct(user_id: int, amount: float, note: str = "盈利分成") -> float:
    bal = max(0, get_fuel_balance(user_id) - amount)
    conn = _get_db()
    conn.execute("INSERT INTO fuel_ledger (user_id, amount, txn_type, balance_after, note) VALUES (?,?,?,?,?)",
                 (user_id, amount, "commission_deduct", bal, note))
    conn.commit()
    conn.close()
    return bal

def get_fuel_history(user_id: int, limit: int = 20) -> list:
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM fuel_ledger WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ═══════════════════════════════════════════
# User Config (data isolation)
# ═══════════════════════════════════════════
def load_user_config(user_id: int) -> dict:
    conn = _get_db()
    row = conn.execute("SELECT config_json FROM user_configs WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if row: return __import__('json').loads(row["config_json"])
    return {}

def save_user_config(user_id: int, config: dict):
    j = __import__('json').dumps(config)
    conn = _get_db()
    conn.execute("INSERT OR REPLACE INTO user_configs (user_id, config_json) VALUES (?,?)", (user_id, j))
    conn.commit()
    conn.close()

# ═══════════════════════════════════════════
# Trade log
# ═══════════════════════════════════════════
def log_trade(user_id: int, symbol: str, direction: str, entry: float, exit_price: float, pnl: float, reason: str):
    conn = _get_db()
    conn.execute("INSERT INTO trade_logs (user_id, symbol, direction, entry, exit_price, pnl, reason) VALUES (?,?,?,?,?,?,?)",
                 (user_id, symbol, direction, entry, exit_price, pnl, reason))
    conn.commit()
    conn.close()

# ═══════════════════════════════════════════
# Admin
# ═══════════════════════════════════════════
def admin_get_all_users() -> list:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def admin_get_stats() -> dict:
    conn = _get_db()
    total_users = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    active_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE active=1").fetchone()["c"]
    total_trades = conn.execute("SELECT COUNT(*) as c FROM trade_logs").fetchone()["c"]
    row = conn.execute("SELECT SUM(pnl) as s FROM trade_logs WHERE pnl>0").fetchone()
    total_profit = row["s"] or 0
    row2 = conn.execute("SELECT SUM(amount) as s FROM fuel_ledger WHERE txn_type='commission_deduct'").fetchone()
    total_fuel = row2["s"] or 0
    conn.close()
    return {
        "total_users": total_users, "active_users": active_users,
        "total_trades": total_trades, "total_profit": round(total_profit, 2),
        "total_fuel_income": round(total_fuel, 2),
    }

def reset_password(user_id: int, new_password: str) -> bool:
    conn = _get_db()
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (_hash_pw(new_password), user_id))
    conn.commit()
    conn.close()
    return True

def admin_set_user_active(user_id: int, active: bool):
    conn = _get_db()
    conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))
    conn.commit()
    conn.close()

# 初始化
init_db()
