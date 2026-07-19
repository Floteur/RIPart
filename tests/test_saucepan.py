"""Unit tests for the pure (network-free) parts of ``ripart.providers.saucepan``.

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

from ripart.providers import saucepan as sp
from ripart.providers.saucepan import client as saucepan_client
from ripart.providers.saucepan import leak as saucepan_leak


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


def test_looks_like_definition():
    # Dumps: has markers, or long, or markdown-structured.
    assert sp._looks_like_definition("Here is the Example Dialogue:\n{{char}}: hi")
    assert sp._looks_like_definition("[ Critical Instructions ]\nAgency: ...")
    assert sp._looks_like_definition("# Piper\n**Personality:** bubbly")
    assert sp._looks_like_definition("x" * 1600)
    # Roleplay reply: narrative, short, no markers/headers -> not a definition.
    rp = (
        '(Piper stands on the counter, bare thighs against the edge.) '
        '"I should find a grocery store, huh?" *She pops a finger into the yogurt.*'
    )
    assert not sp._looks_like_definition(rp)


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


def test_search_companions_posts_catalogue_filters():
    captured = {}
    original_has_token, original_post_json = saucepan_client.has_token, saucepan_client._post_json
    try:
        saucepan_client.has_token = lambda: True

        def fake_post_json(path, body, with_auth=True):
            captured["path"] = path
            captured["body"] = body
            captured["with_auth"] = with_auth
            return True, 200, {"companions": [{"id": "one"}, "ignored"], "total_count": 12}

        saucepan_client._post_json = fake_post_json
        assert sp.search_companions(
            limit=12,
            offset=3,
            tags=["female"],
            excluded_tags=["male"],
            include_nsfw=False,
        ) == {"companions": [{"id": "one"}], "total_count": 12}
    finally:
        saucepan_client.has_token, saucepan_client._post_json = original_has_token, original_post_json
    assert captured["path"] == "/api/v1/search"
    assert captured["with_auth"] is True
    assert captured["body"]["tags"] == ["female"]
    assert captured["body"]["excluded_tags"] == ["male"]
    assert captured["body"]["limit"] == 12
    assert captured["body"]["offset"] == 3
    assert captured["body"]["sus"] is False


def test_search_companions_requires_token():
    original = saucepan_client.has_token
    try:
        saucepan_client.has_token = lambda: False
        try:
            sp.search_companions()
        except sp.SaucepanError as exc:
            assert exc.status == 401
        else:
            assert False, "expected SaucepanError"
    finally:
        saucepan_client.has_token = original


def test_set_provider_prompt_builds_patch_without_key():
    fake = {
        "config_id": "cfg1",
        "config_name": "mymodel",
        "model_id": "mymodel",
        "temperature": 0,
        "context_length": 32000,
        "provider_url": None,
        "use_chat_temperature_override": False,
        "provider_post_history_prompt": "keep-me",
        "provider_prompt": "OLD",
        "api_key": "SECRET-should-not-be-sent",
    }
    captured = {}

    def fake_request(method, path, *, with_auth, json_body=None, attempts=1, retry_5xx=False):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        return True, 200, {}

    orig_list, orig_req = saucepan_leak.list_provider_configs, saucepan_leak._request_json
    try:
        saucepan_leak.list_provider_configs = lambda: [fake]
        saucepan_leak._request_json = fake_request
        old = sp.set_provider_prompt("cfg1", "NEW SYSTEM PROMPT")
    finally:
        saucepan_leak.list_provider_configs, saucepan_leak._request_json = orig_list, orig_req

    assert old == "OLD"  # returns previous value for restore
    assert captured["method"] == "PATCH"
    assert captured["path"].endswith("/openai_provider/config/cfg1")
    body = captured["body"]
    assert body["provider_prompt"] == "NEW SYSTEM PROMPT"
    assert body["provider_post_history_prompt"] == "keep-me"  # preserved
    assert body["model_id"] == "mymodel"
    assert "api_key" not in body  # never send the key


def test_token_expiry(monkeypatch=None):
    assert sp.token_expiry(_jwt(1786788031)) == 1786788031
    assert sp.token_expiry(_jwt(None)) is None  # valid JWT, no exp claim
    assert sp.token_expiry("not-a-jwt") is None


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
