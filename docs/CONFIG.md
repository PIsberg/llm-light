# Configuration reference

Constants live in `llm_light/config.py`. Environment variables override the defaults where shown. How each part is used is in [architecture/](architecture/ARCHITECTURE.md).

**Renamed from `claude_light`.** The old `CLAUDE_LIGHT_*` names are no longer read; use the `LLM_LIGHT_*` names below. The home-directory files are no longer copied either: rename `~/.claude_light_automation_token`, `~/.claude_light_stats.json` and `~/.claude_light_sessions/` to `~/.llm_light_automation_token`, `~/.llm_light_stats.json` and `~/.llm_light_sessions/`. At startup llm_light prints one line on stderr for each `CLAUDE_LIGHT_*` variable set and each old home file whose new name does not exist, naming what to rename it to; it does not use them. This matters most for `CLAUDE_LIGHT_PROXY`: unrenamed, the model traffic no longer goes through the proxy. The `claude-light` command and `claude_light.py` are gone.

| Variable | Purpose |
|---|---|
| `--provider {claude,gemini,codex}` | Command-line flag, default `claude`. Selects the agent (`llm_light/providers/__init__.py: NAMES`, `DEFAULT`) |
| `--permission-mode {default,acceptEdits,plan,bypassPermissions}` | Command-line flag, default `default` (`-y` means `acceptEdits`). Held in `state.permission_mode`; see [architecture/components.md](architecture/components.md#permission-modes) |
| `--dangerously-skip-permissions`, `--yolo` | Command-line flag: start in `bypassPermissions` and allow it for the session. `--allow-dangerously-skip-permissions` allows it without starting in it (`state.bypass_allowed`) |
| `--model NAME` | Command-line flag: pin every question to `opus`, `sonnet`, `haiku`, `fable` or a model id in `MODEL_PRICES` (`state.pinned_model`, as `/model` sets). The budget cap still drops to Haiku. Claude only |
| `--proxy URL` / `LLM_LIGHT_PROXY` | Send the model traffic (Anthropic client, and the `claude`, `codex` and `agy` CLIs) through this HTTPS proxy, e.g. `http://127.0.0.1:8080` or `http://user:token@host:8080`. Wins over an `HTTPS_PROXY` in the environment. See [architecture/components.md](architecture/components.md#proxy) |
| `--proxy-ca PATH` / `LLM_LIGHT_PROXY_CA` | CA certificate of an intercepting proxy, trusted on top of certifi's public roots. Needs a proxy |
| `--llm-fw` | `--proxy http://127.0.0.1:8080 --proxy-ca ~/.llm-fw/ca.crt` (`proxy.LLM_FW_URL`, `proxy.LLM_FW_CA`), a local llm-fw firewall's defaults; an explicit `--proxy` or `--proxy-ca` wins |
| `-p`, `--print` / `-v`, `--version` | Command-line flags: one-shot mode, as `claude -p`; print the version from `pyproject.toml` |
| `commands.py: MENTION_MAX_CHARS` | Most characters attached per `@file` mention (default 50 000); the rest is cut with a note |
| `providers/runner.py: TIMEOUT_SECS` | Seconds an agy (Gemini) or Codex run may take before its process tree is killed (default 600) |
| `TARGET_RETRIEVED_TOKENS` | Token budget for retrieved context per query (default 6 000) |
| `MIN_SCORE` | Absolute cosine similarity floor to include a chunk (default 0.35; 0.45 dropped most natural-language questions) |
| `RELATIVE_SCORE_FLOOR` | Drop chunks below this fraction of the top chunk's score (default 0.60) |
| `HYBRID_RETRIEVAL` | Static + dense index (default on); `LLM_LIGHT_HYBRID=0` restores one auto-tuned model and the two floors above |
| `FAST_EMBED_MODEL` / `DENSE_EMBED_MODEL` | `static-retrieval-mrl-en-v1` / `all-mpnet-base-v2` |
| `HYBRID_MIN_SCORE` | Per-model cosine floor for "anything related at all" (static 0.22, mpnet 0.21: just above the best chunk for off-topic questions) |
| `HYBRID_SCORE_GAP` / `HYBRID_FAST_WEIGHT` | Keep chunks within this fused z-score of the top (1.5) / static model's share of the fused score (0.5) |
| `_RETRIEVAL_BUDGET` | Per-effort token budgets: low=1500, medium=3000, high=6000, max=9000 |
| `MAX_HISTORY_TURNS` | Compress and cap when stored turns exceed this (default 6) |
| `SUMMARIZE_BATCH` | Old turns collapsed per summary call (default 3) |
| `GLOBAL_STATS_FILE` | Path to global savings persistence (`~/.llm_light_stats.json`; override with `LLM_LIGHT_STATS_FILE`) |
| `SESSION_DIR` | Where `session.py` saves each project's conversation for `--continue`, one file per project named by a hash of its path (`~/.llm_light_sessions`; override with `LLM_LIGHT_SESSION_DIR`). In the home directory so a cloned repo cannot supply a conversation. `/clear` keeps the old one next to it as `<name>.saved-<stamp>.json` for `/resume` |
| `SESSION_KEEP` | Earlier conversations kept per project and provider after `/clear`, for `/resume` and `--resume` (default 10; the oldest is deleted beyond that) |
| `HEARTBEAT_SECS` | How often the heartbeat thread wakes (default 30) |
| `CACHE_TTL_SECS` | Seconds since the last turn or warm before the heartbeat warms the cache again (default 3300, five minutes inside the skeleton's 1-hour TTL) |
| `MODEL_HAIKU` / `MODEL_SONNET` / `MODEL_OPUS` | Model ID constants used by the router |
| `MODEL_FABLE` | `claude-fable-5-1`. Never routed to: offered, with Opus 5.5 and Sonnet 5.5, when a question routes to Opus |
| `SUMMARY_MODEL` | Model used for history compression (set to `MODEL_HAIKU`) |
| `MODEL_PRICES` / `model_prices()` | Per-model USD per M tokens (input, 5 m and 1 h cache write, read, output); every call is priced at the model that ran it. `PRICE_INPUT/WRITE/READ/OUTPUT` are the default model's rates (Sonnet 5.5: $2.00 / $2.50 / $0.10 / $10.00) |
| `SKIP_DIRS` | Directory names excluded from indexing |
| `EMBED_MODEL`, `TOP_K` | Set at runtime by `auto_tune()`; do not set manually |
| `LLMLINGUA_ENABLED` | **On** by default (silent no-op if `llmlingua` isn't installed). Opt out via `LLM_LIGHT_LLMLINGUA=0` |
| `LLMLINGUA_MODEL` | Default `microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank` (~280 MB). Override with `LLM_LIGHT_LLMLINGUA_MODEL` |
| `LLMLINGUA_TARGET_RATE` | Fraction of tokens to **keep** (default 0.5 = halve). Override with `LLM_LIGHT_LLMLINGUA_RATE` |
| `LLMLINGUA_MIN_TOKENS` | Skip compression below this size, since overhead exceeds savings (default 800). Override with `LLM_LIGHT_LLMLINGUA_MIN_TOKENS`; the retrieval budgets (1 500–9 000) are all above it |
| `LLMLINGUA_FORCE_TOKENS` | Tokens preserved verbatim (default `["\n", "```", "::", "//"]` so chunk structure survives) |
| `AGENT_SYSTEM_PROMPT` | Replaces `SYSTEM_PROMPT` in OAuth mode when `ALLOWED_COMMANDS` is non-empty. See [architecture/components.md](architecture/components.md#agent-mode-and-allowed-commands) |
| `ALLOWED_COMMANDS` | Shell command prefixes the OAuth agent may run. From `--allow-command` or `LLM_LIGHT_ALLOW_COMMANDS`. Ignored in bypass mode (any command runs) and plan mode (none runs). See [architecture/components.md](architecture/components.md#agent-mode-and-allowed-commands) |
| `ROUTE_ONCE` | Keep the model the session's first question routes to for every later turn; effort is still routed per turn, `/budget` still drops to Haiku at the cap, and `/clear` routes the model again. Default on. In `benchmark_session_cache.py` a session opening on Sonnet cost 71% more than per-turn routing, and one opening on Haiku cost 91% less because every later question, hard ones included, ran on Haiku. `LLM_LIGHT_ROUTE_ONCE=0` routes the model per turn |
| `WORKTREE_BOOST` | Z-score boost for chunks in modified, staged or untracked files; hybrid path only (default 1.0, tuned with `benchmark_models.py --worktree`; 1.5 starts losing answers). Disable with `LLM_LIGHT_WORKTREE_BOOST=0` |
| `LLM_LIGHT_RUN_TIMEOUT` | Timeout in seconds for the `shell` tool and the executor's commands (default 120; 0 or blank disables the cap) |
