"""Run on the designated host. Never prints generated database secrets."""
import os
from pathlib import Path
import secrets
import subprocess

target = Path(__file__).with_name(".env")
if target.exists():
    print("Existing deployment environment preserved.")
else:
    images = {}
    for name, tag in (("PYTHON_IMAGE", "docker.m.daocloud.io/library/python:3.12-slim"),
                      ("POSTGRES_IMAGE", "docker.m.daocloud.io/library/postgres:18-alpine")):
        digest = subprocess.check_output(["docker", "image", "inspect", tag, "--format", "{{index .RepoDigests 0}}"])
        images[name] = digest.decode().strip()
        if "@sha256:" not in images[name]:
            raise RuntimeError("Image must have an immutable digest")
    values = dict(images, OWNER_DB_PASSWORD=secrets.token_hex(32), APP_DB_PASSWORD=secrets.token_hex(32))
    descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        for key, value in values.items():
            handle.write(key + "=" + value + "\n")
    print("Created private environment with pinned images and generated database passwords.")
