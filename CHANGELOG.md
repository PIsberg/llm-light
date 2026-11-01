# Changelog

All notable changes to `llm-light` are listed here. Versions follow [Semantic Versioning](https://semver.org/); until 1.0, a minor version may change behaviour.

## [Unreleased]

### Added

- **Send the model traffic through a proxy, such as the llm-fw firewall.** `--proxy URL` and `--proxy-ca PATH` (or `LLM_LIGHT_PROXY` and `LLM_LIGHT_PROXY_CA`) route the Anthropic client and the `claude`, `codex` and `agy` CLIs through an HTTPS proxy and trust its CA on top of the public roots; `--llm-fw` uses a local llm-fw's defaults. Embedding-model downloads, git and agent commands keep their own connection. `--proxy` wins over an `HTTPS_PROXY` already in the environment, which `llm-fw setup` sets. Without `--proxy-ca` the Anthropic client keeps its default trust, the OS store. `certifi` is now a declared dependency (it was already installed through `requests`).

### Changed

- **Untracked `.md` files stay out of the prompt.** In a git repository the skeleton now carries only the `.md` files git tracks (staged ones included), so a local plan or draft you have not added is no longer sent, and no longer paid for, on every turn and cache write. The directory tree still lists it. On this repo two local drafts were 699 tokens of a 17,576-token cached prefix, and a modelled 43-question session cost 1.1% less (`benchmark_session_cache.py`). Outside a git repository every `.md` file still goes in (#142).
- **Codex sends about half the tokens per question.** llm_light replaces codex's own agent instructions with a short no-tools note and switches off the codex prompt sections and tool features an answer engine never uses. On one repo question (5 runs each, codex-cli 0.166.0) input tokens fell from a mean of 21,766 to 9,190 and output from 926 to 621, and the trimmed runs took no hidden extra model rounds. `tests/benchmarks/benchmark_cli_providers.py` measures it; see `docs/BENCHMARKS.md`, section 2e.
- **Codex turns show their token counts.** The turn line, `/cost`, the exit footer and the status bar show the input, cached and output tokens codex reports, instead of `tokens, cost and caching unavailable for codex`. No dollar figure: codex's prices are not Claude's, so nothing is added to the session cost or the lifetime dollar savings. Gemini still reports none (#148).

## [0.1.0]

The first release on PyPI, as `llm-light-cli`: `pipx install llm-light-cli` or `uv tool install llm-light-cli`. The command is still `llm-light`. It needs Python 3.12 or newer, and the first install downloads PyTorch (about 1.5 GB) through `sentence-transformers`.

### What it does

- **Interactive and one-shot CLI** (`llm-light`, with `claude-light` kept as an alias) for asking about and editing the codebase in the current directory. Questions can be typed, passed as an argument or piped in; `--continue` resumes the project's previous conversation.
- **Providers**: Claude (default; API key or Pro subscription through `claude auth login`), Gemini through the Antigravity CLI `agy`, and Codex through `codex login`, chosen with `--provider`.
- **Retrieval**: tree-sitter splits Java, Python, Go, Rust, JavaScript and TypeScript into one chunk per method. Each question gets the top-K chunks from a hybrid static and dense index, and the agent can fetch more with `fetch_chunk`.
- **Prompt caching**: the project skeleton sits in a cached system prompt, with cache breakpoints after the history and the retrieved chunks. In API-key mode the cache is warmed once per TTL, and only for the model the turns use.
- **Routing**: each question goes to Haiku, Sonnet or a top tier (asked first), and the first turn's model is kept for the session. `--model` pins a model, and `/budget` caps spend.
- **Editing**: edits are lint-checked, shown as a diff and auto-committed as `Claude: [summary]`. `/undo` reverts the last such commit. Permission modes are `default`, `acceptEdits` (`-y`), `plan` and `bypassPermissions` (`--dangerously-skip-permissions`).
- **Shell**: the agent runs only commands that start with an `--allow-command` prefix. Commands that chain, pipe, redirect or substitute are refused.
- **Optional prompt compression** with LLMLingua: `pipx install "llm-light-cli[llmlingua]"`.

### Known limits

- Licensed under PolyForm Noncommercial 1.0.0: free for personal and hobby use; commercial use needs a license.
- Gemini and Codex run without caching, effort, the top tier, `/budget` or the shell tool.

[Unreleased]: https://github.com/PIsberg/llm-light/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/PIsberg/llm-light/releases/tag/v0.1.0
