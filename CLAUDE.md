# CLAUDE.md

Guidance for Claude Code in this repository. This file is kept short on purpose: it routes to the documents that hold the detail. Read the document for your task before changing code.

## What this project is

`llm_light` is an interactive CLI for querying and editing a multi-language codebase with Claude. It caches a project skeleton in the prompt cache and retrieves only the method-level code chunks each question needs (tree-sitter chunks, embedded, top-K per query).

## Where to read

| Task | Read |
|---|---|
| Repo map, what each file does, agent harness | [HARNESS.md](HARNESS.md) |
| How a part works (routing, caching, retrieval, indexing, compression) | [docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) |
| Config constants and env vars | [docs/CONFIG.md](docs/CONFIG.md) |
| Running, adding and fixing tests, test layout | [TESTING.md](TESTING.md) and [docs/test_strategy.md](docs/test_strategy.md) |
| What CI runs on a push or PR | [docs/WORKFLOW.md](docs/WORKFLOW.md) |
| Benchmarks and how to measure a token change | [docs/BENCHMARKS.md](docs/BENCHMARKS.md) |
| Token-savings levers and their measured results | [TOKEN-SAVINGS.md](TOKEN-SAVINGS.md) |
| Compression design rationale | [docs/llmlingua_plan.md](docs/llmlingua_plan.md) |
| Index of all docs | [docs/DOCS.md](docs/DOCS.md) |

## Commands

```bash
# Interactive
ANTHROPIC_API_KEY=sk-ant-... python3 llm_light.py

# One-shot, or piped
python3 llm_light.py "What does OrderService do?"
echo "List all REST endpoints" | python3 llm_light.py

# Resume this project's previous conversation, or pick an earlier one
python3 llm_light.py --continue
python3 llm_light.py --resume
python3 llm_light.py --resume auth-fix   # the one /rename called auth-fix

# Pick the agent: claude (default), gemini or codex
python3 llm_light.py --provider codex "What does OrderService do?"

# Let the OAuth agent run these commands (or type /allow <prefix> at the prompt)
python3 llm_light.py --allow-command "python -m pytest"
python3 llm_light.py --test-mode small        # 5 files, 10 methods each
python3 llm_light.py --web                    # the model may search the web and fetch pages
python3 llm_light.py --add-dir ../shared-lib  # ... and read (not write) this directory too

# Permission modes (shift+tab or /permissions switch them in a session)
python3 llm_light.py -y                                   # acceptEdits
python3 llm_light.py --permission-mode plan               # read-only
python3 llm_light.py --dangerously-skip-permissions       # ask nothing (alias --yolo)

# Send the model traffic through a proxy (llm-fw: --llm-fw for its local defaults)
python3 llm_light.py --proxy http://127.0.0.1:8080 --proxy-ca ~/.llm-fw/ca.crt

# Script it: piped text goes with the question, stdout holds only the answer
git diff | python3 llm_light.py -p --output-format json "review this"
python3 llm_light.py -c -p "and then?"

# Check the install, login and tools, even with no login
python3 llm_light.py --doctor

# Pin the model instead of routing; print the version
python3 llm_light.py --model opus
python3 llm_light.py --version

# Tests (see TESTING.md for the full picture)
pytest
```

### Tools, `/budget` and affected tests

- **`fetch_chunk(id)`**: returns up to 5 method bodies by `path::symbol` or bare name, with callers and callees. It needs the index to be ready.
- **`shell`**: runs one command from the project root, only if it starts with an allowed prefix (`/allow`, `--allow-command`). Commands containing `; & | < > `` ` `` $` or a newline are rejected, and PowerShell commands also `( ) { } @` (an argument in those is evaluated). On Windows it runs in Git Bash when installed, else `cmd.exe`, which also refuses `%` (`user_shell.py`). The OAuth CLI agent gets the same rule from the `cli_shell_guard.py` `PreToolUse` hook, because the CLI's own allow rule checks only the first statement. It is always in `TOOLS`, so the cached prompt prefix stays stable.
- **MCP servers** (`mcp_servers.py`): from `~/.llm_light/mcp.json` only (`LLM_LIGHT_MCP_CONFIG`), never the project's `.mcp.json`. On a subscription the CLI agent gets `--mcp-config` plus an `mcp__<server>` allow rule per server, or `mcp__<server>__<tool>` per entry of the server's optional `allow` list; with an API key `main()` connects through the optional `mcp` SDK before the first turn and `tools.all_tools()` appends `mcp__<server>__<tool>` after `TOOLS`. That list is fixed for the session (it is in the cached prefix), and without servers `all_tools()` is `TOOLS` itself. Plan mode runs no MCP tool; with an API key in default mode `tools._mcp` asks first (`ui.ask_mid_turn`, which pauses the view and the Esc listener), unless there is no terminal. `/mcp` lists them. See [docs/architecture/components.md](docs/architecture/components.md#mcp-servers).
- **Hooks** (`hooks.py`): from `~/.llm_light/hooks.json` only (`LLM_LIGHT_HOOKS_CONFIG`), never a project's `.claude/settings.json`. On a subscription they join the CLI agent's `--settings`, after the shell guard; with an API key `tools.run_tool` runs `PreToolUse`/`PostToolUse` under Claude Code's tool names (`hooks.CLI_NAMES`) and `editor.apply_edits` runs `PostToolUse` before the auto-commit. `/hooks` lists them. See [docs/architecture/components.md](docs/architecture/components.md#hooks).
- **`/budget`**: `/budget 2.50` sets a spend cap, `/budget off` removes it, `/budget` shows cap and spend. At the cap, routing drops to Haiku at low effort. While a budget is set, the route line shows a per-turn estimate.
- **Affected tests**: after an auto-commit, if a pytest command is allowed, the tests that mention the edited module run. On failure it offers to revert the commit and never reverts on its own. It does not run after OAuth-agent edits. In bypass mode it does not ask: the edit stays and `/undo` reverts it.
- **Permission modes** (`permissions.py`): `default`, `acceptEdits` (`-y`), `plan` and `bypassPermissions` (`--dangerously-skip-permissions`, alias `--yolo`). The mode, not an `auto_apply` argument, decides whether edits apply unasked, so `shift+tab` can change it mid-session. Bypass is offered only when a startup flag allowed it. Default-mode requests must stay byte-identical: the plan and bypass notes go in the question, never in the cached system prompt or `TOOLS`. See [docs/architecture/components.md](docs/architecture/components.md#permission-modes).
- **Your own instructions** (`instructions.py`): `~/.llm_light/instructions.md` only (`LLM_LIGHT_INSTRUCTIONS`), never a project's file. `llm._build_system_blocks()` joins `SYSTEM_PROMPT`, `instructions.text()` and `state.extra_system`, so every provider gets it and the claude CLI's swap of `SYSTEM_PROMPT` for `AGENT_SYSTEM_PROMPT` keeps it. Without the file the system text is `SYSTEM_PROMPT` alone, as before. `/memory user` edits it.
- **`--add-dir`, `/add-dir`** (`commands.add_dir`): `config.EXTRA_DIRS`, for the session only. The OAuth agent gets a `Read(//<path>/**)` allow rule per directory (`config.read_rule`; a Windows drive becomes `/c/...`), never the CLI's `--add-dir`, which would let it write there and skip the diff, commit and `/undo`. With an API key `tools._locate` and `tools._grep_base` let `read_file`, `list_files` and `grep` reach them by absolute path (results absolute, project paths still relative), with the tool descriptions unchanged. `llm._extra_dirs_note()` names them in the question, and with an API key says how to reach them; without any the request is unchanged.
- **`/allow`, `/disallow`** (`commands.py`): change `config.ALLOWED_COMMANDS` mid-session and remember the prefix in `session.allowed_file()`, under `SESSION_DIR`, never in the project: a file a cloned repo controls must not grant commands. Entries are re-validated when loaded.
- **`/loop`** (`main._loop_command`): `/loop <n> <question>` or `/loop <interval> <question>`, at most `LOOP_MAX` turns; `--until "<cmd>"` stops when it exits 0. `chat()` swallows Ctrl+C and API errors and returns False, so test the stop rules through the real `chat()` (`tests/unit/test_loop.py`), not a mock that raises.
- **Session commands** (`commands.py`): `/copy`, `/model`, `/effort`, `/export`, `/status`, `/diff`, `/rewind`, `/init`, `/review`, `/memory`, `/vim`, `!command`, `!!command` and `@file`; `/doctor` and `/context` are in `doctor.py`; `/mcp` is in `mcp_servers.py`; `/hooks` is in `hooks.py`; `/resume` and `/rename` are in `session.py`. Custom commands come from `~/.llm_light/commands/<name>.md` only (`config.USER_COMMANDS_DIR`), never the project's directory, which a cloned repo controls. `!command` output and `@file` contents reach `chat()` as `context`, sent after the question; routing and retrieval see the typed question only. `alt+v`/`ctrl+v` (`clipboard.paste_image`) saves the clipboard image under `CACHE_DIR/pastes/` and inserts its `@` path, so it goes the same way; the target path reaches PowerShell, `osascript`, `wl-paste` or `xclip` through `LLM_LIGHT_PASTE_TARGET`, never inside a script; a cache or `pastes` folder that is a link is refused, and pruning never removes a link. `@image` goes as `images=`: a block in API-key mode, a path for the agent to `Read` on a subscription, and only the path in the history.

Run from the root of the project you want to query. Exits with an error if no authentication is found, unless `--test-mode` is used; `--help` works without one. The check is `providers.active().require_auth()`, called by `main()` after the arguments are parsed, not at import.

Authentication, in order: `ANTHROPIC_API_KEY`; `~/.anthropic` or `./.env`; `~/.llm_light_automation_token` (recommended on Windows); `~/.claude/.credentials.json` from `claude auth login`. An API key bills per token; a Pro subscription is flat-rate. The mode follows the credential found. That order is the `claude` provider's. `--provider gemini` needs the Antigravity CLI `agy` on PATH (https://antigravity.google; a missing sign-in shows on the first question, because where agy keeps it depends on the platform; it runs as a custom agent with no tools, so `search_web` cannot run); `--provider codex` needs `codex login`. Both run in an empty temporary directory and have no caching, effort, top tier, `/budget` or shell; each says so when you use one.

Users install with `pipx install llm-light-cli` from PyPI (the distribution name: PyPI refuses `llm-light` as too similar to `LLMlight`; the command and the import stay `llm-light` and `llm_light`, and `commands.DIST_NAME` must match `pyproject.toml`'s name) (released by `.github/workflows/publish.yml`, see [docs/WORKFLOW.md](docs/WORKFLOW.md)); from a checkout, install with `installers/install.sh`, `installers/install_macos.sh` or `installers/install.ps1`. Dependencies come from `requirements.txt`, which the installers and the CI test and fuzz jobs all install. Python 3.12 or newer.

## Invariants: check these before editing

- **Neutral code reaches a provider only through `llm_light/providers/`.** No import of a provider's SDK and no provider binary name outside `providers/<name>.py` (`tests/unit/test_provider_isolation.py` enforces it). Use `providers.active()` and `providers.DEFAULT`.
- **Background threads never print.** The watcher, the heartbeat and the Esc listener (`esc_key.py`) call `ui.notify()` or print nothing. A print lands inside a streamed reply or over the prompt being typed.
- **Shared state goes through `state.py` under `state.lock`.** Modules import each other's globals at load time, so `__init__.py` wraps public functions in `_wrap()` to sync bindings. Do not add a new cross-module global without going through it.
- **`EMBED_MODEL` and `TOP_K` are set at runtime by `auto_tune()`.** Do not set them by hand.
- **`state.turn_count` counts turns.** Do not derive it from `conversation_history`, which compression shrinks.
- **Chunk ids are `filepath::methodName`.** Watchdog paths go through `indexer._project_path()`, or a re-index duplicates the file's chunks.
- **A proxy reaches model traffic only.** `proxy.py` never sets `os.environ`; the Anthropic client gets its own transport (`claude._new_client()`), and a CLI child gets the proxy through `proxy.child_env()`. Add a new CLI child's environment there too.
- **Never load `index.pkl`.** The disk cache loads with `allow_pickle=False` because a cloned repo can ship a hostile pickle.
- **In a `-p` run, only the answer reaches stdout.** `headless.run()` points stdout at stderr for the turn. A new way of showing a reply goes through `ResponseView`, which sends its text to `state.event_sink` when one is set.
- **Settings are read from the home directory only.** `settings.py` must never read a file a cloned repo controls, and never accept `bypassPermissions`.
- **Tests never call the real API or write the real stats or session files.** `tests/conftest.py` sets a mock key, `LLM_LIGHT_HYBRID=0`, a temp `LLM_LIGHT_STATS_FILE`, `LLM_LIGHT_SESSION_DIR`, `LLM_LIGHT_SETTINGS_FILE`, `LLM_LIGHT_MCP_CONFIG`, `LLM_LIGHT_HOOKS_CONFIG` and `LLM_LIGHT_INSTRUCTIONS`. Keep those settings when you add tests.
- **Anything that changes the conversation behind the CLI session's back sets `state.cli_session = None`.** A subscription turn resumes that session (`claude._session_to_resume`), so it still holds any turn the history dropped: `/clear`, `/compact`, `/rewind`, `/undo`, `/resume`, compression and a failed turn all end it, and a change of model or a gap past `CACHE_TTL_SECS` starts over. The next turn then sends the whole history. Test usage through `state.session_cost`, not `total_cost_usd`, which on a resume is the session's running total.
- **Auto-commit is the undo path.** AI edits commit as `Claude: [summary]`. `/undo` refuses unless the last commit is one of those and no file in it has changed since; `/rewind` applies the same rule to every commit after the turn it goes back to (`state.checkpoints`, recorded by `chat()`, cleared when history is compressed).
- **The top-level effort never changes per request.** Requests send `BASE_EFFORT`; `_apply_effort()` puts the turn's effort in an effort-only system message before the question. A per-request `output_config.effort` drops the cached history.
- **`route_query()` is a pure router; `_finalize_route()` applies the session.** The budget cap, the top-tier prompt (Opus 5.5, Fable 5.1 or Sonnet 5.5) and `ROUTE_ONCE` live in `_finalize_route()`, so benchmarks can call `route_query()` without pinning or prompting.
- **The first turn's model is kept for the session** (`config.ROUTE_ONCE`, on by default) in `state.session_model`; `/clear` resets it, and only a top-tier route can change it. Tests rely on the autouse fixture in `tests/conftest.py` that clears it, or one test's route pins the next test's model.
- **A token change needs a measurement.** Run the relevant benchmark before and after, and keep only changes that show a gain. See [docs/BENCHMARKS.md](docs/BENCHMARKS.md).
