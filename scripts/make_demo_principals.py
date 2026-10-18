#!/usr/bin/env python3
"""Generate two demo principals for a local BidBrain and append them to the repo's .env (gitignored).

    python scripts/make_demo_principals.py            # adds manager (bid_manager) + approver (approver)
    python scripts/make_demo_principals.py --force    # replace existing BIDBRAIN_* auth keys

Writes, appending only (existing lines are kept unless --force):
    BIDBRAIN_PRINCIPALS=manager:bid_manager:<sha256>,approver:approver:<sha256>
    BIDBRAIN_AUTH_KEY=<random>
    BIDBRAIN_DEMO_SECRET_MANAGER=<secret>      # plaintext demo secrets, so you can sign in; local use only
    BIDBRAIN_DEMO_SECRET_APPROVER=<secret>

The secrets are never printed. Read them from .env yourself (e.g. `grep DEMO_SECRET .env`). The server
only ever reads the sha256 digests; restart the API after running this so it picks up the new keys.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import secrets
import sys

ENV = pathlib.Path(__file__).resolve().parents[1] / ".env"
KEYS = ("BIDBRAIN_PRINCIPALS", "BIDBRAIN_AUTH_KEY", "BIDBRAIN_DEMO_SECRET_MANAGER", "BIDBRAIN_DEMO_SECRET_APPROVER")


def main(force: bool = False) -> int:
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    present = {ln.split("=", 1)[0].strip() for ln in lines if "=" in ln and not ln.lstrip().startswith("#")}
    if present & set(KEYS) and not force:
        print(f"{ENV} already defines {', '.join(sorted(present & set(KEYS)))}; nothing changed "
              "(use --force to regenerate).")
        return 0
    if force:
        lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in KEYS]
    sec = {"manager": secrets.token_urlsafe(18), "approver": secrets.token_urlsafe(18)}
    digest = {n: hashlib.sha256(s.encode()).hexdigest() for n, s in sec.items()}
    block = ["", "# --- demo principals (scripts/make_demo_principals.py); local use only, never commit ---",
             f"BIDBRAIN_PRINCIPALS=manager:bid_manager:{digest['manager']},approver:approver:{digest['approver']}",
             f"BIDBRAIN_AUTH_KEY={secrets.token_urlsafe(32)}",
             f"BIDBRAIN_DEMO_SECRET_MANAGER={sec['manager']}",
             f"BIDBRAIN_DEMO_SECRET_APPROVER={sec['approver']}"]
    ENV.write_text("\n".join(lines + block) + "\n")
    try:
        os.chmod(ENV, 0o600)
    except OSError:
        pass
    print(f"Two demo principals (manager, approver) written to {ENV}.")
    print("Their secrets are in BIDBRAIN_DEMO_SECRET_MANAGER / BIDBRAIN_DEMO_SECRET_APPROVER there; restart the API.")
    return 0


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv[1:]))
