"""Top-level Saucepan extraction: build a RIPart ``result`` dict from a URL."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from ...common.text import strip_code_fence
from .client import (
    SAUCEPAN_BASE,
    SaucepanError,
    _companion_creator,
    _get_json,
    fetch_avatar,
    has_token,
    parse_companion_id,
)
from .fragments import assemble_fragments
from .leak import (
    DEFAULT_LEAK_PROMPT,
    leak_definition,
    leak_definition_via_echo,
)
from .lorebook import fetch_companion_lorebooks


def _noop(_message: str) -> None:
    pass


def _split_example_section(text: str) -> tuple[str, str]:
    """Best-effort split of a leaked dump into (definition, example_dialogue).

    Looks for an 'Example Dialogue / Messages' header; returns everything before
    it as the definition and the section after as example messages. If no such
    header is found, returns (text, '') - the whole dump stays as the definition.
    """
    header = re.search(
        r"(?im)^\s*[#>\[\*\-\s]*((?:example\s+(?:dialogue|messages?|conversations?))|dialogue\s+examples?)\b.*$",
        text,
    )
    if not header:
        return text, ""
    definition = text[: header.start()].strip()
    example = text[header.end() :].strip()
    # Stop the example section at the next top-level header, if any.
    nxt = re.search(r"(?im)^\s*[#\[]{1,2}\s*\S.*$", example)
    if nxt and nxt.start() > 0:
        example = example[: nxt.start()].strip()
    return (definition or text), example


def _apply_leak(character: dict[str, Any], leaked: str) -> None:
    """Merge a leaked definition dump into a character dict (in place)."""
    text = strip_code_fence(leaked)
    definition, example = _split_example_section(text)
    character["description"] = definition
    if example:
        character["exampleMessages"] = example
    character["definitionSource"] = "saucepan-leak"
    character["reconstruction"] = {"method": "saucepan-chat-leak", "chars": len(text)}


# Top-level ``[ Title ]`` section headers in an assembled prompt (space-padded,
# on their own line): ``[ Critical Instructions ]``, ``[ Background ]``,
# ``[ Example Dialogue ]``, ``[ User Description ]``, ``[ Lore ]``, etc.
_ECHO_SECTION = re.compile(r"(?m)^[ \t]*\[[ \t]+([A-Za-z][^\]\n]{1,48}?)[ \t]+\]\s*$")


def _classify_echo_section(title: str) -> str:
    t = title.lower()
    if re.search(r"example\s+(?:dialogue|messages?)|dialogue\s+examples?", t):
        return "example"
    if re.match(r"lore|world\s*info|lorebook", t):
        return "lore"
    if "user description" in t or t.strip() in ("user", "persona"):
        return "user"
    return "desc"


def _split_echo_definition(text: str) -> dict[str, str]:
    """Carve an assembled prompt into ``description`` / ``example`` / ``lore``.

    Splits on top-level ``[ Title ]`` headers and routes each section: example
    dialogue → ``example``, the (often gated) lorebook block → ``lore``, the
    user-persona block is dropped, and everything else (intro + rules +
    background) stays in ``description``. Nothing is discarded — unlike the old
    example-only split, which lost every section after the example dialogue.
    """
    heads = list(_ECHO_SECTION.finditer(text or ""))
    if not heads:
        definition, example = _split_example_section(text or "")
        return {"description": definition, "example": example, "lore": ""}

    preamble = (text[: heads[0].start()]).strip()
    desc: list[str] = [preamble] if preamble else []
    example: list[str] = []
    lore: list[str] = []
    for i, m in enumerate(heads):
        start, end = (
            m.end(),
            (heads[i + 1].start() if i + 1 < len(heads) else len(text)),
        )
        content = text[start:end].strip()
        kind = _classify_echo_section(m.group(1))
        if kind == "example":
            example.append(content)
        elif kind == "lore":
            lore.append(content)
        elif kind == "user":
            continue  # the user-persona block is not part of the card
        else:
            header = m.group(0).strip()
            desc.append(f"{header}\n{content}" if content else header)
    return {
        "description": "\n\n".join(p for p in desc if p).strip(),
        "example": "\n\n".join(p for p in example if p).strip(),
        "lore": "\n\n".join(p for p in lore if p).strip(),
    }


def _apply_echo_leak(character: dict[str, Any], echo: dict[str, Any]) -> None:
    """Merge a verbatim echo-proxy leak into a character dict (in place).

    Unlike :func:`_apply_leak` this is the exact assembled prompt, so we mark it
    as a verbatim source, route its labelled sections (description / example
    dialogue / lore), and preserve the greetings the echo exposed. The ``[ Lore ]``
    block — the companion's lorebook, injected into the prompt even when the
    ``/lorebooks`` API gates it — is kept in ``lorebookText`` and labelled in
    creator notes so it is never dropped.
    """
    parts = _split_echo_definition(echo.get("definition") or "")
    character["description"] = parts["description"]
    if parts["example"]:
        character["exampleMessages"] = parts["example"]
    if parts["lore"]:
        character["lorebookText"] = parts["lore"]
        notes = str(character.get("creatorNotes") or "").strip()
        character["creatorNotes"] = (
            (notes + "\n\n" if notes else "")
            + "--- Lorebook (leaked via echo) ---\n"
            + parts["lore"]
        ).strip()
    greetings = [g for g in (echo.get("greetings") or []) if str(g).strip()]
    if greetings:
        character["firstMessage"] = greetings[0]
        if len(greetings) > 1:
            character["alternateGreetings"] = greetings[1:]
    character["definitionSource"] = "saucepan-echo"
    character["reconstruction"] = {
        "method": "saucepan-echo-proxy",
        "chars": len(echo.get("definition") or ""),
        "loreChars": len(parts["lore"]),
        "verbatim": True,
    }


def extract_companion(
    url: str,
    *,
    include_lorebooks: bool = True,
    leak: bool = False,
    leak_config: str | None = None,
    leak_model: str | None = None,
    leak_mode: str = "user",
    leak_prompt: str | None = None,
    leak_keep: bool = False,
    leak_echo: bool = False,
    leak_timeout: int = 180,
    log: Callable[[str], None] = _noop,
) -> dict[str, Any]:
    """Fetch a Saucepan companion by URL and build a RIPart ``result`` dict.

    The returned dict is shaped for ``ripart.common.cards.save_to_library``: it
    carries the ``character`` object plus ``meta``/``publicLorebooks``/``url``.
    Requires a stored bearer token.

    Two data sources, either of which may be gated:
      * ``/companion/definition`` - the named prose sections (Companion Core,
        Example Dialogue, Advanced Prompt, Response Formatting). Returns 403 when
        the companion's ``open_definition`` is false (the common case).
      * ``/v2/companions/{id}`` - public metadata plus the body + greeting
        fragments. This is the primary source and works without open_definition.

    When only the v2 endpoint is available we still build a full card (body +
    greetings + metadata + lorebooks); the example dialogue / advanced prompt
    are simply absent, and ``definitionSource`` is marked ``saucepan-partial``.

    When ``leak`` is set, the full definition is recovered — verbatim via the echo
    proxy when ``leak_echo`` and a custom provider config allow it, otherwise via
    a model dump (lossy). ``leak_config`` selects a BYOK provider config (by id or
    name), ``leak_model`` a Saucepan model_alias.
    """
    if not has_token():
        raise SaucepanError(
            "no Saucepan token configured - run `rip saucepan login` first", 401
        )
    companion_id = parse_companion_id(url)
    if not companion_id:
        raise SaucepanError("not a Saucepan companion URL", 400)

    def_ok, def_status, def_data = _get_json(
        f"/api/v1/companion/definition?companion_id={quote(companion_id, safe='')}",
        True,
    )
    comp_ok, comp_status, comp_data = _get_json(
        f"/api/v2/companions/{quote(companion_id, safe='')}", True
    )

    companion = comp_data.get("companion") if isinstance(comp_data, dict) else None
    if not isinstance(companion, dict):
        companion = {}

    # Named prose sections from the definition endpoint (when accessible).
    sections: dict[str, str] = {}
    if def_ok and isinstance(def_data, dict):
        for section in def_data.get("sections") or []:
            if (
                isinstance(section, dict)
                and section.get("title")
                and section.get("content")
            ):
                sections[section["title"]] = assemble_fragments(section["content"])

    # Body: prefer the definition's "Companion Core", fall back to the v2 body.
    description = sections.get("Companion Core") or ""
    if not description and companion.get("full_description_fragments"):
        description = assemble_fragments(companion["full_description_fragments"])

    # If neither source yielded anything usable, surface the real error.
    if not description and not companion:
        message = None
        if isinstance(def_data, dict):
            message = (def_data.get("error") or {}).get("message")
        if not message and isinstance(comp_data, dict):
            message = (comp_data.get("error") or {}).get("message")
        status = def_status if not def_ok else comp_status
        raise SaucepanError(
            message or f"Saucepan HTTP {status}", 401 if status == 401 else 502
        )

    # Greetings live only on the v2 companion as starting scenarios.
    greetings: list[str] = []
    for scenario in companion.get("starting_scenarios_fragments") or []:
        text = assemble_fragments(
            scenario.get("message") if isinstance(scenario, dict) else None
        )
        if text and text.strip():
            greetings.append(text)

    # Advanced Prompt / Response Formatting have no dedicated card field; keep
    # them (labeled) in creator notes so nothing authored is silently dropped.
    notes_parts: list[str] = []
    short_desc = str(companion.get("short_description") or "").strip()
    if short_desc:
        notes_parts.append(short_desc)
    if sections.get("Advanced Prompt"):
        notes_parts.append(f"--- Advanced Prompt ---\n{sections['Advanced Prompt']}")
    if sections.get("Response Formatting Instructions"):
        notes_parts.append(
            f"--- Response Formatting ---\n{sections['Response Formatting Instructions']}"
        )
    if not def_ok:
        notes_parts.append(
            "--- Note ---\nThis companion's definition is not open, so example dialogue and "
            "advanced prompt could not be pulled; the card body and greetings come from Saucepan's "
            "public companion data."
        )

    image = companion.get("image")
    image_id = image.get("id") if isinstance(image, dict) else None
    avatar_base64 = fetch_avatar(image_id)

    name = companion.get("display_name") or companion.get("name") or "Unknown"
    tags = companion.get("tags") if isinstance(companion.get("tags"), list) else []
    creator_name, creator_id = _companion_creator(companion)
    is_nsfw = bool(companion.get("is_nsfw") or companion.get("nsfw"))

    public_lorebooks = (
        fetch_companion_lorebooks(companion_id) if include_lorebooks else []
    )

    character = {
        "name": name,
        "avatarBase64": avatar_base64,
        "description": description,
        "personality": "",
        "scenario": "",
        "firstMessage": greetings[0] if greetings else "",
        "alternateGreetings": greetings[1:],
        "exampleMessages": sections.get("Example Dialogue") or "",
        "creatorNotes": "\n\n".join(notes_parts),
        "tags": tags,
        "definitionSource": "saucepan" if def_ok else "saucepan-partial",
    }

    meta = {
        "name": name,
        "creator_name": creator_name,
        "creator_id": creator_id,
        "is_nsfw": is_nsfw,
        # Saucepan's definition is an exact pull, so treat it as a public card.
        "showdefinition": True,
    }

    leak_chars = 0
    leak_error = ""
    leak_raw = ""
    leak_method = ""
    if leak:
        # Preferred path: verbatim echo-proxy leak (needs a BYOK config with a
        # custom provider_url). Falls back to the lossy model dump if the proxy
        # isn't allowed or fails.
        if leak_echo:
            try:
                echo = leak_definition_via_echo(
                    url,
                    provider_config_id=leak_config,
                    mode=leak_mode,
                    timeout=leak_timeout,
                    log=log,
                )
                _apply_echo_leak(character, echo)
                leak_raw = echo.get("raw") or echo.get("definition") or ""
                leak_chars = len(echo.get("definition") or "")
                leak_method = "echo"
            except SaucepanError as exc:
                leak_error = str(exc)
                log(f"echo leak unavailable: {exc}")

        if not leak_method:
            try:
                leaked = leak_definition(
                    url,
                    provider_config_id=leak_config,
                    model_alias=leak_model,
                    mode=leak_mode,
                    prompt=leak_prompt or DEFAULT_LEAK_PROMPT,
                    timeout=leak_timeout,
                    accept_any=leak_keep,
                    log=log,
                )
                _apply_leak(character, leaked)
                leak_raw = strip_code_fence(leaked)
                leak_chars = len(leak_raw)
                leak_method = "model"
                leak_error = ""
            except SaucepanError as exc:
                # Non-fatal: keep the public-data card; report why the leak failed.
                leak_error = str(exc)

    return {
        "url": url
        if url.startswith(("http://", "https://"))
        else f"{SAUCEPAN_BASE}/companion/{companion_id}",
        "characterId": companion_id,
        "characterName": name,
        "character": character,
        "meta": meta,
        "publicLorebooks": public_lorebooks,
        "entries": [],
        "lorebookText": "",
        # Raw leaked dump (if any) — the CLI writes it to a sidecar for review,
        # since the parsed merge into the card is lossy.
        "leakRaw": leak_raw,
        "diagnostics": {
            "greetings": len(greetings),
            "sections": sorted(sections.keys()),
            "definitionOpen": bool(def_ok),
            "lorebooks": len(public_lorebooks),
            "lorebookEntries": sum(
                len((b.get("worldInfo") or {}).get("entries") or {})
                for b in public_lorebooks
            ),
            "leakChars": leak_chars,
            "leakError": leak_error,
            "leakMethod": leak_method,
        },
    }
