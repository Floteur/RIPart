"""Janitor lorebook attachment-index parsing."""

from __future__ import annotations

import json

from ripart.providers.janitor.browser_tasks import (
    _public_entry_count,
    _public_lorebook_count,
    _public_lorebook_from_response,
)


def test_lorebook_response_preserves_linked_character_urls():
    response = {
        "status": 200,
        "body": json.dumps(
            {
                "id": "book-42",
                "title": "Shared setting",
                "description": "<p>A shared <strong>setting</strong>.</p>",
                "is_public": True,
                "is_code_public": False,
                "script": None,
                "characters": [
                    {"id": "char-a", "name": "Alpha", "creator_name": "Maker"},
                    {"id": "char-a", "name": "Duplicate"},
                    {"id": "char-b", "name": "Beta"},
                ],
            }
        ),
    }

    book = _public_lorebook_from_response({"id": "book-42", "title": ""}, response)

    assert book["accessible"] is False
    assert book["description"] == "A shared setting."
    assert book["referencedCharacters"] == [
        {
            "id": "char-a",
            "name": "Alpha",
            "url": "https://janitorai.com/characters/char-a",
            "creator": "Maker",
        },
        {
            "id": "char-b",
            "name": "Beta",
            "url": "https://janitorai.com/characters/char-b",
        },
    ]


def test_public_lorebook_count_excludes_closed_attachments():
    books = [
        {
            "id": "closed-book",
            "isPublic": True,
            "isCodePublic": False,
            "accessible": False,
            "worldInfo": {
                "entries": {"0": {"content": "Owner-visible private entry"}}
            },
        },
        {
            "id": "public-book",
            "isPublic": True,
            "isCodePublic": True,
            "accessible": True,
            "worldInfo": {"entries": {"0": {"content": "Public entry"}}},
        },
    ]

    assert _public_lorebook_count(books) == 1
    assert _public_entry_count(books) == 1
