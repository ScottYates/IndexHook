"""Rules: decide what to do with a transcript.

Rules are checked in order. A rule matches on the text and names one action.
Phrase matching ignores case and punctuation, so "Note to self," and
"note-to-self" both match the phrase "note to self".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .db import DB

MATCH_TYPES = [
    ("starts_with", "Starts with a phrase"),
    ("contains", "Contains a phrase"),
    ("regex", "Matches a regular expression"),
    ("always", "Always"),
]
ACTION_TYPES = [
    ("email", "Send an email"),
    ("ntfy", "Send an ntfy notification"),
    ("webhook", "POST JSON to a URL"),
]


@dataclass
class Match:
    rule: dict
    text: str  # text passed to the action, with the trigger phrase removed if asked


def _phrase_regex(phrase: str) -> str | None:
    words = [re.escape(w) for w in re.findall(r"[^\W_]+", phrase)]
    return r"[\W_]+".join(words) if words else None


def match_rule(rule: dict, text: str) -> str | None:
    """Return the text for the action when the rule matches, else None."""
    kind = rule["match_type"]
    if kind == "always":
        return text
    patterns = [p.strip() for p in rule["pattern"].splitlines() if p.strip()]
    if kind == "regex":
        for p in patterns:
            try:
                if re.search(p, text, flags=re.IGNORECASE):
                    return text
            except re.error:
                continue
        return None
    # Longest phrase first, so "note to self" wins over "note".
    for p in sorted(patterns, key=len, reverse=True):
        core = _phrase_regex(p)
        if not core:
            continue
        if kind == "starts_with":
            m = re.match(r"[\W_]*" + core + r"(?![^\W_])[\W_]*", text, flags=re.IGNORECASE)
            if m:
                rest = text[m.end():]
                if rule["strip_trigger"] and rest:
                    return rest[0].upper() + rest[1:]
                return text
        elif re.search(r"(?<![^\W_])" + core + r"(?![^\W_])", text, flags=re.IGNORECASE):
            return text
    return None


def evaluate(rules: list[dict], text: str) -> list[Match]:
    out = []
    for r in rules:
        if not r["enabled"]:
            continue
        t = match_rule(r, text)
        if t is None:
            continue
        out.append(Match(r, t))
        if r["stop"]:
            break
    return out


def user_rules(db: DB, user_id: int) -> list[dict]:
    rows = db.q("SELECT * FROM rules WHERE user_id=? ORDER BY position, id", user_id)
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["config"] = json.loads(d["action_config"] or "{}")
        except ValueError:
            d["config"] = {}
        out.append(d)
    return out


def seed_rules(db: DB, user_id: int):
    """Starter rules for a new account: note to self, then a catch-all so nothing is dropped."""
    seed = [
        ("Note to self", "starts_with",
         "note to self\nnotes to self\nnote to myself\nnote for self\nremind me",
         1, {"subject": "Note: {first_words}", "attach_audio": False}),
        ("Everything else", "always", "", 0, {"subject": "Ring: {first_words}", "attach_audio": False}),
    ]
    for i, (name, mt, pat, strip, cfg) in enumerate(seed):
        db.run(
            "INSERT INTO rules(user_id,position,name,match_type,pattern,strip_trigger,stop,action_type,action_config)"
            " VALUES(?,?,?,?,?,?,1,'email',?)", user_id, i, name, mt, pat, strip, json.dumps(cfg))
