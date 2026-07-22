"""Publish ripped cards and lorebooks to their respective Discord forums.

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

The character archive uses ``DISCORD_FORUM_CHANNEL_ID``.  The optional separate
lorebook archive uses ``DISCORD_LOREBOOK_FORUM_CHANNEL_ID`` and upserts the
durable lorebook-library JSON after every extraction.  Both look through active
and archived threads, and both are best-effort so a Discord outage never breaks
a rip.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .cards import build_character_book
from .env import env_paths as _env_paths, load_env as _load_env

_LOG = logging.getLogger(__name__)

_API = "https://discord.com/api/v10"
_UA = "ripart (https://github.com/, forum-archiver)"

# 36-char canonical UUID, used to recover the key from a thread title.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_LOREBOOK_MARKER_RE = re.compile(r"ripart-lorebook:([0-9a-f]{64})")

# Forum-title hard cap and per-post applied-tag cap (both Discord limits).
_TITLE_CAP = 100
_MAX_APPLIED_TAGS = 5
# Discord embed limits. Keep these next to the message-level caps so changes to
# the presentation cannot accidentally produce an Invalid Form Body response.
_EMBED_TITLE_CAP = 256
_EMBED_DESCRIPTION_CAP = 4096
_EMBED_FIELD_CAP = 25
_EMBED_FIELD_NAME_CAP = 256
_EMBED_FIELD_VALUE_CAP = 1024
_EMBED_FOOTER_CAP = 2048
_EMBED_AUTHOR_CAP = 256
_EMBEDS_PER_MESSAGE_CAP = 10
_EMBED_MESSAGE_CHAR_CAP = 6000
_FILES_PER_MESSAGE_CAP = 10

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


def _embed_for(
    *,
    name: str,
    url: str,
    card_tags: list[Any],
    meta: dict[str, Any],
    definition_source: str,
    image_filename: str,
) -> dict[str, Any]:
    """Build the readable forum-card embed posted with the downloadable PNG."""
    clean_name = (name or "Character").strip()[:_EMBED_TITLE_CAP]
    platform = _platform_of(url) or "unknown"
    tags = ", ".join(str(tag).strip() for tag in card_tags if str(tag).strip())
    source = (definition_source or "unknown").strip()
    fields = [
        {"name": "Platform", "value": platform, "inline": True},
        {
            "name": "NSFW",
            "value": "yes" if (meta or {}).get("is_nsfw") else "no",
            "inline": True,
        },
        {
            "name": "Definition",
            "value": source[:_EMBED_FIELD_VALUE_CAP],
            "inline": True,
        },
    ]
    if url:
        fields.append(
            {"name": "Source", "value": url[:_EMBED_FIELD_VALUE_CAP], "inline": False}
        )
    if tags:
        fields.append(
            {
                "name": f"Card tags ({len(card_tags)})",
                "value": tags[:_EMBED_FIELD_VALUE_CAP],
                "inline": False,
            }
        )
    creator = (meta or {}).get("creator_name")
    if creator:
        fields.append(
            {
                "name": "Creator",
                "value": str(creator)[:_EMBED_FIELD_VALUE_CAP],
                "inline": True,
            }
        )
    return {
        "title": clean_name,
        "url": url or None,
        "fields": fields,
        "image": {"url": f"attachment://{image_filename}"},
        "footer": {"text": "ripart archive"},
    }


def _split_embed_text(text: str) -> list[str]:
    """Split text into Discord-safe embed descriptions, favouring line breaks."""
    text = str(text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > _EMBED_DESCRIPTION_CAP:
        cut = remaining.rfind("\n", 0, _EMBED_DESCRIPTION_CAP + 1)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, _EMBED_DESCRIPTION_CAP + 1)
        if cut <= 0:
            cut = _EMBED_DESCRIPTION_CAP
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _detail_embeds_for(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Render recovered card fields (but not lorebooks) as follow-up embeds."""
    character = result.get("character") or {}
    sections: list[tuple[str, str]] = []
    for label, key in (
        ("Definition", "description"),
        ("Personality", "personality"),
        ("Scenario", "scenario"),
        ("First message", "firstMessage"),
        ("Example dialogue", "exampleMessages"),
        ("Creator notes", "creatorNotes"),
    ):
        value = str(character.get(key) or "").strip()
        if value:
            sections.append((label, value))

    embeds: list[dict[str, Any]] = []
    for title, content in sections:
        chunks = _split_embed_text(content)
        for index, chunk in enumerate(chunks, start=1):
            chunk_title = (
                title if len(chunks) == 1 else f"{title} ({index}/{len(chunks)})"
            )
            embeds.append(
                {
                    "title": chunk_title[:_EMBED_TITLE_CAP],
                    "description": chunk,
                    "footer": {"text": "ripart archive"},
                }
            )
    return embeds


def _lorebook_files_for(
    character_id: str, result: dict[str, Any]
) -> list[tuple[str, bytes]]:
    """Return importable SillyTavern character-book JSON attachments.

    Recovered private lore has no original trigger keys, so it is exported as one
    disabled review book. Public books retain their real keys and each gets its
    own file. This keeps large lorebooks readable and avoids flooding a forum
    thread with one embed per entry.
    """
    files: list[tuple[str, bytes]] = []

    def _encode(filename: str, book: dict[str, Any] | None) -> None:
        if not book or not book.get("entries"):
            return
        files.append(
            (
                filename,
                json.dumps(book, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        )

    recovered_entries: list[str] = []
    for entry in result.get("entries") or []:
        if isinstance(entry, dict):
            entry = entry.get("content") or entry.get("text") or ""
        text = str(entry or "").strip()
        if text:
            recovered_entries.append(text)
    recovered = build_character_book(recovered_entries)
    _encode(f"{character_id}-recovered-lorebook.json", recovered)

    for index, public_book in enumerate(result.get("publicLorebooks") or [], start=1):
        if not isinstance(public_book, dict):
            continue
        book = build_character_book(None, [public_book])
        if book:
            book["name"] = str(
                public_book.get("title")
                or public_book.get("name")
                or f"Public book {index}"
            )
        _encode(f"{character_id}-public-lorebook-{index}.json", book)

    return files


def _lorebook_file_batches(
    files: list[tuple[str, bytes]],
) -> list[list[tuple[str, bytes]]]:
    """Split attachments to Discord's ten-files-per-message limit."""
    return [
        files[index : index + _FILES_PER_MESSAGE_CAP]
        for index in range(0, len(files), _FILES_PER_MESSAGE_CAP)
    ]


def _embed_char_count(embed: dict[str, Any]) -> int:
    """Count the text Discord includes in its 6000-character embed budget."""
    footer = embed.get("footer") or {}
    author = embed.get("author") or {}
    return sum(
        len(str(value or ""))
        for value in (
            embed.get("title"),
            embed.get("description"),
            footer.get("text"),
            author.get("name"),
        )
    ) + sum(
        len(str(field.get("name") or "")) + len(str(field.get("value") or ""))
        for field in embed.get("fields") or []
    )


def _detail_embed_batches(embeds: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Pack detail embeds under Discord's per-message count and text limits."""
    batches: list[list[dict[str, Any]]] = []
    batch: list[dict[str, Any]] = []
    char_count = 0
    for embed in embeds:
        size = _embed_char_count(embed)
        if batch and (
            len(batch) >= _EMBEDS_PER_MESSAGE_CAP
            or char_count + size > _EMBED_MESSAGE_CHAR_CAP
        ):
            batches.append(batch)
            batch, char_count = [], 0
        batch.append(embed)
        char_count += size
    if batch:
        batches.append(batch)
    return batches


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
        import random as _random

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
                backoff = min(delay + (2**attempt) + _random.uniform(0, 0.5), 30.0)
                _LOG.warning(
                    "rate limited (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    attempts,
                    backoff,
                )
                time.sleep(backoff)
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

    def _thread_index(self, pattern: re.Pattern[str]) -> dict[str, str]:
        """Map marker keys in active and archived threads to thread ids.

        Forum posts auto-archive, so both listings are needed for a real upsert.
        """
        found: dict[str, str] = {}

        active = self._request("GET", f"/guilds/{self.guild_id}/threads/active")
        if active.status_code == 200:
            for thread in active.json().get("threads") or []:
                if thread.get("parent_id") == self.forum_id:
                    self._record(found, thread, pattern)

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
                self._record(found, thread, pattern)
            if not body.get("has_more") or not threads:
                break
            before = (threads[-1].get("thread_metadata") or {}).get("archive_timestamp")
            if not before:
                break
        return found

    def thread_index(self) -> dict[str, str]:
        """Map character UUIDs currently in the forum to their thread ids."""
        return self._thread_index(_UUID_RE)

    def lorebook_thread_index(self) -> dict[str, str]:
        """Map stable lorebook markers currently in the forum to thread ids."""
        return self._thread_index(_LOREBOOK_MARKER_RE)

    @staticmethod
    def _record(
        found: dict[str, str], thread: dict[str, Any], pattern: re.Pattern[str]
    ) -> None:
        match = pattern.search(thread.get("name") or "")
        if match:
            key = (match.group(1) if match.lastindex else match.group()).lower()
            found.setdefault(key, thread["id"])

    # -- create / reply --------------------------------------------------- #

    def _png_file(self, png_path: Path) -> dict[str, Any]:
        return {"files[0]": (png_path.name, png_path.read_bytes(), "image/png")}

    @staticmethod
    def _json_files(files: list[tuple[str, bytes]]) -> dict[str, Any]:
        return {
            f"files[{index}]": (filename, content, "application/json")
            for index, (filename, content) in enumerate(files)
        }

    def create_post(
        self,
        *,
        title: str,
        embed: dict[str, Any],
        applied_tags: list[str],
        png_path: Path,
    ) -> dict[str, Any] | None:
        """Create a forum post with the card PNG and summary embed."""
        payload = {
            "name": title,
            "applied_tags": applied_tags,
            "message": {"embeds": [embed]},
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

    def create_file_post(
        self,
        *,
        title: str,
        embed: dict[str, Any],
        filename: str,
        content: bytes,
    ) -> dict[str, Any] | None:
        """Create a forum post with one JSON attachment and its summary embed."""
        payload = {"name": title, "message": {"embeds": [embed]}}
        resp = self._request(
            "POST",
            f"/channels/{self.forum_id}/threads",
            data={"payload_json": json.dumps(payload)},
            files=self._json_files([(filename, content)]),
        )
        if resp.status_code in (200, 201):
            return resp.json()
        raise RuntimeError(
            f"create_file_post HTTP {resp.status_code}: {resp.text[:200]}"
        )

    def reply(
        self, thread_id: str, *, embed: dict[str, Any], png_path: Path
    ) -> dict[str, Any]:
        # A forum post auto-archives; un-archive before appending so the bump
        # sticks (a webhook couldn't wake an archived thread, but the bot can).
        self._request("PATCH", f"/channels/{thread_id}", json_body={"archived": False})
        resp = self._request(
            "POST",
            f"/channels/{thread_id}/messages",
            data={"payload_json": json.dumps({"embeds": [embed]})},
            files=self._png_file(png_path),
        )
        if resp.status_code in (200, 201):
            return resp.json()
        raise RuntimeError(f"reply HTTP {resp.status_code}: {resp.text[:200]}")

    def reply_embeds(
        self, thread_id: str, *, embeds: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Post a Discord-limit-safe batch of detail embeds after the card."""
        resp = self._request(
            "POST",
            f"/channels/{thread_id}/messages",
            json_body={"embeds": embeds},
        )
        if resp.status_code in (200, 201):
            return resp.json()
        raise RuntimeError(f"detail reply HTTP {resp.status_code}: {resp.text[:200]}")

    def reply_lorebook_files(
        self, thread_id: str, *, files: list[tuple[str, bytes]]
    ) -> dict[str, Any]:
        """Attach the card's lorebook exports as JSON files in one reply."""
        if not files:
            return {}
        resp = self._request(
            "POST",
            f"/channels/{thread_id}/messages",
            data={
                "payload_json": json.dumps(
                    {
                        "content": "Lorebook JSON export(s), ready to import into SillyTavern.",
                    }
                )
            },
            files=self._json_files(files),
        )
        if resp.status_code in (200, 201):
            return resp.json()
        raise RuntimeError(
            f"lorebook attachment reply HTTP {resp.status_code}: {resp.text[:200]}"
        )

    def reply_lorebook_record(
        self,
        thread_id: str,
        *,
        embed: dict[str, Any],
        filename: str,
        content: bytes,
    ) -> dict[str, Any]:
        """Append one versioned lorebook-library record to its forum thread."""
        self._request("PATCH", f"/channels/{thread_id}", json_body={"archived": False})
        resp = self._request(
            "POST",
            f"/channels/{thread_id}/messages",
            data={"payload_json": json.dumps({"embeds": [embed]})},
            files=self._json_files([(filename, content)]),
        )
        if resp.status_code in (200, 201):
            return resp.json()
        raise RuntimeError(
            f"lorebook record reply HTTP {resp.status_code}: {resp.text[:200]}"
        )

    # -- the upsert ------------------------------------------------------- #

    def upsert(
        self,
        *,
        uuid: str,
        name: str,
        url: str,
        card_tags: list[Any],
        meta: dict[str, Any],
        definition_source: str = "",
        detail_embeds: list[dict[str, Any]] | None = None,
        lorebook_files: list[tuple[str, bytes]] | None = None,
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
        embed = _embed_for(
            name=name,
            url=url,
            card_tags=card_tags,
            meta=meta,
            definition_source=definition_source,
            image_filename=png_path.name,
        )

        if existing:
            if self.on_duplicate == "skip":
                return {"action": "skip", "thread_id": existing, "uuid": uuid}
            self.reply(existing, embed=embed, png_path=png_path)
            for batch in _lorebook_file_batches(lorebook_files or []):
                self.reply_lorebook_files(existing, files=batch)
            for batch in _detail_embed_batches(detail_embeds or []):
                self.reply_embeds(existing, embeds=batch)
            return {"action": "repost", "thread_id": existing, "uuid": uuid}

        platform = _platform_of(url)
        applied = _tags_for(card_tags, platform, self.available_tags())
        created = self.create_post(
            title=_title_for(name, uuid),
            embed=embed,
            applied_tags=applied,
            png_path=png_path,
        )
        thread_id = (created or {}).get("id")
        if thread_id:
            for batch in _lorebook_file_batches(lorebook_files or []):
                self.reply_lorebook_files(thread_id, files=batch)
            for batch in _detail_embed_batches(detail_embeds or []):
                self.reply_embeds(thread_id, embeds=batch)
        if thread_index is not None and thread_id:
            thread_index[key] = thread_id  # keep the shared map fresh in a batch
        return {"action": "create", "thread_id": thread_id, "uuid": uuid}

    def upsert_lorebook(
        self,
        *,
        key: str,
        title: str,
        record: dict[str, Any],
        filename: str,
        thread_index: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Upsert one durable lorebook record, retaining each extraction version."""
        marker = hashlib.sha256(key.encode("utf-8")).hexdigest()
        index = self.lorebook_thread_index() if thread_index is None else thread_index
        record_bytes = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
        entry_count = record.get("entryCount", 0)
        fields = [
            {
                "name": "Source",
                "value": str(record.get("source") or "unknown"),
                "inline": True,
            },
            {"name": "Entries", "value": str(entry_count), "inline": True},
            {
                "name": "Updated",
                "value": str(record.get("updatedAt") or "unknown"),
                "inline": False,
            },
        ]
        if record.get("sourceLorebookId"):
            fields.append(
                {
                    "name": "Provider lorebook ID",
                    "value": str(record["sourceLorebookId"]),
                    "inline": False,
                }
            )
        if record.get("characterIds"):
            fields.append(
                {
                    "name": "Captured characters",
                    "value": str(len(record["characterIds"])),
                    "inline": True,
                }
            )
        embed = {
            "title": title[:_EMBED_TITLE_CAP],
            "description": "Latest extracted lorebook record is attached as JSON.",
            "fields": fields,
            "footer": {"text": "ripart lorebook archive"},
        }
        thread_title = _title_for(title, f"ripart-lorebook:{marker}")
        existing = index.get(marker)
        if existing:
            if self.on_duplicate == "skip":
                return {"action": "skip", "thread_id": existing, "key": key}
            self.reply_lorebook_record(
                existing, embed=embed, filename=filename, content=record_bytes
            )
            return {"action": "repost", "thread_id": existing, "key": key}
        created = self.create_file_post(
            title=thread_title, embed=embed, filename=filename, content=record_bytes
        )
        thread_id = (created or {}).get("id")
        if thread_index is not None and thread_id:
            thread_index[marker] = thread_id
        return {"action": "create", "thread_id": thread_id, "key": key}


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


def _lorebook_publisher_from_env() -> ForumPublisher | None:
    """Build the optional publisher dedicated to durable lorebook records."""
    _load_env()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild = os.environ.get("DISCORD_GUILD_ID", "").strip()
    forum = os.environ.get("DISCORD_LOREBOOK_FORUM_CHANNEL_ID", "").strip()
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
    when publishing is disabled or the UUID is unusable. An attempted but failed
    publish returns ``{"action": "error", "error": ...}``; it never raises, so a
    Discord outage cannot break a rip or hide the failure from the CLI.
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
            definition_source=character.get("definitionSource") or "",
            detail_embeds=_detail_embeds_for(result),
            png_path=Path(png_path),
        )
    except Exception as exc:  # noqa: BLE001 - publishing is best-effort
        return {"action": "error", "uuid": character_id, "error": str(exc)}


def publish_saved_card(
    character_id: str, result: dict[str, Any], paths: dict[str, Any]
) -> dict[str, Any]:
    """Explicit post-save event that publishes a card and its lorebooks."""
    outcome = publish_card(character_id, result, Path(paths["png"]))
    if outcome and outcome.get("thread_id"):
        paths["discord_thread"] = outcome["thread_id"]
    elif outcome and outcome.get("action") == "error":
        paths["discord_error"] = outcome.get("error") or "unknown Discord error"
    publish_lorebooks(paths.get("lorebooks") or [])
    return paths


def publish_lorebooks(lorebook_paths: list[str]) -> list[dict[str, Any]]:
    """Best-effort upsert of changed library lorebooks to their dedicated forum.

    ``update_lorebook_library`` returns both provider-lorebook records and
    private/unassigned observation records.  Both are useful extraction evidence,
    so each gets a stable thread and every later retrieval becomes a new reply.
    """
    publisher = _lorebook_publisher_from_env()
    if publisher is None:
        return []
    outcomes: list[dict[str, Any]] = []
    try:
        shared = publisher.lorebook_thread_index()
    except Exception as exc:  # noqa: BLE001 - preserve extraction on Discord failure
        return [{"action": "error", "error": str(exc)}]
    for raw_path in lorebook_paths:
        path = Path(raw_path)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError("lorebook record is not a JSON object")
            if _is_fully_attributed_observation_record(path, record):
                # The corresponding provider record already contains these
                # recovered observations. Publishing this local audit file too
                # would create a misleading second thread for one lorebook.
                continue
            source = str(record.get("source") or path.parent.name or "unknown")
            source_id = str(record.get("sourceLorebookId") or "").strip()
            key = f"{source}:{source_id or path.stem}"
            title = str(record.get("title") or "Private observations").strip()
            filename = f"{_safe_filename(source)}-{_safe_filename(source_id or path.stem)}.json"
            outcomes.append(
                publisher.upsert_lorebook(
                    key=key,
                    title=f"{source}: {title}",
                    record=record,
                    filename=filename,
                    thread_index=shared,
                )
            )
        except Exception as exc:  # noqa: BLE001 - continue through changed records
            outcomes.append({"action": "error", "path": str(path), "error": str(exc)})
    return outcomes


def _is_fully_attributed_observation_record(path: Path, record: dict[str, Any]) -> bool:
    """Whether an unassigned audit file is wholly represented by a book record."""
    if path.parent.name != "unassigned":
        return False
    observations = record.get("observations")
    if not isinstance(observations, list) or not observations:
        return False
    for observation in observations:
        attribution = (
            observation.get("attribution") if isinstance(observation, dict) else None
        )
        candidates = (
            attribution.get("candidates") if isinstance(attribution, dict) else None
        )
        if not (
            isinstance(attribution, dict)
            and attribution.get("status") == "inferred"
            and isinstance(candidates, list)
            and len(candidates) == 1
        ):
            return False
    return True


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "lorebook"


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
                    definition_source=entry.get("definitionSource") or "",
                    png_path=png,
                    thread_index=shared,
                )
            )
        except Exception as exc:  # noqa: BLE001 - keep going through the library
            outcomes.append({"action": "error", "uuid": uuid, "error": str(exc)})
    return outcomes
