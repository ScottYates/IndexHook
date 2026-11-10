"""Command line: run the server, or manage accounts and settings without the web page."""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys

from . import auth
from .config import BY_KEY
from .db import DB


def main(argv=None):
    p = argparse.ArgumentParser(prog="indexhook")
    p.add_argument("--data-dir", default=os.environ.get("INDEXHOOK_DATA_DIR", "./data"),
                   help="where the database and audio are stored (env INDEXHOOK_DATA_DIR)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the web server (default)")
    s.add_argument("--host", default=os.environ.get("INDEXHOOK_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("INDEXHOOK_PORT", "8787")))
    s.add_argument("--root-path", default=os.environ.get("INDEXHOOK_ROOT_PATH", ""),
                   help="URL prefix when served under a sub-path, for example /indexhook")

    u = sub.add_parser("create-user", help="create an account")
    u.add_argument("username")
    u.add_argument("--email", default="")
    u.add_argument("--admin", action="store_true")
    u.add_argument("--no-password", action="store_true", help="account for reverse proxy login only")

    pw = sub.add_parser("set-password", help="reset a password")
    pw.add_argument("username")

    st = sub.add_parser("set", help="set a server-level setting, for example: set trusted_header Ynh-User")
    st.add_argument("key")
    st.add_argument("value", nargs="?", default=None, help="omit to reset to the default")

    args = p.parse_args(argv)
    cmd = args.cmd or "serve"

    if cmd == "serve":
        import uvicorn
        from .app import create_app
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        app = create_app(args.data_dir, getattr(args, "root_path", os.environ.get("INDEXHOOK_ROOT_PATH", "")))
        # proxy_headers is off on purpose: the app needs the address of the directly
        # connected peer to decide whether a login header can be trusted.
        uvicorn.run(app, host=getattr(args, "host", "127.0.0.1"), port=getattr(args, "port", 8787),
                    proxy_headers=False, log_level="info")
        return 0

    db = DB(args.data_dir)
    if cmd == "create-user":
        password = None if args.no_password else getpass.getpass("Password: ")
        try:
            auth.create_user(db, args.username, password, args.email, admin=True if args.admin else None)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"Created {args.username}.")
    elif cmd == "set-password":
        user = db.one("SELECT id FROM users WHERE username=?", args.username)
        if not user:
            print("No such user.", file=sys.stderr)
            return 1
        password = getpass.getpass("New password: ")
        if len(password) < 8:
            print("The password needs at least 8 characters.", file=sys.stderr)
            return 1
        db.run("UPDATE users SET password_hash=? WHERE id=?", auth.hash_password(password), user["id"])
        db.run("DELETE FROM sessions WHERE user_id=?", user["id"])
        print("Password changed.")
    elif cmd == "set":
        if args.key not in BY_KEY:
            print("Unknown setting. Known keys: " + ", ".join(sorted(BY_KEY)), file=sys.stderr)
            return 1
        db.set_setting("global", args.key, args.value or None)
        print(f"{args.key} = {args.value if args.value else '(default)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
