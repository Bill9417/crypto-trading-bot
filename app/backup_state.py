"""
Daily state backup — every *.json state file + users.db into one zip.

The whole stack's memory (signal history, alert levels, halt flags, the
Telegram ledger, login DB) lives as single files in app/. One corrupted
file used to mean starting that feature from zero. Now the first sweep of
each day zips them into backups/state-YYYY-MM-DD.zip (kept KEEP days,
gitignored). Restore = unzip over app/ while the stack is stopped.
"""
import glob
import os
import zipfile
from datetime import datetime

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(APP_DIR, "backups")
KEEP = 14
# Second copy OFF this disk (a dead disk otherwise takes the data AND all 14
# local backups with it). Default: the Mac's iCloud Drive folder — synced to
# Apple's servers automatically, no extra accounts. Empty = local-only.
OFFSITE_DIR = os.getenv(
    "BACKUP_OFFSITE_DIR",
    os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/"
                       "WolfScannerBackups")).strip()


def _targets() -> list:
    """Every state file worth keeping: app/*.json + the login DB."""
    files = sorted(glob.glob(os.path.join(APP_DIR, "*.json")))
    db = os.path.join(APP_DIR, "instance", "users.db")
    if os.path.exists(db):
        files.append(db)
    return files


def make_backup(dest_dir: str, files: list, date_str: str) -> str:
    """Write the zip (idempotent per day) and prune old ones. Pure enough to
    unit-test with a tmp dir. Returns the zip path ('' if nothing to back up)."""
    if not files:
        return ""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, f"state-{date_str}.zip")
    if os.path.exists(path):
        return path
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            try:
                z.write(f, os.path.basename(f))
            except OSError:
                continue                      # a vanished tmp file must not abort
    os.replace(tmp, path)
    zips = sorted(glob.glob(os.path.join(dest_dir, "state-*.zip")))
    for old in zips[:-KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path


def offsite_copy(zip_path: str, offsite_dir: str) -> bool:
    """Mirror one zip into the offsite dir and prune it to KEEP. The parent
    (e.g. iCloud Drive) must already exist — we only create our subfolder,
    never a fake iCloud path on a machine without it."""
    if not (zip_path and offsite_dir):
        return False
    parent = os.path.dirname(offsite_dir.rstrip("/"))
    if not os.path.isdir(parent):
        return False
    os.makedirs(offsite_dir, exist_ok=True)
    dest = os.path.join(offsite_dir, os.path.basename(zip_path))
    if not os.path.exists(dest):
        import shutil
        shutil.copy2(zip_path, dest)
    for old in sorted(glob.glob(os.path.join(offsite_dir, "state-*.zip")))[:-KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return True


def tick() -> bool:
    """Scanner hook: one backup per calendar day; True when one was written."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(BACKUP_DIR, f"state-{date_str}.zip")
    if os.path.exists(path):
        return False
    made = make_backup(BACKUP_DIR, _targets(), date_str)
    if made:
        print(f"[backup] wrote {os.path.basename(made)}")
        try:
            if offsite_copy(made, OFFSITE_DIR):
                print(f"[backup] offsite copy → {OFFSITE_DIR}")
        except Exception as exc:  # noqa: BLE001 — offsite is best-effort
            print(f"[backup] offsite copy failed: {exc}")
    return bool(made)
