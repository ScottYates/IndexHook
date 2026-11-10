from indexhook import actions, rules, stt
from indexhook.auth import check_password, hash_password


def rule(match_type, pattern="", strip=1, stop=1, enabled=1, name="r"):
    return {"name": name, "match_type": match_type, "pattern": pattern, "strip_trigger": strip,
            "stop": stop, "enabled": enabled, "action_type": "email", "action_config": "{}"}


def test_vocabulary_parsing():
    terms, fixes = stt.parse_vocabulary("Shivani\n# comment\n\nno to self => note to self\nShivani\n")
    assert terms == ["Shivani", "note to self"]
    assert fixes == [("no to self", "note to self")]


def test_fixes_are_whole_word_and_ignore_case_and_punctuation():
    fixes = [("no to self", "note to self"), ("you know host", "YunoHost")]
    assert stt.apply_fixes("No, to self: update You know host.", fixes) == "note to self: update YunoHost."
    assert stt.apply_fixes("piano to selfish", fixes) == "piano to selfish"


def test_similarity_ignores_case_and_punctuation():
    assert stt.similarity("Note to self, buy milk.", "note to self buy milk") == 1.0
    assert stt.similarity("buy milk today", "sell the car tomorrow") < 0.3


def test_starts_with_strips_trigger_and_tolerates_punctuation():
    r = rule("starts_with", "note to self\nremind me")
    assert rules.match_rule(r, "Note to self, buy milk.") == "Buy milk."
    assert rules.match_rule(r, "note-to-self: buy milk") == "Buy milk"
    assert rules.match_rule(r, "Remind me to call mum") == "To call mum"
    assert rules.match_rule(r, "A note to self") is None
    assert rules.match_rule(r, "Note to selfish people") is None


def test_starts_with_keeps_text_when_only_the_trigger_was_said():
    assert rules.match_rule(rule("starts_with", "note to self"), "Note to self.") == "Note to self."


def test_contains_and_regex_and_always():
    assert rules.match_rule(rule("contains", "shopping list"), "add eggs to the Shopping-List please") is not None
    assert rules.match_rule(rule("contains", "shop"), "shopping") is None
    assert rules.match_rule(rule("regex", r"^text (\w+)"), "Text Anna I am late") is not None
    assert rules.match_rule(rule("regex", "(["), "anything") is None
    assert rules.match_rule(rule("always"), "") == ""


def test_evaluate_order_stop_and_disabled():
    rs = [rule("contains", "milk", stop=0, name="a"), rule("always", enabled=0, name="b"),
          rule("always", name="c"), rule("always", name="d")]
    assert [m.rule["name"] for m in rules.evaluate(rs, "buy milk")] == ["a", "c"]
    assert [m.rule["name"] for m in rules.evaluate(rs, "buy eggs")] == ["c"]


def test_template_rendering_does_not_evaluate():
    ctx = actions.context("Buy milk and eggs and bread and jam and tea and more things", "Note", "scott", 0)
    assert actions.render("{rule}: {first_words} {user.__class__} {nope}", ctx) == \
        "Note: Buy milk and eggs and bread and jam… {user.__class__} {nope}"
    assert actions.context("", "r", "u", 0)["text"] == actions.NO_TEXT


def test_password_hash_roundtrip():
    h = hash_password("correct horse")
    assert check_password("correct horse", h)
    assert not check_password("wrong", h)
    assert not check_password("x", None)
