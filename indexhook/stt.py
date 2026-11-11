"""Speech to text.

Pipeline for one recording:

1. Measure the audio. Silent recordings stop here, because engines invent text for silence.
2. Clean the audio (optional): 16 kHz mono FLAC, rumble filter, loudness normalisation.
3. Run the main engine and the optional cross-check engine at the same time, each with the
   user's language, vocabulary and context.
4. Compare the transcripts. When a correction model is configured, it gets every
   transcript plus the vocabulary and returns one text. Its answer is rejected when it
   drifts from the main engine's transcript.
5. Apply the user's "heard => meant" fixes.
6. Fall back to the ring's own transcript when every engine failed.

The result records every candidate so the web page can show them side by side.
"""
from __future__ import annotations

import asyncio
import difflib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from . import audio
from .config import CHAT_NAMES, ENGINES, Settings

TIMEOUT = httpx.Timeout(120, connect=15)
AGREE_THRESHOLD = 0.85   # below this, two engines are said to disagree
DRIFT_THRESHOLD = 0.6    # a corrected text less similar than this to the main one is rejected


class EngineError(Exception):
    """An engine failed. `retry` says whether trying again later can help."""

    def __init__(self, msg: str, retry: bool = True):
        super().__init__(msg)
        self.retry = retry


@dataclass
class Candidate:
    engine: str
    model: str = ""
    text: str = ""
    confidence: float | None = None
    error: str = ""
    retry: bool = False


@dataclass
class Result:
    text: str = ""
    source: str = ""            # engine name, "corrected", "ring", "silence" or ""
    needs_review: bool = False
    retry: bool = False         # nothing usable yet and a later attempt may work
    candidates: list[Candidate] = field(default_factory=list)
    agreement: float | None = None
    notes: list[str] = field(default_factory=list)
    duration: float | None = None
    max_db: float | None = None
    cleaned: bool = False

    def detail(self) -> dict:
        d = asdict(self)
        d.pop("text")
        return d


# vocabulary -----------------------------------------------------------
def parse_vocabulary(raw: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Split the vocabulary box into hint terms and (heard, meant) replacements."""
    terms: list[str] = []
    fixes: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" in line:
            heard, meant = (p.strip() for p in line.split("=>", 1))
            if heard and meant:
                fixes.append((heard, meant))
                if meant not in terms:
                    terms.append(meant)
        elif line not in terms:
            terms.append(line)
    return terms, fixes


def apply_fixes(text: str, fixes: list[tuple[str, str]]) -> str:
    for heard, meant in fixes:
        words = [re.escape(w) for w in re.findall(r"\w+", heard)]
        if not words:
            continue
        pat = r"(?<!\w)" + r"[\W_]+".join(words) + r"(?!\w)"
        text = re.sub(pat, lambda m: meant, text, flags=re.IGNORECASE)
    return text


def normalise(text: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:'[^\W_]+)?", text.lower())


def similarity(a: str, b: str) -> float:
    """Word-level similarity from 0 to 1, ignoring case and punctuation."""
    wa, wb = normalise(a), normalise(b)
    if not wa and not wb:
        return 1.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def build_prompt(context: str, terms: list[str]) -> str:
    parts = [context.strip()] if context.strip() else []
    if terms:
        parts.append("Names and terms that may appear: " + ", ".join(terms) + ".")
    return " ".join(parts)


# engines --------------------------------------------------------------
HINTS = {401: "the key was not accepted", 403: "the key is not allowed to do this",
         402: "the account has no credit", 404: "wrong URL or unknown model",
         429: "rate limit or quota reached"}


def _error_text(r: httpx.Response) -> str:
    """Pull the human-readable message out of an error body, whatever its shape."""
    try:
        j = r.json()
    except ValueError:
        return r.text[:200].replace("\n", " ").strip()
    for path in (("error", "message"), ("error",), ("detail", "message"), ("detail",), ("err_msg",), ("message",)):
        v = j
        for k in path:
            v = v.get(k) if isinstance(v, dict) else None
        if isinstance(v, str) and v:
            return v[:200]
    return str(j)[:200]


def _check(r: httpx.Response, engine: str):
    if r.status_code < 300:
        return
    # 4xx other than timeouts and rate limits means the request itself is wrong
    # (bad key, bad model). Retrying will not help.
    retry = r.status_code in (408, 409, 425, 429) or r.status_code >= 500
    hint = HINTS.get(r.status_code)
    raise EngineError(f"{engine} answered HTTP {r.status_code}" + (f" ({hint})" if hint else "")
                      + f": {_error_text(r)}", retry)


async def _openai_style(client, s: Settings, path: Path, mime: str, terms: list[str],
                        name: str, base: str, key: str, model: str) -> Candidate:
    """Any service that implements OpenAI's /audio/transcriptions."""
    data = {"model": model, "response_format": "json", "temperature": "0"}
    if s.get("stt_language"):
        data["language"] = s.get("stt_language")
    prompt = build_prompt(s.get("stt_context"), terms)
    if prompt:
        data["prompt"] = prompt
    r = await client.post(
        base.rstrip("/") + "/audio/transcriptions", data=data,
        files={"file": (path.name, path.read_bytes(), mime)},
        headers={"Authorization": f"Bearer {key}"} if key else {})
    _check(r, name)
    return Candidate(name, model, (r.json().get("text") or "").strip())


async def _openai(client, s: Settings, path: Path, mime: str, terms: list[str]) -> Candidate:
    if not s.get("openai_api_key"):
        raise EngineError("no API key is set for OpenAI", False)
    return await _openai_style(client, s, path, mime, terms, "openai", s.get("openai_base_url"),
                               s.get("openai_api_key"), s.get("openai_model"))


async def _groq(client, s: Settings, path: Path, mime: str, terms: list[str]) -> Candidate:
    if not s.get("groq_api_key"):
        raise EngineError("no API key is set for Groq", False)
    return await _openai_style(client, s, path, mime, terms, "groq", "https://api.groq.com/openai/v1",
                               s.get("groq_api_key"), s.get("groq_model"))


async def _local(client, s: Settings, path: Path, mime: str, terms: list[str]) -> Candidate:
    if not s.get("local_base_url"):
        raise EngineError("no URL is set for your own speech server", False)
    return await _openai_style(client, s, path, mime, terms, "local", s.get("local_base_url"),
                               s.get("local_api_key"), s.get("local_model"))


# Language tags MiniMax ASR documents for its "language" header.
MINIMAX_LANGUAGES = {"zh", "yue", "en", "ja", "ko", "th", "vi", "id", "ms", "fil", "ar", "tr",
                     "fr", "de", "es", "it", "pt", "pl", "ru", "uk"}


async def _minimax(client, s: Settings, path: Path, mime: str, terms: list[str]) -> Candidate:
    """MiniMax ASR. The API has no vocabulary or prompt parameter, so `terms` is not sent."""
    model = s.get("minimax_model")
    key = s.get("minimax_api_key")
    if not key:
        raise EngineError("no API key is set for MiniMax", False)
    headers = {"Authorization": f"Bearer {key}"}
    # The language goes in a request header, not a form field. An unlisted tag is left
    # out, which makes the service detect the language.
    lang = s.get("stt_language").lower()
    if lang in MINIMAX_LANGUAGES:
        headers["language"] = lang
    r = await client.post(
        s.get("minimax_base_url").rstrip("/") + "/speech_to_text",
        data={"model": model, "response_format": "json", "stream": "false"},
        files={"file": (path.name, path.read_bytes(), mime)}, headers=headers)
    _check(r, "minimax")
    return Candidate("minimax", model, (r.json().get("text") or "").strip())


# Gemini wants language codes with a region. Short codes are mapped for the common cases;
# a code that is not listed here and has no region is left out, so the model detects it.
GOOGLE_REGIONS = {"en": "en-US", "es": "es-ES", "fr": "fr-FR", "de": "de-DE", "it": "it-IT",
                  "pt": "pt-BR", "nl": "nl-NL", "sv": "sv-SE", "da": "da-DK", "nb": "nb-NO",
                  "fi": "fi-FI", "pl": "pl-PL", "cs": "cs-CZ", "ru": "ru-RU", "uk": "uk-UA",
                  "tr": "tr-TR", "ja": "ja-JP", "ko": "ko-KR", "zh": "cmn-Hans-CN", "hi": "hi-IN",
                  "ar": "ar-XA", "he": "he-IL", "id": "id-ID", "vi": "vi-VN", "th": "th-TH",
                  "el": "el-GR", "hu": "hu-HU", "ro": "ro-RO"}
GOOGLE_MIME = {"audio/mp4": "audio/m4a", "audio/x-m4a": "audio/m4a", "video/webm": "audio/webm",
               "audio/x-wav": "audio/wav"}


async def _google(client, s: Settings, path: Path, mime: str, terms: list[str]) -> Candidate:
    """Gemini 3.5 Transcribe: upload through the Files API, then one Interactions request."""
    model = s.get("google_model")
    key = s.get("google_api_key")
    if not key:
        raise EngineError("no API key is set for Google", False)
    base = s.get("google_base_url").rstrip("/")
    auth = {"x-goog-api-key": key}
    mime = GOOGLE_MIME.get(mime, mime)
    data = path.read_bytes()

    # 1. Resumable upload: a start request returns the URL the bytes go to.
    r = await client.post(
        base + "/upload/v1beta/files", json={"file": {"display_name": path.name}},
        headers={**auth, "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                 "X-Goog-Upload-Header-Content-Length": str(len(data)),
                 "X-Goog-Upload-Header-Content-Type": mime})
    _check(r, "google")
    upload_url = r.headers.get("x-goog-upload-url")
    if not upload_url:
        raise EngineError("google did not return an upload address")
    r = await client.post(upload_url, content=data,
                          headers={"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"})
    _check(r, "google")
    try:
        file = r.json()["file"]
        uri = file["uri"]
    except (KeyError, TypeError, ValueError):
        raise EngineError("google returned no file address after the upload")

    try:
        # 2. Transcribe.
        tc: dict = {}
        lang = s.get("stt_language")
        code = lang if "-" in lang else GOOGLE_REGIONS.get(lang.lower())
        if code:
            tc["language_codes"] = [code]
        if terms:
            tc["custom_vocabulary"] = terms[:1000]
        if s.get("google_mode") == "smart":
            tc["mode"] = "smart"
        body: dict = {"model": model, "store": False,
                      "input": [{"type": "audio", "uri": uri, "mime_type": mime}]}
        if tc:
            body["generation_config"] = {"transcription_config": tc}
        r = await client.post(base + "/v1beta/interactions", json=body, headers=auth)
        _check(r, "google")
        j = r.json()
        parts = [c.get("text", "") for step in j.get("steps") or [] if step.get("type") == "model_output"
                 for c in step.get("content") or [] if c.get("type") == "text"]
        if not parts and isinstance(j.get("output_text"), str):
            parts = [j["output_text"]]
        if j.get("status") not in (None, "completed"):
            raise EngineError(f"google finished with status {j.get('status')}")
        return Candidate("google", model, " ".join(p.strip() for p in parts if p).strip())
    finally:
        # 3. Remove the uploaded copy. Google deletes it after 48 hours anyway.
        try:
            if file.get("name"):
                await client.delete(f"{base}/v1beta/{file['name']}", headers=auth)
        except httpx.HTTPError:
            pass


async def _elevenlabs(client, s: Settings, path: Path, mime: str, terms: list[str]) -> Candidate:
    model = s.get("elevenlabs_model")
    key = s.get("elevenlabs_api_key")
    if not key:
        raise EngineError("no API key is set for ElevenLabs", False)
    data: dict = {"model_id": model, "tag_audio_events": "false"}
    if s.get("stt_language"):
        data["language_code"] = s.get("stt_language")
    if model.startswith("scribe_v2"):
        # Documented limits: 100 keyterms, each under 50 characters and at most 5 words.
        ok = [t for t in terms if len(t) < 50 and len(t.split()) <= 5][:100]
        if ok:
            data["keyterms"] = ok   # sent as one repeated form field
    files = {"file": (path.name, path.read_bytes(), mime)}
    r = await client.post("https://api.elevenlabs.io/v1/speech-to-text",
                          data=data, files=files, headers={"xi-api-key": key})
    _check(r, "elevenlabs")
    j = r.json()
    return Candidate("elevenlabs", model, (j.get("text") or "").strip(), j.get("language_probability"))


async def _deepgram(client, s: Settings, path: Path, mime: str, terms: list[str]) -> Candidate:
    model = s.get("deepgram_model")
    key = s.get("deepgram_api_key")
    if not key:
        raise EngineError("no API key is set for Deepgram", False)
    params: list[tuple[str, str]] = [("model", model), ("smart_format", "true")]
    if s.get("stt_language"):
        params.append(("language", s.get("stt_language")))
    if model.startswith("nova-3"):
        params += [("keyterm", t) for t in terms[:100]]
    r = await client.post("https://api.deepgram.com/v1/listen", params=params,
                          content=path.read_bytes(),
                          headers={"Authorization": f"Token {key}", "Content-Type": mime})
    _check(r, "deepgram")
    try:
        alt = r.json()["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError, TypeError):
        raise EngineError("deepgram returned no transcript", True)
    return Candidate("deepgram", model, (alt.get("transcript") or "").strip(), alt.get("confidence"))


ENGINE_FUNCS = {"minimax": _minimax, "google": _google, "openai": _openai, "elevenlabs": _elevenlabs,
                "deepgram": _deepgram, "groq": _groq, "local": _local}


def engine_ready(s: Settings, engine: str) -> bool:
    """True when the engine has what it needs to be called (a key, or a URL for "local")."""
    if engine == "local":
        return bool(s.get("local_base_url"))
    return engine in ENGINE_FUNCS and bool(s.get(f"{engine}_api_key"))


def resolve_engine(s: Settings) -> str | None:
    """The engine to use. "auto" picks the first one that is ready, in the order of ENGINES."""
    chosen = s.get("stt_engine")
    if chosen != "auto":
        return chosen if chosen in ENGINE_FUNCS else None
    return next((e for e, _ in ENGINES if engine_ready(s, e)), None)


async def _run_engine(client, engine: str, s: Settings, path: Path, mime: str, terms) -> Candidate:
    try:
        return await ENGINE_FUNCS[engine](client, s, path, mime, terms)
    except EngineError as e:
        return Candidate(engine, error=str(e), retry=e.retry)
    except (httpx.HTTPError, ValueError, OSError) as e:
        return Candidate(engine, error=f"{engine}: {type(e).__name__}: {e}", retry=True)


# correction pass --------------------------------------------------------
CORRECTION_SYSTEM = """You fix speech recognition errors in a short voice note.
You get one or more machine transcripts of the SAME recording and a list of names and terms the speaker uses.
Return the single most likely wording of what was said.
Rules:
- Start from the transcript marked MAIN. Change a word only when another transcript or the term list shows it was misheard.
- Do not rephrase, summarise, translate, answer, or add anything. Keep the speaker's wording, including filler they clearly said.
- Fix spelling of names and terms to match the list.
- Use normal punctuation and capitalisation.
- Output only the corrected transcript, with no quotes and no commentary."""


def chat_ready(s: Settings, provider: str) -> bool:
    if provider == "custom":
        return bool(s.get("llm_base_url") and s.get("llm_model"))
    return provider in CHAT_NAMES and bool(s.get(f"{provider}_api_key"))


async def chat(client, s: Settings, provider: str, system: str, user: str) -> str:
    """One question, one answer, from the chosen text model. Raises EngineError."""
    if provider == "claude":
        # Anthropic Messages API.
        r = await client.post(
            s.get("claude_base_url").rstrip("/") + "/v1/messages",
            json={"model": s.get("claude_model"), "max_tokens": 1024, "system": system,
                  "messages": [{"role": "user", "content": user}]},
            headers={"x-api-key": s.get("claude_api_key"), "anthropic-version": "2023-06-01"})
        _check(r, "claude")
        try:
            out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        except (KeyError, TypeError, AttributeError):
            raise EngineError("claude returned an unexpected answer")
    else:
        # OpenAI-style chat completions. No temperature is sent: some models accept
        # only their default, and MiniMax rejects 0.
        base, key, model = {
            "minimax": ("minimax_base_url", "minimax_api_key", "minimax_chat_model"),
            "openai": ("openai_base_url", "openai_api_key", "openai_chat_model"),
            "custom": ("llm_base_url", "llm_api_key", "llm_model"),
        }[provider]
        r = await client.post(
            s.get(base).rstrip("/") + "/chat/completions",
            json={"model": s.get(model), "messages": [{"role": "system", "content": system},
                                                      {"role": "user", "content": user}]},
            headers={"Authorization": f"Bearer {s.get(key)}"} if s.get(key) else {})
        _check(r, provider)
        try:
            out = r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise EngineError(f"{provider} returned an unexpected answer")
    # Reasoning models may wrap their thinking in <think> tags.
    return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()


async def correct(client, s: Settings, provider: str, main: Candidate, others: list[Candidate],
                  terms: list[str]) -> tuple[str | None, str]:
    """Ask a text model to reconcile the transcripts. Returns (text or None, note)."""
    lines = [f"MAIN ({main.engine}): {main.text}"]
    lines += [f"OTHER ({c.engine}): {c.text}" for c in others if c.text]
    lines.append("Terms: " + (", ".join(terms) if terms else "(none)"))
    try:
        out = await chat(client, s, provider, CORRECTION_SYSTEM, "\n".join(lines))
    except EngineError as e:
        return None, f"correction pass failed: {e}"
    except (httpx.HTTPError, ValueError) as e:
        return None, f"correction pass failed: {type(e).__name__}"
    out = out.strip('"').strip()
    if not out:
        return None, "correction model returned nothing"
    sim = similarity(out, main.text)
    if sim < DRIFT_THRESHOLD:
        return None, f"correction rejected: only {sim:.0%} similar to the main transcript"
    return out, ""


# connection checks ------------------------------------------------------
def tiny_wav() -> bytes:
    """One second of quiet 440 Hz tone, 16 kHz mono. Enough to prove a key and model work."""
    import io
    import math
    import struct
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 16000)))
                               for i in range(16000)))
    return buf.getvalue()


async def check_engine(s: Settings, engine: str, transport=None) -> tuple[bool, str]:
    """Send a one-second test clip to a speech engine. Returns (ok, message)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "test.wav"
        path.write_bytes(tiny_wav())
        async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10), transport=transport) as client:
            c = await _run_engine(client, engine, s, path, "audio/wav", [])
    return (False, c.error) if c.error else (True, f"answered (model {c.model})")


async def check_chat(s: Settings, provider: str, transport=None) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10), transport=transport) as client:
            out = await chat(client, s, provider, "You are a connection test.", "Reply with the single word OK.")
    except EngineError as e:
        return False, str(e)
    except (httpx.HTTPError, ValueError) as e:
        return False, f"{provider}: {type(e).__name__}: {e}"
    return (True, "answered") if out else (False, f"{provider} returned an empty answer")


# pipeline ---------------------------------------------------------------
async def transcribe(s: Settings, src: Path, mime: str, ring_text: str = "",
                     transport: httpx.AsyncBaseTransport | None = None) -> Result:
    res = Result()
    terms, fixes = parse_vocabulary(s.get("vocabulary"))
    ring_text = (ring_text or "").strip()

    prep = await audio.prepare(src, mime, s.bool("audio_cleanup"))
    res.duration, res.max_db, res.cleaned = prep.duration, prep.max_db, prep.cleaned
    if prep.note:
        res.notes.append(prep.note)

    try:
        if prep.max_db is not None and prep.max_db < s.int("silence_db"):
            res.source = "silence"
            res.notes.append(f"peak level {prep.max_db:.0f} dB is below the silence threshold; no engine was called")
            return res

        first = resolve_engine(s)
        engines = [first] if first else []
        second = s.get("stt_second_engine")
        if second in ENGINE_FUNCS and second not in engines:
            engines.append(second)
        if not first:
            res.notes.append("no speech service has a key yet (Settings, AI services)")

        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
            res.candidates = list(await asyncio.gather(
                *[_run_engine(client, e, s, prep.path, prep.mime, terms) for e in engines]))
            good = [c for c in res.candidates if c.text and not c.error]

            if ring_text:
                res.candidates.append(Candidate("ring", "Index app", ring_text))

            if not good:
                if engines and all(c.error == "" for c in res.candidates if c.engine != "ring"):
                    # Engines answered but heard no words.
                    res.notes.append("the engines returned an empty transcript")
                res.retry = any(c.retry for c in res.candidates)
                if ring_text and s.bool("use_ring_transcript"):
                    res.text, res.source, res.needs_review = apply_fixes(ring_text, fixes), "ring", True
                    res.notes.append("used the ring's transcript because no engine produced one")
                return res

            main = good[0]
            res.text, res.source = main.text, main.engine
            if main.engine != first:
                res.needs_review = True
                res.notes.append(f"the main engine failed; used {main.engine}")

            if len(good) > 1:
                res.agreement = similarity(good[0].text, good[1].text)
                if res.agreement < AGREE_THRESHOLD:
                    res.needs_review = True
                    res.notes.append(f"the engines agree on only {res.agreement:.0%} of the words")

            provider = s.get("llm_provider")
            if provider != "none" and chat_ready(s, provider):
                others = [c for c in res.candidates if c is not main and c.text]
                # Nothing to reconcile when there is a single transcript and no vocabulary.
                if others or terms:
                    fixed, note = await correct(client, s, provider, main, others, terms)
                    if fixed is not None:
                        if fixed != main.text:
                            res.source = "corrected"
                        res.text = fixed
                        if res.agreement is not None and res.agreement < AGREE_THRESHOLD:
                            res.notes.append("the correction model settled the disagreement")
                    elif note:
                        res.notes.append(note)

        res.text = apply_fixes(res.text, fixes)
        return res
    finally:
        if prep.cleaned:
            prep.path.unlink(missing_ok=True)
