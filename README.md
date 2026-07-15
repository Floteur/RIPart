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
| `rip extract URL` | Rip the private card + lorebook via `generateAlpha`. Writes the capture, raw lorebook, character card, and avatar under `output/cli/extracts/<name>/`. |
| `rip completion [SHELL]` | Print instructions to enable tab-completion. |

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
├── browser_tasks.py   # Botasaurus tasks that drive the browser
├── helpers.py         # parsing / formatting utilities
└── __main__.py        # enables `python -m ripart`
```
