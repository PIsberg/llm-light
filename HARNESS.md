---
name: llm_light
description: Lets an agent find, change and test llm_light, an interactive CLI that answers questions about and edits a codebase with Claude, using prompt caching and retrieval.
---

You are working on `llm_light`, a Python CLI. It sends a cached project skeleton and the code chunks each question retrieves to Claude, and applies the SEARCH/REPLACE edits Claude returns. Change the package with the tests as the guard, and measure any change that affects tokens before you keep it.

- `llm_light/`: the package, one module per responsibility. Module roles and the data flow are in [docs/architecture/components.md](docs/architecture/components.md).
- `llm_light/hooks.py`: the user's hooks from `~/.llm_light/hooks.json`, passed to the claude CLI agent on a subscription and run by llm_light around its tools and edits with an API key. Tests: `tests/unit/test_hooks.py`, which runs real hook processes.
- `llm_light/mcp_servers.py`: the user's MCP servers from `~/.llm_light/mcp.json`, passed to the claude CLI agent on a subscription and connected through the optional `mcp` SDK with an API key. Tests: `tests/unit/test_mcp_servers.py` and `tests/integration/test_mcp_stdio.py`, which runs the real stdio server in `tests/fixtures/mcp_nonce_server.py` and needs `mcp` installed (CI installs it).
- `llm_light/proxy.py`: `--proxy`, `--proxy-ca` and `--llm-fw`, the HTTPS proxy (such as the llm-fw firewall) the model traffic goes through. Tests: `tests/unit/test_proxy.py`.
- `llm_light/providers/`: the agents behind one interface, six modules: `__init__.py` (registry), `base.py` (`Provider`, `ProviderError`, capabilities, `json_events`), `claude.py`, `gemini.py` (the `agy` CLI), `codex.py` and `runner.py` (CLI subprocess runner). Select one with `--provider`. Their tests are `tests/unit/test_providers.py`, `test_provider_runner.py`, `test_provider_gemini.py`, `test_provider_codex.py`, `test_provider_isolation.py`, `test_provider_route.py`, `test_provider_startup.py`, `test_provider_usage.py` and `test_session_provider.py`.
- `tests/`: unit, integration and utility suites, regression fixtures and benchmark scripts. See [TESTING.md](TESTING.md).
- `docs/`: architecture, configuration, benchmarks and CI reference. See [docs/DOCS.md](docs/DOCS.md).
- `installers/`: install scripts for Linux, macOS and Windows.
- `scripts/`: SBOM generation, a startup benchmark, and `release.py` (version and changelog edits for a release).
- `.claude/skills/release/`: the `/release` skill, the only tracked part of `.claude/`.
- `.github/workflows/`: CI. See [docs/WORKFLOW.md](docs/WORKFLOW.md).

Repository rules, invariants and commands are in [CLAUDE.md](CLAUDE.md).
