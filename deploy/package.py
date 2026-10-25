"""Create a credential-free, deterministic deployment source archive."""
import hashlib
import json
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parents[1]
output = root / ".local" / "codex-usage-monitor-0.3.0.tar.gz"
manifest = {}
with tarfile.open(output, "w:gz") as archive:
    for folder in ("server", "deploy"):
        for path in sorted((root / folder).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.name.startswith("."):
                continue
            # Operational status contains no credentials but is unrelated to the runtime.
            name = path.relative_to(root).as_posix()
            manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            archive.add(path, arcname=name, recursive=False)
    for path in sorted((root / ".local" / "wheels").glob("*.whl")):
        name = "wheels/" + path.name
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        archive.add(path, arcname=name, recursive=False)
    for name in ("docs/MULTI-ACCOUNT-DESIGN.md","docs/MULTI-ACCOUNT-OPERATIONS.md","docs/LOCAL-ACCOUNT-UI.md","docs/QUOTA-CYCLES.md"):
        path=root/name
        if path.exists():
            manifest[name]=hashlib.sha256(path.read_bytes()).hexdigest()
            archive.add(path,arcname=name,recursive=False)
    archive.add(root / ".dockerignore", arcname=".dockerignore")
    if (root / "README.md").exists():
        archive.add(root / "README.md", arcname="README.md")
manifest_path = output.with_suffix(".manifest.json")
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(json.dumps(dict(path=str(output), bytes=output.stat().st_size,
                     sha256=hashlib.sha256(output.read_bytes()).hexdigest(), files=len(manifest))))
