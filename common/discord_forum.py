"""Publish ripped cards to a Discord forum, one thread per character (UUID-keyed).

Every rip funnels through :func:`ripart.common.cards.save_to_library`, which calls
:func:`publish_card` here. Publishing is an *upsert* keyed on the character UUID,
which is embedded in the thread title (``<name> · <uuid>``, capped at Discord's
100-char forum-title limit):

* **UUID already has a thread** → post the fresh card as a new message in that
  thread (un-archiving it first if Discord auto-archived it). Re-running a rip
  therefore appends an updated copy instead of spawning a duplicate post. Set
  ``DISCORD_ON_DUPLICATE=skip`` to leave existing threads untouched.
* **UUID is new** → create a forum post titled ``<name> · <uuid>``, tagged with
  its platform tag plus up to four content tags, with the card PNG attached.

The dedup lookup lists the forum's *active and archived* threads (forum posts
auto-archive), recovering the UUID→thread map even for old posts. Everything is
best-effort: a Discord failure is swallowed so it can never break a rip. The
whole feature is off unless ``DISCORD_BOT_TOKEN`` (+ guild/forum ids) is set in
``.env``.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

# Project root (parent of this ``common/`` package) — where ``.env`` lives.
_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _ROOT / ".env"

_API = "https://discord.com/api/v10"
_UA = "ripart (https://github.com/, forum-archiver)"

# 36-char canonical UUID, used to recover the key from a thread title.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Forum-title hard cap and per-post applied-tag cap (both Discord limits).
_TITLE_CAP = 100
_MAX_APPLIED_TAGS = 5

# URL substring → platform tag name (matches the forum's platform tags).
_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("chub.ai", "chub"),
    ("character-tavern", "tavern"),
    ("saucepan", "saucepan"),
    ("clank.world", "clank"),
    ("spicychat", "spicychat"),
    ("janitor", "janitor"),
)

# Normalised card tag → forum content-tag name. Merges obvious variants so the
# ~1000 free-form card tags collapse onto the small curated forum set.
_CONTENT_TAGS: dict[str, str] = {
    "female": "Female",
    "romance": "Romance",
    "romantic": "Romance",
    "malepov": "MalePOV",
    "male pov": "MalePOV",
    "femalepov": "FemPOV",
    "fempov": "FemPOV",
    "female pov": "FemPOV",
    "transformation": "transformation",
    "transform": "transformation",
    "tf": "transformation",
    "oc": "OC",
    "original character": "OC",
    "cheating": "cheating",
    "ntr": "cheating",
    "roommate": "roommate",
    "nsfw": "NSFW",
    "smut": "NSFW",
    "submissive": "Submissive",
    "sub": "Submissive",
    "angst": "Angst",
    "comedy": "Comedy",
    "slowburn": "slowburn",
    "slow burn": "slowburn",
    "obsession": "obsessive",
    "obsessive": "obsessive",
    "multiple characters": "Multiple",
    "multiple": "Multiple",
}


def _load_env() -> None:
    """Read simple ``KEY=value`` lines from ``.env`` into ``os.environ`` (once).

    Only fills keys that are not already set, and only parses ``KEY=value`` lines
    so the pre-existing ``user:`` / ``pass:`` (colon-style) JanitorAI lines are
    left untouched. Idempotent and dependency-free (no python-dotenv).
    """
    if os.environ.get("_RIPART_ENV_LOADED"):
        return
    os.environ["_RIPART_ENV_LOADED"] = "1"
    try:
        text = _ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _platform_of(url: str) -> str | None:
    low = (url or "").lower()
    for needle, label in _PLATFORMS:
        if needle in low:
            return label
    return None


def _title_for(name: str, uuid: str) -> str:
    """``<name> · <uuid>`` truncated so the whole title fits the 100-char cap."""
    sep = " · "
    room = _TITLE_CAP - len(sep) - len(uuid)
    stem = (name or "card").strip()[: max(1, room)].rstrip()
    return f"{stem}{sep}{uuid}"


def _tags_for(
    card_tags: list[Any], platform: str | None, available: dict[str, str]
) -> list[str]:
    """Resolve applied-tag ids: platform tag + up to four matched content tags.

    ``available`` maps the forum's lower-cased tag name → tag id. Only tags that
    actually exist in the forum are returned, so the code never references a tag
    the forum doesn't have.
    """
    ids: list[str] = []
    if platform and platform.lower() in available:
        ids.append(available[platform.lower()])
    for raw in card_tags or []:
        name = _CONTENT_TAGS.get(str(raw).strip().lower())
        if not name:
            continue
        tag_id = available.get(name.lower())
        if tag_id and tag_id not in ids:
            ids.append(tag_id)
        if len(ids) >= _MAX_APPLIED_TAGS:
            break
    return ids


def _body_for(name: str, meta: dict[str, Any], url: str) -> str:
    """The message content posted alongside the card PNG (kept well under 2000)."""
    lines = [f"**{(name or 'Character').strip()}**"]
    creator = (meta or {}).get("creator_name")
    if creator:
        lines.append(f"by {creator}")
    if url:
        lines.append(f"<{url}>")
    return "\n".join(lines)[:1900]


class ForumPublisher:
    """A minimal Discord REST client scoped to one archive forum channel."""

    def __init__(
        self,
        *,
        token: str,
        guild_id: str,
        forum_id: str,
        on_duplicate: str = "repost",
    ) -> None:
        self.guild_id = guild_id
        self.forum_id = forum_id
        self.on_duplicate = (on_duplicate or "repost").lower()
        self._client = httpx.Client(
            base_url=_API,
            timeout=30,
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": _UA,
            },
        )
        self._available_tags: dict[str, str] | None = None

    # -- low-level request with 429 handling ------------------------------- #

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
        attempts: int = 4,
    ) -> httpx.Response:
        for attempt in range(attempts):
            resp = self._client.request(
                method, path, json=json_body, data=data, files=files
            )
            if resp.status_code == 429 and attempt < attempts - 1:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 1.0
                except ValueError:
                    delay = 1.0
                time.sleep(min(delay + 0.1, 10.0))
                continue
            return resp
        return resp  # pragma: no cover - loop returns on the last attempt

    # -- forum tag catalogue ---------------------------------------------- #

    def available_tags(self) -> dict[str, str]:
        """Lower-cased forum tag name → tag id (fetched once, then cached)."""
        if self._available_tags is None:
            resp = self._request("GET", f"/channels/{self.forum_id}")
            catalogue: dict[str, str] = {}
            if resp.status_code == 200:
                for tag in resp.json().get("available_tags") or []:
                    catalogue[str(tag.get("name", "")).lower()] = tag["id"]
            self._available_tags = catalogue
        return self._available_tags

    # -- UUID → thread discovery ------------------------------------------ #

    def thread_index(self) -> dict[str, str]:
        """Map every UUID currently in the forum to its thread id.

        Scans active threads plus every page of archived public threads (forum
        posts auto-archive), so re-running a rip finds the original thread no
        matter how old it is.
        """
        found: dict[str, str] = {}

        active = self._request("GET", f"/guilds/{self.guild_id}/threads/active")
        if active.status_code == 200:
            for thread in active.json().get("threads") or []:
                if thread.get("parent_id") == self.forum_id:
                    self._record(found, thread)

        before: str | None = None
        for _ in range(50):  # generous page cap (100 threads/page)
            path = f"/channels/{self.forum_id}/threads/archived/public?limit=100"
            if before:
                before_enc = httpx.QueryParams({"before": before})
                path = f"{path}&{before_enc}"
            page = self._request("GET", path)
            if page.status_code != 200:
                break
            body = page.json()
            threads = body.get("threads") or []
            for thread in threads:
                self._record(found, thread)
            if not body.get("has_more") or not threads:
                break
            before = (threads[-1].get("thread_metadata") or {}).get(
                "archive_timestamp"
            )
            if not before:
                break
        return found

    @staticmethod
    def _record(found: dict[str, str], thread: dict[str, Any]) -> None:
        match = _UUID_RE.search(thread.get("name") or "")
        if match:
            found.setdefault(match.group().lower(), thread["id"])

    # -- create / reply --------------------------------------------------- #

    def _png_file(self, png_path: Path) -> dict[str, Any]:
        return {"files[0]": (png_path.name, png_path.read_bytes(), "image/png")}

    def create_post(
        self,
        *,
        title: str,
        body: str,
        applied_tags: list[str],
        png_path: Path,
    ) -> dict[str, Any] | None:
        payload = {
            "name": title,
            "applied_tags": applied_tags,
            "message": {"content": body},
        }
        resp = self._request(
            "POST",
            f"/channels/{self.forum_id}/threads",
            data={"payload_json": json.dumps(payload)},
            files=self._png_file(png_path),
        )
        if resp.status_code in (200, 201):
            return resp.json()
        raise RuntimeError(f"create_post HTTP {resp.status_code}: {resp.text[:200]}")

    def reply(self, thread_id: str, *, body: str, png_path: Path) -> dict[str, Any]:
        # A forum post auto-archives; un-archive before appending so the bump
        # sticks (a webhook couldn't wake an archived thread, but the bot can).
        self._request("PATCH", f"/channels/{thread_id}", json_body={"archived": False})
        resp = self._request(
            "POST",
            f"/channels/{thread_id}/messages",
            data={"payload_json": json.dumps({"content": body})},
            files=self._png_file(png_path),
        )
        if resp.status_code in (200, 201):
            return resp.json()
        raise RuntimeError(f"reply HTTP {resp.status_code}: {resp.text[:200]}")

    # -- the upsert ------------------------------------------------------- #

    def upsert(
        self,
        *,
        uuid: str,
        name: str,
        url: str,
        card_tags: list[Any],
        meta: dict[str, Any],
        png_path: Path,
        thread_index: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Find-or-create the thread for ``uuid`` and (re)post the card.

        Pass a pre-built ``thread_index`` when publishing many cards in a loop so
        the forum listing is fetched once, not once per card.
        """
        key = uuid.lower()
        index = self.thread_index() if thread_index is None else thread_index
        existing = index.get(key)
        body = _body_for(name, meta, url)

        if existing:
            if self.on_duplicate == "skip":
                return {"action": "skip", "thread_id": existing, "uuid": uuid}
            self.reply(existing, body=body, png_path=png_path)
            return {"action": "repost", "thread_id": existing, "uuid": uuid}

        platform = _platform_of(url)
        applied = _tags_for(card_tags, platform, self.available_tags())
        created = self.create_post(
            title=_title_for(name, uuid),
            body=body,
            applied_tags=applied,
            png_path=png_path,
        )
        thread_id = (created or {}).get("id")
        if thread_index is not None and thread_id:
            thread_index[key] = thread_id  # keep the shared map fresh in a batch
        return {"action": "create", "thread_id": thread_id, "uuid": uuid}


def _publisher_from_env() -> ForumPublisher | None:
    """Build a publisher from ``.env`` config, or ``None`` when disabled."""
    _load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild = os.environ.get("DISCORD_GUILD_ID", "").strip()
    forum = os.environ.get("DISCORD_FORUM_CHANNEL_ID", "").strip()
    if not (token and guild and forum):
        return None
    return ForumPublisher(
        token=token,
        guild_id=guild,
        forum_id=forum,
        on_duplicate=os.environ.get("DISCORD_ON_DUPLICATE", "repost"),
    )


def publish_card(
    character_id: str, result: dict[str, Any], png_path: Path
) -> dict[str, Any] | None:
    """Best-effort forum publish for one just-saved card.

    Returns the upsert outcome (``{"action", "thread_id", "uuid"}``), or ``None``
    when publishing is disabled or the UUID is unusable. Never raises — a Discord
    outage must not break a rip; set ``RIP_DEBUG`` to surface the reason.
    """
    if not (character_id and _UUID_RE.fullmatch(character_id)):
        return None  # only real UUIDs get a stable, dedupable title
    try:
        publisher = _publisher_from_env()
        if publisher is None:
            return None
        character = result.get("character") or {}
        return publisher.upsert(
            uuid=character_id,
            name=result.get("characterName") or character.get("name") or "card",
            url=result.get("url") or "",
            card_tags=character.get("tags") or [],
            meta=result.get("meta") or {},
            png_path=Path(png_path),
        )
    except Exception as exc:  # noqa: BLE001 - publishing is best-effort
        if os.environ.get("RIP_DEBUG"):
            print(f"[discord] publish skipped: {exc}", flush=True)
        return None


def publish_library(library_dir: Path) -> list[dict[str, Any]]:
    """Backfill: publish every card in ``index.json`` (one forum listing, reused).

    Use this to seed the forum from an existing library. Re-runnable — the UUID
    upsert means already-posted cards are updated (or skipped), not duplicated.
    """
    publisher = _publisher_from_env()
    if publisher is None:
        raise RuntimeError("Discord publishing is not configured in .env")
    index_path = Path(library_dir) / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    shared = publisher.thread_index()
    outcomes: list[dict[str, Any]] = []
    for uuid, entry in index.items():
        if not _UUID_RE.fullmatch(uuid):
            continue
        png = Path(library_dir) / entry.get("file", f"{uuid}.png")
        if not png.exists():
            continue
        try:
            outcomes.append(
                publisher.upsert(
                    uuid=uuid,
                    name=entry.get("name") or "card",
                    url=entry.get("url") or "",
                    card_tags=entry.get("tags") or [],
                    meta={
                        "creator_name": entry.get("creator") or "",
                        "is_nsfw": entry.get("nsfw"),
                    },
                    png_path=png,
                    thread_index=shared,
                )
            )
        except Exception as exc:  # noqa: BLE001 - keep going through the library
            outcomes.append({"action": "error", "uuid": uuid, "error": str(exc)})
    return outcomes
