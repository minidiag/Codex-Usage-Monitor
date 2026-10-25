"""Add only one include to the existing, verified IP HTTPS server; validate before reload."""
from pathlib import Path
import shutil
import subprocess
from datetime import datetime

config = Path("/etc/nginx/conf.d/benchlink.conf")
include = "    include /etc/nginx/codex-usage.location.conf;"
source = Path("/opt/codex-usage-monitor/deploy/nginx/codex-usage.location.conf")
target = Path("/etc/nginx/codex-usage.location.conf")
text = config.read_text()
marker = "    include /etc/nginx/ota-location.conf;"
if text.count(marker) != 1 or "listen 443 ssl default_server;" not in text:
    raise RuntimeError("Existing Nginx layout changed; refusing automatic edit")
backup = Path("/opt/codex-usage-monitor/private/nginx-" + datetime.utcnow().strftime("%Y%m%dT%H%M%S") + ".conf")
shutil.copy2(str(config), str(backup))
had_target = target.exists()
previous_target = target.read_bytes() if had_target else None
try:
    shutil.copyfile(str(source), str(target))
    if include not in text:
        config.write_text(text.replace(marker, marker + "\n" + include))
    subprocess.check_call(["nginx", "-t"])
    subprocess.check_call(["systemctl", "reload", "nginx"])
except BaseException:
    config.write_text(text)
    if had_target:
        target.write_bytes(previous_target)
    else:
        target.unlink()
    raise
print("Nginx validation and reload succeeded; existing configuration backed up privately.")
