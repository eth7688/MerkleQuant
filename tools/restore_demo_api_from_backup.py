import json
import os
import shutil
import sys
import tarfile
import time


SECRET_FIELDS = [
    "api_key",
    "api_secret",
    "testnet_api_key",
    "testnet_api_secret",
    "bitget_api_key",
    "bitget_api_secret",
    "bitget_api_pass",
]

CONNECTION_FIELDS = [
    "mode",
    "testnet",
    "market_type",
    "leverage",
    "is_lead_trader",
]


def load_demo_config_from_tar(path):
    with tarfile.open(path, "r:gz") as tar:
        member = None
        for item in tar.getmembers():
            if item.isfile() and item.name.endswith("demo_bot_config.json"):
                member = item
                break
        if member is None:
            raise RuntimeError("demo_bot_config.json not found in backup")
        fh = tar.extractfile(member)
        if fh is None:
            raise RuntimeError("cannot read demo_bot_config.json from backup")
        return json.load(fh)


def flag_map(data, fields):
    return {k: bool(data.get(k)) for k in fields}


def main():
    if len(sys.argv) != 3:
        print("usage: restore_demo_api_from_backup.py BACKUP_TAR CONFIG_JSON")
        return 2

    backup_path = sys.argv[1]
    config_path = sys.argv[2]
    backup_cfg = load_demo_config_from_tar(backup_path)
    with open(config_path, "r", encoding="utf-8") as f:
        current = json.load(f)

    before = flag_map(current, SECRET_FIELDS)
    backup_has = flag_map(backup_cfg, SECRET_FIELDS)

    restored_secret = []
    for key in SECRET_FIELDS:
        if backup_cfg.get(key):
            current[key] = backup_cfg[key]
            restored_secret.append(key)

    restored_connection = []
    for key in CONNECTION_FIELDS:
        if key in backup_cfg:
            current[key] = backup_cfg[key]
            restored_connection.append(key)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    safety_copy = f"{config_path}.bak_api_restore_{stamp}"
    shutil.copy2(config_path, safety_copy)
    tmp_path = f"{config_path}.tmp_api_restore"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, config_path)

    after = flag_map(current, SECRET_FIELDS)
    print(json.dumps({
        "ok": True,
        "safety_copy": safety_copy,
        "before_has_secret": before,
        "backup_has_secret": backup_has,
        "after_has_secret": after,
        "restored_secret_fields": restored_secret,
        "restored_connection_fields": restored_connection,
        "market_type": current.get("market_type"),
        "mode": current.get("mode"),
        "testnet": current.get("testnet"),
        "leverage": current.get("leverage"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
