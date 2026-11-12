"""Setting definitions.

Every setting is edited on a web page. A "user" setting can be set per user.
When a user leaves it blank, the server default (set by an admin) applies,
then the built-in default. A "global" setting exists only at server level.

Fields marked advanced have working defaults and are folded away on the page.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .db import DB

# Speech engines, in the order "Automatic" tries them.
ENGINES = [
    ("minimax", "MiniMax"),
    ("google", "Google Gemini Transcribe"),
    ("openai", "OpenAI"),
    ("elevenlabs", "ElevenLabs Scribe"),
    ("deepgram", "Deepgram"),
    ("groq", "Groq (Whisper)"),
    ("local", "Own server (OpenAI-compatible, see Advanced)"),
]
ENGINE_NAMES = dict(ENGINES)

# Text models for the correction pass. Claude reads text only, so it appears here
# and not in the list of speech engines.
CHAT_PROVIDERS = [
    ("minimax", "MiniMax"),
    ("claude", "Claude"),
    ("openai", "OpenAI"),
    ("custom", "Own server (OpenAI-compatible, see Advanced)"),
]
CHAT_NAMES = dict(CHAT_PROVIDERS)


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"  # text, secret, bool, select, textarea, int
    default: str = ""
    help: str = ""
    choices: list[tuple[str, str]] = field(default_factory=list)
    advanced: bool = False
    scope: str = "user"  # user or global
    section: str = ""
    intro: str = ""


SECTION_INTROS = {
    "AI services": "Paste a key for each service you want to use. One key is enough to start: "
                   "with the engine set to Automatic, the first service that has a key does the "
                   "transcription. \"Save and test\" checks every key you entered.",
    "Speech to text": "The defaults work. Vocabulary is the setting that most improves names.",
}


def _f(section, scope, *fields):
    for f in fields:
        f.section, f.scope = section, scope
    return list(fields)


def _adv(*fields):
    for f in fields:
        f.advanced = True
    return fields


FIELDS: list[Field] = [
    *_f(
        "AI services", "user",
        Field("minimax_api_key", "MiniMax API key", "secret",
              help="Speech recognition, and the correction pass if you choose MiniMax for it. "
                   "From platform.minimax.io: either the Subscription Key under Billing, Token Plan, "
                   "or a pay-as-you-go key under API Keys."),
        Field("google_api_key", "Google Gemini API key", "secret",
              help="Speech recognition with Gemini 3.5 Transcribe. From aistudio.google.com/apikey."),
        Field("openai_api_key", "OpenAI API key", "secret",
              help="Speech recognition and the correction pass. From platform.openai.com/api-keys."),
        Field("claude_api_key", "Claude API key", "secret",
              help="Correction pass only. The Claude API reads text and images, not audio, so "
                   "it cannot transcribe a recording by itself. From platform.claude.com, "
                   "API keys. Tie the key to a workspace when you create it."),
        Field("elevenlabs_api_key", "ElevenLabs API key", "secret",
              help="Speech recognition. From elevenlabs.io/app/developers/api-keys."),
        Field("deepgram_api_key", "Deepgram API key", "secret",
              help="Speech recognition. From console.deepgram.com, Settings, API Keys."),
        Field("groq_api_key", "Groq API key", "secret", help="Speech recognition (hosted Whisper). From console.groq.com/keys."),
        *_adv(
            Field("minimax_base_url", "MiniMax base URL", "text", "https://api.minimax.io/v1",
                  help="Accounts on the China platform use https://api.minimaxi.com/v1."),
            Field("minimax_model", "MiniMax speech model", "text", "asr-1.0"),
            Field("minimax_chat_model", "MiniMax text model", "text", "MiniMax-M3",
                  help="Used for the correction pass."),
            Field("google_model", "Google speech model", "text", "gemini-3.5-transcribe"),
            Field("google_mode", "Google transcript style", "select", "verbatim",
                  choices=[("verbatim", "Word for word"), ("smart", "Cleaned up (smart mode)")],
                  help="Smart mode drops filler words, keeps only the corrected version when you "
                       "correct yourself, and tidies the grammar. Word for word is the safer "
                       "choice for trigger phrases."),
            Field("google_base_url", "Google base URL", "text", "https://generativelanguage.googleapis.com"),
            Field("openai_base_url", "OpenAI base URL", "text", "https://api.openai.com/v1"),
            Field("openai_model", "OpenAI speech model", "text", "gpt-4o-transcribe"),
            Field("openai_chat_model", "OpenAI text model", "text", "gpt-4o-mini",
                  help="Used for the correction pass."),
            Field("claude_base_url", "Claude base URL", "text", "https://api.anthropic.com"),
            Field("claude_model", "Claude model", "text", "claude-haiku-5-5",
                  help="Used for the correction pass."),
            Field("claude_workspace_id", "Claude workspace ID", "text", "",
                  help="Only for a key that is not tied to a workspace. Starts with wrkspc_."),
            Field("elevenlabs_model", "ElevenLabs model", "text", "scribe_v2"),
            Field("deepgram_model", "Deepgram model", "text", "nova-3"),
            Field("groq_model", "Groq model", "text", "whisper-large-v3"),
            Field("local_base_url", "Own speech server URL", "text", "",
                  help="A server that implements /v1/audio/transcriptions, such as speaches or "
                       "whisper.cpp. Example: http://127.0.0.1:8000/v1"),
            Field("local_api_key", "Own speech server key", "secret", help="Leave blank if the server needs none."),
            Field("local_model", "Own speech server model", "text", "whisper-large-v3"),
            Field("llm_base_url", "Own text server URL", "text", "",
                  help="An OpenAI-compatible chat API for the correction pass. Example: http://127.0.0.1:11434/v1"),
            Field("llm_api_key", "Own text server key", "secret"),
            Field("llm_model", "Own text server model", "text", ""),
        ),
    ),
    *_f(
        "Speech to text", "user",
        Field("stt_engine", "Engine", "select", "auto",
              choices=[("auto", "Automatic (first service with a key)")] + ENGINES,
              help="The service that transcribes your recordings."),
        Field("stt_second_engine", "Cross-check engine", "select", "none",
              choices=[("none", "None")] + ENGINES,
              help="A second service that transcribes the same audio. If the two disagree, the "
                   "recording is marked for review."),
        Field("llm_provider", "Correction pass", "select", "none",
              choices=[("none", "Off")] + CHAT_PROVIDERS,
              help="A text model reads every transcript of the recording plus your vocabulary and "
                   "fixes misheard words. A result that drifts too far from the engine's "
                   "transcript is thrown away."),
        Field("stt_language", "Language", "text", "en",
              help="ISO 639-1 code such as en. A fixed language avoids wrong language "
                   "detection on short clips. Leave blank to detect."),
        Field("vocabulary", "Vocabulary and fixes", "textarea", "",
              help="One entry per line. A plain entry (a name, a product, a place) is given to "
                   "the engine as a hint and to the correction pass. MiniMax speech recognition "
                   "takes no hints, so with it the entries reach only the correction pass. An "
                   "entry written as heard => meant is replaced in the final text, for example: "
                   "no to self => note to self"),
        Field("stt_context", "Context", "textarea", "",
              help="One or two sentences about what you usually record. Sent as a prompt to "
                   "engines that accept one."),
        *_adv(
            Field("audio_cleanup", "Clean up audio before transcription", "bool", "1",
                  help="Converts to 16 kHz mono FLAC, removes low rumble and evens out the volume. "
                       "Needs ffmpeg on the server. Compare on the Test page before turning it off."),
            Field("silence_db", "Silence threshold (dB)", "int", "-50",
                  help="A recording whose loudest point is below this level is treated as empty "
                       "and is not sent to an engine. Engines can invent text for silent audio."),
            Field("use_ring_transcript", "Keep the ring's own transcript as a fallback", "bool", "1",
                  help="Used when every engine fails. It is also shown next to the result."),
        ),
    ),
    *_f(
        "Email", "user",
        Field("note_to", "Send notes to", "text", "",
              help="Address for email actions. Blank uses your account email."),
        Field("smtp_host", "SMTP server", "text", "",
              help="On YunoHost: localhost, port 25, no encryption. Gmail: smtp.gmail.com, port 587, "
                   "STARTTLS, your address as the user and an app password (not your normal "
                   "password). Steps are in deploy/yunohost.md."),
        Field("smtp_port", "SMTP port", "int", "587"),
        Field("smtp_security", "Encryption", "select", "starttls",
              choices=[("starttls", "STARTTLS"), ("ssl", "SSL/TLS"), ("none", "None")]),
        Field("smtp_user", "SMTP user", "text", ""),
        Field("smtp_password", "SMTP password", "secret"),
        Field("mail_from", "From address", "text", "", help="Blank uses the SMTP user, then your own address."),
    ),
    *_f(
        "ntfy", "user",
        Field("ntfy_topic", "Topic", "text", "", help="Only needed if a rule sends ntfy notifications."),
        Field("ntfy_token", "Access token", "secret"),
        *_adv(Field("ntfy_url", "ntfy server", "text", "https://ntfy.sh")),
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
    "minimax_api_key": ["minimax_base_url"],
    "google_api_key": ["google_base_url"],
    "claude_api_key": ["claude_base_url"],
    "local_api_key": ["local_base_url"],
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


def save_form(db: DB, scope: str, fields: list[Field], form) -> None:
    """Store submitted values. Blank means "inherit". A blank secret keeps the stored one."""
    for f in fields:
        if f.kind == "secret":
            if form.get(f"clear_{f.key}"):
                db.set_setting(scope, f.key, None)
            elif form.get(f.key):
                db.set_setting(scope, f.key, form.get(f.key).strip())
            continue
        if f.key not in form:
            continue   # a form that shows only some fields leaves the others alone
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
