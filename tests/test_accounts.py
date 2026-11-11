"""Login, isolation between users, CSRF, reverse proxy login, sub-path hosting."""
import re

from conftest import csrf_of, new_token, register, set_user


def login(c, name, password):
    return c.post("/login", data={"username": name, "password": password})


def test_first_user_is_admin_and_registration_then_closes(client):
    register(client)
    assert client.db.one("SELECT is_admin FROM users WHERE username='scott'")["is_admin"] == 1
    assert client.get("/admin").status_code == 200
    client.cookies.clear()
    r = client.get("/register")
    assert r.status_code == 303 and "/login" in r.headers["location"]
    client.db.set_setting("global", "registration_open", "1")
    register(client, "anna", "another pass", "anna@example.org")
    assert client.db.one("SELECT is_admin FROM users WHERE username='anna'")["is_admin"] == 0
    assert client.get("/admin").status_code == 403
    assert client.get("/admin/defaults").status_code == 403


def test_pages_require_login_and_login_works(client):
    register(client)
    client.cookies.clear()
    for path in ["/", "/settings", "/rules", "/recordings", "/test", "/admin", "/account"]:
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert login(client, "scott", "wrong").status_code == 401
    assert login(client, "nobody", "correct horse").status_code == 401
    assert login(client, "scott", "correct horse").status_code == 303
    assert client.get("/").status_code == 200


def test_login_is_throttled(client):
    register(client)
    client.cookies.clear()
    for _ in range(5):
        login(client, "scott", "wrong")
    r = login(client, "scott", "correct horse")
    assert r.status_code == 401 and "Too many attempts" in r.text


def test_post_needs_csrf_and_same_origin(client):
    csrf = register(client)
    assert client.post("/settings", data={"stt_language": "de"}).status_code == 403
    assert client.post("/settings", data={"csrf": "x", "stt_language": "de"}).status_code == 403
    assert client.post("/settings", data={"csrf": csrf, "stt_language": "de"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/settings", data={"csrf": csrf, "stt_language": "de"}).status_code == 303
    assert 'value="de"' in client.get("/settings").text


def test_session_cookie_flags(client):
    r = client.post("/register", data={"username": "scott", "password": "correct horse", "password2": "correct horse"})
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie
    # behind a trusted proxy that reports https, the cookie is marked Secure
    client.db.set_setting("global", "trusted_proxies", "testclient")
    client.cookies.clear()
    r = client.post("/login", data={"username": "scott", "password": "correct horse"},
                    headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers["set-cookie"]


def test_users_cannot_see_each_other(client, tone):
    register(client, "anna", "another pass", "anna@example.org")
    anna = client.db.one("SELECT id FROM users WHERE username='anna'")["id"]
    client.db.set_setting("global", "registration_open", "1")
    rid = client.db.run("INSERT INTO recordings(user_id,received_at,audio_path,audio_mime,status,transcript) "
                        "VALUES(?,?,?,?,'done','annas secret')", anna, 1.0, str(tone), "audio/mp4")
    did = client.db.run("INSERT INTO deliveries(recording_id,rule_name,action_type,action_config,text,status,updated_at)"
                        " VALUES(?,?,?,?,?,'failed',1)", rid, "r", "email", "{}", "t")
    rule = client.db.one("SELECT id FROM rules WHERE user_id=?", anna)["id"]
    client.cookies.clear()
    csrf = register(client, "bob", "bobs password", "bob@example.org")

    assert "annas secret" not in client.get("/recordings").text
    assert client.get(f"/recordings/{rid}").status_code == 303
    assert client.get(f"/recordings/{rid}/audio").status_code == 404
    client.post(f"/recordings/{rid}/act", data={"csrf": csrf, "act": "delete"})
    client.post(f"/deliveries/{did}/retry", data={"csrf": csrf})
    client.post(f"/rules/{rule}", data={"csrf": csrf, "act": "delete"})
    client.post("/rules/save", data={"csrf": csrf, "id": str(rule), "name": "hijack", "match_type": "always",
                                     "action_type": "email"})
    assert client.db.one("SELECT COUNT(*) n FROM recordings WHERE id=?", rid)["n"] == 1
    assert client.db.one("SELECT status FROM deliveries WHERE id=?", did)["status"] == "failed"
    assert client.db.one("SELECT name FROM rules WHERE id=?", rule)["name"] == "Note to self"


def test_settings_are_per_user_with_server_defaults(client):
    from indexhook.config import Settings
    register(client)
    client.db.set_setting("global", "registration_open", "1")
    client.db.set_setting("global", "openai_api_key", "sk-server")
    client.db.set_setting("global", "stt_language", "de")
    client.cookies.clear()
    csrf = register(client, "anna", "another pass")
    anna = client.db.one("SELECT id FROM users WHERE username='anna'")["id"]
    page = client.get("/settings").text
    assert "sk-server" not in page and "using the server" in page     # usable, not readable
    assert Settings(client.db, anna).get("openai_api_key") == "sk-server"
    assert Settings(client.db, anna).get("stt_language") == "de"
    client.post("/settings", data={"csrf": csrf, "stt_language": "fr", "audio_cleanup": "0"})
    assert Settings(client.db, anna).get("stt_language") == "fr"
    assert Settings(client.db, anna).bool("audio_cleanup") is False
    assert Settings(client.db, 1).get("stt_language") == "de"
    # a user who points the engine at their own URL does not get the server's key
    client.post("/settings", data={"csrf": csrf, "openai_base_url": "https://attacker.example/v1"})
    assert Settings(client.db, anna).get("openai_api_key") == ""
    client.db.set_setting("global", "minimax_api_key", "mm-server")
    assert Settings(client.db, anna).get("minimax_api_key") == "mm-server"
    client.post("/settings", data={"csrf": csrf, "minimax_base_url": "https://attacker.example/v1"})
    assert Settings(client.db, anna).get("minimax_api_key") == ""
    client.db.set_setting("global", "claude_api_key", "sk-ant-server")
    assert Settings(client.db, anna).get("claude_api_key") == "sk-ant-server"
    client.post("/settings", data={"csrf": csrf, "claude_base_url": "https://attacker.example"})
    assert Settings(client.db, anna).get("claude_api_key") == ""


def test_secret_fields_keep_and_clear(client):
    from indexhook.config import Settings
    csrf = register(client)
    client.post("/settings", data={"csrf": csrf, "deepgram_api_key": "dg-1"})
    assert "dg-1" not in client.get("/settings").text
    client.post("/settings", data={"csrf": csrf, "deepgram_api_key": ""})           # blank keeps it
    assert Settings(client.db, 1).get("deepgram_api_key") == "dg-1"
    client.post("/settings", data={"csrf": csrf, "clear_deepgram_api_key": "1"})
    assert Settings(client.db, 1).get("deepgram_api_key") == ""


def test_hook_token_is_stored_hashed(client):
    csrf = register(client)
    token = new_token(client, csrf)
    row = client.db.one("SELECT * FROM users")
    assert token not in str(dict(row)) and token not in client.get("/").text


def test_proxy_header_login(client):
    register(client)
    client.cookies.clear()
    # off by default
    assert client.get("/", headers={"Ynh-User": "scott"}).status_code == 303
    client.db.set_setting("global", "trusted_header", "Ynh-User")
    client.db.set_setting("global", "trusted_header_email", "Ynh-User-Email")
    # header from an address that is not a trusted proxy is ignored
    assert client.get("/", headers={"Ynh-User": "scott"}).status_code == 303
    client.db.set_setting("global", "trusted_proxies", "testclient")
    r = client.get("/", headers={"Ynh-User": "scott"})
    assert r.status_code == 200 and "Log out" not in r.text
    # unknown user is created, not admin, with the email from the proxy
    r = client.get("/", headers={"Ynh-User": "carol", "Ynh-User-Email": "carol@example.org"})
    assert r.status_code == 200
    carol = client.db.one("SELECT * FROM users WHERE username='carol'")
    assert carol["email"] == "carol@example.org" and not carol["is_admin"] and carol["password_hash"] is None
    assert client.db.one("SELECT COUNT(*) n FROM rules WHERE user_id=?", carol["id"])["n"] == 2
    # forms work for header logins
    csrf = csrf_of(r.text)
    assert client.post("/settings", data={"csrf": csrf, "stt_language": "es"}, headers={"Ynh-User": "carol"}).status_code == 303
    # a password-less account cannot log in with a password
    assert login(client, "carol", "").status_code == 401
    client.db.set_setting("global", "header_auto_create", "0")
    assert client.get("/", headers={"Ynh-User": "dave"}).status_code == 303


def test_admin_manages_users(client):
    csrf = register(client)
    client.post("/admin/users", data={"csrf": csrf, "username": "anna", "password": "another pass", "email": "a@example.org"})
    anna = client.db.one("SELECT * FROM users WHERE username='anna'")
    assert anna and not anna["is_admin"]
    # the only admin cannot be removed
    client.post("/admin/users/1", data={"csrf": csrf, "act": "delete"})
    client.post("/admin/users/1", data={"csrf": csrf, "act": "demote"})
    assert client.db.one("SELECT is_admin FROM users WHERE id=1")["is_admin"] == 1
    client.post(f"/admin/users/{anna['id']}", data={"csrf": csrf, "act": "disable"})
    client.cookies.clear()
    assert login(client, "anna", "another pass").status_code == 401


def test_served_under_a_sub_path(make_client, tone):
    c = make_client(root_path="/indexhook")
    # works whether the proxy keeps the prefix or strips it
    for prefix in ("/indexhook", ""):
        r = c.get(prefix + "/login")
        assert r.status_code == 200 and 'action="/indexhook/login"' in r.text and 'href="/indexhook/static/style.css"' in r.text
    assert c.get("/indexhook/static/style.css").status_code == 200
    r = c.post("/indexhook/register", data={"username": "scott", "password": "correct horse", "password2": "correct horse"})
    assert r.headers["location"] == "/indexhook/" and "Path=/indexhook" in r.headers["set-cookie"]
    page = c.get("/indexhook/").text
    assert "http://testserver/indexhook/hook" in page
    token = new_token(c, csrf_of(page), "/indexhook")
    r = c.post("/indexhook/hook", headers={"X-Hook-Token": token}, data={"recordedAt": "1", "client": "ring"},
               files={"audio": ("a.m4a", tone.read_bytes(), "audio/mp4")})
    assert r.status_code == 202


def test_public_base_url_setting(client):
    register(client)
    client.db.set_setting("global", "public_base_url", "https://example.org/indexhook/")
    assert "https://example.org/indexhook/hook" in client.get("/").text


def test_upload_limit(client, tone):
    csrf = register(client)
    token = new_token(client, csrf)
    client.db.set_setting("global", "max_upload_mb", "0")
    r = client.post("/hook", headers={"X-Hook-Token": token}, data={"recordedAt": "1"},
                    files={"audio": ("a.m4a", b"x" * 200000, "audio/mp4")})
    assert r.status_code == 413
    assert list(client.db.audio_dir.rglob("*.m4a")) == []
