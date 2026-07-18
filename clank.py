"""clank.world native extraction (no browser).

clank.world gates a character's real definition: the tRPC read API exposes only
public metadata (name, avatar, a story blurb) and returns ``description: null``
for the character body. The full definition lives only in the *system prompt*
clank sends to the model at generation time.

The leak recovers it verbatim with an **echo proxy** — an OpenAI-compatible
endpoint that echoes the request body straight back as the assistant reply
(see ``DEFAULT_ECHO_BASE_URL``). When a chat's *custom LLM provider* points at
that proxy and a message is sent, clank posts its whole prompt to the proxy and
stores the echoed JSON as an assistant message. That JSON's ``developer``
message is the character's system prompt, so the definition, greeting, and
example dialogue come back byte-for-byte — no model paraphrasing, unlike the
Saucepan chat-leak.

Two flows:

* **read an existing echo** — if a chat already has an echoed reply in its
  history (proxy configured + a message sent by hand), :func:`extract_chat`
  parses it into a card. No mutation, nothing spent.
* **auto-leak** (``leak=True``) — point the chat's provider at the proxy, send a
  throwaway message to trigger a generation, read the echo, then restore the
  original provider. Needs the write procedures wired (see ``_MUTATIONS``).

Auth is the browser's ``next-auth`` session cookie (a JWE), persisted to a
gitignored ``.clank-session.json`` and reused by every command — mirroring the
Saucepan bearer token. Mutations additionally need the CSRF cookie.

tRPC wire format::

    GET  /api/trpc/<proc>?batch=1&input=<urlenc {"0": <input>}>
      -> {"0": {"result": {"data": <data>}}}   (or {"0": {"error": ...}})
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .helpers import split_text_chunks

CLANK_BASE = "https://www.clank.world"
CLANK_ORIGIN = "https://www.clank.world"
CLANK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
TIMEOUT = 30
MAX_IMAGE_BYTES = 12 * 1024 * 1024

# Session cookies live next to the code, gitignored. A small JSON object:
# {"session_token": "...", "csrf_token": "..."}. Never printed back to the user.
SESSION_FILE = Path(__file__).resolve().parent / ".clank-session.json"

# An OpenAI-compatible worker that echoes the request body back as the assistant
# message. Point a chat's custom LLM provider at it and the echoed system prompt
# is the character definition, verbatim. Overridable per call.
DEFAULT_ECHO_BASE_URL = "https://echollm.ecorsiste.workers.dev/v1"


class ClankError(Exception):
    """User-facing clank.world failure; carries an optional HTTP-ish status code."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Shared HTTP client (connection pooling + keep-alive)
# --------------------------------------------------------------------------- #

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    base_url=CLANK_BASE,
                    timeout=TIMEOUT,
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_connections=20, max_keepalive_connections=10
                    ),
                )
    return _client


# --------------------------------------------------------------------------- #
# Persisted session cookies
# --------------------------------------------------------------------------- #

_session: dict[str, str] | None = None

# Per-thread session override (see ``use_session``), mirroring Saucepan's
# per-thread bearer token so several accounts could drive clank concurrently.
_session_override = threading.local()


def _active_session() -> dict[str, str]:
    """The session cookies for the calling thread: its override, else the global."""
    override = getattr(_session_override, "value", None)
    return override if override else load_session()


@contextmanager
def use_session(session: dict[str, str]):
    """Run a block with ``session`` as the active cookies for this thread only."""
    prev = getattr(_session_override, "value", None)
    _session_override.value = dict(session or {})
    try:
        yield
    finally:
        _session_override.value = prev


def load_session() -> dict[str, str]:
    """Read the persisted session cookies into memory (called on first use)."""
    global _session
    if _session is None:
        if SESSION_FILE.exists():
            try:
                if (SESSION_FILE.stat().st_mode & 0o077) != 0:
                    os.chmod(SESSION_FILE, 0o600)
            except OSError:
                pass
            try:
                data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
                _session = data if isinstance(data, dict) else {}
            except (ValueError, OSError):
                _session = {}
        else:
            _session = {}
    return _session


def set_session(session_token: str, csrf_token: str | None = None) -> None:
    """Store the session cookies in memory and on disk (owner-only file perms)."""
    global _session
    _session = {"session_token": str(session_token or "").strip()}
    if csrf_token:
        _session["csrf_token"] = str(csrf_token).strip()
    SESSION_FILE.write_text(json.dumps(_session), encoding="utf-8")
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass


def clear_session() -> None:
    """Forget the stored session (log out)."""
    global _session
    _session = {}
    SESSION_FILE.unlink(missing_ok=True)


def has_session() -> bool:
    return bool(_active_session().get("session_token"))


def _cookie_header(session: dict[str, str] | None = None) -> str:
    session = session if session is not None else _active_session()
    parts = []
    if session.get("session_token"):
        parts.append(f"__Secure-next-auth.session-token={session['session_token']}")
    if session.get("csrf_token"):
        parts.append(f"__Host-next-auth.csrf-token={session['csrf_token']}")
    return "; ".join(parts)


def _headers(*, referer: str | None = None, json_body: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": CLANK_UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": CLANK_ORIGIN,
        "Referer": referer or f"{CLANK_ORIGIN}/",
    }
    cookie = _cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


# --------------------------------------------------------------------------- #
# HTTP wire tracing (deep --verbose levels)
# --------------------------------------------------------------------------- #

_trace_level = 0


def set_trace_level(level: int) -> None:
    global _trace_level
    _trace_level = max(0, int(level or 0))


def _trace_preview(value: Any, limit: int = 800) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = re.sub(r"\s+", " ", text)
    return text[:limit] + ("…" if len(text) > limit else "")


# --------------------------------------------------------------------------- #
# tRPC helpers
# --------------------------------------------------------------------------- #

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = 0.75
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _unwrap_trpc(payload: Any) -> Any:
    """Return ``result.data`` from a single-proc tRPC batch response, or raise."""
    if not isinstance(payload, list) or not payload:
        raise ClankError("unexpected tRPC response shape")
    entry = payload[0]
    if isinstance(entry, dict) and entry.get("error"):
        err = entry["error"]
        message = None
        if isinstance(err, dict):
            j = err.get("json") if isinstance(err.get("json"), dict) else err
            message = (j or {}).get("message")
        raise ClankError(message or "tRPC error")
    return (((entry or {}).get("result") or {}).get("data")) if isinstance(entry, dict) else None


def trpc_query(proc: str, input: dict[str, Any] | None = None) -> Any:
    """GET a single tRPC procedure and return its ``result.data``.

    Retries transient network / 429 / 5xx failures with exponential backoff.
    """
    query = quote(json.dumps({"0": input if input is not None else {}}), safe="")
    path = f"/api/trpc/{proc}?batch=1&input={query}"
    client = _get_client()
    headers = _headers()
    for attempt in range(_RETRY_ATTEMPTS):
        last = attempt == _RETRY_ATTEMPTS - 1
        try:
            response = client.get(path, headers=headers)
        except httpx.HTTPError as exc:
            if last:
                raise ClankError(f"network error talking to clank.world: {exc}") from exc
            time.sleep(_RETRY_BACKOFF * (2**attempt))
            continue
        if _trace_level >= 2:
            print(f"[clank-http] GET {proc} -> {response.status_code}", flush=True)
        if _trace_level >= 3:
            print(f"[clank-http]   resp: {_trace_preview(response.text)}", flush=True)
        if response.status_code == 404:
            raise ClankError(f"tRPC procedure not found: {proc}", 404)
        if response.status_code in _RETRYABLE_STATUS and not last:
            time.sleep(_RETRY_BACKOFF * (2**attempt))
            continue
        if response.status_code in (401, 403):
            raise ClankError("not authenticated - run `rip clank login`", 401)
        try:
            data = response.json()
        except ValueError as exc:
            raise ClankError(f"clank.world returned non-JSON ({response.status_code})") from exc
        return _unwrap_trpc(data)
    raise ClankError("request failed")  # pragma: no cover


def trpc_mutation(proc: str, input: dict[str, Any] | None = None) -> Any:
    """POST a single tRPC mutation and return its ``result.data``.

    Non-idempotent (create/generate), so it is attempted once.
    """
    body = {"0": input if input is not None else {}}
    path = f"/api/trpc/{proc}?batch=1"
    try:
        response = _get_client().post(
            path, headers=_headers(json_body=True), json=body
        )
    except httpx.HTTPError as exc:
        raise ClankError(f"network error talking to clank.world: {exc}") from exc
    if _trace_level >= 2:
        print(f"[clank-http] POST {proc} -> {response.status_code}", flush=True)
    if _trace_level >= 3:
        print(f"[clank-http]   body: {_trace_preview(body)}", flush=True)
        print(f"[clank-http]   resp: {_trace_preview(response.text)}", flush=True)
    if response.status_code == 404:
        raise ClankError(f"tRPC procedure not found: {proc}", 404)
    if response.status_code in (401, 403):
        raise ClankError("not authenticated - run `rip clank login`", 401)
    try:
        data = response.json()
    except ValueError as exc:
        raise ClankError(f"clank.world returned non-JSON ({response.status_code})") from exc
    return _unwrap_trpc(data)


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #

_UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


_UUID_ANY = re.compile(_UUID_RE, re.I)


def parse_chat_id(url: str) -> str | None:
    """Extract the chat UUID from a ``clank.world/chat/<uuid>`` URL (or bare UUID)."""
    match = re.search(rf"clank\.world/chat/({_UUID_RE})", url or "", re.I)
    if match:
        return match.group(1)
    bare = (url or "").strip()
    if re.fullmatch(_UUID_RE, bare, re.I):
        return bare
    return None


def parse_target(url: str) -> tuple[str | None, str | None]:
    """Classify a clank URL: ``("chat", chat_id)`` or ``("character", slug)``.

    Accepts ``clank.world/chat/<uuid>`` (or a bare UUID) and character pages
    ``clank.world/@<slug>`` (e.g. ``@c/physical-longer-top``). Returns
    ``(None, None)`` if neither matches.
    """
    chat_id = parse_chat_id(url)
    if chat_id:
        return "chat", chat_id
    match = re.search(r"clank\.world/@([^?#\s]+)", url or "", re.I)
    if match:
        return "character", match.group(1).rstrip("/")
    return None, None


def is_clank_url(url: str) -> bool:
    return "clank.world" in (url or "").lower()


def _fetch_html(path: str) -> str:
    """GET a clank.world page as text (authenticated), '' on error."""
    try:
        response = _get_client().get(
            path,
            headers={
                "User-Agent": CLANK_UA,
                "Accept": "text/html,*/*",
                "Cookie": _cookie_header(),
                "Referer": f"{CLANK_ORIGIN}/",
            },
        )
        return "" if response.is_error else response.text
    except httpx.HTTPError:
        return ""


def get_chat_history(limit: int = 100) -> list[dict[str, Any]]:
    """Return the viewer's chat list (``agent.get_user_chat_history_with_pagination``).

    Items: ``{chat_id, scene_id, agent_name, agent_image, last_message, ...}`` —
    ``last_message`` holds the most recent message (which, for a leaked chat, is
    the echoed request body).
    """
    data = trpc_query(
        "agent.get_user_chat_history_with_pagination",
        {"limit": max(1, int(limit)), "direction": "forward"},
    )
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def resolve_character_chat(slug: str) -> str | None:
    """Find the viewer's existing chat for a character page ``@<slug>``.

    The character page embeds the UUIDs of the viewer's active chat / scene for
    that character; we intersect those with the chat history to recover the
    ``chat_id``. Returns None if the viewer has no chat with this character.
    """
    html = _fetch_html(f"/@{slug}")
    page_ids = {m.group(0).lower() for m in _UUID_ANY.finditer(html)}
    if not page_ids:
        return None
    items = get_chat_history(limit=100)
    # Prefer a chat whose own id is on the page; fall back to a scene match.
    for it in items:
        if str(it.get("chat_id") or "").lower() in page_ids:
            return str(it["chat_id"])
    for it in items:
        if str(it.get("scene_id") or "").lower() in page_ids:
            return str(it["chat_id"])
    return None


# --------------------------------------------------------------------------- #
# Read procedures
# --------------------------------------------------------------------------- #


def get_chat_info(chat_id: str) -> dict[str, Any]:
    data = trpc_query("agent.get_chat_info", {"chat_id": chat_id})
    return data if isinstance(data, dict) else {}


def get_chat_llm_settings(chat_id: str) -> dict[str, Any]:
    data = trpc_query("agent.get_chat_llm_settings", {"chat_id": chat_id})
    return data if isinstance(data, dict) else {}


def get_story(scene_id: str) -> dict[str, Any]:
    """Return a scene/story's public data (``agent.get_clank_story_by_id``).

    Includes ``prompt`` (the scenario), ``initial_message`` (greetings),
    ``tags``, ``characters``, ``visibility`` — richer than ``get_chat_info``'s
    embedded ``story_data``. Returns ``{}`` on any failure (non-fatal).
    """
    try:
        data = trpc_query("agent.get_clank_story_by_id", {"scene_id": scene_id})
    except ClankError:
        return {}
    return data if isinstance(data, dict) else {}


def list_lorebooks(agent_id: str | None = None) -> list[dict[str, Any]]:
    """Return the viewer's lorebooks (``lorebook.list``).

    NOTE: this lists the *viewer's own* lorebooks, not a character creator's —
    clank injects creator lorebook entries into the prompt on keyword triggers
    rather than exposing them via a read endpoint, so they surface only in an
    echo whose recent messages hit their triggers. Usually ``[]`` when ripping
    someone else's character.
    """
    try:
        data = trpc_query("lorebook.list", {"agent_id": agent_id} if agent_id else {})
    except ClankError:
        return []
    return data if isinstance(data, list) else []


def get_lorebook(lorebook_id: str) -> dict[str, Any]:
    """Return one lorebook and its entries (``lorebook.get``)."""
    try:
        data = trpc_query("lorebook.get", {"lorebook_id": lorebook_id})
    except ClankError:
        return {}
    return data if isinstance(data, dict) else {}


def get_chat_messages(chat_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return the chat's messages (oldest first), from the paginated endpoint."""
    data = trpc_query(
        "agent.get_chat_messages_paginated", {"chat_id": chat_id, "limit": limit}
    )
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


# --------------------------------------------------------------------------- #
# Browse feed — list stories/characters newest-first (or by "trending")
# --------------------------------------------------------------------------- #

FEED_SORTS = ("new", "trending")


def list_stories(
    *,
    sort: str = "new",
    limit: int = 20,
    cursor: str | None = None,
    tags: list[str] | None = None,
    include_nsfw: bool = True,
    use_stricter_nsfw: bool = False,
) -> dict[str, Any]:
    """Return one page of the public browse feed (``agent.get_all_clank_stories``).

    ``sort="new"`` is newest-first; ``"trending"`` is clank's ranked feed. Returns
    ``{"items": [...], "nextCursor": str | None, "hasMore": bool}``. Pass the
    returned ``nextCursor`` back as ``cursor`` for the next page. ``tags`` filters
    by tag (ignored for the personalised "for you" tab, which this does not use).

    Each item is a *story/scene*: ``{id, agent_id, agent_username, agent_name,
    title, prompt, image_url, initial_message, tags, is_nsfw, possible_minor,
    characters, total_chats, active_chat_id, created_at, ...}``. ``prompt`` is the
    public scenario blurb — the gated character definition is NOT here (use the
    echo leak for that).
    """
    if sort not in FEED_SORTS:
        raise ClankError(f"sort must be one of {FEED_SORTS}, got {sort!r}")
    payload: dict[str, Any] = {
        "limit": max(1, int(limit)),
        "sort_by": sort,
        "include_nsfw": bool(include_nsfw),
        "use_stricter_nsfw": bool(use_stricter_nsfw),
        "direction": "forward",
    }
    if cursor:
        payload["cursor"] = cursor
    if tags:
        payload["selected_tags"] = list(tags)
    data = trpc_query("agent.get_all_clank_stories", payload)
    if not isinstance(data, dict):
        return {"items": [], "nextCursor": None, "hasMore": False}
    return {
        "items": data.get("items") or [],
        "nextCursor": data.get("nextCursor"),
        "hasMore": bool(data.get("hasMore")),
    }


def iter_stories(
    *,
    sort: str = "new",
    limit: int | None = None,
    page_size: int = 20,
    tags: list[str] | None = None,
    include_nsfw: bool = True,
):
    """Yield feed items across pages (newest-first for ``sort="new"``).

    Stops after ``limit`` items (``None`` = until the feed is exhausted),
    following ``nextCursor`` between pages.
    """
    yielded = 0
    cursor: str | None = None
    while True:
        page = list_stories(
            sort=sort, limit=page_size, cursor=cursor, tags=tags, include_nsfw=include_nsfw
        )
        for item in page["items"]:
            yield item
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        cursor = page["nextCursor"]
        if not page["hasMore"] or not cursor:
            return


def story_character_url(item: dict[str, Any]) -> str:
    """The character page URL for a feed item (``clank.world/@<agent_username>``)."""
    username = str(item.get("agent_username") or "").strip()
    return f"{CLANK_BASE}/@{username}" if username else CLANK_BASE


def fetch_avatar(image: str | None) -> str:
    """Download an avatar (URL or path) as a ``data:`` URI (or '' on failure)."""
    if not image:
        return ""
    url = image if str(image).startswith(("http://", "https://")) else f"{CLANK_BASE}/{str(image).lstrip('/')}"
    try:
        response = _get_client().get(
            url,
            headers={
                "User-Agent": CLANK_UA,
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                "Referer": f"{CLANK_ORIGIN}/",
            },
        )
        if response.is_error:
            return ""
        content = response.content
        if len(content) > MAX_IMAGE_BYTES:
            return ""
        content_type = response.headers.get("content-type") or "image/jpeg"
        return f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"
    except httpx.HTTPError:
        return ""


# --------------------------------------------------------------------------- #
# Echo parsing — pull the character definition out of the echoed prompt
# --------------------------------------------------------------------------- #

# The developer/system message opens with this and the generic clank RP rules
# begin at the boilerplate marker; everything between is the character body.
_CHAR_PREFIX = re.compile(r"^\s*You are the following character:\s*", re.I)
_BOILERPLATE_MARK = re.compile(
    r"\n\s*Your job is to stay fully in character at all times\.", re.I
)
_DLG_HEADER = re.compile(r"#+\s*DIALOGUE EXAMPLES", re.I)
_DLG_END = re.compile(r"End of dialogue examples", re.I)
_PLACEHOLDER_LINE = re.compile(r"\{\{user\}\}|\{\{char\}\}")


def _norm_ws(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def find_echo_body(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the newest echoed request body found in the chat messages.

    The echo proxy stores its reply as an assistant message whose ``content`` is
    the JSON request body clank sent (``{"model", "messages": [...]}``). We scan
    newest-first and return the first parseable one.
    """
    for msg in reversed(messages or []):
        content = (msg or {}).get("content")
        if not isinstance(content, str):
            continue
        stripped = content.strip()
        if not (stripped.startswith("{") and '"messages"' in stripped):
            continue
        try:
            body = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(body, dict) and isinstance(body.get("messages"), list):
            return body
    return None


def _system_message(body: dict[str, Any]) -> str:
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") in ("developer", "system"):
            return str(msg.get("content") or "")
    return ""


def _greeting_message(body: dict[str, Any]) -> str:
    """First assistant turn in the echoed prompt = the character's greeting."""
    for msg in body.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return str(msg.get("content") or "")
    return ""


def split_definition(system_prompt: str) -> dict[str, str]:
    """Split a clank system prompt into definition / example / boilerplate.

    * ``definition``  — the character body (description, appearance, personality,
      habits, speaking style): between ``You are the following character:`` and
      the generic-rules boilerplate marker.
    * ``example``     — the ``{{user}}``/``{{char}}`` lines under the
      ``## DIALOGUE EXAMPLES`` section.
    * ``boilerplate`` — the generic clank RP/formatting/NSFW rules and persona
      injection (identical across characters); kept for creator notes.
    """
    text = system_prompt or ""
    mark = _BOILERPLATE_MARK.search(text)
    if mark:
        definition = _CHAR_PREFIX.sub("", text[: mark.start()]).strip()
        boilerplate = text[mark.start() :].strip()
    else:
        definition = _CHAR_PREFIX.sub("", text).strip()
        boilerplate = ""

    example = ""
    header = _DLG_HEADER.search(text)
    if header:
        section = text[header.end() :]
        end = _DLG_END.search(section)
        if end:
            section = section[: end.start()]
        lines = [ln for ln in section.splitlines() if _PLACEHOLDER_LINE.search(ln)]
        example = "\n".join(lines).strip()

    return {"definition": definition, "example": example, "boilerplate": boilerplate}


# --------------------------------------------------------------------------- #
# Card assembly
# --------------------------------------------------------------------------- #


def _agent_and_story(info: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    agents = info.get("agent_data") if isinstance(info.get("agent_data"), list) else []
    agent = agents[0] if agents and isinstance(agents[0], dict) else {}
    story = info.get("story_data") if isinstance(info.get("story_data"), dict) else {}
    return agent, story


def _build_result(
    chat_id: str,
    url: str,
    info: dict[str, Any],
    parsed: dict[str, str] | None,
    greeting: str,
    *,
    scene: dict[str, Any] | None = None,
    keep_boilerplate: bool = False,
    leak_error: str = "",
) -> dict[str, Any]:
    agent, story = _agent_and_story(info)
    scene = scene or {}
    name = str(agent.get("name") or story.get("title") or "Unknown").strip()

    # Greetings: prefer the verbatim echo greeting for first_mes; take any extra
    # scene greetings as alternates.
    greetings = [g for g in (scene.get("initial_message") or story.get("initial_message") or []) if str(g).strip()]
    if not greeting:
        greeting = greetings[0] if greetings else ""
    alternates = [g for g in greetings if str(g).strip() and _norm_ws(g) != _norm_ws(greeting)]

    # Scenario: the scene's public setup prompt.
    scenario = str(scene.get("prompt") or story.get("description") or "").strip()

    # Tags from the scene.
    tags = scene.get("tags") if isinstance(scene.get("tags"), list) else []

    creator = agent.get("created_by") if isinstance(agent.get("created_by"), dict) else {}
    avatar = fetch_avatar(agent.get("image") or story.get("image"))

    notes_parts: list[str] = []

    if parsed:
        definition = parsed["definition"]
        example = parsed["example"]
        if keep_boilerplate and parsed.get("boilerplate"):
            notes_parts.append(f"--- clank system boilerplate ---\n{parsed['boilerplate']}")
        source = "clank-echo-leak"
    else:
        definition = ""
        example = ""
        notes_parts.append(
            "--- Note ---\nThe character definition is gated on clank.world and was not "
            "leaked; only public metadata (name, avatar, story blurb) is present. "
            "Configure the echo proxy on this chat and re-run with --leak."
        )
        source = "clank-partial"

    character = {
        "name": name,
        "avatarBase64": avatar,
        "description": definition,
        "personality": "",
        "scenario": scenario,
        "firstMessage": greeting,
        "alternateGreetings": alternates,
        "exampleMessages": example,
        "creatorNotes": "\n\n".join(notes_parts),
        "tags": [str(t) for t in tags],
        "definitionSource": source,
    }
    if parsed:
        character["reconstruction"] = {
            "method": "clank-echo-proxy",
            "chars": len(definition),
        }

    meta = {
        "name": name,
        "creator_name": str(creator.get("display_name") or creator.get("username") or "").strip(),
        "creator_id": str(creator.get("user_id") or ""),
        "is_nsfw": bool(scene.get("is_nsfw")),
        "showdefinition": bool(parsed),
    }

    return {
        "url": url if url.startswith(("http://", "https://")) else f"{CLANK_BASE}/chat/{chat_id}",
        "characterId": str(agent.get("id") or chat_id),
        "characterName": name,
        "character": character,
        "meta": meta,
        "publicLorebooks": [],
        "entries": [],
        "lorebookText": "",
        "leakRaw": _system_message_raw(parsed),
        "diagnostics": {
            "chatId": chat_id,
            "definitionChars": len(character["description"]),
            "exampleChars": len(character["exampleMessages"]),
            "scenarioChars": len(scenario),
            "tags": character["tags"],
            "alternateGreetings": len(alternates),
            "hasEcho": bool(parsed),
            "leakError": leak_error,
        },
    }


def _system_message_raw(parsed: dict[str, str] | None) -> str:
    if not parsed:
        return ""
    # Reconstruct a readable raw dump for the sidecar (.leak.txt).
    parts = [parsed["definition"]]
    if parsed.get("example"):
        parts.append("## DIALOGUE EXAMPLES\n" + parsed["example"])
    return "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# Auto-leak — set the proxy, trigger a generation, read the echo, restore
# --------------------------------------------------------------------------- #

# The message-send / generation (:func:`trigger_generation`) IS wired — it posts
# to clank's Vercel-AI-SDK endpoint ``/api/chat``. So the lorebook dump works on a
# chat that already has the echo proxy configured. What is NOT wired is the
# *provider-set* mutation (``agent.set_chat_llm_provider`` /
# ``upsert_user_llm_provider``), so ``--leak`` auto-configuration of a fresh chat
# still needs those captured; this flag gates the provider restore step.
_MUTATIONS_WIRED = False


def set_chat_llm_provider(
    chat_id: str, base_url: str = DEFAULT_ECHO_BASE_URL, model: str = "echo"
) -> dict[str, Any]:
    """Point the chat's custom LLM provider at ``base_url`` (returns prior settings).

    NOT YET WIRED — see ``_MUTATIONS_WIRED``. Needs the captured provider-set
    request.
    """
    del chat_id, base_url, model  # intentionally unused until the mutation is wired
    raise ClankError(
        "auto-leak is not wired yet: the 'set custom LLM provider' request must be "
        "captured from the browser first. For now, configure the echo proxy on the "
        "chat by hand, send one message, then run `rip clank extract` (no --leak)."
    )


def _last_assistant_id(chat_id: str) -> str | None:
    """The id of the most recent assistant message in the chat (branch point)."""
    for msg in reversed(get_chat_messages(chat_id)):
        if isinstance(msg, dict) and msg.get("message_type") == "assistant" and msg.get("id"):
            return str(msg["id"])
    return None


def trigger_generation(
    chat_id: str,
    message: str = "hi",
    *,
    chosen_last_assistant_id: str | None = None,
    timeout: int = 120,
) -> None:
    """Send ``message`` as a user turn to trigger a generation (the echo).

    Posts to clank's Vercel-AI-SDK endpoint ``/api/chat``; the response is a UI
    message stream which we drain to completion (that's when the assistant reply
    — here, the echoed request body — is persisted). The chat must already have a
    provider configured (the echo proxy for a leak). Needs the CSRF cookie.
    """
    if chosen_last_assistant_id is None:
        chosen_last_assistant_id = _last_assistant_id(chat_id)
    body = {
        "message": {
            "id": str(uuid.uuid4()),
            "role": "user",
            "parts": [{"type": "text", "text": str(message)}],
        },
        "id": chat_id,
        "selected_persona_id": None,
        "chat_id": chat_id,
        "use_draft_snapshot": False,
        "chosen_last_assistant_id": chosen_last_assistant_id,
    }
    headers = _headers(referer=f"{CLANK_ORIGIN}/chat/{chat_id}", json_body=True)
    if _trace_level >= 2:
        print(f"[clank-http] POST /api/chat (chat {chat_id})", flush=True)
    if _trace_level >= 3:
        print(f"[clank-http]   body: {_trace_preview(body)}", flush=True)
    try:
        response = _get_client().post("/api/chat", headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ClankError(f"network error sending message: {exc}") from exc
    if response.status_code in (401, 403):
        raise ClankError(
            "not authenticated for /api/chat — re-login with the CSRF token "
            "(`rip clank login --csrf-token …`)",
            response.status_code,
        )
    if response.is_error:
        raise ClankError(f"send failed: HTTP {response.status_code} {response.text[:200]}", response.status_code)
    _ = response.text  # drain the stream so the assistant echo is persisted


def _noop(_message: str) -> None:
    pass


def leak_chat_definition(
    chat_id: str,
    *,
    base_url: str = DEFAULT_ECHO_BASE_URL,
    message: str = "hi",
    timeout: int = 60,
    log: Callable[[str], None] = _noop,
) -> dict[str, Any] | None:
    """Auto-leak: set the proxy, trigger a generation, poll for the echo, restore.

    Returns the parsed echo body, or None. Restores the original provider in a
    ``finally``. Requires the write procedures to be wired.
    """
    prior = get_chat_llm_settings(chat_id)
    log("pointing chat LLM provider at the echo proxy …")
    set_chat_llm_provider(chat_id, base_url=base_url)
    try:
        log(f"sending trigger message ({message!r}) …")
        trigger_generation(chat_id, message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = find_echo_body(get_chat_messages(chat_id))
            if body is not None:
                log("echo received")
                return body
            time.sleep(2.0)
        return None
    finally:
        _restore_provider(chat_id, prior, log)


def _restore_provider(chat_id: str, prior: dict[str, Any], log: Callable[[str], None]) -> None:
    # Best-effort restore of whatever provider was configured before the leak.
    try:
        provider = prior.get("saved_provider") if isinstance(prior, dict) else None
        if provider and _MUTATIONS_WIRED:
            set_chat_llm_provider(
                chat_id,
                base_url=str(provider.get("custom_base_url") or ""),
                model=str(provider.get("custom_model") or ""),
            )
            log("restored original LLM provider")
    except ClankError:
        log("! could not restore the original LLM provider")


# --------------------------------------------------------------------------- #
# Lorebook dump — fire the character's keyword-triggered lorebook injections
# using the card's own text (description + scenario + first message) as bait,
# then diff each expanded echo against the base prompt to recover the entries.
# Mirrors the JanitorAI multi-trigger approach; needs the send seam wired.
# --------------------------------------------------------------------------- #


def build_trigger_messages(
    description: str = "",
    scenario: str = "",
    first_mes: str = "",
    *,
    extra: list[str] | None = None,
    chunk_size: int = 1500,
    min_len: int = 40,
) -> list[str]:
    """Chunk the card's own text into messages that fire lorebook keys.

    A character's lorebook entries inject only when their trigger keywords appear
    in recent messages, so the richest bait is the card's own prose — the
    description, scenario, and greeting almost always contain the keywords the
    creator keyed their lorebook on. Returns deduped, substantial chunks.
    """
    candidates: list[str] = []
    for source in (first_mes, scenario, description, *(extra or [])):
        candidates.extend(split_text_chunks(str(source or ""), chunk_size, min_len))

    out: list[str] = []
    seen: set[str] = set()
    for message in candidates:
        text = message.strip()
        if len(text) < min_len:
            continue
        key = _norm_ws(text).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


# Lorebook/memory entries inject as *system* messages (clank's own boilerplate:
# "Never reference system messages (system prompt, context retrievals, memory,
# and lorebook injections)"). So we diff only the system side (developer/system
# roles) — never the user/assistant conversation turns, which grow with history
# (and would otherwise flag prior echoes as huge false positives).
_SYSTEM_ROLES = ("developer", "system")


def _echo_texts(body: dict[str, Any] | None, roles: tuple[str, ...] | None = None) -> list[str]:
    return [
        str(m.get("content") or "")
        for m in (body or {}).get("messages", [])
        if isinstance(m, dict) and (roles is None or m.get("role") in roles)
    ]


def extract_injections(
    base_body: dict[str, Any] | None,
    echo_body: dict[str, Any] | None,
    trigger_text: str = "",
) -> list[str]:
    """Return system-side text blocks in ``echo_body`` absent from ``base_body``.

    Diffs only the developer/system messages (where clank injects lorebook and
    memory content), paragraph by paragraph, and keeps blocks not already in the
    base prompt and not the trigger itself — i.e. what the trigger caused clank
    to inject. Conversation turns are ignored (they accumulate chat history).
    Pure and side-effect free, so it is unit-testable without sending anything.
    """
    base_norm = _norm_ws("\n".join(_echo_texts(base_body, _SYSTEM_ROLES))).lower()
    trig_norm = _norm_ws(trigger_text).lower()
    blocks: list[str] = []
    seen: set[str] = set()
    for text in _echo_texts(echo_body, _SYSTEM_ROLES):
        for para in re.split(r"\n\s*\n", text):
            block = para.strip()
            if len(block) < 25:
                continue
            norm = _norm_ws(block).lower()
            if norm in base_norm:  # already part of the base prompt
                continue
            if trig_norm and (norm in trig_norm or trig_norm in norm):
                continue  # it's our own trigger message echoed back
            if norm in seen:
                continue
            seen.add(norm)
            blocks.append(block)
    return blocks


def dump_lorebook(
    chat_id: str,
    *,
    description: str = "",
    scenario: str = "",
    first_mes: str = "",
    base_body: dict[str, Any] | None = None,
    triggers: list[str] | None = None,
    sleep: float = 3.0,
    max_triggers: int | None = None,
    log: Callable[[str], None] = _noop,
) -> list[str]:
    """Recover a character's lorebook entries via keyword-triggered echoes.

    For each trigger message (built from the card text unless ``triggers`` is
    given) it sends the message, reads the freshly expanded echo, and diffs it
    against ``base_body`` to collect any injected blocks. Returns the deduped
    injected entries (empty if the character has no lorebook).

    Requires the send seam (:func:`trigger_generation`) to be wired; until then
    it raises with guidance. The pure diff core (:func:`extract_injections`) and
    trigger builder (:func:`build_trigger_messages`) work without it.
    """
    if base_body is None:
        base_body = find_echo_body(get_chat_messages(chat_id))
    if base_body is None:
        raise ClankError(
            "no base echo in this chat — set the echo proxy as the chat's LLM "
            "provider and send one message first",
            404,
        )
    if triggers is None:
        triggers = build_trigger_messages(description, scenario, first_mes)
    if max_triggers is not None:
        triggers = triggers[:max_triggers]

    entries: list[str] = []
    seen: set[str] = set()
    for i, trigger in enumerate(triggers, 1):
        log(f"lorebook trigger {i}/{len(triggers)} ({len(trigger)} chars) …")
        trigger_generation(chat_id, trigger)  # send seam — raises until wired
        time.sleep(sleep)
        body = find_echo_body(get_chat_messages(chat_id))
        for block in extract_injections(base_body, body, trigger):
            key = _norm_ws(block).lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append(block)
            log(f"  + injected block ({len(block)} chars)")
    return entries


# --------------------------------------------------------------------------- #
# Top-level extraction
# --------------------------------------------------------------------------- #


def extract_chat(
    url: str,
    *,
    leak: bool = False,
    keep_boilerplate: bool = False,
    echo_base_url: str = DEFAULT_ECHO_BASE_URL,
    trigger_message: str = "hi",
    leak_timeout: int = 60,
    with_lorebook: bool = False,
    lorebook_sleep: float = 3.0,
    max_triggers: int | None = None,
    log: Callable[[str], None] = _noop,
) -> dict[str, Any]:
    """Rip a clank.world chat into a RIPart ``result`` dict.

    Reads the chat's public metadata and, if an echoed prompt is present in the
    history (or ``leak`` triggers one), parses the verbatim character definition
    into the card. The result is shaped for ``helpers.save_to_library``.

    * ``leak`` — auto-configure the echo proxy and send a throwaway message to
      force an echo when the history has none (requires the write procedures to
      be wired; see :func:`leak_chat_definition`).
    * ``keep_boilerplate`` — keep the generic clank RP rules in creator notes.
    """
    if not has_session():
        raise ClankError("no clank.world session - run `rip clank login` first", 401)
    kind, ident = parse_target(url)
    if kind == "chat":
        chat_id = ident
    elif kind == "character":
        log(f"resolving character {ident!r} to an existing chat …")
        chat_id = resolve_character_chat(ident)
        if not chat_id:
            raise ClankError(
                f"no existing chat found for character '{ident}'. Open a chat with them "
                "on clank.world (set the echo proxy as the chat's LLM provider and send "
                "one message), then re-run — or pass the clank.world/chat/<id> URL.",
                404,
            )
        log(f"resolved to chat {chat_id}")
    else:
        raise ClankError(
            "not a clank.world chat or character URL "
            "(expected clank.world/chat/<uuid> or clank.world/@<slug>)",
            400,
        )

    info = get_chat_info(chat_id)
    # Richer scene data (scenario prompt, tags, greetings) when we have a scene id.
    scene_id = ((info.get("story_data") or {}).get("id")) if isinstance(info.get("story_data"), dict) else None
    scene = get_story(str(scene_id)) if scene_id else {}
    messages = get_chat_messages(chat_id)
    body = find_echo_body(messages)

    leak_error = ""
    if body is None and leak:
        try:
            body = leak_chat_definition(
                chat_id,
                base_url=echo_base_url,
                message=trigger_message,
                timeout=leak_timeout,
                log=log,
            )
            if body is None:
                leak_error = "no echo appeared before the timeout"
        except ClankError as exc:
            leak_error = str(exc)

    parsed = None
    greeting = ""
    if body is not None:
        system_prompt = _system_message(body)
        parsed = split_definition(system_prompt)
        greeting = _greeting_message(body)
        if not parsed["definition"]:
            # Echo present but the prompt didn't match the expected layout.
            leak_error = leak_error or "echoed prompt did not match the expected layout"

    result = _build_result(
        chat_id,
        url,
        info,
        parsed,
        greeting,
        scene=scene,
        keep_boilerplate=keep_boilerplate,
        leak_error=leak_error,
    )

    # Optional: fire the character's lorebook via keyword triggers built from the
    # card's own text, and fold the recovered entries into the result.
    if with_lorebook and body is not None and parsed:
        try:
            entries = dump_lorebook(
                chat_id,
                description=parsed["definition"],
                scenario=str(scene.get("prompt") or ""),
                first_mes=greeting,
                base_body=body,
                sleep=lorebook_sleep,
                max_triggers=max_triggers,
                log=log,
            )
            result["lorebookEntries"] = entries
            result["diagnostics"]["lorebookEntries"] = len(entries)
            if entries:
                note = "--- Recovered lorebook entries ---\n" + "\n\n".join(entries)
                character = result.get("character") or {}
                character["creatorNotes"] = (character.get("creatorNotes", "") + "\n\n" + note).strip()
        except ClankError as exc:
            result["diagnostics"]["lorebookError"] = str(exc)

    return result


def parse_scene_id(value: str) -> str | None:
    """Extract a scene UUID from a ``?scene=<uuid>`` URL, a story item, or bare UUID."""
    match = re.search(r"[?&]scene=(" + _UUID_RE + r")", value or "", re.I)
    if match:
        return match.group(1)
    bare = (value or "").strip()
    return bare if re.fullmatch(_UUID_RE, bare, re.I) else None


def extract_story(
    scene: str | dict[str, Any],
    *,
    log: Callable[[str], None] = _noop,
) -> dict[str, Any]:
    """Build a **partial** card from a scene's public data — no chat, no echo.

    Captures everything clank exposes publicly (name, scenario, greeting(s),
    tags, avatar, creator, sibling characters, audio flag) but NOT the gated
    character definition (``description`` stays empty, ``definitionSource`` is
    ``clank-partial``). Accepts a scene UUID, a ``?scene=<uuid>`` URL, or a feed
    ``item`` dict (from :func:`list_stories`). Use the echo leak on a chat to
    fill in the definition.
    """
    if isinstance(scene, dict):
        story = scene if scene.get("characters") is not None else {}
        scene_id = str(scene.get("id") or "")
        if not story and scene_id:
            story = get_story(scene_id)
    else:
        scene_id = parse_scene_id(scene) or str(scene)
        story = get_story(scene_id)
    if not story:
        raise ClankError(f"scene not found: {scene_id}", 404)

    chars = story.get("characters") if isinstance(story.get("characters"), list) else []
    primary = chars[0] if chars and isinstance(chars[0], dict) else {}
    name = str(primary.get("display_name") or story.get("title") or "Unknown").strip()
    log(f"partial card: {name!r} (scene {scene_id})")

    greetings = [g for g in (story.get("initial_message") or []) if str(g).strip()]
    scenario = str(story.get("prompt") or "").strip()
    tags = [str(t) for t in (story.get("tags") or [])]
    avatar = fetch_avatar(primary.get("image_url") or story.get("image_url"))

    creator = primary.get("created_by") if isinstance(primary.get("created_by"), dict) else {}
    if not creator:
        creator = {
            "user_id": story.get("created_by_user_id"),
            "username": story.get("created_by_username"),
            "display_name": story.get("created_by_display_name"),
        }

    notes_parts: list[str] = []
    others = [str(c.get("display_name")) for c in chars[1:] if isinstance(c, dict)]
    if others:
        notes_parts.append("Other characters in this scene: " + ", ".join(others))
    if story.get("audio"):
        notes_parts.append("This scene has narration/voice audio (not downloaded).")
    notes_parts.append(
        "--- Note ---\nCharacter definition is gated on clank.world and is NOT included "
        "here. Open a chat with this character (set the echo proxy as the chat's LLM "
        "provider, send one message), then run `rip clank extract` for the full definition."
    )

    character = {
        "name": name,
        "avatarBase64": avatar,
        "description": "",
        "personality": "",
        "scenario": scenario,
        "firstMessage": greetings[0] if greetings else "",
        "alternateGreetings": greetings[1:],
        "exampleMessages": "",
        "creatorNotes": "\n\n".join(notes_parts),
        "tags": tags,
        "definitionSource": "clank-partial",
    }
    character_id = str(primary.get("id") or scene_id)
    username = str(primary.get("username") or "").strip()
    return {
        "url": f"{CLANK_BASE}/@{username}" if username else f"{CLANK_BASE}/?scene={scene_id}",
        "characterId": character_id,
        "characterName": name,
        "character": character,
        "meta": {
            "name": name,
            "creator_name": str(creator.get("display_name") or creator.get("username") or "").strip(),
            "creator_id": str(creator.get("user_id") or ""),
            "is_nsfw": bool(story.get("is_nsfw")),
            "showdefinition": False,
        },
        "publicLorebooks": [],
        "entries": [],
        "lorebookText": "",
        "leakRaw": "",
        "diagnostics": {
            "sceneId": scene_id,
            "definitionChars": 0,
            "scenarioChars": len(scenario),
            "tags": tags,
            "alternateGreetings": max(0, len(greetings) - 1),
            "otherCharacters": others,
            "hasAudio": bool(story.get("audio")),
            "hasEcho": False,
        },
    }
