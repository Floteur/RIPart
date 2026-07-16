# RIPart

A small, browser-driven command-line tool for ripping character cards and
lorebooks from [JanitorAI](https://janitorai.com). It drives a real (headless)
Chrome via [Botasaurus](https://github.com/omkarcloud/botasaurus), so it sees
exactly what your browser sees — including private card definitions surfaced
through the `generateAlpha` endpoint.

## Requirements

- Python 3.12+
- The project's virtual environment (managed with [`uv`](https://docs.astral.sh/uv/))

## Install

From the project root:

```bash
uv sync            # install dependencies into .venv
```

This installs a `rip` command into the environment. Everywhere below you can use
either form:

```bash
uv run rip --help          # via uv
python -m ripart --help    # as a module
```

## Quickstart

```bash
rip login            # 1. log in once — the session is saved and reused
rip status           # 2. confirm you're logged in
rip inspect <url>    # 3. peek at a character's public metadata
rip extract <url>    # 4. rip the full card + lorebook
```

`<url>` can be a full JanitorAI character URL **or** just its UUID.

Results are written under `output/cli/` (relative to the project root).

## Commands

Run `rip COMMAND --help` for the full, colour-coded help of any command.

| Command | What it does |
| --- | --- |
| `rip status` | Check whether the browser profile is logged in. Exit code `0` = yes, `1` = no. |
| `rip login` | Open JanitorAI and wait for you to sign in. `--timeout SECONDS` (default 180). |
| `rip import-session PATH` | Import a cookie/localStorage JSON dump into the profile — handy on headless servers where you can't log in interactively. |
| `rip inspect URL` | Fetch a character's public metadata and public lorebooks (read-only). Writes `output/cli/inspections/<name>.json`. |
| `rip extract URL` | Rip the private card + lorebook via `generateAlpha`. Writes the capture, raw lorebook, character card, and avatar under `output/cli/extracts/<name>/`. A `saucepan.ai` URL is routed to the Saucepan path below. |
| `rip saucepan …` | Rip companions from [Saucepan](https://saucepan.ai) via its REST API (no browser). See below. |
| `rip completion [SHELL]` | Print instructions to enable tab-completion. |

## Saucepan (saucepan.ai)

Saucepan serves companion definitions through an authenticated REST API, so
ripping needs **no browser** — it is a direct, exact pull rather than a
reconstruction. Log in once (the bearer token is saved to a gitignored
`.saucepan-token` and reused), then extract:

```bash
rip saucepan login                 # store a bearer token (prompts for username + password)
rip saucepan status                # confirm a token is configured & unexpired (exit 0 = yes, 1 = no)
rip saucepan extract <url>         # rip a companion card + lorebooks (URL or bare companion id)
rip saucepan extract <url> --no-lorebooks   # card only, skip attached lorebooks
rip saucepan logout                # forget the stored token
```

A `saucepan.ai/companion/<id>` URL passed to plain `rip extract` is routed here
automatically. Extracted cards land in the same UUID-keyed library as the
JanitorAI path (`output/cli/library/<id>.png`, a self-contained V3 card with the
lorebook embedded).

**What's pulled.** The companion body and greetings come from Saucepan's public
companion data, and any attached lorebooks are embedded as keyed
`character_book` entries (their activation / secondary keys are recovered). If
the companion's definition is **open**, the named prose sections are used too:
`Companion Core` → description, `Example Dialogue` → example messages, starting
scenarios → first message + alternate greetings; `Advanced Prompt` / `Response
Formatting` are preserved (labelled) in creator notes.

Most companions keep their definition **closed** (`open_definition = false`), so
the definition endpoint returns *"You do not have permission to do that."* That
is not fatal: the card is still built from the public body + greetings +
lorebooks, and is marked `definitionSource: saucepan-partial` (only the example
dialogue / advanced prompt are then unavailable).

### Recovering a gated definition (`--leak`)

The gated example dialogue / advanced prompt are still injected into the chat
context, so a model can be asked to dump them. `--leak` creates a throwaway chat,
has a model repeat its full instructions, parses the dump into the card, and
archives the chat:

```bash
rip saucepan providers                              # list your model provider configs
rip saucepan extract <url> --leak                   # auto-picks your first provider config
rip saucepan extract <url> --leak --leak-config mistral-small-latest
rip saucepan extract <url> --leak --leak-model <saucepan-alias>
rip saucepan extract <url> --leak --leak-mode user  # if 'director' (default) refuses
```

Notes and caveats:

- **A compliant model is required.** Saucepan's own default model refuses to
  reveal its instructions, so the leak runs through a model you choose: either a
  **BYOK provider config** (`--leak-config`, set up on saucepan.ai under Model
  Providers — see `rip saucepan providers`) or a Saucepan `--leak-model` alias.
- **It creates a chat and spends a generation** on every attempt (through your
  chosen model/provider). The throwaway chat is archived automatically.
- **It is lossy and non-deterministic.** The model may refuse or paraphrase;
  RIPart retries a few times and detects refusals. On success the card is marked
  `definitionSource: saucepan-leak`; if every attempt fails it falls back to the
  normal `saucepan-partial` card and tells you why.

### Examples

```bash
# Rip a character by UUID
rip extract 12345678-90ab-cdef-1234-567890abcdef

# Watch it work in a visible browser (debugging)
rip extract <url> --headed

# Do a single trigger pass and print diagnostics
rip extract <url> --no-multi-trigger --verbose

# Import a session dump on a headless box, retrying past Cloudflare
rip import-session ./session.json --bypass-cloudflare --verbose
```

## Tab-completion

`rip` supports shell completion (bash, zsh, fish). Print the snippet for your
shell and add it to your startup file:

```bash
rip completion bash   # then add the printed line to ~/.bashrc
rip completion zsh    # ~/.zshrc
rip completion fish   # ~/.config/fish/config.fish
```

## Output layout

```
output/cli/
├── inspections/
│   └── <character>.json        # `rip inspect`
└── extracts/
    └── <character>/            # `rip extract`
        ├── capture.json         # raw capture + diagnostics
        ├── raw_lorebook.txt      # merged lorebook text
        ├── <character>.json      # character card (chara_card_v2)
        └── <character>.png       # avatar, if available
```

## Troubleshooting

- **Not logged in?** Run `rip login` (or `rip import-session` on a headless
  box). `rip status` tells you the current state.
- **Blocked by Cloudflare on import?** Retry with `--bypass-cloudflare`.
- **Something crashed with a one-line error?** Re-run with `RIP_DEBUG=1` set to
  see the full traceback, e.g. `RIP_DEBUG=1 rip extract <url>`.
- **Want to see the browser?** Add `--headed` to any command.

## Layout

```
ripart/
├── pyproject.toml     # project metadata, deps, and the `rip` entry point
├── cli.py             # command-line interface (this is the user-facing layer)
├── browser_tasks.py   # Botasaurus tasks that drive the browser (JanitorAI)
├── saucepan.py        # Saucepan REST extraction (no browser)
├── helpers.py         # parsing / formatting utilities
└── __main__.py        # enables `python -m ripart`
```
