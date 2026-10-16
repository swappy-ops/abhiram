"""Create a consistent SQLite backup of the local Campaign Control Desk database."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent
source=Path(os.environ.get("CCD_DB",ROOT/"data"/"campaigns.sqlite3"))
if not source.exists():
    raise SystemExit(f"Database not found: {source}. Start the application once first.")
target_dir=Path(os.environ.get("CCD_BACKUP_DIR",ROOT/"backups"))
target_dir.mkdir(parents=True,exist_ok=True)
stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
target=target_dir/f"campaigns-{stamp}.sqlite3"
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
print(f"Backup written: {target}")
