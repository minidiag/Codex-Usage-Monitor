"""Per-Windows-user runtime paths; shared program files never imply shared mutable state."""
import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def user_root(root=ROOT):
    key = hashlib.sha256(str(root.resolve()).lower().encode()).hexdigest()[:16].upper()
    local = Path(os.environ.get("LOCALAPPDATA", Path.home()/"AppData/Local"))
    return local/"CodexUsageMonitor"/key


def state_path(): return user_root()/"state"
def credential_path(): return user_root()/"client.credentials"
