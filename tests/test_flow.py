"""End-to-end tests: a simulated ring POST, fake speech engines, a real local SMTP sink."""
from conftest import csrf_of, detail, new_token, register, set_user, wait_done


def ring_post(c, token, tone, transcription="note to self call she vani", recorded_at=1762813440000, header="x-hook-token", path="/hook"):
    files = {"audio": ("recording.m4a", tone.read_bytes(), "audio/mp4")} if tone else None
    data = {"recordedAt": str(recorded_at), "client": "ring"}
    if transcription:
        data["transcription"] = transcription
    headers = {"Authorization": f"Bearer {token}"} if header == "authorization" else ({header: token} if header else {})
    if not files:
        files = {"client": (None, "ring")}  # force multipart
    return c.post(path, data=data, files=files, headers=headers)


def setup_user(c, smtp, **extra):
    csrf = register(c)
    token = new_token(c, csrf)
    uid = c.db.one("SELECT id FROM users WHERE username='scott'")["id"]
    set_user(c, uid, openai_api_key="sk-test", smtp_host="127.0.0.1", smtp_port=str(smtp.port),
             smtp_security="none", mail_from="ring@example.org", **extra)
    return csrf, token, uid


def test_note_to_self_end_to_end(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp, vocabulary="Shivani\nYunohost => YunoHost",
                                  stt_context="Short personal reminders.")
    r = ring_post(client, token, tone)
    assert r.status_code == 202
    rec = wait_done(client, r.json()["id"])

    assert rec["status"] == "done" and rec["transcript_source"] == "openai"
    # the "heard => meant" fix was applied to the engine's text
    assert rec["transcript"] == "Note to self, call Shivani about the YunoHost upgrade."
    assert not rec["needs_review"]
    d = detail(rec)
    assert d["cleaned"] is True and d["duration"] > 1.5
    assert [c["engine"] for c in d["candidates"]] == ["openai", "ring"]

    # the engine got cleaned FLAC, the language, and a prompt carrying the vocabulary
    call = world.to("api.openai.com")[0]
    assert call["headers"]["authorization"] == "Bearer sk-test"
    assert b"fLaC" in call["body"] and b'name="language"\r\n\r\nen' in call["body"]
    assert b"Short personal reminders. Names and terms that may appear: Shivani, YunoHost." in call["body"]

    # one email, trigger phrase stripped, subject from the first words
    assert len(smtp.messages) == 1
    msg = smtp.messages[0]
    assert msg.rcpt_tos == ["scott@example.org"]
    content = msg.content.decode()
    assert "Subject: Note: Call Shivani about the YunoHost upgrade." in content
    assert "\r\n\r\nCall Shivani about the YunoHost upgrade." in content
    assert "Note to self" not in content.split("\r\n\r\n", 1)[1]

    # the cleaned temp file is removed, the original is kept and playable
    files = list((client.db.audio_dir / str(uid)).iterdir())
    assert [f.suffix for f in files] == [".m4a"]
    assert client.get(f"/recordings/{rec['id']}/audio").content == tone.read_bytes()
    page = client.get(f"/recordings/{rec['id']}").text
    assert "sent to scott@example.org" in page and "call she vani" in page


def test_hook_auth_variants(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp)
    assert ring_post(client, "", tone, header=None).status_code == 401
    assert ring_post(client, "ih_wrong", tone).status_code == 401
    assert ring_post(client, token, tone, header="authorization", recorded_at=1).status_code == 202
    assert ring_post(client, token, tone, header=None, path=f"/hook/{token}", recorded_at=2).status_code == 202
    # a replaced token stops working
    new = new_token(client, csrf)
    assert ring_post(client, token, tone, recorded_at=3).status_code == 401
    assert ring_post(client, new, tone, recorded_at=3).status_code == 202


def test_duplicate_post_is_ignored(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp)
    a = ring_post(client, token, tone)
    b = ring_post(client, token, tone)
    assert b.status_code == 200 and b.json() == {"ok": True, "id": a.json()["id"], "duplicate": True}
    wait_done(client, a.json()["id"])
    assert len(smtp.messages) == 1


def test_engines_disagree_marks_review(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp, stt_second_engine="deepgram", deepgram_api_key="dg",
                                  vocabulary="Shivani")
    world.texts["deepgram"] = "Note to self, fall for Ronnie about the union host of grade"
    rec = wait_done(client, ring_post(client, token, tone).json()["id"])
    d = detail(rec)
    assert rec["needs_review"] and d["agreement"] < 0.85
    assert rec["transcript_source"] == "openai"
    dg = world.to("api.deepgram.com")[0]
    assert "model=nova-3" in dg["query"] and "keyterm=Shivani" in dg["query"] and "language=en" in dg["query"]
    assert dg["headers"]["authorization"] == "Token dg" and dg["headers"]["content-type"] == "audio/flac"
    assert "may contain errors" in smtp.messages[0].content.decode()


def test_engines_agree_no_review(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp, stt_engine="elevenlabs", elevenlabs_api_key="el",
                                  stt_second_engine="deepgram", deepgram_api_key="dg", vocabulary="Shivani")
    rec = wait_done(client, ring_post(client, token, tone).json()["id"])
    assert not rec["needs_review"] and detail(rec)["agreement"] == 1.0
    assert rec["transcript_source"] == "elevenlabs"
    el = world.to("api.elevenlabs.io")[0]
    assert el["headers"]["xi-api-key"] == "el"
    assert b'name="model_id"\r\n\r\nscribe_v2' in el["body"] and b'name="keyterms"\r\n\r\nShivani' in el["body"]
    assert b'name="language_code"\r\n\r\nen' in el["body"]


def test_correction_pass_accepts_small_fix_and_rejects_rewrite(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp, llm_enabled="1", llm_api_key="k", llm_base_url="https://llm.test/v1",
                                  vocabulary="Shivani\nYunoHost")
    world.llm = "<think>hmm</think>Note to self, call Shivani about the YunoHost upgrade."
    rec = wait_done(client, ring_post(client, token, tone, recorded_at=1).json()["id"])
    assert rec["transcript"] == "Note to self, call Shivani about the YunoHost upgrade."
    assert rec["transcript_source"] == "corrected"
    sent = world.to("llm.test")[0]["body"].decode()
    assert "MAIN (openai)" in sent and "OTHER (ring)" in sent and "Shivani, YunoHost" in sent

    world.llm = "Sure! Here is a summary: you want to phone a colleague regarding server maintenance."
    rec = wait_done(client, ring_post(client, token, tone, recorded_at=2).json()["id"])
    assert rec["transcript"] == world.texts["openai"] and rec["transcript_source"] == "openai"
    assert any("correction rejected" in n for n in detail(rec)["notes"])


def test_bad_key_falls_back_to_ring_transcript_without_waiting(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp)
    world.status["openai"] = 401
    rec = wait_done(client, ring_post(client, token, tone).json()["id"])
    assert rec["status"] == "done" and rec["transcript_source"] == "ring" and rec["needs_review"]
    assert rec["attempts"] == 1     # a 401 is not retried
    assert "call she vani" in smtp.messages[0].content.decode().lower()


def test_outage_is_retried_then_recovers(client, world, smtp, tone, monkeypatch):
    from indexhook import worker
    monkeypatch.setattr(worker, "STT_BACKOFF", [1, 1, 1])
    monkeypatch.setattr(worker.Worker, "TICK", 0.2)
    csrf, token, uid = setup_user(client, smtp)
    world.status["openai"] = 503
    rid = ring_post(client, token, tone).json()["id"]
    import time
    time.sleep(0.7)
    assert client.db.one("SELECT status, error FROM recordings WHERE id=?", rid)["status"] == "queued"
    assert smtp.messages == []
    world.status["openai"] = 200
    rec = wait_done(client, rid)
    assert rec["transcript_source"] == "openai" and rec["attempts"] >= 2
    assert len(smtp.messages) == 1


def test_no_transcript_at_all_still_delivers_audio(client, world, smtp, tone, monkeypatch):
    from indexhook import worker
    monkeypatch.setattr(worker, "STT_BACKOFF", [])
    csrf, token, uid = setup_user(client, smtp)
    world.status["openai"] = 500
    rec = wait_done(client, ring_post(client, token, tone, transcription="").json()["id"])
    assert rec["status"] == "failed" and rec["needs_review"]
    # only the catch-all rule matches; the recording is attached
    content = smtp.messages[0].content.decode()
    assert "Subject: Ring: voice recording" in content and "No transcript" in content
    assert "audio/mp4" in content and "recording-" in content


def test_silence_is_not_sent_to_an_engine(client, world, smtp, silence):
    csrf, token, uid = setup_user(client, smtp)
    rec = wait_done(client, ring_post(client, token, silence, transcription="").json()["id"])
    assert rec["status"] == "empty" and world.to("api.openai.com") == [] and smtp.messages == []


def test_text_only_webhook(client, world, smtp):
    csrf, token, uid = setup_user(client, smtp)
    rec = wait_done(client, ring_post(client, token, None, transcription="Note to self, water the plants").json()["id"])
    assert rec["transcript_source"] == "ring" and world.to("api.openai.com") == []
    assert "\r\n\r\nWater the plants" in smtp.messages[0].content.decode()


def test_smtp_down_then_manual_retry(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp)
    set_user(client, uid, smtp_port="1")
    rid = ring_post(client, token, tone).json()["id"]
    import time
    end = time.time() + 10
    while time.time() < end:
        d = client.db.one("SELECT * FROM deliveries WHERE recording_id=?", rid)
        if d and d["attempts"] == 1 and d["status"] == "pending":
            break
        time.sleep(0.1)
    assert d["status"] == "pending" and "retry in 1 min" in d["detail"]
    set_user(client, uid, smtp_port=str(smtp.port))
    assert client.post(f"/deliveries/{d['id']}/retry", data={"csrf": csrf}).status_code == 303
    wait_done(client, rid)
    assert len(smtp.messages) == 1


def test_ntfy_and_webhook_actions(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp, ntfy_topic="ring-notes", ntfy_token="tk")
    client.db.run("DELETE FROM rules WHERE user_id=?", uid)
    for name, at, cfg in [("Ping", "ntfy", {"cfg_ntfy_title": "{rule} {time}"}),
                          ("Forward", "webhook", {"cfg_webhook_url": "https://hooks.test/in?k=1",
                                                  "cfg_webhook_headers": "X-Key: abc"})]:
        r = client.post("/rules/save", data={"csrf": csrf, "id": "", "name": name, "match_type": "always",
                                             "pattern": "", "enabled": "1", "action_type": at, **cfg})
        assert r.status_code == 303
    rec = wait_done(client, ring_post(client, token, tone).json()["id"])
    import json
    n = world.to("ntfy.sh")[0]
    body = json.loads(n["body"])
    assert body["topic"] == "ring-notes" and body["message"] == world.texts["openai"] and body["title"].startswith("Ping ")
    assert n["headers"]["authorization"] == "Bearer tk"
    w = world.to("hooks.test")[0]
    assert w["headers"]["x-key"] == "abc" and json.loads(w["body"])["recording_id"] == rec["id"]


def test_edit_transcript_and_rerun_rules(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp)
    rec = wait_done(client, ring_post(client, token, tone).json()["id"])
    r = client.post(f"/recordings/{rec['id']}/act", data={"csrf": csrf, "act": "save", "rerun": "1",
                                                           "transcript": "Note to self, call Siobhan."})
    assert r.status_code == 303
    wait_done(client, rec["id"])
    assert len(smtp.messages) == 2 and "\r\n\r\nCall Siobhan." in smtp.messages[1].content.decode()


def test_test_page_upload_skips_rules(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp)
    r = client.post("/test", data={"csrf": csrf}, files={"audio": ("t.m4a", tone.read_bytes(), "audio/mp4")})
    rid = int(r.headers["location"].rsplit("/", 1)[1])
    rec = wait_done(client, rid)
    assert rec["status"] == "done" and rec["source"] == "test" and smtp.messages == []


def test_restart_resumes_queued_recording(make_client, world, smtp, tone):
    c1 = make_client()
    csrf, token, uid = setup_user(c1, smtp)
    c1.__exit__(None, None, None)           # stop the worker
    c1.db.run("INSERT INTO recordings(user_id,received_at,audio_path,audio_mime,status) VALUES(?,?,?,?,'transcribing')",
              uid, 1.0, str(tone), "audio/mp4")
    c1.__enter__()                          # start again on the same data
    rec = wait_done(c1, 1)
    assert rec["status"] == "done" and len(smtp.messages) == 1


def test_minimax_engine(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp, stt_engine="minimax", minimax_api_key="mm-key",
                                  vocabulary="Shivani")
    rec = wait_done(client, ring_post(client, token, tone, recorded_at=1).json()["id"])
    assert rec["status"] == "done" and rec["transcript_source"] == "minimax"
    assert rec["transcript"] == world.texts["minimax"]
    call = world.to("api.minimax.io")[0]
    assert call["path"] == "/v1/speech_to_text"
    assert call["headers"]["authorization"] == "Bearer mm-key"
    assert call["headers"]["language"] == "en"            # sent as a header, per the API
    body = call["body"]
    assert b'name="model"\r\n\r\nasr-1.0' in body and b'name="response_format"\r\n\r\njson' in body
    assert b'name="stream"\r\n\r\nfalse' in body and b"fLaC" in body
    assert b"Shivani" not in body and b'name="language"' not in body
    assert "\r\n\r\nCall Shivani" in smtp.messages[0].content.decode()


def test_minimax_settings_are_configurable(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp, stt_engine="minimax", minimax_api_key="mm-key")
    r = client.post("/settings", data={"csrf": csrf, "minimax_base_url": "https://api.minimaxi.com/v1/",
                                       "minimax_model": "asr-2.0", "stt_language": "sv",
                                       "stt_engine": "minimax"})
    assert r.status_code == 303
    page = client.get("/settings").text
    assert "MiniMax engine" in page and "mm-key" not in page
    wait_done(client, ring_post(client, token, tone, recorded_at=2).json()["id"])
    call = world.to("api.minimaxi.com")[0]
    assert call["path"] == "/v1/speech_to_text" and b"asr-2.0" in call["body"]
    assert "language" not in call["headers"]              # Swedish is not a listed tag: auto-detect


def test_minimax_out_of_credit_falls_back_without_retry(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp, stt_engine="minimax", minimax_api_key="mm-key")
    world.status["minimax"] = 402
    rec = wait_done(client, ring_post(client, token, tone, recorded_at=3).json()["id"])
    assert rec["transcript_source"] == "ring" and rec["needs_review"] and rec["attempts"] == 1


def test_minimax_as_cross_check(client, world, smtp, tone):
    csrf, token, uid = setup_user(client, smtp, stt_second_engine="minimax", minimax_api_key="mm-key",
                                  vocabulary="Yunohost => YunoHost")
    rec = wait_done(client, ring_post(client, token, tone, recorded_at=4).json()["id"])
    d = detail(rec)
    assert [c["engine"] for c in d["candidates"]] == ["openai", "minimax", "ring"]
    assert d["agreement"] == 1.0 and not rec["needs_review"]
