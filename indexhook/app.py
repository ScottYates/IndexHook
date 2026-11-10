"""Web application: webhook intake, login, settings, rules, recordings."""
from __future__ import annotations

import contextlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import audio, auth, rules as rules_mod, stt
from .config import GLOBAL_FIELDS, USER_FIELDS, Settings, save_form
from .db import DB, loads, now
from .worker import Worker

HERE = Path(__file__).parent
EXT = {"audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/x-m4a": ".m4a", "audio/mpeg": ".mp3",
       "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac", "audio/ogg": ".ogg",
       "audio/webm": ".webm", "video/webm": ".webm", "audio/aac": ".aac"}
DEDUPE_WINDOW = 600


def _fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def create_app(data_dir: str | None = None, root_path: str | None = None, transport=None) -> Starlette:
    data_dir = data_dir or os.environ.get("INDEXHOOK_DATA_DIR", "./data")
    root = (root_path if root_path is not None else os.environ.get("INDEXHOOK_ROOT_PATH", "")).rstrip("/")
    db = DB(data_dir)
    worker = Worker(db, transport=transport)
    throttle = auth.Throttle()
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["ts"] = _fmt_time
    templates.env.filters["pct"] = lambda v: "" if v is None else f"{v:.0%}"

    # helpers -----------------------------------------------------------
    def peer(request: Request) -> str | None:
        return request.client.host if request.client else None

    def is_https(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        return auth.proxy_is_trusted(db, peer(request)) and \
            request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"

    def base_url(request: Request) -> str:
        configured = Settings(db).get("public_base_url").rstrip("/")
        if configured:
            return configured
        host = request.headers.get("host", "localhost")
        return f"{'https' if is_https(request) else 'http'}://{host}{root}"

    def go(path: str, msg: str = "") -> RedirectResponse:
        url = root + path
        if msg:
            url += ("&" if "?" in url else "?") + "m=" + quote(msg)
        return RedirectResponse(url, status_code=303)

    def current(request: Request):
        """Return (user, csrf, via_header)."""
        user = auth.header_user(db, request.headers, peer(request))
        if user is not None:
            return user, auth.header_csrf(db, user["id"]), True
        user, csrf = auth.session_user(db, request.cookies.get(auth.COOKIE))
        return user, csrf, False

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        if not origin or origin == "null":
            return origin is None
        return urlparse(origin).netloc == request.headers.get("host", "")

    def render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
        st = getattr(request.state, "auth", (None, None, False))
        base = {"root": root, "user": st[0], "csrf": st[1], "via_header": st[2],
                "msg": request.query_params.get("m", ""), "path": request.url.path[len(root):] or "/"}
        return templates.TemplateResponse(request, name, {**base, **(ctx or {})}, status_code=status)

    def page(admin: bool = False):
        """Wrap a handler: require login, check CSRF on POST, hand over the parsed form."""
        def deco(fn):
            async def handler(request: Request):
                user, csrf, via = current(request)
                if user is None:
                    return go("/login")
                if admin and not user["is_admin"]:
                    return PlainTextResponse("Admins only.", 403)
                request.state.auth = (user, csrf, via)
                form = None
                if request.method == "POST":
                    if not same_origin(request):
                        return PlainTextResponse("Cross-site request refused.", 403)
                    form = await request.form(max_part_size=4 * 1024 * 1024)
                    if not hmac.compare_digest(str(form.get("csrf", "")), csrf):
                        return PlainTextResponse("The form expired. Go back, reload the page and try again.", 403)
                return await fn(request, user, form)
            return handler
        return deco

    def own_recording(user, request: Request):
        return db.one("SELECT * FROM recordings WHERE id=? AND user_id=?",
                      int(request.path_params["id"]), user["id"])

    async def store_audio(user_id: int, up: UploadFile, limit: int) -> tuple[Path, str] | None:
        mime = (up.content_type or "audio/mp4").split(";")[0].strip().lower()
        ext = EXT.get(mime) or (Path(up.filename or "").suffix.lower()[:6] if up.filename else "") or ".m4a"
        if not ext.startswith(".") or not ext[1:].isalnum():
            ext = ".m4a"
        folder = db.audio_dir / str(user_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}{ext}"
        size = 0
        with open(path, "wb") as f:
            while chunk := await up.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    f.close()
                    path.unlink(missing_ok=True)
                    return None
                f.write(chunk)
        if size == 0:
            path.unlink(missing_ok=True)
            return None
        return path, mime

    # webhook -------------------------------------------------------------
    async def hook(request: Request):
        token = request.path_params.get("token") or request.headers.get("x-hook-token") or ""
        if not token:
            scheme, _, value = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() == "bearer":
                token = value.strip()
        user = auth.user_for_hook_token(db, token)
        if user is None:
            return JSONResponse({"ok": False, "error": "unknown or missing token"}, 401)
        limit = Settings(db).int("max_upload_mb") * 1024 * 1024
        try:
            if int(request.headers.get("content-length", "0")) > limit + 65536:
                return JSONResponse({"ok": False, "error": "recording too large"}, 413)
            form = await request.form(max_part_size=1024 * 1024)
        except Exception:
            return JSONResponse({"ok": False, "error": "expected multipart/form-data"}, 400)

        ring_text = str(form.get("transcription") or "").strip()
        recorded_at = None
        try:
            recorded_at = int(str(form.get("recordedAt"))) / 1000
        except (TypeError, ValueError):
            pass
        if recorded_at is not None:
            dup = db.one("SELECT id FROM recordings WHERE user_id=? AND recorded_at=? AND received_at>? AND source='ring'",
                         user["id"], recorded_at, now() - DEDUPE_WINDOW)
            if dup:
                return JSONResponse({"ok": True, "id": dup["id"], "duplicate": True})

        saved = None
        up = form.get("audio")
        if isinstance(up, UploadFile):
            saved = await store_audio(user["id"], up, limit)
            if saved is None and not ring_text:
                return JSONResponse({"ok": False, "error": "audio empty or too large"}, 413)
        elif not ring_text:
            return JSONResponse({"ok": False, "error": "no audio and no transcription field"}, 400)

        rec_id = db.run(
            "INSERT INTO recordings(user_id,source,received_at,recorded_at,audio_path,audio_mime,ring_transcript)"
            " VALUES(?,?,?,?,?,?,?)", user["id"], "ring", now(), recorded_at,
            str(saved[0]) if saved else None, saved[1] if saved else None, ring_text)
        worker.wake()
        # The ring's handling of responses is not documented. Answer fast, work afterwards.
        return JSONResponse({"ok": True, "id": rec_id}, 202)

    async def healthz(request: Request):
        return JSONResponse({"ok": True})

    # login -----------------------------------------------------------------
    def registration_allowed() -> bool:
        return db.one("SELECT COUNT(*) c FROM users")["c"] == 0 or Settings(db).bool("registration_open")

    def login_response(request: Request, user_id: int):
        resp = go("/")
        resp.set_cookie(auth.COOKIE, auth.start_session(db, user_id), httponly=True, samesite="lax",
                        secure=is_https(request), path=root or "/",
                        max_age=(Settings(db).int("session_days") or 30) * 86400)
        return resp

    async def login(request: Request):
        if current(request)[0] is not None:
            return go("/")
        ctx = {"can_register": registration_allowed(), "error": ""}
        if request.method == "POST":
            if not same_origin(request):
                return PlainTextResponse("Cross-site request refused.", 403)
            form = await request.form()
            name, pw = str(form.get("username", "")).strip(), str(form.get("password", ""))
            key = f"{name.lower()}|{peer(request)}"
            wait = throttle.wait(key)
            if wait:
                ctx["error"] = f"Too many attempts. Try again in {wait} seconds."
            else:
                user = db.one("SELECT * FROM users WHERE username=? AND disabled=0", name)
                # Hash even for unknown users so timing does not reveal which names exist.
                ok = auth.check_password(pw, user["password_hash"] if user else auth.hash_password("x"))
                if user and ok:
                    throttle.ok(key)
                    return login_response(request, user["id"])
                throttle.fail(key)
                ctx["error"] = "Wrong user name or password."
            ctx["username"] = name
        return render(request, "login.html", ctx, 200 if not ctx["error"] else 401)

    async def register(request: Request):
        if not registration_allowed():
            return go("/login", "Registration is closed. Ask the admin for an account.")
        ctx = {"error": "", "first": db.one("SELECT COUNT(*) c FROM users")["c"] == 0}
        if request.method == "POST":
            if not same_origin(request):
                return PlainTextResponse("Cross-site request refused.", 403)
            form = await request.form()
            try:
                if form.get("password") != form.get("password2"):
                    raise ValueError("The two passwords differ.")
                uid = auth.create_user(db, str(form.get("username", "")), str(form.get("password", "")),
                                       str(form.get("email", "")))
                return login_response(request, uid)
            except ValueError as e:
                ctx.update(error=str(e), username=form.get("username", ""), email=form.get("email", ""))
        return render(request, "register.html", ctx)

    @page()
    async def logout(request, user, form):
        auth.end_session(db, request.cookies.get(auth.COOKIE))
        resp = go("/login")
        resp.delete_cookie(auth.COOKIE, path=root or "/")
        return resp

    # dashboard ---------------------------------------------------------------
    def checks(user) -> list[tuple[bool, str]]:
        s = Settings(db, user["id"])
        out = []
        out.append((bool(user["hook_token_hash"]), "Webhook token created" if user["hook_token_hash"]
                    else "No webhook token yet. Create one below."))
        eng = s.get("stt_engine")
        out.append((stt.engine_ready(s, eng), f"Main speech engine ({eng}) is configured" if stt.engine_ready(s, eng)
                    else f"The main speech engine ({eng}) has no API key. Recordings fall back to the ring's own transcript."))
        out.append((audio.have_ffmpeg(), "ffmpeg found" if audio.have_ffmpeg()
                    else "ffmpeg is not installed. Audio clean-up and silence detection are off."))
        active = [r for r in rules_mod.user_rules(db, user["id"]) if r["enabled"]]
        if any(r["action_type"] == "email" for r in active):
            ok = bool(s.get("smtp_host")) and bool(s.get("note_to") or user["email"])
            out.append((ok, "Email is set up" if ok else "A rule sends email, but the SMTP server or your address is missing."))
        if any(r["action_type"] == "ntfy" and not r["config"].get("topic") for r in active) and not s.get("ntfy_topic"):
            out.append((False, "A rule sends ntfy notifications, but no topic is set."))
        if not active:
            out.append((False, "No rule is enabled, so recordings are transcribed and nothing else happens."))
        return out

    def dashboard_ctx(request, user, token: str = ""):
        recs = db.q("SELECT * FROM recordings WHERE user_id=? ORDER BY id DESC LIMIT 8", user["id"])
        return {"checks": checks(user), "hook_url": base_url(request) + "/hook", "new_token": token,
                "recordings": recs}

    @page()
    async def dashboard(request, user, form):
        return render(request, "dashboard.html", dashboard_ctx(request, user))

    @page()
    async def token_new(request, user, form):
        token = auth.new_hook_token(db, user["id"])
        request.state.auth = (db.one("SELECT * FROM users WHERE id=?", user["id"]), *request.state.auth[1:])
        return render(request, "dashboard.html", dashboard_ctx(request, request.state.auth[0], token))

    # account -----------------------------------------------------------------
    @page()
    async def account(request, user, form):
        error = ""
        if form is not None:
            if form.get("what") == "email":
                db.run("UPDATE users SET email=? WHERE id=?", str(form.get("email", "")).strip(), user["id"])
                return go("/account", "Email saved.")
            new = str(form.get("new", ""))
            if user["password_hash"] and not auth.check_password(str(form.get("old", "")), user["password_hash"]):
                error = "The current password is wrong."
            elif len(new) < 8:
                error = "The new password needs at least 8 characters."
            elif new != form.get("new2"):
                error = "The two new passwords differ."
            else:
                db.run("UPDATE users SET password_hash=? WHERE id=?", auth.hash_password(new), user["id"])
                db.run("DELETE FROM sessions WHERE user_id=?", user["id"])
                return login_response(request, user["id"])
        return render(request, "account.html", {"error": error})

    # settings ----------------------------------------------------------------
    def field_rows(fields, scope_values: dict, inherited: Settings | None):
        """Build what the settings template needs for each field."""
        sections: dict[str, list] = {}
        for f in fields:
            own = scope_values.get(f.key, "")
            if inherited is not None:   # user page: show what applies when left blank
                fallback = inherited.get(f.key)
                has_fallback = bool(fallback) if f.kind == "secret" else True
            else:
                fallback, has_fallback = f.default, bool(f.default)
            label = dict(f.choices).get(fallback, fallback) if f.kind == "select" else fallback
            if f.kind == "bool":
                label = "on" if fallback == "1" else "off"
            sections.setdefault(f.section, []).append({
                "f": f, "own": own, "fallback": "" if f.kind == "secret" else label,
                "has_fallback": has_fallback, "is_set": bool(own)})
        return sections

    @page()
    async def settings(request, user, form):
        scope = f"user:{user['id']}"
        if form is not None:
            save_form(db, scope, USER_FIELDS, form)
            return go("/settings", "Settings saved.")
        return render(request, "settings.html", {
            "title": "Settings", "action": "/settings", "mode": "user",
            "intro": "These settings apply to your recordings only. A blank field uses the server default.",
            "sections": field_rows(USER_FIELDS, db.scope_settings(scope), Settings(db))})

    @page(admin=True)
    async def admin_defaults(request, user, form):
        if form is not None:
            save_form(db, "global", USER_FIELDS, form)
            return go("/admin/defaults", "Server defaults saved.")
        return render(request, "settings.html", {
            "title": "Server defaults", "action": "/admin/defaults", "mode": "global",
            "intro": "Values used for every user who leaves the same field blank on their own Settings page. "
                     "A user can use a key set here but cannot read it.",
            "sections": field_rows(USER_FIELDS, db.scope_settings("global"), None)})

    @page(admin=True)
    async def admin(request, user, form):
        if form is not None:
            save_form(db, "global", GLOBAL_FIELDS, form)
            return go("/admin", "Server settings saved.")
        users = db.q("SELECT u.*, (SELECT COUNT(*) FROM recordings r WHERE r.user_id=u.id) n FROM users u ORDER BY username")
        return render(request, "admin.html", {
            "sections": field_rows(GLOBAL_FIELDS, db.scope_settings("global"), None), "users": users,
            "mode": "global"})

    @page(admin=True)
    async def admin_user_new(request, user, form):
        try:
            auth.create_user(db, str(form.get("username", "")), str(form.get("password", "")),
                             str(form.get("email", "")), admin=bool(form.get("is_admin")))
        except ValueError as e:
            return go("/admin", str(e))
        return go("/admin", "User created.")

    @page(admin=True)
    async def admin_user(request, user, form):
        target = db.one("SELECT * FROM users WHERE id=?", int(request.path_params["id"]))
        if target is None:
            return go("/admin", "No such user.")
        act = form.get("act")
        other_admins = db.one("SELECT COUNT(*) c FROM users WHERE is_admin=1 AND disabled=0 AND id<>?", target["id"])["c"]
        if act in ("disable", "demote", "delete") and target["is_admin"] and not other_admins:
            return go("/admin", "That is the only admin.")
        if act == "disable":
            db.run("UPDATE users SET disabled=1 WHERE id=?", target["id"])
            db.run("DELETE FROM sessions WHERE user_id=?", target["id"])
        elif act == "enable":
            db.run("UPDATE users SET disabled=0 WHERE id=?", target["id"])
        elif act == "promote":
            db.run("UPDATE users SET is_admin=1 WHERE id=?", target["id"])
        elif act == "demote":
            db.run("UPDATE users SET is_admin=0 WHERE id=?", target["id"])
        elif act == "password":
            pw = str(form.get("password", ""))
            if len(pw) < 8:
                return go("/admin", "The password needs at least 8 characters.")
            db.run("UPDATE users SET password_hash=? WHERE id=?", auth.hash_password(pw), target["id"])
            db.run("DELETE FROM sessions WHERE user_id=?", target["id"])
        elif act == "delete":
            for r in db.q("SELECT audio_path FROM recordings WHERE user_id=? AND audio_path IS NOT NULL", target["id"]):
                Path(r["audio_path"]).unlink(missing_ok=True)
            db.run("DELETE FROM settings WHERE scope=?", f"user:{target['id']}")
            db.run("DELETE FROM users WHERE id=?", target["id"])
        return go("/admin", "Done.")

    # rules -------------------------------------------------------------------
    @page()
    async def rules_page(request, user, form):
        ctx = {"rules": rules_mod.user_rules(db, user["id"]), "match_types": rules_mod.MATCH_TYPES,
               "action_types": rules_mod.ACTION_TYPES, "edit": None, "tested": None, "phrase": ""}
        edit = request.query_params.get("edit")
        if edit == "new":
            ctx["edit"] = {"id": "", "name": "", "enabled": 1, "match_type": "starts_with", "pattern": "",
                           "strip_trigger": 1, "stop": 1, "action_type": "email", "config": {}}
        elif edit:
            ctx["edit"] = next((r for r in ctx["rules"] if str(r["id"]) == edit), None)
        if form is not None:   # "try a phrase"
            ctx["phrase"] = str(form.get("phrase", ""))
            ctx["tested"] = rules_mod.evaluate(ctx["rules"], ctx["phrase"])
        return render(request, "rules.html", ctx)

    @page()
    async def rule_save(request, user, form):
        mt, at = form.get("match_type"), form.get("action_type")
        if mt not in dict(rules_mod.MATCH_TYPES) or at not in dict(rules_mod.ACTION_TYPES):
            return go("/rules", "Unknown rule type.")
        cfg = {}
        prefix = f"cfg_{at}_"
        for k, v in form.items():
            if k.startswith(prefix) and str(v).strip():
                cfg[k[len(prefix):]] = str(v).replace("\r\n", "\n").strip()
        if "attach_audio" in cfg:
            cfg["attach_audio"] = True
        vals = (str(form.get("name", "")).strip() or "Rule", int(bool(form.get("enabled"))), mt,
                str(form.get("pattern", "")).replace("\r\n", "\n").strip(),
                int(bool(form.get("strip_trigger"))), int(bool(form.get("stop"))), at, json.dumps(cfg))
        rid = form.get("id")
        if rid:
            db.run("UPDATE rules SET name=?,enabled=?,match_type=?,pattern=?,strip_trigger=?,stop=?,action_type=?,"
                   "action_config=? WHERE id=? AND user_id=?", *vals, int(rid), user["id"])
        else:
            pos = db.one("SELECT COALESCE(MAX(position),-1)+1 p FROM rules WHERE user_id=?", user["id"])["p"]
            db.run("INSERT INTO rules(name,enabled,match_type,pattern,strip_trigger,stop,action_type,action_config,"
                   "user_id,position) VALUES(?,?,?,?,?,?,?,?,?,?)", *vals, user["id"], pos)
        return go("/rules", "Rule saved.")

    @page()
    async def rule_act(request, user, form):
        rid = int(request.path_params["id"])
        ids = [r["id"] for r in rules_mod.user_rules(db, user["id"])]
        if rid not in ids:
            return go("/rules")
        act = form.get("act")
        if act == "delete":
            db.run("DELETE FROM rules WHERE id=? AND user_id=?", rid, user["id"])
            ids.remove(rid)
        elif act in ("up", "down"):
            i = ids.index(rid)
            j = i - 1 if act == "up" else i + 1
            if 0 <= j < len(ids):
                ids[i], ids[j] = ids[j], ids[i]
        for pos, id_ in enumerate(ids):
            db.run("UPDATE rules SET position=? WHERE id=?", pos, id_)
        return go("/rules")

    # recordings ----------------------------------------------------------------
    @page()
    async def recordings(request, user, form):
        try:
            pg = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            pg = 1
        only_review = request.query_params.get("review") == "1"
        where = "user_id=?" + (" AND needs_review=1" if only_review else "")
        rows = db.q(f"SELECT * FROM recordings WHERE {where} ORDER BY id DESC LIMIT 51 OFFSET ?",
                    user["id"], (pg - 1) * 50)
        return render(request, "recordings.html", {"recordings": rows[:50], "page": pg, "more": len(rows) > 50,
                                                   "only_review": only_review})

    @page()
    async def recording(request, user, form):
        rec = own_recording(user, request)
        if rec is None:
            return go("/recordings", "No such recording.")
        return render(request, "recording.html", {
            "rec": rec, "detail": loads(rec["detail"], {}),
            "deliveries": db.q("SELECT * FROM deliveries WHERE recording_id=? ORDER BY id", rec["id"]),
            "has_audio": bool(rec["audio_path"]) and Path(rec["audio_path"]).exists(),
            "busy": rec["status"] in ("queued", "transcribing")})

    @page()
    async def recording_audio(request, user, form):
        rec = own_recording(user, request)
        if rec is None or not rec["audio_path"] or not Path(rec["audio_path"]).exists():
            return PlainTextResponse("No audio.", 404)
        return FileResponse(rec["audio_path"], media_type=rec["audio_mime"] or "audio/mp4")

    @page()
    async def recording_act(request, user, form):
        rec = own_recording(user, request)
        if rec is None:
            return go("/recordings")
        act = form.get("act")
        here = f"/recordings/{rec['id']}"
        if act == "save":
            text = str(form.get("transcript", "")).replace("\r\n", "\n").strip()
            db.run("UPDATE recordings SET transcript=?, transcript_source='edited', needs_review=0, status='done', error='' WHERE id=?",
                   text, rec["id"])
            if form.get("rerun"):
                n = worker.apply_rules(rec["id"])
                return go(here, f"Saved. {n} rule(s) matched and ran.")
            return go(here, "Saved.")
        if act == "reviewed":
            db.run("UPDATE recordings SET needs_review=0 WHERE id=?", rec["id"])
            return go(here)
        if act == "retranscribe":
            if not rec["audio_path"] or not Path(rec["audio_path"]).exists():
                return go(here, "The audio is no longer stored.")
            db.run("UPDATE recordings SET status='queued', attempts=0, next_try=0, run_rules=?, error='' WHERE id=?",
                   int(bool(form.get("rerun"))), rec["id"])
            worker.wake()
            return go(here, "Transcribing again.")
        if act == "delete":
            if rec["audio_path"]:
                Path(rec["audio_path"]).unlink(missing_ok=True)
            db.run("DELETE FROM recordings WHERE id=?", rec["id"])
            return go("/recordings", "Recording deleted.")
        return go(here)

    @page()
    async def delivery_retry(request, user, form):
        d = db.one("SELECT d.* FROM deliveries d JOIN recordings r ON r.id=d.recording_id WHERE d.id=? AND r.user_id=?",
                   int(request.path_params["id"]), user["id"])
        if d and d["status"] in ("failed", "pending"):
            db.run("UPDATE deliveries SET status='pending', next_try=0, attempts=0, detail='' WHERE id=?", d["id"])
            worker.wake()
        return go(f"/recordings/{d['recording_id']}" if d else "/recordings")

    # test page -------------------------------------------------------------------
    @page()
    async def test_page(request, user, form):
        if form is not None:
            up = form.get("audio")
            limit = Settings(db).int("max_upload_mb") * 1024 * 1024
            saved = await store_audio(user["id"], up, limit) if isinstance(up, UploadFile) else None
            if saved is None:
                return go("/test", "Choose or record an audio file first.")
            rec_id = db.run(
                "INSERT INTO recordings(user_id,source,run_rules,received_at,recorded_at,audio_path,audio_mime)"
                " VALUES(?,?,?,?,?,?,?)", user["id"], "test", int(bool(form.get("run_rules"))), now(), now(),
                str(saved[0]), saved[1])
            worker.wake()
            return go(f"/recordings/{rec_id}")
        s = Settings(db, user["id"])
        return render(request, "test.html", {"engine": s.get("stt_engine"), "second": s.get("stt_second_engine")})

    # root path handling ------------------------------------------------------------
    class RootPath:
        """Serve under a path prefix whether or not the reverse proxy strips it."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if root and scope["type"] in ("http", "websocket"):
                path = scope["path"]
                if not (path == root or path.startswith(root + "/")):
                    path = root + path
                scope = dict(scope, path=path, root_path=root, raw_path=path.encode())
            await self.app(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        worker.start()
        yield
        await worker.stop()

    P = ["GET", "POST"]
    routes = [
        Route("/hook", hook, methods=["POST"]),
        Route("/hook/{token}", hook, methods=["POST"]),
        Route("/healthz", healthz),
        Route("/login", login, methods=P),
        Route("/register", register, methods=P),
        Route("/logout", logout, methods=["POST"]),
        Route("/", dashboard),
        Route("/token", token_new, methods=["POST"]),
        Route("/account", account, methods=P),
        Route("/settings", settings, methods=P),
        Route("/rules", rules_page, methods=P),
        Route("/rules/save", rule_save, methods=["POST"]),
        Route("/rules/{id:int}", rule_act, methods=["POST"]),
        Route("/recordings", recordings),
        Route("/recordings/{id:int}", recording),
        Route("/recordings/{id:int}/audio", recording_audio),
        Route("/recordings/{id:int}/act", recording_act, methods=["POST"]),
        Route("/deliveries/{id:int}/retry", delivery_retry, methods=["POST"]),
        Route("/test", test_page, methods=P),
        Route("/admin", admin, methods=P),
        Route("/admin/defaults", admin_defaults, methods=P),
        Route("/admin/users", admin_user_new, methods=["POST"]),
        Route("/admin/users/{id:int}", admin_user, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.db, app.state.worker = db, worker
    wrapped = RootPath(app)
    wrapped.state = app.state  # type: ignore[attr-defined]
    return wrapped  # type: ignore[return-value]
