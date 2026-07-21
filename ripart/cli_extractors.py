"""Provider extraction workflows used by the Click command layer.

This module owns provider-specific extraction orchestration and reporting.  The
``cli`` module supplies a small output context, keeping command registration and
argument parsing separate from network work and result formatting.
"""

from __future__ import annotations

import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .common.cards import save_to_library
from .common.discord_forum import publish_saved_card
from .providers import chub as cb
from .providers import clank as ck
from .providers import saucepan as sp
from .providers import spicychat as sc
from .providers import tavern as tv


@dataclass(frozen=True)
class ExtractionUI:
    """The CLI output and storage services required by extraction workflows."""

    library_dir: Path
    print: Callable[[str], None]
    error: Callable[[str], None]
    ok: Callable[[str], None]
    no: Callable[[str], None]
    field: Callable[[str, object], None]
    path: Callable[[str, object], None]
    duration: Callable[[float], str]


def _log(ui: ExtractionUI, verbose: int) -> Callable[[str], None]:
    return (lambda message: ui.print(f"[dim]  · {message}[/]")) if verbose else lambda _message: None


def _save_leak(ui: ExtractionUI, result: dict) -> Path | None:
    leak_raw = result.get("leakRaw") or ""
    if not leak_raw:
        return None
    leak_path = ui.library_dir / f"{result.get('characterId') or 'card'}.leak.txt"
    leak_path.write_text(leak_raw, encoding="utf-8")
    return leak_path


def _save_and_publish(ui: ExtractionUI, character_id: str, result: dict) -> dict[str, Any]:
    paths = save_to_library(ui.library_dir, character_id, result)
    return publish_saved_card(character_id, result, paths)


def library_has_card(library_dir: Path, character_id: object, *, source_url: str = "") -> bool:
    """Return whether an item is already represented by a saved library card."""
    identifier = str(character_id or "").strip()
    if identifier and (library_dir / f"{identifier}.png").is_file():
        return True
    if not source_url:
        return False
    try:
        index = json.loads((library_dir / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(index, dict) and any(
        isinstance(entry, dict)
        and entry.get("url") == source_url
        and (library_dir / str(entry.get("file") or "")).is_file()
        for entry in index.values()
    )


def save_listed_cards(
    ui: ExtractionUI,
    items: list[dict],
    *,
    item_id: Callable[[dict], str],
    item_name: Callable[[dict], str],
    extract: Callable[[dict], dict],
    source_url: Callable[[dict], str] | None = None,
    suffix: str = "",
) -> None:
    """Extract listed provider items, skipping cards already in the library."""
    saved = skipped = 0
    for item in items:
        identifier = item_id(item)
        if library_has_card(ui.library_dir, identifier, source_url=source_url(item) if source_url else ""):
            skipped += 1
            continue
        try:
            result = extract(item)
            _save_and_publish(ui, result.get("characterId") or identifier, result)
            saved += 1
        except Exception as exc:  # provider errors are reported per item, not fatal for a batch
            ui.error(f"[yellow]![/] {item_name(item)}: {exc}")
    ui.ok(f"saved [bold]{saved}[/] card(s); skipped [bold]{skipped}[/] existing card(s) to {ui.library_dir}{suffix}")


def _rank_leak_configs(configs: list[dict]) -> list[dict]:
    visible = [config for config in configs if config.get("is_visible")]

    def rank(config: dict) -> tuple:
        is_custom = int(str(config.get("provider") or "").lower() == "custom")
        order = config.get("sort_order")
        return is_custom, order if isinstance(order, int) else 999

    return sorted(visible, key=rank)


def saucepan_extract(
    ui: ExtractionUI,
    url: str,
    *,
    include_lorebooks: bool = True,
    leak: bool = False,
    leak_config: str | None = None,
    leak_model: str | None = None,
    leak_mode: str = "user",
    leak_prompt: str | None = None,
    leak_system: str | None = None,
    leak_keep: bool = False,
    leak_echo: bool = True,
    verbose: int = 0,
) -> None:
    """Extract and report a Saucepan companion."""
    log = _log(ui, verbose)
    sp.set_trace_level(verbose)
    if leak and leak_config and leak_model:
        ui.print("[yellow]![/] both --leak-config and --leak-model given; using --leak-config")
        leak_model = None
    if leak and not leak_model:
        if leak_config:
            resolved = sp.resolve_provider_config(leak_config)
            if not resolved:
                ui.no(f"no provider config matching [bold]{leak_config}[/] - see [bold]rip saucepan providers[/]")
                raise SystemExit(1)
            leak_config = resolved
        else:
            configs = _rank_leak_configs(sp.list_provider_configs())
            if not configs:
                ui.no("no BYOK provider config for --leak - add one on saucepan.ai, or pass --leak-model")
                raise SystemExit(1)
            leak_config = configs[0].get("config_id")
            ui.print(f"[dim]leak model: {configs[0].get('config_name')} ({configs[0].get('model_id')})[/]")

    restore: tuple[str, str | None] | None = None
    if leak and leak_system:
        if not leak_config:
            ui.no("--leak-system needs a BYOK --leak-config (not --leak-model)")
            raise SystemExit(1)
        try:
            previous = sp.set_provider_prompt(leak_config, leak_system)
            restore = (leak_config, previous)
            log("set provider system prompt for leak")
        except sp.SaucepanError as exc:
            ui.no(f"could not set --leak-system: {exc}")
            raise SystemExit(1)

    started = time.monotonic()
    try:
        result = sp.extract_companion(url, include_lorebooks=include_lorebooks, leak=leak, leak_config=leak_config, leak_model=leak_model, leak_mode=leak_mode, leak_prompt=leak_prompt, leak_keep=leak_keep, leak_echo=leak_echo, log=log)
    except sp.SaucepanError as exc:
        ui.no(str(exc))
        if exc.status == 401:
            ui.error("[dim]run [bold]rip saucepan login[/] to authenticate[/]")
        raise SystemExit(1)
    finally:
        sp.set_trace_level(0)
        if restore is not None:
            try:
                sp.set_provider_prompt(*restore)
                log("restored provider system prompt")
            except sp.SaucepanError:
                ui.error("[yellow]![/] could not restore the provider system prompt — " f"check config [bold]{leak_config}[/] in Saucepan settings")
    elapsed = time.monotonic() - started
    paths = _save_and_publish(ui, result.get("characterId") or "", result)
    leak_path = _save_leak(ui, result)
    ui.ok(f"extracted [bold]{result.get('characterName') or url}[/] [dim](saucepan)[/]")
    ui.path("card png", paths["png"])
    if leak_path:
        ui.path("raw leak", leak_path)
    character, diagnostics = result.get("character") or {}, result.get("diagnostics") or {}
    ui.field("greetings", (1 if character.get("firstMessage") else 0) + len(character.get("alternateGreetings") or []))
    ui.field("lorebook entries", f"{diagnostics.get('lorebookEntries', 0)} in {diagnostics.get('lorebooks', 0)} book(s)")
    source = character.get("definitionSource")
    if source == "saucepan-echo":
        ui.field("definition", f"[green]leaked {diagnostics.get('leakChars', 0)} chars verbatim via echo proxy[/]")
    elif source == "saucepan-leak":
        ui.field("definition", f"[green]leaked {diagnostics.get('leakChars', 0)} chars via model[/] [dim](lossy)[/]")
    elif leak and diagnostics.get("leakError"):
        ui.field("definition", f"[yellow]leak failed: {diagnostics['leakError']} - kept public data[/]")
    elif source == "saucepan-partial":
        ui.field("definition", "[yellow]partial - definition gated, body/greetings from public data[/]")
    ui.field("time", ui.duration(elapsed))
    if verbose:
        ui.field("definition open", diagnostics.get("definitionOpen"))
        ui.field("definition sections", diagnostics.get("sections") or [])
        ui.field("lorebooks", diagnostics.get("lorebooks", 0))
        if leak and diagnostics.get("leakError"):
            ui.field("leak error", diagnostics["leakError"])


def clank_extract(ui: ExtractionUI, url: str, *, leak: bool = False, keep_boilerplate: bool = False, trigger_message: str = "hi", with_lorebook: bool = False, max_triggers: int = 8, verbose: int = 0) -> None:
    """Extract and report a clank.world character."""
    log = _log(ui, verbose)
    ck.set_trace_level(verbose)
    started = time.monotonic()
    try:
        result = ck.extract_chat(url, leak=leak, keep_boilerplate=keep_boilerplate, trigger_message=trigger_message, with_lorebook=with_lorebook, max_triggers=max_triggers, log=log)
    except ck.ClankError as exc:
        ui.no(str(exc))
        if exc.status == 401:
            ui.error("[dim]run [bold]rip clank login[/] to authenticate[/]")
        raise SystemExit(1)
    finally:
        ck.set_trace_level(0)
    elapsed = time.monotonic() - started
    paths = _save_and_publish(ui, result.get("characterId") or "", result)
    leak_path = _save_leak(ui, result)
    ui.ok(f"extracted [bold]{result.get('characterName') or url}[/] [dim](clank)[/]")
    ui.path("card png", paths["png"])
    if leak_path:
        ui.path("raw leak", leak_path)
    character, diagnostics = result.get("character") or {}, result.get("diagnostics") or {}
    source = character.get("definitionSource")
    if source == "clank-echo-leak":
        ui.field("definition", f"[green]leaked {diagnostics.get('definitionChars', 0)} chars verbatim via echo proxy[/]")
    elif diagnostics.get("leakError"):
        ui.field("definition", f"[yellow]not leaked: {diagnostics['leakError']}[/]")
    else:
        ui.field("definition", "[yellow]partial - no echo in chat; configure the proxy + send a message, or use --leak[/]")
    ui.field("greetings", (1 if character.get("firstMessage") else 0) + len(character.get("alternateGreetings") or []))
    ui.field("scenario", f"{diagnostics.get('scenarioChars', 0)} chars")
    ui.field("example dialogue", f"{diagnostics.get('exampleChars', 0)} chars")
    if tags := diagnostics.get("tags"):
        ui.field("tags", ", ".join(tags))
    if with_lorebook:
        if "lorebookEntries" in diagnostics:
            count = diagnostics["lorebookEntries"]
            ui.field("lorebook", f"[green]{count} entr{'y' if count == 1 else 'ies'} recovered via triggers[/]" if count else "no lorebook entries fired (character may have none)")
        elif diagnostics.get("lorebookError"):
            ui.field("lorebook", f"[yellow]not run: {diagnostics['lorebookError']}[/]")
    ui.field("time", ui.duration(elapsed))


def spicychat_extract(ui: ExtractionUI, url: str, *, leak: bool = False, leak_model: str = sc.DEFAULT_LEAK_MODEL, leak_attempts: int = 4, leak_prompt: str | None = None, leak_keep: bool = False, verbose: int = 0) -> None:
    """Extract and report a spicychat.ai character."""
    log = _log(ui, verbose)
    sc.set_trace_level(verbose)
    started = time.monotonic()
    try:
        result = sc.extract_character(url, leak=leak, leak_prompt=leak_prompt or sc.DEFAULT_LEAK_PROMPT, leak_model=leak_model, leak_attempts=leak_attempts, leak_keep=leak_keep, log=log)
    except sc.SpicyChatError as exc:
        ui.no(str(exc))
        raise SystemExit(1)
    finally:
        sc.set_trace_level(0)
    elapsed = time.monotonic() - started
    paths = _save_and_publish(ui, result.get("characterId") or "", result)
    ui.ok(f"extracted [bold]{result.get('characterName') or url}[/] [dim](spicychat)[/]")
    ui.path("card png", paths["png"])
    character, diagnostics = result.get("character") or {}, result.get("diagnostics") or {}
    source = character.get("definitionSource")
    if source == "spicychat-api":
        ui.field("definition", f"[green]public — {diagnostics.get('definitionChars', 0)} chars[/]")
    elif source == "spicychat-leak":
        ui.field("definition", f"[cyan]leaked via model dump — {diagnostics.get('definitionChars', 0)} chars (lossy paraphrase)[/]")
    else:
        ui.field("definition", "[yellow]gated (definition_visible=false) — partial card (greeting + metadata); add --leak to recover[/]")
    ui.field("greeting", f"{diagnostics.get('greetingChars', 0)} chars")
    ui.field("scenario", f"{diagnostics.get('scenarioChars', 0)} chars")
    ui.field("example dialogue", f"{diagnostics.get('exampleChars', 0)} chars")
    if tags := diagnostics.get("tags"):
        ui.field("tags", ", ".join(tags))
    if diagnostics.get("lorebookCount"):
        ui.field("lorebooks", f"{diagnostics['lorebookCount']} attached [dim](entries gated)[/]")
    ui.field("time", ui.duration(elapsed))


def _report_open_card(ui: ExtractionUI, result: dict, *, platform: str, url: str, elapsed: float) -> None:
    paths = _save_and_publish(ui, result.get("characterId") or "card", result)
    ui.ok(f"extracted [bold]{result.get('characterName') or url}[/] [dim]({platform})[/]")
    ui.path("card png", paths["png"])
    diagnostics = result.get("diagnostics") or {}
    ui.field("definition", f"[green]public — {diagnostics.get('descriptionChars', 0)} chars[/]")
    ui.field("first message", f"{diagnostics.get('firstMessageChars', 0)} chars")
    ui.field("example dialogue", f"{diagnostics.get('exampleChars', 0)} chars")
    if diagnostics.get("alternateGreetings"):
        ui.field("alt greetings", diagnostics["alternateGreetings"])
    if diagnostics.get("lorebookEntries"):
        ui.field("lorebook", f"{diagnostics['lorebookEntries']} entries [dim](keys preserved)[/]")
    if tags := diagnostics.get("tags"):
        ui.field("tags", ", ".join(tags[:8]))
    ui.field("time", ui.duration(elapsed))


def chub_extract(ui: ExtractionUI, url: str, *, verbose: int = 0) -> None:
    """Extract and report a public Chub card."""
    log = _log(ui, verbose)
    cb.set_trace_level(verbose)
    started = time.monotonic()
    try:
        result = cb.extract_character(url, log=log)
    except cb.ChubError as exc:
        ui.no(str(exc))
        raise SystemExit(1)
    finally:
        cb.set_trace_level(0)
    _report_open_card(ui, result, platform="chub", url=url, elapsed=time.monotonic() - started)


def tavern_extract(ui: ExtractionUI, url: str, *, verbose: int = 0) -> None:
    """Extract and report a direct card file or Character Tavern card."""
    log = _log(ui, verbose)
    tv.set_trace_level(verbose)
    started = time.monotonic()
    try:
        result = tv.extract_card(url, log=log)
    except tv.TavernCardError as exc:
        ui.no(str(exc))
        raise SystemExit(1)
    finally:
        tv.set_trace_level(0)
    platform = result.get("diagnostics", {}).get("cardKind") or "card"
    _report_open_card(ui, result, platform=f"tavern-{platform}", url=url, elapsed=time.monotonic() - started)
