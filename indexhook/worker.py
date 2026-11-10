"""Background processing.

A recording is written to disk and to the database before the webhook answers.
This loop then transcribes it, evaluates the rules and runs the actions.
State lives in the database, so a restart continues where it stopped.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from . import actions, rules, stt
from .config import Settings
from .db import DB, loads, now

log = logging.getLogger("indexhook.worker")

STT_BACKOFF = [20, 90, 300]                        # seconds before transcription attempts 2..4
DELIVERY_BACKOFF = [60, 300, 1800, 7200, 21600]    # seconds before delivery attempts 2..6


class Worker:
    TICK = 15   # seconds between checks for due retries

    def __init__(self, db: DB, transport=None):
        self.db = db
        self.transport = transport      # test hook for outgoing HTTP
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._busy: set[tuple[str, int]] = set()
        self._sem: asyncio.Semaphore | None = None
        self._last_cleanup = 0.0

    def start(self):
        # Anything left mid-flight by a crash goes back in the queue. An action that was
        # being sent may run twice. A duplicate note is preferred over a lost one.
        self.db.run("UPDATE recordings SET status='queued' WHERE status='transcribing'")
        self.db.run("UPDATE deliveries SET status='pending' WHERE status='sending'")
        self._wake = asyncio.Event()
        self._sem = asyncio.Semaphore(3)
        self._busy.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def wake(self):
        if self._wake is not None:
            self._wake.set()

    async def _loop(self):
        while True:
            try:
                self._dispatch()
                if now() - self._last_cleanup > 3600:
                    self._last_cleanup = now()
                    self.cleanup_audio()
            except Exception:
                log.exception("worker tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.TICK)
            except asyncio.TimeoutError:
                pass

    def _dispatch(self):
        t = now()
        for r in self.db.q("SELECT id FROM recordings WHERE status='queued' AND next_try<=? ORDER BY id", t):
            self._spawn("rec", r["id"], self.process_recording)
        for d in self.db.q("SELECT id FROM deliveries WHERE status='pending' AND next_try<=? ORDER BY id", t):
            self._spawn("del", d["id"], self.deliver)

    def _spawn(self, kind: str, id_: int, fn):
        key = (kind, id_)
        if key in self._busy:
            return
        self._busy.add(key)

        async def run():
            try:
                async with self._sem:
                    await fn(id_)
            except Exception:
                log.exception("%s %s failed", kind, id_)
            finally:
                self._busy.discard(key)
                self.wake()

        asyncio.create_task(run())

    # transcription ---------------------------------------------------------
    async def process_recording(self, rec_id: int):
        rec = self.db.one("SELECT * FROM recordings WHERE id=?", rec_id)
        if rec is None or rec["status"] != "queued":
            return
        self.db.run("UPDATE recordings SET status='transcribing', attempts=attempts+1 WHERE id=?", rec_id)
        attempts = rec["attempts"] + 1
        s = Settings(self.db, rec["user_id"])
        path = Path(rec["audio_path"]) if rec["audio_path"] else None

        if path and path.exists():
            try:
                res = await stt.transcribe(s, path, rec["audio_mime"] or "audio/mp4",
                                           rec["ring_transcript"], transport=self.transport)
            except Exception as e:  # a bug here must not lose the recording
                log.exception("transcription crashed for recording %s", rec_id)
                res = stt.Result(retry=True, notes=[f"internal error: {type(e).__name__}: {e}"])
        else:
            # The ring was set to send text only, or sent no audio.
            ring = rec["ring_transcript"].strip()
            _, fixes = stt.parse_vocabulary(s.get("vocabulary"))
            res = stt.Result(text=stt.apply_fixes(ring, fixes), source="ring" if ring else "",
                             notes=["no audio was received; used the ring's transcript"] if ring
                             else ["the request had neither audio nor a transcript"])

        final_attempt = attempts > len(STT_BACKOFF)
        # Retry while a transient engine failure may still clear up. On the last attempt
        # the result (the ring's transcript, or nothing) is accepted and the rules run.
        if res.retry and not final_attempt and res.source in ("", "ring"):
            delay = STT_BACKOFF[attempts - 1]
            self.db.run("UPDATE recordings SET status='queued', next_try=?, detail=?, error=? WHERE id=?",
                        now() + delay, json.dumps(res.detail()),
                        f"transcription failed; attempt {attempts + 1} in {delay}s", rec_id)
            return

        if res.source == "silence":
            status, error = "empty", ""
        elif not res.text:
            status, error, res.needs_review = "failed", "no transcript could be produced", True
        else:
            status, error = "done", ""
        self.db.run(
            "UPDATE recordings SET status=?, transcript=?, transcript_source=?, needs_review=?, detail=?, error=? WHERE id=?",
            status, res.text, res.source, int(res.needs_review), json.dumps(res.detail()), error, rec_id)
        if rec["run_rules"] and status != "empty":
            self.apply_rules(rec_id)

    def apply_rules(self, rec_id: int) -> int:
        """Create deliveries for the rules that match. Returns how many were created."""
        rec = self.db.one("SELECT * FROM recordings WHERE id=?", rec_id)
        matches = rules.evaluate(rules.user_rules(self.db, rec["user_id"]), rec["transcript"])
        for m in matches:
            self.db.run(
                "INSERT INTO deliveries(recording_id,rule_name,action_type,action_config,text,updated_at)"
                " VALUES(?,?,?,?,?,?)", rec_id, m.rule["name"], m.rule["action_type"],
                m.rule["action_config"], m.text, now())
        self.wake()
        return len(matches)

    # delivery --------------------------------------------------------------
    async def deliver(self, delivery_id: int):
        d = self.db.one("SELECT * FROM deliveries WHERE id=?", delivery_id)
        if d is None or d["status"] != "pending":
            return
        rec = self.db.one("SELECT * FROM recordings WHERE id=?", d["recording_id"])
        user = self.db.one("SELECT * FROM users WHERE id=?", rec["user_id"])
        self.db.run("UPDATE deliveries SET status='sending', attempts=attempts+1, updated_at=? WHERE id=?",
                    now(), delivery_id)
        attempts = d["attempts"] + 1
        s = Settings(self.db, user["id"])
        cfg = loads(d["action_config"], {})
        ctx = actions.context(d["text"], d["rule_name"], user["username"], rec["recorded_at"])
        note = ""
        if rec["needs_review"]:
            detail = loads(rec["detail"], {})
            note = "This transcript may contain errors. " + " ".join(
                n[0].upper() + n[1:] + "." for n in detail.get("notes", []))
        audio = Path(rec["audio_path"]) if rec["audio_path"] else None
        try:
            if d["action_type"] == "email":
                msg = await actions.send_email(s, cfg, ctx, user["email"], audio, rec["audio_mime"], note)
            elif d["action_type"] == "ntfy":
                msg = await actions.send_ntfy(s, cfg, ctx, self.transport)
            elif d["action_type"] == "webhook":
                msg = await actions.send_webhook(
                    cfg, ctx, {"recording_id": rec["id"], "recorded_at": rec["recorded_at"],
                               "needs_review": bool(rec["needs_review"]),
                               "full_text": rec["transcript"]}, self.transport)
            else:
                raise actions.ActionError(f"unknown action {d['action_type']}", False)
        except actions.ActionError as e:
            if e.retry and attempts <= len(DELIVERY_BACKOFF):
                delay = DELIVERY_BACKOFF[attempts - 1]
                self.db.run("UPDATE deliveries SET status='pending', next_try=?, detail=?, updated_at=? WHERE id=?",
                            now() + delay, f"{e} (retry in {delay // 60} min)", now(), delivery_id)
            else:
                self.db.run("UPDATE deliveries SET status='failed', detail=?, updated_at=? WHERE id=?",
                            str(e), now(), delivery_id)
            return
        self.db.run("UPDATE deliveries SET status='ok', detail=?, updated_at=? WHERE id=?", msg, now(), delivery_id)

    # retention ---------------------------------------------------------------
    def cleanup_audio(self):
        for u in self.db.q("SELECT id FROM users"):
            days = Settings(self.db, u["id"]).int("keep_audio_days")
            if days <= 0:
                continue
            # Audio is kept while an action that may attach it is still waiting.
            old = self.db.q(
                "SELECT id, audio_path FROM recordings r WHERE user_id=? AND audio_path IS NOT NULL "
                "AND received_at<? AND status NOT IN ('queued','transcribing') AND NOT EXISTS "
                "(SELECT 1 FROM deliveries d WHERE d.recording_id=r.id AND d.status IN ('pending','sending'))",
                u["id"], now() - days * 86400)
            for r in old:
                Path(r["audio_path"]).unlink(missing_ok=True)
                self.db.run("UPDATE recordings SET audio_path=NULL WHERE id=?", r["id"])
