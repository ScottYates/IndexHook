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
from .config import Settings

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
def _check(r: httpx.Response, engine: str):
    if r.status_code < 300:
        return
    body = r.text[:300].replace("\n", " ")
    # 4xx other than timeouts and rate limits means the request itself is wrong
    # (bad key, bad model). Retrying will not help.
    retry = r.status_code in (408, 409, 425, 429) or r.status_code >= 500
    raise EngineError(f"{engine} answered HTTP {r.status_code}: {body}", retry)


async def _openai(client, s: Settings, path: Path, mime: str, terms: list[str]) -> Candidate:
    model = s.get("openai_model")
    key = s.get("openai_api_key")
    base = s.get("openai_base_url").rstrip("/")
    local = not base.startswith("https://api.")
    if not key and not local:
        raise EngineError("no API key is set for the OpenAI-compatible engine", False)
    data = {"model": model, "response_format": "json", "temperature": "0"}
    if s.get("stt_language"):
        data["language"] = s.get("stt_language")
    prompt = build_prompt(s.get("stt_context"), terms)
    if prompt:
        data["prompt"] = prompt
    r = await client.post(
        base + "/audio/transcriptions", data=data,
        files={"file": (path.name, path.read_bytes(), mime)},
        headers={"Authorization": f"Bearer {key}"} if key else {})
    _check(r, "openai")
    return Candidate("openai", model, (r.json().get("text") or "").strip())


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


ENGINE_FUNCS = {"openai": _openai, "elevenlabs": _elevenlabs, "deepgram": _deepgram}


def engine_ready(s: Settings, engine: str) -> bool:
    if engine == "openai":
        return bool(s.get("openai_api_key")) or not s.get("openai_base_url").startswith("https://api.")
    return engine in ENGINE_FUNCS and bool(s.get(f"{engine}_api_key"))


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


async def correct(client, s: Settings, main: Candidate, others: list[Candidate], terms: list[str]) -> tuple[str | None, str]:
    """Ask a chat model to reconcile the transcripts. Returns (text or None, note)."""
    lines = [f"MAIN ({main.engine}): {main.text}"]
    lines += [f"OTHER ({c.engine}): {c.text}" for c in others if c.text]
    lines.append("Terms: " + (", ".join(terms) if terms else "(none)"))
    body = {
        "model": s.get("llm_model"),
        "temperature": 0,
        "messages": [{"role": "system", "content": CORRECTION_SYSTEM},
                     {"role": "user", "content": "\n".join(lines)}],
    }
    try:
        r = await client.post(s.get("llm_base_url").rstrip("/") + "/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {s.get('llm_api_key')}"})
        if r.status_code >= 300:
            return None, f"correction model answered HTTP {r.status_code}"
        out = r.json()["choices"][0]["message"]["content"] or ""
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as e:
        return None, f"correction model failed: {type(e).__name__}"
    # Reasoning models may wrap their thinking in <think> tags.
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip().strip('"').strip()
    if not out:
        return None, "correction model returned nothing"
    sim = similarity(out, main.text)
    if sim < DRIFT_THRESHOLD:
        return None, f"correction rejected: only {sim:.0%} similar to the main transcript"
    return out, ""


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

        engines = [s.get("stt_engine")]
        second = s.get("stt_second_engine")
        if second in ENGINE_FUNCS and second != engines[0]:
            engines.append(second)

        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
            res.candidates = list(await asyncio.gather(
                *[_run_engine(client, e, s, prep.path, prep.mime, terms) for e in engines]))
            good = [c for c in res.candidates if c.text and not c.error]

            if ring_text:
                res.candidates.append(Candidate("ring", "Index app", ring_text))

            if not good:
                if all(c.error == "" for c in res.candidates if c.engine != "ring") and res.candidates:
                    # Engines answered but heard no words.
                    res.notes.append("the engines returned an empty transcript")
                res.retry = any(c.retry for c in res.candidates)
                if ring_text and s.bool("use_ring_transcript"):
                    res.text, res.source, res.needs_review = apply_fixes(ring_text, fixes), "ring", True
                    res.notes.append("used the ring's transcript because no engine produced one")
                return res

            main = good[0]
            res.text, res.source = main.text, main.engine
            if main.engine != engines[0]:
                res.needs_review = True
                res.notes.append(f"the main engine failed; used {main.engine}")

            if len(good) > 1:
                res.agreement = similarity(good[0].text, good[1].text)
                if res.agreement < AGREE_THRESHOLD:
                    res.needs_review = True
                    res.notes.append(f"the engines agree on only {res.agreement:.0%} of the words")

            if s.bool("llm_enabled") and s.get("llm_api_key"):
                others = [c for c in res.candidates if c is not main and c.text]
                # Nothing to reconcile when there is a single transcript and no vocabulary.
                if others or terms:
                    fixed, note = await correct(client, s, main, others, terms)
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
