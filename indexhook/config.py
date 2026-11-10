"""Setting definitions.

Every setting is edited on a web page. A "user" setting can be set per user.
When a user leaves it blank, the server default (set by an admin) applies,
then the built-in default. A "global" setting exists only at server level.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .db import DB

ENGINES = [
    ("openai", "OpenAI-compatible API (OpenAI, Groq, a local Whisper server)"),
    ("elevenlabs", "ElevenLabs Scribe"),
    ("deepgram", "Deepgram Nova"),
]


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"  # text, secret, bool, select, textarea, int
    default: str = ""
    help: str = ""
    choices: list[tuple[str, str]] = field(default_factory=list)
    scope: str = "user"  # user or global
    section: str = ""


def _f(section, scope, *fields):
    for f in fields:
        f.section, f.scope = section, scope
    return list(fields)


FIELDS: list[Field] = [
    *_f(
        "Speech to text", "user",
        Field("stt_engine", "Main engine", "select", "openai", choices=ENGINES,
              help="The engine whose transcript is used."),
        Field("stt_second_engine", "Cross-check engine", "select", "none",
              choices=[("none", "None")] + ENGINES,
              help="Runs on the same audio. A disagreement with the main engine marks the "
                   "recording for review, and the correction pass (below) can settle it."),
        Field("stt_language", "Language", "text", "en",
              help="ISO 639-1 code such as en. A fixed language avoids wrong language "
                   "detection on short clips. Leave blank to detect."),
        Field("vocabulary", "Vocabulary and fixes", "textarea", "",
              help="One entry per line. A plain entry (a name, a product, a place) is given to "
                   "the engine as a hint. An entry written as heard => meant is replaced in the "
                   "final text, for example: no to self => note to self"),
        Field("stt_context", "Context", "textarea", "",
              help="One or two sentences about what you usually record. Sent as a prompt to "
                   "engines that accept one."),
        Field("audio_cleanup", "Clean up audio before transcription", "bool", "1",
              help="Converts to 16 kHz mono FLAC, removes low rumble and evens out the volume. "
                   "Needs ffmpeg on the server. Compare on the Test page before turning it off."),
        Field("silence_db", "Silence threshold (dB)", "int", "-50",
              help="A recording whose loudest point is below this level is treated as empty "
                   "and is not sent to an engine. Engines can invent text for silent audio."),
        Field("use_ring_transcript", "Keep the ring's own transcript as a fallback", "bool", "1",
              help="Used when every engine fails. It is also shown next to the result."),
    ),
    *_f(
        "OpenAI-compatible engine", "user",
        Field("openai_base_url", "Base URL", "text", "https://api.openai.com/v1",
              help="Groq: https://api.groq.com/openai/v1. A local server such as speaches or "
                   "whisper.cpp: its /v1 address."),
        Field("openai_api_key", "API key", "secret"),
        Field("openai_model", "Model", "text", "gpt-4o-transcribe",
              help="Groq and local Whisper servers: whisper-large-v3."),
    ),
    *_f(
        "ElevenLabs engine", "user",
        Field("elevenlabs_api_key", "API key", "secret"),
        Field("elevenlabs_model", "Model", "text", "scribe_v2"),
    ),
    *_f(
        "Deepgram engine", "user",
        Field("deepgram_api_key", "API key", "secret"),
        Field("deepgram_model", "Model", "text", "nova-3"),
    ),
    *_f(
        "Correction pass", "user",
        Field("llm_enabled", "Correct the transcript with a language model", "bool", "0",
              help="The model sees every transcript of the recording and your vocabulary. It may "
                   "only fix misheard words. A result that drifts too far from the main engine's "
                   "transcript is thrown away."),
        Field("llm_base_url", "Chat API base URL", "text", "https://api.openai.com/v1",
              help="Any OpenAI-compatible chat API, for example https://api.minimax.io/v1."),
        Field("llm_api_key", "API key", "secret"),
        Field("llm_model", "Model", "text", "gpt-4o-mini"),
    ),
    *_f(
        "Email", "user",
        Field("note_to", "Send notes to", "text", "",
              help="Address for email actions. Blank uses your account email."),
        Field("mail_from", "From address", "text", ""),
        Field("smtp_host", "SMTP server", "text", "",
              help="On YunoHost: localhost, port 25, no encryption."),
        Field("smtp_port", "SMTP port", "int", "587"),
        Field("smtp_security", "Encryption", "select", "starttls",
              choices=[("starttls", "STARTTLS"), ("ssl", "SSL/TLS"), ("none", "None")]),
        Field("smtp_user", "SMTP user", "text", ""),
        Field("smtp_password", "SMTP password", "secret"),
    ),
    *_f(
        "ntfy", "user",
        Field("ntfy_url", "ntfy server", "text", "https://ntfy.sh"),
        Field("ntfy_topic", "Default topic", "text", ""),
        Field("ntfy_token", "Access token", "secret"),
    ),
    *_f(
        "Storage", "user",
        Field("keep_audio_days", "Keep audio for (days)", "int", "30",
              help="0 keeps audio until you delete the recording. Transcripts are kept either way."),
    ),
    *_f(
        "Accounts", "global",
        Field("registration_open", "Let anyone register", "bool", "0",
              help="The first account can always be created and becomes the admin."),
        Field("session_days", "Stay logged in for (days)", "int", "30"),
    ),
    *_f(
        "Reverse proxy login", "global",
        Field("trusted_header", "User name header", "text", "",
              help="When set, a request that carries this header from a trusted proxy is logged "
                   "in as that user without a password. YunoHost: Ynh-User. Leave blank to turn off."),
        Field("trusted_header_email", "Email header", "text", "",
              help="Optional. YunoHost: Ynh-User-Email."),
        Field("trusted_proxies", "Trusted proxy addresses", "text", "127.0.0.1, ::1",
              help="The header is ignored when the connection comes from any other address."),
        Field("header_auto_create", "Create an account on first visit", "bool", "1"),
    ),
    *_f(
        "Server", "global",
        Field("public_base_url", "Public address", "text", "",
              help="For example https://example.org/indexhook. Used to show the webhook URL. "
                   "Blank uses the address of the current page."),
        Field("max_upload_mb", "Largest accepted recording (MB)", "int", "25"),
    ),
]

BY_KEY = {f.key: f for f in FIELDS}
USER_FIELDS = [f for f in FIELDS if f.scope == "user"]
GLOBAL_FIELDS = [f for f in FIELDS if f.scope == "global"]


# secret -> settings that say where the secret is sent
SECRET_GUARDS = {
    "openai_api_key": ["openai_base_url"],
    "llm_api_key": ["llm_base_url"],
    "ntfy_token": ["ntfy_url"],
    "smtp_password": ["smtp_host", "smtp_user"],
}


class Settings:
    """Resolved settings for one user (or the server when user_id is None)."""

    def __init__(self, db: DB, user_id: int | None = None):
        self.user = db.scope_settings(f"user:{user_id}") if user_id else {}
        self.glob = db.scope_settings("global")

    def get(self, key: str) -> str:
        f = BY_KEY[key]
        v = self.user.get(key)
        if v:
            return v
        # A server-level secret is not handed to an endpoint the user chose. Otherwise a
        # user could point the base URL at their own machine and read the server's key.
        if key in SECRET_GUARDS and any(self.user.get(k) for k in SECRET_GUARDS[key]):
            return f.default
        return self.glob.get(key) or f.default

    def bool(self, key: str) -> bool:
        return self.get(key) == "1"

    def int(self, key: str) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return int(BY_KEY[key].default or 0)

    def source(self, key: str) -> str:
        if self.user.get(key):
            return "user"
        if self.glob.get(key):
            return "server"
        return "default"


def save_form(db: DB, scope: str, fields: list[Field], form) -> None:
    """Store submitted values. Blank means "inherit". A blank secret keeps the stored one."""
    for f in fields:
        if f.kind == "secret":
            if form.get(f"clear_{f.key}"):
                db.set_setting(scope, f.key, None)
            elif form.get(f.key):
                db.set_setting(scope, f.key, form.get(f.key).strip())
            continue
        if f.kind == "bool":
            v = form.get(f.key, "")
            v = v if v in ("0", "1") else ""
        else:
            v = (form.get(f.key) or "").strip()
            if f.kind == "int" and v:
                try:
                    v = str(int(v))
                except ValueError:
                    continue
            if f.kind == "select" and v not in [c[0] for c in f.choices]:
                v = ""
            if f.kind == "textarea":
                v = v.replace("\r\n", "\n")
        db.set_setting(scope, f.key, v if v != "" else None)
