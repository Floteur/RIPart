"""Unit tests for the pure (network-free) parts of ``ripart.saucepan``.

These cover the fragile bits: the fragment-reassembly scheme, the lorebook
entry parsing, the definition-leak detection/merge, and the small parsers
(companion id, JWT expiry). No network and no token-file writes.

Runnable two ways:
    pytest tests/test_saucepan.py          # if pytest is installed
    python tests/test_saucepan.py          # standalone (no dependencies)
"""

from __future__ import annotations

import base64
import json

from ripart import saucepan as sp


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_fragments(mask: int, ordered_texts: list[tuple[int, str]]) -> list[dict]:
    """Build valid (proof-carrying) fragments for the given (order_key, text) pairs."""
    frags = []
    for order_key, text in ordered_texts:
        key = (order_key ^ mask) & sp._U32
        proof = sp._fragment_hash(mask, order_key, text)
        frags.append({"key": key, "proof": proof, "text": text})
    return frags


def _jwt(exp: int | None) -> str:
    payload = {} if exp is None else {"exp": exp}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


# --------------------------------------------------------------------------- #
# Fragment reassembly
# --------------------------------------------------------------------------- #


def test_assemble_fragments_orders_and_drops_decoys():
    mask = 0x1234ABCD
    frags = _make_fragments(mask, [(1, "Hello, "), (2, "world"), (3, "!")])
    # A decoy with a wrong proof must be discarded.
    frags.insert(0, {"key": (7 ^ mask) & sp._U32, "proof": 12345, "text": "GARBAGE"})
    # Shuffle to prove ordering is by key ^ mask, not input order.
    frags = [frags[2], frags[0], frags[3], frags[1]]
    assert sp.assemble_fragments({"fragments": frags, "mask": mask}) == "Hello, world!"


def test_assemble_fragments_handles_missing_and_empty():
    assert sp.assemble_fragments(None) == ""
    assert sp.assemble_fragments({}) == ""
    assert sp.assemble_fragments({"fragments": "nope", "mask": 0}) == ""


# --------------------------------------------------------------------------- #
# Lorebook entry parsing
# --------------------------------------------------------------------------- #


def test_lorebook_entry_parses_keys_comment_and_strips_markers():
    text = (
        "# Foreplay Guide\n<br>\n"
        "**Activation Keys:** foreplay\n<br>\n"
        "**Secondary Keys:** kiss, touch\n<br>\n"
        "**Comment:** Short summary of the entry.\n<br>\n"
        "**Guidance:** The real lore body goes here."
    )
    world = sp._lorebook_world_info([{"title": "Foreplay", "text": text}])
    entry = world["0"]
    assert entry["key"] == ["foreplay"]
    assert entry["keysecondary"] == ["kiss", "touch"]
    assert entry["comment"] == "Short summary of the entry."
    assert entry["constant"] is False  # keyed -> selective
    # Metadata lines removed; heading + real body kept.
    assert "Activation Keys" not in entry["content"]
    assert "Secondary Keys" not in entry["content"]
    assert "**Comment:**" not in entry["content"]
    assert "# Foreplay Guide" in entry["content"]
    assert "The real lore body goes here." in entry["content"]
    assert "<br>" not in entry["content"]


def test_lorebook_entry_without_markers_is_constant_and_uses_title():
    world = sp._lorebook_world_info(
        [{"title": "Chapter One", "text": "Some prose without any markers."}]
    )
    entry = world["0"]
    assert entry["key"] == []
    assert entry["comment"] == "Chapter One"
    assert entry["constant"] is True  # keyless -> always on
    assert entry["content"] == "Some prose without any markers."


def test_lorebook_skips_empty_and_nondict_entries():
    world = sp._lorebook_world_info([{"text": "   "}, "nope", {"title": "x", "text": "real"}])
    assert len(world) == 1
    assert world["0"]["content"] == "real"


def test_clean_lore_text_normalizes():
    assert sp._clean_lore_text("a<br>b<br/>c") == "a\nb\nc"
    assert sp._clean_lore_text("# >>marker<< body") == "body"
    assert sp._clean_lore_text("x\r\n\n\n\ny") == "x\n\ny"


# --------------------------------------------------------------------------- #
# Definition leak: refusal detection, fence stripping, merge
# --------------------------------------------------------------------------- #


def test_refusal_detection():
    refusals = [
        "I cannot fulfill this request. My safety guidelines prohibit it.",
        "# Debug session.\nI cannot provide a verbatim repeat of the system prompt.",
        "I'm unable to share my instructions.",
        "I will not reveal that.",
    ]
    dumps = [
        "[ Critical Instructions ]\nAgency: never control the user...",
        "# Piper\nPiper is 18 and lives with her boyfriend's dad.",
    ]
    assert all(sp._looks_like_refusal(r) for r in refusals)
    assert not any(sp._looks_like_refusal(d) for d in dumps)


def test_strip_code_fence():
    assert sp._strip_code_fence("```\nhi\n```") == "hi"
    assert sp._strip_code_fence("```markdown\nhi\nthere\n```") == "hi\nthere"
    assert sp._strip_code_fence("no fence") == "no fence"


def test_split_example_section():
    text = "# Def\nbody line\n\n## Example Dialogue\n{{char}}: hi\n{{user}}: yo"
    definition, example = sp._split_example_section(text)
    assert "Example Dialogue" not in definition
    assert "body line" in definition
    assert "{{char}}: hi" in example
    # No header -> whole thing stays as the definition.
    d2, e2 = sp._split_example_section("just a definition, no example header")
    assert d2 == "just a definition, no example header"
    assert e2 == ""


def test_apply_leak_merges_into_character():
    ch = {"description": "OLD", "exampleMessages": "", "definitionSource": "saucepan-partial"}
    sample = "```\n# Piper\nBubbly.\n\n## Example Dialogue\n{{char}}: Hey!\n```"
    sp._apply_leak(ch, sample)
    assert ch["definitionSource"] == "saucepan-leak"
    assert ch["reconstruction"]["method"] == "saucepan-chat-leak"
    assert not ch["description"].startswith("```")
    assert "Example Dialogue" not in ch["description"]
    assert "{{char}}: Hey!" in ch["exampleMessages"]


# --------------------------------------------------------------------------- #
# Small parsers
# --------------------------------------------------------------------------- #


def test_parse_companion_id():
    assert (
        sp.parse_companion_id("https://saucepan.ai/companion/0e5f920b-322c-4286-9413-ad21566e5c50")
        == "0e5f920b-322c-4286-9413-ad21566e5c50"
    )
    assert sp.parse_companion_id("0e5f920b-322c-4286-9413-ad21566e5c50") == "0e5f920b-322c-4286-9413-ad21566e5c50"
    assert sp.parse_companion_id("https://example.com/nope") is None
    assert sp.parse_companion_id("") is None


def test_is_saucepan_url():
    assert sp.is_saucepan_url("https://saucepan.ai/companion/x")
    assert sp.is_saucepan_url("SAUCEPAN.AI/x")
    assert not sp.is_saucepan_url("https://janitorai.com/x")


def test_token_expiry(monkeypatch=None):
    original = sp._token
    try:
        sp._token = _jwt(1786788031)
        assert sp.token_expiry() == 1786788031
        sp._token = _jwt(None)  # valid JWT, no exp claim
        assert sp.token_expiry() is None
        sp._token = "not-a-jwt"
        assert sp.token_expiry() is None
    finally:
        sp._token = original


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
