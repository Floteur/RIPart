"""Janitor lorebook attachment-index parsing."""

from __future__ import annotations

import json

from ripart.providers.janitor.browser_tasks import _public_lorebook_from_response


def test_lorebook_response_preserves_linked_character_urls():
    response = {
        "status": 200,
        "body": json.dumps(
            {
                "id": "book-42",
                "title": "Shared setting",
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
