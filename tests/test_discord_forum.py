"""Pure tests for the Discord forum-card presentation."""

from __future__ import annotations

import os

from ripart.common import discord_forum, env
from ripart.common.cards import save_to_library
from ripart.common.discord_forum import (
    ForumPublisher,
    _detail_embed_batches,
    _detail_embeds_for,
    _embed_char_count,
    _embed_for,
    _lorebook_file_batches,
    _lorebook_files_for,
)


def test_load_env_checks_cwd_and_project_dotenv_files_without_overwriting_env(
    monkeypatch, tmp_path
):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".env").write_text(
        "DISCORD_BOT_TOKEN=project-token\nDISCORD_GUILD_ID=123\n", encoding="utf-8"
    )
    working_dir = project_dir / "nested" / "working"
    working_dir.mkdir(parents=True)
    (working_dir / ".env").write_text(
        "DISCORD_BOT_TOKEN=cwd-token\nDISCORD_COMMAND_CHANNEL_ID=456\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(working_dir)
    monkeypatch.setattr(env, "_PROJECT_ROOT", project_dir)
    monkeypatch.delenv("_RIPART_ENV_LOADED", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    monkeypatch.setenv("DISCORD_COMMAND_CHANNEL_ID", "from-environment")

    discord_forum._load_env()

    assert os.environ["DISCORD_BOT_TOKEN"] == "cwd-token"
    assert os.environ["DISCORD_GUILD_ID"] == "123"
    assert os.environ["DISCORD_COMMAND_CHANNEL_ID"] == "from-environment"


def test_embed_for_includes_card_metadata_and_attachment_image():
    embed = _embed_for(
        name="Talia & Friends",
        url="https://spicychat.ai/chatbot/character-id",
        card_tags=["NSFW", "Roommate", "Female"],
        meta={"is_nsfw": True, "creator_name": "Creator"},
        definition_source="spicychat-leak",
        image_filename="character-id.png",
    )

    assert embed["title"] == "Talia & Friends"
    assert embed["url"] == "https://spicychat.ai/chatbot/character-id"
    assert embed["image"] == {"url": "attachment://character-id.png"}
    assert embed["footer"] == {"text": "ripart archive"}
    assert embed["fields"] == [
        {"name": "Platform", "value": "spicychat", "inline": True},
        {"name": "NSFW", "value": "yes", "inline": True},
        {"name": "Definition", "value": "spicychat-leak", "inline": True},
        {
            "name": "Source",
            "value": "https://spicychat.ai/chatbot/character-id",
            "inline": False,
        },
        {
            "name": "Card tags (3)",
            "value": "NSFW, Roommate, Female",
            "inline": False,
        },
        {"name": "Creator", "value": "Creator", "inline": True},
    ]


def test_publish_card_reports_discord_errors(monkeypatch, tmp_path):
    """A failed publish is visible to callers but never aborts card saving."""
    png_path = tmp_path / "card.png"
    png_path.write_bytes(b"png")

    class BrokenPublisher:
        def upsert(self, **_kwargs):
            raise RuntimeError("Discord rejected the payload")

    monkeypatch.setattr(discord_forum, "_publisher_from_env", lambda: BrokenPublisher())
    outcome = discord_forum.publish_card(
        "c49da2b4-2b9c-479a-a99e-2b979cc22f82",
        {"character": {}},
        png_path,
    )

    assert outcome == {
        "action": "error",
        "uuid": "c49da2b4-2b9c-479a-a99e-2b979cc22f82",
        "error": "Discord rejected the payload",
    }


def test_saving_a_card_does_not_publish_to_discord(monkeypatch, tmp_path):
    """Persistence is local; callers opt into forum publishing separately."""
    monkeypatch.setattr(
        discord_forum,
        "publish_card",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("published")),
    )

    paths = save_to_library(
        tmp_path,
        "card-1",
        {"character": {"name": "Card"}, "entries": []},
    )

    assert paths == {"png": str(tmp_path / "card-1.png")}


def test_publish_lorebooks_uses_the_dedicated_publisher(monkeypatch, tmp_path):
    record_path = tmp_path / "lorebooks" / "janitor" / "book-42.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        discord_forum.json.dumps(
            {
                "source": "janitor",
                "sourceLorebookId": "book-42",
                "title": "Shared book",
                "entryCount": 3,
                "updatedAt": "2026-07-20T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    captured: dict = {}

    class LorebookPublisher:
        def lorebook_thread_index(self):
            return {}

        def upsert_lorebook(self, **kwargs):
            captured.update(kwargs)
            return {"action": "create", "thread_id": "lorebook-thread"}

    monkeypatch.setattr(
        discord_forum, "_lorebook_publisher_from_env", lambda: LorebookPublisher()
    )

    outcome = discord_forum.publish_lorebooks([str(record_path)])

    assert outcome == [{"action": "create", "thread_id": "lorebook-thread"}]
    assert captured["key"] == "janitor:book-42"
    assert captured["title"] == "janitor: Shared book"
    assert captured["filename"] == "janitor-book-42.json"


def test_publish_lorebooks_does_not_duplicate_fully_attributed_observations(
    monkeypatch, tmp_path
):
    record_path = tmp_path / "lorebooks" / "janitor" / "unassigned" / "character.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        discord_forum.json.dumps(
            {
                "source": "janitor",
                "observations": [
                    {
                        "attribution": {
                            "status": "inferred",
                            "candidates": ["book-42"],
                        }
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class LorebookPublisher:
        def lorebook_thread_index(self):
            return {}

        def upsert_lorebook(self, **_kwargs):
            raise AssertionError("fully attributed audit file must not be published")

    monkeypatch.setattr(
        discord_forum, "_lorebook_publisher_from_env", lambda: LorebookPublisher()
    )

    assert discord_forum.publish_lorebooks([str(record_path)]) == []


def test_upsert_creates_a_thread_with_the_rich_embed(tmp_path):
    """The thread-creation payload uses the embed, not the former text body."""
    png_path = tmp_path / "card.png"
    png_path.write_bytes(b"png")
    publisher = object.__new__(ForumPublisher)
    publisher.on_duplicate = "repost"
    publisher.thread_index = lambda: {}
    publisher.available_tags = lambda: {}
    captured: dict = {}

    def create_post(**kwargs):
        captured.update(kwargs)
        return {"id": "thread-id"}

    publisher.create_post = create_post
    outcome = publisher.upsert(
        uuid="c49da2b4-2b9c-479a-a99e-2b979cc22f82",
        name="Card",
        url="https://janitorai.com/characters/c49da2b4-2b9c-479a-a99e-2b979cc22f82",
        card_tags=[],
        meta={},
        definition_source="proxy",
        png_path=png_path,
    )

    assert outcome == {
        "action": "create",
        "thread_id": "thread-id",
        "uuid": "c49da2b4-2b9c-479a-a99e-2b979cc22f82",
    }
    assert captured["embed"]["title"] == "Card"


def test_detail_embeds_include_card_text_but_not_lorebook_content():
    embeds = _detail_embeds_for(
        {
            "character": {"description": "A full definition.", "scenario": "A scenario."},
            "entries": ["Recovered private lore."],
            "publicLorebooks": [
                {
                    "title": "Public book",
                    "worldInfo": {"entries": {"0": {"content": "Public lore."}}},
                }
            ],
        }
    )

    assert [(embed["title"], embed["description"]) for embed in embeds] == [
        ("Definition", "A full definition."),
        ("Scenario", "A scenario."),
    ]


def test_lorebook_files_are_importable_character_books():
    files = _lorebook_files_for(
        "c49da2b4-2b9c-479a-a99e-2b979cc22f82",
        {
            "entries": ["Recovered private lore."],
            "publicLorebooks": [
                {
                    "title": "Public book",
                    "worldInfo": {
                        "entries": {
                            "0": {
                                "content": "Public lore for {user}.",
                                "key": ["public-key"],
                                "comment": "Public entry",
                            }
                        }
                    },
                }
            ],
        },
    )

    assert [filename for filename, _content in files] == [
        "c49da2b4-2b9c-479a-a99e-2b979cc22f82-recovered-lorebook.json",
        "c49da2b4-2b9c-479a-a99e-2b979cc22f82-public-lorebook-1.json",
    ]
    recovered = discord_forum.json.loads(files[0][1])
    public = discord_forum.json.loads(files[1][1])
    assert recovered["entries"][0]["content"] == "Recovered private lore."
    assert recovered["entries"][0]["constant"] is False
    assert recovered["entries"][0]["enabled"] is False
    assert recovered["entries"][0]["extensions"]["ripart"]["recovery"][
        "trigger_status"
    ] == "unknown"
    assert public["name"] == "Public book"
    assert public["entries"][0]["keys"] == ["public-key"]
    assert public["entries"][0]["content"] == "Public lore for {{user}}."


def test_detail_embed_batches_obey_discord_message_limits():
    embeds = [
        {"title": f"Entry {index}", "description": "x" * 1000, "footer": {"text": "ripart archive"}}
        for index in range(12)
    ]

    batches = _detail_embed_batches(embeds)

    assert len(batches) == 3
    assert all(len(batch) <= 10 for batch in batches)
    assert all(sum(_embed_char_count(embed) for embed in batch) <= 6000 for batch in batches)


def test_lorebook_file_batches_obey_discord_attachment_limit():
    files = [(f"book-{index}.json", b"{}") for index in range(11)]

    batches = _lorebook_file_batches(files)

    assert [len(batch) for batch in batches] == [10, 1]
