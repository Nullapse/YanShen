import os
import sys
from pathlib import Path

APP_DIR_NAME = "GongkaoShenlun"


def resource_root():
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root)
    return Path(__file__).resolve().parent.parent


def seed_db_path():
    return resource_root() / "data" / "gongkao_seed.sqlite3"


def user_data_dir():
    override = os.environ.get("GONGKAO_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / APP_DIR_NAME
    return Path.home() / ".gongkao-shenlun"


def user_db_path():
    override = os.environ.get("GONGKAO_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return user_data_dir() / "gongkao.sqlite3"


def log_path():
    return user_data_dir() / "gongkao.log"
