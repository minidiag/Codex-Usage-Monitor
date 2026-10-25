# Release sanitization

This public tree is a sanitized release copy.

- Excluded: `.local/`, per-user state, credentials, backups, virtualenvs, `__pycache__`, wheels.
- Replaced: personal absolute paths and the real server host with `YOUR_SERVER_HOST` / `C:\Users\<USERNAME>`.
- Runtime config (credentials, `.env`, private deploy records) is delivered separately and is not in this tree.
- Verify the packed archive members, not only the source folder, before publishing.
- No plaintext credential leak was found in this round; revoke/reissue only if one appears later.
