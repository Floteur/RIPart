"""Tests for the serial Discord CLI runner without requiring discord.py."""

from __future__ import annotations

import asyncio

import pytest

from ripart.common.discord_bot import (
    CommandResult,
    SerialCommandRunner,
    _PROVIDER_ACTIONS,
    _ROOT_ACTIONS,
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

    assert set(_ROOT_ACTIONS) == set(main.commands) - {
        "janitor",
        "saucepan",
        "clank",
        "spicychat",
        "discord-bot",
    }
    for provider, actions in _PROVIDER_ACTIONS.items():
        assert set(actions) == set(main.commands[provider].commands)


def test_every_discord_action_generates_a_form_schema():
    for action in _ROOT_ACTIONS:
        action_options((), action)
    for provider, actions in _PROVIDER_ACTIONS.items():
        for action in actions:
            action_options((provider,), action)


def test_discord_command_schema_descriptions_fit_the_platform_limit():
    fields = [
        *(
            option
            for action in _ROOT_ACTIONS
            for option in action_options((), action)
        ),
        *(
            option
            for provider, actions in _PROVIDER_ACTIONS.items()
            for action in actions
            for option in action_options((provider,), action)
        ),
    ]
    assert sum(len(option.description) for option in fields) < 4_000


def test_serial_command_runner_never_overlaps_work():
    async def exercise() -> int:
        runner = SerialCommandRunner()
        active = 0
        peak_active = 0

        async def fake_run(argv: list[str]) -> CommandResult:
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return CommandResult(tuple(argv), 0, "ok", False)

        runner._run = fake_run  # type: ignore[method-assign]
        first = asyncio.create_task(runner.submit(["status"]))
        second = asyncio.create_task(runner.submit(["saucepan", "status"]))
        await asyncio.gather(first, second)
        await runner.close()
        return peak_active

    assert asyncio.run(exercise()) == 1


def test_serial_command_runner_reports_queue_positions():
    async def exercise() -> tuple[int, int]:
        runner = SerialCommandRunner()

        async def fake_run(argv: list[str]) -> CommandResult:
            return CommandResult(tuple(argv), 0, "ok", False)

        runner._run = fake_run  # type: ignore[method-assign]
        first_position, first = runner.enqueue(["status"])
        second_position, second = runner.enqueue(["saucepan", "status"])
        await asyncio.gather(first, second)
        await runner.close()
        return first_position, second_position

    assert asyncio.run(exercise()) == (1, 2)
