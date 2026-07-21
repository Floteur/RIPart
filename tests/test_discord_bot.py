"""Tests for the serial Discord CLI runner without requiring discord.py."""

from __future__ import annotations

import asyncio

import pytest

from ripart.common.discord_bot import (
    ExtractionQueue,
    JobResult,
    _PROVIDER_ACTIONS,
    _ROOT_ACTIONS,
    _route_provider,
    action_allowed,
    action_argv,
    action_argv_from_options,
    action_options,
    parse_command,
)


def test_parse_command_uses_shell_style_quoting_without_a_shell():
    assert parse_command('saucepan list --tag "slow burn"') == [
        "saucepan",
        "list",
        "--tag",
        "slow burn",
    ]


@pytest.mark.parametrize("command", ["", "   ", "discord-bot", "discord-bot --anything"])
def test_parse_command_rejects_empty_and_recursive_commands(command):
    with pytest.raises(ValueError):
        parse_command(command)


def test_parse_command_does_not_interpret_shell_metacharacters():
    assert parse_command("status; whoami") == ["status;", "whoami"]


def test_action_argv_only_requires_the_action_arguments():
    assert action_argv(("saucepan",), "extract", '"https://example.test/card" --no-lorebooks') == [
        "saucepan",
        "extract",
        "https://example.test/card",
        "--no-lorebooks",
    ]


def test_extract_uses_a_required_uuid_field_and_typed_options():
    options = action_options(("clank",), "extract")

    uuid = options[0]
    assert uuid.name == "uuid"
    assert uuid.annotation is str
    assert uuid.required
    assert uuid.positional
    assert {option.name for option in options} >= {"leak", "trigger_message", "max_triggers"}
    assert next(option for option in options if option.name == "max_triggers").annotation is int


def test_typed_discord_fields_build_the_expected_cli_argv():
    assert action_argv_from_options(
        ("clank",),
        "extract",
        {"uuid": "abc-123", "leak": True, "max_triggers": 12},
    ) == ["clank", "extract", "abc-123", "--leak", "--max-triggers", "12"]


def test_no_argument_action_has_no_discord_fields():
    assert action_options(("clank",), "status") == ()


def test_logout_actions_are_limited_to_the_designated_owner():
    admin_ids = {566580404279181341, 42}
    assert action_allowed(action="logout", user_id=566580404279181341, admin_ids=admin_ids)
    assert action_allowed(action="logout", user_id=42, admin_ids=admin_ids)
    assert not action_allowed(action="logout", user_id=1, admin_ids=admin_ids)
    assert action_allowed(action="status", user_id=1, admin_ids=admin_ids)


def test_discord_payload_loggers_are_silenced_at_verbose_levels():
    import logging

    from ripart.common.discord_bot import _configure_logging

    _configure_logging(3)

    assert logging.getLogger("discord.http").level == logging.WARNING
    assert logging.getLogger("discord.gateway").level == logging.WARNING


def test_discord_actions_cover_the_public_cli_tree():
    from ripart.cli import main

    # `help` is a Discord-only command with no CLI counterpart.
    discord_only = {"help"}
    assert set(_ROOT_ACTIONS) - discord_only == set(main.commands) - {
        "janitor",
        "saucepan",
        "clank",
        "spicychat",
        "discord-bot",
        "completion",  # shell-completion setup is local-CLI only
    }
    for provider, actions in _PROVIDER_ACTIONS.items():
        assert set(actions) == set(main.commands[provider].commands)


def test_every_discord_action_generates_a_form_schema():
    for action in _ROOT_ACTIONS:
        if action == "help":
            continue  # Discord-only; no CLI command to introspect.
        action_options((), action)
    for provider, actions in _PROVIDER_ACTIONS.items():
        for action in actions:
            action_options((provider,), action)


def test_discord_command_schema_descriptions_fit_the_platform_limit():
    fields = [
        *(
            option
            for action in _ROOT_ACTIONS
            if action != "help"
            for option in action_options((), action)
        ),
        *(
            option
            for provider, actions in _PROVIDER_ACTIONS.items()
            for action in actions
            for option in action_options((provider,), action)
        ),
    ]
    assert sum(len(option.description) for option in fields) < 5_000


def test_queue_serialises_a_provider_but_runs_providers_in_parallel():
    import threading
    import time as _time

    async def exercise() -> tuple[dict[str, int], int]:
        queue = ExtractionQueue()
        lock = threading.Lock()
        per_provider_peak: dict[str, int] = {}
        active_now = {"a": 0, "b": 0}
        total_active = 0
        cross_peak = 0

        def make(provider: str):
            def thunk() -> JobResult:
                nonlocal total_active, cross_peak
                with lock:
                    active_now[provider] += 1
                    total_active += 1
                    per_provider_peak[provider] = max(
                        per_provider_peak.get(provider, 0), active_now[provider]
                    )
                    cross_peak = max(cross_peak, total_active)
                _time.sleep(0.05)
                with lock:
                    active_now[provider] -= 1
                    total_active -= 1
                return JobResult(True, provider)

            return thunk

        await asyncio.gather(
            queue.run("a", make("a")),
            queue.run("a", make("a")),
            queue.run("b", make("b")),
            queue.run("b", make("b")),
        )
        return per_provider_peak, cross_peak

    per_provider_peak, cross_peak = asyncio.run(exercise())
    assert per_provider_peak == {"a": 1, "b": 1}  # same provider never overlaps
    assert cross_peak >= 2  # different providers ran at the same time


def test_queue_reports_position_within_a_provider_lane():
    async def exercise() -> list[int]:
        queue = ExtractionQueue()
        positions: list[int] = []

        async def record(position: int) -> None:
            positions.append(position)

        await asyncio.gather(
            queue.run("clank", lambda: JobResult(True, ""), on_queued=record),
            queue.run("clank", lambda: JobResult(True, ""), on_queued=record),
            queue.run("clank", lambda: JobResult(True, ""), on_queued=record),
        )
        return sorted(positions)

    assert asyncio.run(exercise()) == [1, 2, 3]


def test_queue_allows_one_extraction_reservation_per_user():
    queue = ExtractionQueue()
    assert queue.reserve(7, "janitor extract", "janitor") is True
    assert queue.reserve(7, "another command", "janitor") is False
    queue.release(7)
    assert queue.reserve(7, "janitor extract", "janitor") is True


def test_extract_urls_route_to_the_matching_provider_lane():
    assert _route_provider("https://saucepan.ai/companion/abc") == "saucepan"
    assert _route_provider("https://clank.world/chat/abc") == "clank"
    assert _route_provider("https://spicychat.ai/chatbot/abc") == "spicychat"
    # A bare JanitorAI UUID falls through to the browser-driven janitor lane.
    assert _route_provider("fbe26f87-db0e-4a1b-9c2d-000000000000") == "janitor"
