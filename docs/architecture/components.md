# Component Architecture

> Part of the [architecture index](ARCHITECTURE.md). Source: the original `docs/architecture.md`, split by topic.


This diagram shows the main components and their relationships within LLM Light.

![Component Diagram](../plantuml/LLM_Light_Component_Diagram.png)

### Key Components

| Component | Responsibility |
|-----------|----------------|
| `main.py` | Entry point: parses the flags, runs the prompt loop and dispatches the slash commands, `!command` and `@file` mentions |
| `permissions.py` | The permission mode (`default`, `acceptEdits`, `plan`, `bypassPermissions`) held in `state.permission_mode`: whether edits apply unasked, whether the shell is off, allowlisted or open, and the note put before the question in plan and bypass modes |
| `commands.py` | The session commands modelled on Claude Code's: `/copy` (clipboard or OSC 52), `/model`, `/export`, `/status`, `/diff`, `/allow` and `/disallow`, `/effort`, `/init`, `/review`, `/memory`, `/vim`, `!command` output queued for the next question, `!!command` for programs that need the terminal, `@file` attachment and completion, and the version |
| `llm.py` | Turn loop, routing and response processing. It reaches an agent only through `providers.active()` |
| `providers/` | One module per agent behind the shared `Provider` interface (`base.py`): `claude.py` (the `anthropic` client, the API-key and OAuth transports, the tool loop, login), `gemini.py` and `codex.py` (CLI subprocesses), `runner.py` (`run_cli`, `kill_process_tree`, `new_session`, `TIMEOUT_SECS`; every CLI child, the `shell` tool's included, starts in its own process group on POSIX so `kill_process_tree` can kill the group) and `__init__.py` (`NAMES`, `DEFAULT`, `get`, `select`, `active`) |
| `retrieval.py` | RAG engine, performs similarity search and chunk selection |
| `callgraph.py` | In-memory call graph over the chunk store (`name(` matched against chunk symbols), used by `fetch_chunk` to list callers and callees |
| `tools.py` | API-mode tools: `read_file`, `fetch_chunk`, `grep`, `list_files`, and `shell` (allowlisted commands only; any command in bypass mode, none in plan mode). `all_tools()` adds the connected MCP servers' tools after them |
| `mcp_servers.py` | The user's MCP servers from `~/.llm_light/mcp.json`: the claude CLI agent's `--mcp-config` and allow rules on a subscription, the `mcp` SDK client and the `mcp__<server>__<tool>` tools with an API key, `/mcp`. See [MCP servers](#mcp-servers) |
| `settings.py` | `~/.llm_light/settings.json` and `/config`. Home only, and it cannot hold `bypassPermissions`. A flag beats a setting |
| `completions.py` | `--completion <shell>`, built from the argument parser |
| `doctor.py` | `/doctor` and `--doctor`, which only look, so they run no program (the repository check looks for `.git`, not `git rev-parse`) and send nothing; each provider adds its own lines through `Provider.diagnose()`. Also `/context` |
| `headless.py` | `-p --output-format json\|stream-json` and `-c -p`: points stdout at stderr for the turn and writes only the answer to the real stdout, from the events `ResponseView` sends to `state.event_sink` |
| `user_shell.py` | Which shell runs `!command`, `/run` and the API-mode `shell` tool: Git Bash on Windows when installed, else `cmd.exe`; `sh` elsewhere |
| `cli_shell_guard.py` | `PreToolUse` hook for the OAuth CLI agent: blocks a Bash or PowerShell command that chains, pipes, redirects or substitutes (issue #70); holds the refused characters `tools.py` uses too |
| `affected_tests.py` | After an auto-commit, runs the test files that mention the edited modules, if a pytest command is allowed |
| `editor.py` | Code editor, parses and applies SEARCH/REPLACE blocks; runs the user's `PostToolUse` hooks on each written file before the auto-commit |
| `esc_key.py` | Esc cancels a turn: reads keys on a thread while `chat()` waits for the provider, interrupts the main thread, and keeps other typed keys for the next prompt. See [Reliability and lifecycle](#reliability-and-lifecycle) |
| `hooks.py` | The user's hooks from `~/.llm_light/hooks.json`: merged into the CLI agent's `--settings` on a subscription, run around `tools.run_tool` and after edits with an API key, `/hooks`. See [Hooks](#hooks) |
| `indexer.py` | File watcher, manages incremental indexing and cache |
| `skeleton.py` | Builds compressed directory tree and markdown documentation |
| `state.py` | Shared state module for thread-safe access to session data |
| `session.py` | Saves the conversation, turn count and session model after each interactive turn; `--continue` restores them into `state` (the history list in place, since `_wrap()` re-binds it). `/clear` moves the file aside as `<name>.saved-<stamp>.json` (newest `SESSION_KEEP` unnamed ones kept), and `/resume` or `--resume` swaps one back in. `/rename` stores `name` in the file (`state.session_name`; unique per project and provider, ignoring case; one line, at most 60 characters, not starting with `-`), and `main()` turns `--resume NAME` into a swap plus `--continue` before anything else runs, so it also works with `-p` |
| `config.py` | Configuration constants and dual-mode authentication resolution |
| `proxy.py` | The HTTPS proxy the model traffic goes through (`--proxy`, `--proxy-ca`, `--llm-fw`; see [Proxy](#proxy)) |

### Authentication Modes

`llm_light` supports two distinct authentication paths:

*   **API Key Mode**: Standard developer access. Authenticates via the `x-api-key` header. Billing is usage-based (USD).
*   **OAuth/Pro Mode**: Accesses your Claude Pro subscription via the official CLI session. Authenticates via the `Authorization: Bearer` header. Billing is flat-rate (Tokens shown, USD hidden).

### Providers

`--provider {claude,gemini,codex}` picks the agent; the default is `claude`, with the authentication order above. Each provider checks its own login in `require_auth()`: `gemini` needs the Antigravity CLI `agy` on PATH (`agy not found. Install the Antigravity CLI: https://antigravity.google`; where agy keeps its sign-in depends on the platform, so nothing is checked up front and a missing sign-in surfaces as a `ProviderError` on the first question); `codex` needs the `codex` CLI plus `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`, from `codex login`). Claude's capabilities are caching, effort, top tier, USD budget, usage, agent and shell. Codex and Gemini have usage only (Gemini's from the `usage` on agy's `result` event, `gemini._usage`, #159), so the route line shows `effort unavailable for <provider>` (and `top tier unavailable` on a top-tier route), `/budget` answers `Budget unavailable for <provider>`. Usage without USD budget (`ui.priced()` is false) means token counts at prices llm_light does not know: Codex's and Gemini's turn stats, `/cost`, footer and status bar show tokens and the cached share, never dollars, and `_record_usage()` adds them to the session tokens without touching the session cost or the lifetime dollar stats. `codex._usage()` sums the `turn.completed` usage events and splits `cached_input_tokens` out of `input_tokens` (codex counts them inside it, Claude's usage apart); unreadable usage is `None`, which reads as unavailable for that turn. Finally, `--allow-command` prints that shell access is disabled (the agent cannot run commands). Neutral code never names a provider or its binary (`tests/unit/test_provider_isolation.py`).

Gemini runs `agy` in plan mode with a per-run `--settings` that denies command, mcp and file (agy otherwise runs the user's globally allowed MCP shell), reading and writing NDJSON events (`--input-format/--output-format stream-json`) and taking the `result` event's `response`. Every message starts with a no-tools preamble (`gemini._PREAMBLE`): without it real agy called `run_command` in 3 of 3 edit runs, was denied, and returned an empty response; with it 3 of 3 returned SEARCH/REPLACE blocks. A SUCCESS result with an empty response and a non-empty `denied_actions` raises a `ProviderError` naming the denied action. agy also runs as a custom agent with no tools (`--agent llm-light`, `gemini.AGENT_NAME`): the run writes `gemini._AGENT`, whose frontmatter sets `excludeDefaultComponents: true` and `tools: []`, to `.gemini/config/agents/llm-light.md` in its temporary home, which agy reads custom agents from, so the working directory stays empty. That drops agy's built-in tools and its own prompt sections. No permission kind stops `search_web`, so before this a Gemini turn could put repo code into web-search queries (#122); probed with agy 1.4.12 on a prompt that asks for a web search, the default agent ran `search_web` 3 times at 34,648 input tokens, the custom agent ran no tool in 5 of 5 runs at 1,249 input tokens. The run fails closed by allowlist: a finished (`DONE`) `step_update` passes only if its tool is read-only (`read_file`, `view_file`, `list_dir`, `find_by_name`, `grep_search`), or it is a command (`run_command`, `send_command_input`), mcp (`call_mcp_tool`) or file (`write_to_file`, `replace_file_content`, `multi_replace_file_content`, `sed_file`, `notebook_edit`) tool and the count of finished steps of that kind does not exceed the count of `denied_actions` records of that kind. One exception: a `write_file` step whose `Path` is inside the run's own temporary home under `.gemini/antigravity-cli/brain/` passes, because that is agy's plan-mode plan artifact (`gemini._is_plan_artifact`; seen in 4 of 5 direct web-search probes of agy 1.4.12). Any other tool raises `agy ran <tool>, which llm_light forbids. Ask again; if it keeps happening, choose another --provider.` agy also runs with `HOME` and `USERPROFILE` set to a second empty temporary directory (`gemini._isolated_env`), so it loads none of the user's plugins, hooks, rules or MCP servers from `~/.gemini` and keeps its conversations and logs out of the user's history. `gemini._carry_sign_in` keeps the sign-in working there, which differs by platform (agy 1.4.12, #124). Windows needs nothing: the Credential Manager ignores `HOME`, and a real signed-in turn answers. Without a usable OS keychain agy reads `.gemini/antigravity-cli/antigravity-oauth-token` under `HOME` (strace on Linux), so that one file is copied in, mode 0600, and removed with the home; a token agy refreshes during the run is not written back. On macOS agy calls `/usr/bin/security`, which finds no login keychain once `HOME` moves (exit 44, macOS 26.6.2) and finds it with only `Library/Keychains` linked, so that directory is symlinked in; `shutil.rmtree` removes the link without following it. Signed out, agy waits for a browser sign-in if it has a controlling terminal and exits with an error if it has none, and `runner.new_session()` gives it none on POSIX. The user's chosen model is read from `~/.gemini/antigravity-cli/settings.json` and added to `--settings` as `model` (`gemini._settings`), since the empty home would otherwise give agy's default model. `search_web` is not on the read-only allowlist: the agent has no tools, so a search means the agent did not load, and since the query may quote repo code the turn fails rather than answer. Codex runs with read-only flags, and with its own prompt trimmed (`codex._argv_tail`): `-c model_instructions_file` replaces codex's agent instructions with `codex.INSTRUCTIONS`, a no-tools note written to a file in a second temporary directory, and `-c` switches drop codex's skills list, permissions text, collaboration-mode text, environment block and the tool schemas of twelve unused features (`codex._TRIM_CONFIG`). Every switch is a `-c` key, never `--disable`, because codex exits 1 on `--disable <unknown feature>` but ignores an unknown config key. Without the no-tools instructions codex took hidden extra model rounds, each re-sending the whole prompt (`docs/BENCHMARKS.md`, section 2e). Both run in an empty temporary working directory (`runner.empty_temp_dir()`), removed after the run. The repo is not their working directory because a CLI started in the project loads that project's configuration: a real `gemini` CLI run there executed a repo's `.gemini/settings.json` hooks and `tools.discoveryCommand`. Their answers come only from the transcript on standard input (skeleton and retrieved chunks). A failing run raises `ProviderError`, whose message names the provider, the exit code and the last error line of stderr (stack frames skipped), and a run is stopped after `TIMEOUT_SECS` (600). The conversation is saved per provider, so `--continue` does not mix histories.

### External Dependencies

- **Anthropic API** - Claude models (Haiku, Sonnet, Opus)
- **sentence-transformers** - Text embeddings for RAG
- **tree-sitter** - AST parsing for method-level chunking
- **Git** - Version control for auto-commit feature
- **mcp** (optional, `llm-light-cli[mcp]`) - the MCP client for API-key mode

<details>
<summary>View PlantUML Source</summary>

```plantuml
@startuml LLM_Light_Component_Diagram
title LLM Light - Component Architecture

skinparam componentStyle rectangle
skinparam backgroundColor #FEFEFE
skinparam componentBackgroundColor #FFFFFF
skinparam arrowColor #555555

package "User Interface Layer" {
  [main.py\n(Entry Point)] as Main
  [ui.py\n(Terminal UI)] as UI
  [state.py\n(Shared State)] as State
}

package "Core Processing" {
  [llm.py\n(LLM Client)] as LLM
  [retrieval.py\n(RAG Engine)] as Retrieval
  [editor.py\n(Code Editor)] as Editor
  [executor.py\n(Command Runner)] as Executor
}

package "Indexing & Caching" {
  [indexer.py\n(File Watcher)] as Indexer
  [skeleton.py\n(Skeleton Builder)] as Skeleton
  [parsing.py\n(Code Parser)] as Parsing
  [config.py\n(Configuration)] as Config
}

package "External Services" {
  database "Anthropic API\n(Claude)" as Anthropic
  [sentence-transformers\n(Embeddings)] as Embeddings
  [tree-sitter\n(AST Parsing)] as TreeSitter
  [Git Repository] as Git
}

package "Support Modules" {
  [linter.py\n(Syntax Check)] as Linter
  [retry.py\n(Backoff)] as Retry
  [streaming.py\n(Stream Handler)] as Streaming
  [git_manager.py\n(Git Ops)] as GitManager
}

' User interactions
Main --> UI : displays status
Main --> State : reads/writes session data

' Main orchestration
Main --> LLM : chat/one_shot
Main --> Indexer : file watch events

' LLM processing flow
LLM --> Retrieval : retrieve relevant chunks
LLM --> Skeleton : get cached skeleton
LLM --> Anthropic : API calls
LLM --> Streaming : stream responses
LLM --> Retry : error handling
LLM --> Editor : parse & apply edits
LLM --> State : update tokens/cost

' Retrieval pipeline
Retrieval --> Embeddings : encode queries/chunks
Retrieval --> State : access chunk_store
Retrieval --> Parsing : get code snippets

' Indexing pipeline
Indexer --> TreeSitter : parse AST
Indexer --> Parsing : chunk files
Indexer --> Embeddings : generate embeddings
Indexer --> Skeleton : update on file changes
Indexer --> State : update chunk_store

' Editor flow
Editor --> Linter : validate syntax
Editor --> GitManager : auto-commit changes
Editor --> UI : show diffs

' Git integration
GitManager --> Git : git operations
Main --> GitManager : /undo command

' Configuration
Config -[hidden]--> LLM : API keys, models
Config -[hidden]--> Retrieval : thresholds
Config -[hidden]--> Indexer : extensions

note right of State
  Shared state includes:
  - chunk_store (embeddings)
  - conversation_history
  - session_tokens
  - global_stats (lifetime savings)
  - skeleton_context
  - file_hashes
end note

note bottom of Anthropic
  3-tier caching:
  1. Skeleton (session)
  2. History (turns)
  3. Chunks (queries)
end note

@enduml
```

</details>

---


## Module wiring

Modules import each other's globals at load time, so `__init__.py` wraps every public function in a `_wrap()` decorator. The wrapper calls `_sync_bindings()` before and `_refresh_exports()` after each call, which keeps every module's references to shared state (`state.embedder`, `state.chunk_store`, ...) consistent without circular imports. Shared mutable state lives in `state.py` behind `threading.Lock`.

## Agent mode and allowed commands

In OAuth mode, `AGENT_SYSTEM_PROMPT` replaces `SYSTEM_PROMPT` when `ALLOWED_COMMANDS` is non-empty. The agent then edits with its own Edit/Write tools and runs the allowed commands to check its work. SEARCH/REPLACE blocks only land after the turn, which is too late to test.

`ALLOWED_COMMANDS` holds shell command prefixes, each becoming `Bash(<prefix>:*)`, or `PowerShell(<prefix>:*)` for a `powershell:` or `pwsh:` entry. They come from `--allow-command`, `LLM_LIGHT_ALLOW_COMMANDS` (`;`-separated) or `/allow`. `/allow` applies from the next turn, also when the CLI session is resumed (`/allow`, then the agent ran the command in the same session; `/disallow`, then a command that leaves a marker file did not run, checked live with claude 2.1.317). It saves the entry in `session.allowed_file()` under `SESSION_DIR`, outside the project, so a cloned repo cannot supply one. `commands.load_saved_allowed()` re-validates each entry at startup and `main()` names them on stderr. `/allow` refuses a prefix with a character the guard would refuse, since it could never match, and `,`, `(`, `)` or `*`, which would change the meaning of the `Bash(<prefix>:*)` rule. The CLI runs with `--permission-mode acceptEdits`, `--setting-sources user` and `--strict-mcp-config`, so the target repo's `.claude/settings*.json` (allow rules, hooks) and `.mcp.json` never apply. A shell stays disallowed unless an entry names it: Bash for untagged or `bash:` entries, PowerShell for `powershell:` or `pwsh:`. The CLI matches an allow rule against a command's first statement only, so `echo a && touch b` ran under `Bash(echo:*)` (issue #70, claude 2.1.314). With an allowlist, the `--settings` file therefore also installs a `PreToolUse` hook for `Bash|PowerShell` that runs `cli_shell_guard.py` with `python -I` and blocks (exit 2) any command containing a character from the `shell` tool's refused set below, chosen by the hook's `tool_name`. The guard is stdlib-only and fails closed: input it cannot read also exits 2. Refused commands are listed after the turn.

In API mode the same prefixes gate the `shell` tool in `tools.py`. A command must start with an allowed prefix on a word boundary, and any of `; & | < > `` ` `` $` or a newline is refused before whitespace is collapsed. A PowerShell command also may not contain `( ) { } @`: PowerShell evaluates an argument written as `(New-Item x)` or `@(...)`, which runs a second command with none of the other characters. `cli_shell_guard.forbidden_chars(tool)` holds both sets, and any tool name other than `Bash` gets the PowerShell one. It runs from the project root with a 120 s timeout (`LLM_LIGHT_RUN_TIMEOUT`); on timeout the whole process tree is killed (`taskkill /T` on Windows, the process group on POSIX). The tool is always in `TOOLS`, even with no allowlist, so the cached prompt prefix does not change between sessions.

## Commands the user runs

`!command` and `/run` go through `executor._run_command`, which needs no allowlist because the user typed the command. A thread queues the output lines and the main thread prints them, so they appear as they arrive and neither a blocking read nor a chatty child can stop Ctrl+C or the timeout (`LLM_LIGHT_RUN_TIMEOUT`, 120 s) from being noticed. Both kill the whole process tree (`kill_process_tree`): on Windows, killing only the `cmd.exe` of `shell=True` left its child holding the pipe, so a 1 s timeout took 30 s. Ctrl+C is re-raised, so the caller returns to the prompt; it used to end the session. Stdin is closed, because nothing on screen would say a command is waiting for input. Once the command has exited, reading stops after `_RUN_DRAIN_SECS` even if a background child still holds the pipe, and that child is left running.

On Windows both this and the `shell` tool run through `user_shell.command_argv`: Git Bash when Git for Windows is installed (from its install locations, then a `git.exe` in a known layout on an absolute PATH entry outside the project; never `bash` on PATH, which is WSL's), else `cmd.exe`; `LLM_LIGHT_SHELL` overrides it. So a line means the same typed after `!` as in the agent's Bash tool. Under `cmd.exe` the `shell` tool refuses `%` too, the counterpart of the `$` the list already refuses. `/status` names the shell in use.

## Repeating a question

`/loop` runs in the prompt thread and sends each turn through `main._ask`, as the prompt and `/run` do, so a turn gets the queued `!command` output, its `@file` contents read afresh, the budget prompt and the login offer. `chat()` handles Ctrl+C and API errors itself and returns False, which is how the loop knows to stop. An interval waits with `time.sleep`, because `Event.wait` on Windows does not notice Ctrl+C until it ends, and is capped at `LOOP_MAX` turns like a count, so a loop left running has a spending limit even without `/budget`. `--until` runs its command before each turn and queues a failure's output for that turn. An exit of 126, 127 or 9009, or a timeout, stops the loop instead of counting as "not yet": a mistyped check could never pass, and would spend all 20 turns.

## Permission modes

`permissions.py` holds the mode in `state.permission_mode`; `--permission-mode`, `-y`, `--dangerously-skip-permissions` (alias `--yolo`), `/permissions` and `shift+tab` set it. `bypassPermissions` is offered only when `state.bypass_allowed` is set, which only the startup flags do.

| Mode | SEARCH/REPLACE edits | Agent commits | API `shell` tool | OAuth CLI agent |
|---|---|---|---|---|
| `default` | asked | asked | allowlist | `--permission-mode acceptEdits`, `--tools Read,Grep,Glob,Edit,Write` plus allowlisted shells, allowlist rules, guard hook |
| `acceptEdits` | applied | made | allowlist | as `default` |
| `plan` | shown, not applied | none | refuses every command | `--permission-mode plan`, `--tools Read,Grep,Glob`, Bash, PowerShell, Edit, Write and NotebookEdit disallowed |
| `bypassPermissions` | applied | made | any command via the shell, unchecked | `--permission-mode bypassPermissions`, `--tools` adds Bash, PowerShell, WebFetch and WebSearch, no disallowed tools, no allow rules, no guard hook; `AGENT_SYSTEM_PROMPT` plus a line saying any command may run |

MCP tools run in `default`, `acceptEdits` and `bypassPermissions` and never in `plan`: the API `tools.run_tool()` refuses them and the OAuth agent gets no servers. See [MCP servers](#mcp-servers).

`claude._agent_tools()` builds the OAuth `--tools` list. Without it the CLI sends every built-in schema on every model round, about 11,000 input tokens (`TOKEN-SAVINGS.md`, lever 7), and offers tools that act outside the turn, which reads untrusted repo text: `Task` and `CronCreate` ran with no prompt under the old flags. `NotebookEdit` is added only when the project has a `.ipynb` (`_project_has_notebooks`), because `Edit` refuses notebooks and the schema costs 630 tokens a round. The web tools come with bypass, or in any mode with `--web` (`config.WEB_ACCESS`, also `LLM_LIGHT_WEB=1`), which also passes `WebSearch` and `WebFetch` in `--allowedTools` (`claude._web_rules`): offered without a rule, a `-p` run denies them, since there is no one to ask. Plan mode gets them too, as reads. Off by default, because a fetched URL can carry repo text out of the machine. The two schemas add 984 input tokens a model round (claude 2.1.322, Haiku: 4,251 to 5,235, the same in 2 of 2 runs). `main._web_note` says at startup when `--web` has no effect: an API key, or a provider without the agent. `--disallowed-tools` stays as the backstop, since CLI 2.1.118 ignored `--tools ""`. Hidden calls (the history summary) still get `--tools ""`. API-key requests are untouched.

The `shell` tool's description (in the cached `TOOLS`) still names the allowlist in bypass mode, and `SYSTEM_PROMPT` says nothing of plan mode. Rather than change the cached prefix, `permissions.question_note()` puts a one-line note before the question in the uncached last turn: always in plan mode, and in bypass mode when the model has the API `shell` tool. In `default` and `acceptEdits` it is empty, so those requests are byte-for-byte what they were before the modes existed.

## Hooks

`hooks.load()` reads `~/.llm_light/hooks.json` (`LLM_LIGHT_HOOKS_CONFIG` moves it), never a project's `.claude/settings.json`: a hook runs a command, and a cloned repo must not choose one. It is not part of `settings.json` because `/config` rewrites that file with only the keys `settings.py` knows. The shape is Claude Code's `hooks` setting; an unknown event, a matcher that is not a regular expression, or a hook that is not `{"type": "command", "command": ...}` with a positive `timeout` is skipped and named at startup and by `/doctor`.

- **Subscription.** For an agent turn (`tools=True`), `_make_cli_subprocess_call` appends the user's groups to its `--settings` file, after the `cli_shell_guard` `PreToolUse` hook, so no user hook runs before the guard and changes the command it checks. The user's `~/.claude/settings.json` hooks never reached the agent, whose home is an empty temp dir; that is also why a hook command should use absolute paths. Checked live with Haiku (claude CLI): `PostToolUse` on `Read` logged the call, `Stop` fired, and a `PreToolUse` exiting 2 blocked the read and the model answered with the nonce from the hook's stderr.
- **API key.** `tools.run_tool` runs `PreToolUse`, the tool, then `PostToolUse` when the file has any hook (`hooks.configured()` empty skips all of it). Names are mapped to Claude Code's (`hooks.CLI_NAMES`: `read_file` is `Read`, `grep` `Grep`, `list_files` `Glob`, `shell` `Bash`; `fetch_chunk` and `mcp__` tools keep theirs), and `Read` gets an absolute `file_path`. A command runs through `user_shell.command_argv` from the project root, with the event JSON (`hook_event_name`, `tool_name`, `tool_input`, `cwd`, and `tool_response` after the call) on stdin. Exit 2 on `PreToolUse` returns `Error: a PreToolUse hook blocked this call: <stderr>` without running the tool; on `PostToolUse` the stderr is appended to the result. Another exit code, a failure to start or a timeout (`timeout`, default 60 s; the process tree is killed) goes to `ui.notify` and the call goes on. `editor.apply_edits` runs `PostToolUse` as `Edit` (file existed) or `Write` (new) for each written file before `git_manager.auto_commit`, and prints exit 2's stderr, since no model waits for that result. Other events are not run in this mode.

## MCP servers

`mcp_servers.load()` reads `~/.llm_light/mcp.json` (`LLM_LIGHT_MCP_CONFIG` moves it), never the project's `.mcp.json`: a cloned repo would otherwise start programs and add tools. It takes the shape of Claude Code's `.mcp.json` and keeps only `type` (`stdio`, `http`, `sse`), `command`, `args`, `env`, `url` and `headers`; a server name must be 1 to 32 of `[A-Za-z0-9_-]`, because it becomes the `mcp__<name>` allow rule, where a comma, bracket or star would change the rule. An entry that does not fit is skipped and named at startup and by `/doctor`. Only a provider with the `MCP` capability (Claude) uses the servers; for Gemini and Codex `startup_lines()` says they are not used. `Provider.start_mcp()` decides how:

- **Subscription.** Nothing starts in llm_light. Each agent turn writes `mcp_servers.cli_config()` to `llm_light_mcp.json` (mode 0600, since `env` and `headers` may hold tokens) in the CLI's isolated home and passes `--mcp-config <file>`, still with `--strict-mcp-config`, plus an `mcp__<server>` rule per server in `--allowedTools`. Without the rule a `-p` run denied every call: `permission_denials` named `mcp__nonce__nonce` (claude 2.1.321); with it the tool ran. `--tools` lists built-in tools only and does not hide MCP tools (same check). Each stdio server gets `HOME` and `USERPROFILE` set to the real home unless its `env` sets them, since the CLI runs with an empty one; checked live through `llm_light.py -p`, where a server reported the real home directory and a second server's nonce came back. Plan mode and the tool-free hidden calls (`tools=False`) get no `--mcp-config`. Bypass gets the servers without rules. Cost, measured through `llm_light.py -p` with Haiku on a one-word answer: 4,651 tokens a request with the 4-tool test server against 4,347 without, so +304, the same in 3 of 3 runs each; the turn took 15 s against 14 s.
- **API key.** `mcp_servers.start()` runs once in `main()`, before the first turn, and blocks until each server has listed its tools, failed or passed `MCP_START_TIMEOUT` (60 s). A `_Manager` holds one asyncio loop in a daemon thread and one task per server, which keeps that server's `mcp.Client` open until `stop()` (registered with `atexit`): anyio needs a context left in the task that entered it. A stdio server runs in the project directory with its stderr in `SESSION_DIR/mcp/<name>.log`: on the terminal it would land in a streamed reply, and a server holding llm_light's stderr pipe kept a probe from exiting. `${VAR}` and `${VAR:-default}` expand from the environment, as the CLI expands them. Each tool becomes `mcp__<server>__<tool>` (other characters become `_`; a name over the API's 64 is dropped), with its description cut to 2,048 characters and its `input_schema` as the server gave it. `tools.all_tools()` puts them after `TOOLS` in every round and in `warm()`, and returns `TOOLS` itself when there are none, so a session without servers sends the same bytes as before (the analytical token benchmark: $0.0622 before and after). The list never changes in a session, because tools render first in the cached prefix: `tools/list_changed` is ignored, and a server that dies keeps its tools, whose calls return an error. `tools.run_tool()` refuses an `mcp__` call in plan mode, as it refuses `shell`, and otherwise calls `mcp_servers.call()`, which waits `MCP_CALL_TIMEOUT` (120 s). An error that reaches the model or the screen goes through `_redact()` first, which masks the entry's env and header values, the bearer token alone, and the URL's query and password: an HTTP error can quote them. `result_text()` joins the text blocks, names an image, audio or binary resource instead of sending it, falls back to `structured_content`, and caps the result at 40,000 characters like the other tools.

MCP traffic is not model traffic, so `--proxy` does not apply to llm_light's own MCP client. On a subscription the CLI starts the servers with its own environment, which `proxy.child_env()` has given the proxy settings.

## Proxy

`main._configure_proxy()` reads `--proxy` and `--proxy-ca`, else `LLM_LIGHT_PROXY` and `LLM_LIGHT_PROXY_CA`, else, with `--llm-fw`, `proxy.LLM_FW_URL` (`http://127.0.0.1:8080`) and `proxy.LLM_FW_CA` (`~/.llm-fw/ca.crt`), and passes them to `proxy.configure()`. A URL that is not `http(s)://host[:port]`, a missing CA file or one with no PEM certificate is a usage error (exit 2) before any request. The CA is appended to certifi's bundle in a temporary file removed at exit, because a proxy that only tunnels a host (llm-fw does so for every non-LLM host) passes the real certificate through.

Only the model traffic goes through it. `os.environ` is never changed, so embedding-model downloads, git and the `shell` tool keep their own connection. The Anthropic client (`claude._new_client()`) gets an `httpx2.HTTPTransport` (the SDK's HTTP client since anthropic 2.0) with the proxy and `proxy.ssl_context()`, or the SDK's default trust (the OS store, through `truststore`) when no CA is given; a transport rather than `proxy=`, because `anthropic.DefaultHttpxClient` mounts the environment's `HTTPS_PROXY` on `https://`, which beats the `all://` mount of `proxy=`, and `llm-fw setup` sets `HTTPS_PROXY` for the whole user. The `claude`, `codex` and `agy` children get `proxy.child_env()`: `HTTPS_PROXY` (never `HTTP_PROXY`: llm-fw forwards CONNECT only), `NODE_EXTRA_CA_CERTS` (the CA, for Node) and `SSL_CERT_FILE` (the combined bundle, for other clients). Without a proxy, `child_env()` returns its argument unchanged, so codex still inherits the environment as before.

`agy` was checked end to end on Windows (agy 1.4.12, llm-fw 0.9.4, #153), through `llm_light.py --provider gemini --proxy ... --proxy-ca ...` on a private `LLM_FW_DIR`, with nothing added to the OS store. It honours `HTTPS_PROXY`: all 58 samples of its sockets during a turn had the proxy as their peer. It verifies certificates: a leaf from a private CA for the right host was accepted with `--proxy-ca` and refused without it (`x509: certificate signed by unknown authority`), and either refusal fails the turn in about 11 s with that error. The model host is `daily-cloudcode-pa.googleapis.com`. Stock llm-fw tunnels it (`inspected: false`), so nothing is scanned until `extraTargets` lists it (`inspected: true`, `POST /v1internal:streamGenerateContent`).

## Test mode

`--test-mode small` generates 5 files with 10 methods each. `--test-mode extra-large` generates 1000 files with 20 methods each. Both need no API key. Helpers live in `testing.py`. The fakes themselves are `MockManager` in `tests/utilities/test_mocks.py`, which patches functions by name and so breaks silently when their signatures change (#100). `tests/integration/test_test_mode.py` runs `--test-mode small` end to end as a subprocess, so CI catches that drift.

## Reliability and lifecycle

- **Esc cancels a turn** (`esc_key.py`, #209): `chat()` wraps `providers.active().complete()` in `esc_key.listening()`, so keys are read only while the model answers, never during the edit approval prompt or a menu, and never in `-p` (`state.event_sink` set), without a terminal, or with `LLM_LIGHT_ESC=0`. POSIX puts the terminal in cbreak mode (no echo) and restores it on the way out; Windows polls `msvcrt`. `Parser` tells a lone Esc from an arrow or function key (`ESC [ A`, `ESC O P`, Windows' `\xe0` and `\x00` prefixes) and drops Enter, Tab and other control keys, so nothing is submitted for the user; printable keys collect in `state.typeahead`, which the next prompt gets as its `default`. On Esc, POSIX sends SIGINT to the process, which breaks a blocking read as Ctrl+C does. Windows calls `_thread.interrupt_main()` and kills the turn's agent process (`state.turn_process`, set by `claude._make_cli_subprocess_call` and `runner.run` through `esc_key.running()`): the interrupt alone waits for a blocking read to end (a pipe read with no output ran its full 8 s; with the child killed it stopped in 0.5 s), and a console `CTRL_C_EVENT` reaches no process that started with Ctrl+C ignored, a flag children inherit. With an API key there is no child, so on Windows the turn ends at the next piece of the streamed reply. `stop()` waits 0.5 s after an Esc so the interrupt is raised inside `chat()`'s `try`, not at the prompt, where it would quit. Tested with real keys: `tests/unit/test_esc_key.py` types into a pty on POSIX (runs on Linux CI) and into a console of its own on Windows through `WriteConsoleInputW` (not run on CI, which is Linux). Checked live on Windows: Esc during a subscription turn printed `Interrupted. The agent was stopped...` and the text typed after it waited at the prompt.
- **Auto-retry** (`retry.py`): exponential backoff 2 s, 4 s, 8 s (capped at 60 s), max 3 attempts. Non-retriable errors (401/403/404/400) fail immediately. `retry.py` checks `sys.modules` instead of importing the SDK.
- **Thread-safe state** (`state.py`): all shared state is protected by `threading.Lock`. Global stats (`~/.llm_light_stats.json`, or `LLM_LIGHT_STATS_FILE`) load on startup and save on every usage update. `state.turn_count` counts turns; do not derive it from `conversation_history`, which compression shrinks.
- **Streaming** (`streaming.py`, `ui.ResponseView`): both auth modes render through one `ResponseView` per turn. Finished markdown blocks print once; only the block still arriving is redrawn. Thinking changes the spinner label. OAuth history summaries go through the CLI (`render=False`), since that mode has no API key.
- **Startup**: the `anthropic` SDK is imported lazily (`providers/claude.py: _LazyClient`), so OAuth sessions never load it. The prompt appears in about 0.5 s instead of 2 s warm and 7 s cold.
- **Startup imports**: `tests/unit/test_startup_imports.py` pins what `import llm_light.main` must not load (`numpy`, `tqdm`, `markdown_it`, `pygments`, `watchdog.observers`, `anthropic`, `torch`). Import them inside the function that needs them. `main.Observer` is a function, so it still patches like the class.
- **Watchdog lifecycle** (`main.py`): the observer starts on its own thread, so importing it and scheduling a large tree do not delay the prompt; a failure goes to `ui.notify`. It is stopped and joined on exit (5 s timeout). Ctrl+C cancels the current turn and kills the CLI agent's process tree (`kill_process_tree` in `providers/runner.py`; `taskkill /T` on Windows, where the CLI runs under `cmd.exe`, and the CLI's process group on POSIX), then returns to the prompt. At the prompt it exits. SIGTERM exits, and `start_chat()`'s `finally` sets `stop_event`.
