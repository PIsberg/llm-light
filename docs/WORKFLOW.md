# CI workflow

Every workflow lives in `.github/workflows/`. This page lists what each one runs, in order, and the command to run the same step locally. Read the YAML for the exact versions and pinned action SHAs, which change often.

Every job starts with the same `step-security/harden-runner` step in `audit` mode (in `installers.yml`, on the Linux job only), which records outbound calls without blocking them. Permissions are read-only at the workflow level (`read-all` or `contents: read`). Four jobs grant more: `scorecards.yml` asks for `id-token: write` to publish its results, the `sbom` job in `sbom.yml` asks for `contents: write`, which its release-upload step needs, and the `testpypi` and `pypi` jobs in `publish.yml` ask for `id-token: write`, which trusted publishing needs. A job's own `permissions` block replaces the workflow-level one, so a job-level grant must list every permission the job uses.

## At a glance

| Workflow file | Name in GitHub | Runs on | Purpose |
|---|---|---|---|
| `test.yml` | Test Suite | push and PR to `main` | Unit, integration and utilities tests; token and retrieval regression checks |
| `fuzz.yml` | Fuzz Testing | push and PR to `main`, weekly Monday 00:00 UTC | Hypothesis tests of the parsers and router |
| `build-wheel.yml` | Build Wheel | push and PR to `main`, manual | Build and validate the sdist and wheel |
| `publish.yml` | Publish to PyPI | release published, manual | Uploads the sdist and wheel to PyPI on a release, or to TestPyPI when run by hand |
| `sbom.yml` | Generate SBOM | push and PR to `main`, release published, manual | CycloneDX software bill of materials |
| `codeql.yml` | CodeQL | push and PR to `main`, weekly Monday 00:00 UTC | Static security analysis |
| `scorecards.yml` | Scorecard supply-chain security | push to `main`, weekly Tuesday 07:20 UTC, branch-protection change | OpenSSF Scorecard |
| `dependency-review.yml` | Dependency Review | every PR | Flags vulnerable or disallowed dependencies a PR adds |
| `diagrams.yml` | Diagrams | PR to `main` that changes `docs/plantuml/**` | Fails if a changed `.puml` source comes without its re-rendered PNG |
| `installers.yml` | Installers | push and PR to `main` that change `installers/**` or `requirements.txt`, weekly Wednesday 06:40 UTC, manual | Runs each installer on a clean Linux, macOS and Windows runner and starts the installed tool |

Which of these are required to merge is a repository setting, not something the YAML states. This page does not verify it.

## `test.yml`: Test Suite

Runs on Python 3.12 on `ubuntu-latest`. Steps, in order:

1. **Harden the runner**, then **checkout** and **set up Python 3.12**.
2. **Install dependencies.** `pip install -r requirements.txt` plus `pytest` and `pytest-timeout`, so the suite runs on the dependencies the installers give a user (#72). `datasets` is not installed: only the live SWE-bench mode of `benchmark_retrieval.py` needs it, and CI runs the offline fixture.
3. **Run unit tests.** Three commands:
   - `tests/unit/test_editor.py` first, with a 60 s per-test timeout. It runs before `MockManager` patches `Path`.
   - `tests/unit/test_config.py`, same reason, same timeout.
   - Every other file in `tests/unit/`, with a 120 s timeout and `-x` (stop at the first failure).
4. **Run integration and utilities tests.** `pytest tests/integration/ tests/utilities/` with a 120 s timeout and `-x`. These are import and wiring checks, and they need no network.
5. **Run analytical benchmark.** Runs `tests/benchmarks/benchmark.py --json` into `tests/baseline_token_stats_new.json`, then `tests/linting/check_regression.py tokens` compares it with `tests/fixtures/baseline_token_stats.json`. The step fails if `check_regression.py` finds the numbers regressed against the baseline.
6. **Run retrieval benchmark (offline fixture).** Runs `benchmark_retrieval.py` on `tests/fixtures/retrieval_cases.json`, then `check_regression.py retrieval` compares it with `tests/fixtures/baseline_retrieval_stats.json`.

Steps 5 and 6 write their `*_new.json` output into `tests/`. Those files are not meant to be committed. Do not commit them by accident.

Not run by this workflow: the benchmark scripts that need a model or an API key, and `tests/test_fuzz.py`, which `fuzz.yml` covers. See [TESTING.md](../TESTING.md).

Local equivalent: the unit commands and the integration and utilities command above, then the two benchmark and regression pairs.

## `fuzz.yml`: Fuzz Testing

Python 3.12. Installs `requirements.txt` (which includes `hypothesis`) and `pytest`, then runs `python -m pytest tests/test_fuzz.py -v --tb=short` with `HYPOTHESIS_MAX_EXAMPLES=500` and `HYPOTHESIS_DEADLINE=2000` (2 s per example). `fail-fast` is off. The weekly schedule catches inputs the push-time run did not generate.

Local equivalent: the same pytest command, with the two environment variables.

## `build-wheel.yml`: Build Wheel

Builds the distribution the way a release would, then checks it:

1. **Install build tooling**, then **Build sdist and wheel** with `python -m build`, which writes to `dist/`.
2. **Validate package metadata** with `twine check dist/*`.
3. **Resolve dependencies (dry-run install).** Creates a venv in `/tmp` and runs `pip install --dry-run dist/*.whl`. This catches an install that fails only from the wheel's declared requirements.
4. **Upload built distributions** as the `dist` artifact.

Local equivalent: `python -m build && twine check dist/*`.

## `publish.yml`: Publish to PyPI

Uploads the package with trusted publishing: PyPI checks the workflow's OIDC token against a publisher registered on pypi.org, so no API token is stored in the repository. The upload carries PEP 740 attestations, the action's default. It never runs on a push or a PR, so CI does not exercise it before a release; the manual TestPyPI run is the dry run.

1. **build**: on a release, `tests/linting/check_release_tag.py` first fails the run unless the release tag is `v` plus the `pyproject.toml` version. Then `python -m build` and `twine check --strict dist/*`, and the `dist` artifact is uploaded.
2. **testpypi** runs only when the workflow is started by hand (`workflow_dispatch`). It uploads to https://test.pypi.org/project/llm-light/ under the `testpypi` environment.
3. **pypi** runs only on a published release. It uploads to https://pypi.org/project/llm-light/ under the `pypi` environment.

PyPI and TestPyPI accept a version once, even after it is deleted. A second dry run of the same version fails with `File already exists`; bump the version before another one.

One-time setup, done by the maintainer and not by the YAML: register a trusted publisher on pypi.org and on test.pypi.org (owner `PIsberg`, repository `llm-light`, workflow `publish.yml`, environment `pypi` or `testpypi`), and create the two environments under the repository's Settings, Environments. A required reviewer on `pypi` makes every release wait for approval.

To release:

1. Set `version` in `pyproject.toml` and move the `[Unreleased]` entries in `CHANGELOG.md` under the new version, in a PR.
2. After it merges, run **Publish to PyPI** by hand on `main`. In a clean venv, fetch only the wheel from TestPyPI and take its dependencies from PyPI, then run the tool:

   ```bash
   pip download --no-deps --index-url https://test.pypi.org/simple/ -d testpypi-dist llm-light==<version>
   pip install testpypi-dist/llm_light-<version>-py3-none-any.whl
   llm-light --version
   ```

   Keep TestPyPI away from dependency resolution: with `--extra-index-url`, pip takes the highest version of each dependency from either index, so anyone can shadow `rich` or `numpy` there.
3. Publish a GitHub release whose tag is `v<version>`, for example `v0.1.0`. That runs the `pypi` job, and `sbom.yml` attaches `sbom.json` to the same release.

Local equivalent of the tag check: `python tests/linting/check_release_tag.py v0.1.0`.

## `sbom.yml`: Generate SBOM

Generates a CycloneDX bill of materials from `requirements.txt`:

1. **Generate SBOM**: `cyclonedx-py requirements -i requirements.txt -o sbom.json`.
2. **Upload SBOM Artifact** as the `sbom` artifact.
3. **Upload SBOM to Release** runs only when the event is a published release. It attaches `sbom.json` to that release.

The SBOM is built from `requirements.txt` alone, so it reflects what the installers install, not the CI test list.

Local equivalent: `bash scripts/generate_sbom.sh` (Linux and macOS) or `.\scripts\generate_sbom.ps1` (Windows).

## `codeql.yml`: CodeQL

Static analysis for security issues, with the default query set:

1. **Initialize CodeQL** for the Python language.
2. **Autobuild**: CodeQL's automatic build step for the detected language.
3. **Perform CodeQL Analysis**, which uploads results to the Security tab.

Runs on push and PR to `main`, and weekly on Monday at 00:00 UTC.

## `scorecards.yml`: Scorecard supply-chain security

Runs the OpenSSF Scorecard analysis (`ossf/scorecard-action`) and publishes the results. It has the permissions it needs to publish, including `id-token: write` for signing. Steps:

1. **Checkout code**.
2. **Run analysis** with the Scorecard action, which writes SARIF output.
3. **Upload artifact** with the raw results.
4. **Upload to code-scanning**: SARIF goes to the Security tab.

Runs on a schedule (Tuesday 07:20 UTC), on push to `main`, and when branch protection changes.

## `dependency-review.yml`: Dependency Review

On every pull request: checks out the repo and runs `actions/dependency-review-action`. It compares the dependency changes in the PR with the base branch and fails the check if the PR adds a dependency with a known vulnerability. Use it to see why a PR's dependency change turns red.

## `installers.yml`: Installers

Runs each installer end to end on a clean GitHub-hosted runner for its OS, then starts the installed tool (#75). Before this workflow the installers were only syntax-checked. It installs PyTorch (about 1.5 GB) on every OS, so it runs only when `installers/**`, `requirements.txt` or the workflow itself changes, weekly on Wednesday at 06:40 UTC (a new PyPI release can break resolution without a change here), and on demand.

One job per OS, `fail-fast: false` so one failure does not hide the others:

| Runner | Installer | Python |
|---|---|---|
| `ubuntu-latest` | `bash installers/install.sh` | 3.12 from `setup-python` |
| `macos-latest` | `bash installers/install_macos.sh` | whatever the installer finds (Homebrew first), as on a user's machine; it creates `.venv` and `run.sh` |
| `windows-latest` | `.\installers\install.ps1` under Windows PowerShell 5.1 | 3.12 from `setup-python` |

Then **Start the installed tool** runs `llm_light.py --help` (`./run.sh --help` on macOS) and imports every dependency from `requirements.txt` with the interpreter the installer used. It sets no API key: `--help` must work on a fresh install with no credential (#101).

The harden-runner step runs on the Linux job only.

Local equivalent: run the installer for your OS in a clean checkout, then `python llm_light.py --help`.

## `diagrams.yml`: Diagrams

Runs on a PR that touches `docs/plantuml/`. `tests/linting/check_diagram_pngs.py` lists the files changed against the base branch (full-history checkout) and fails for each changed `.puml` whose PNG, named by its `@startuml` line, did not change too (#107). It does not render and compare: PNG bytes depend on the PlantUML version, Graphviz and fonts. Without it, `indexing_flow.puml` was edited while its PNG stayed months older (#67).

Local equivalent: `python tests/linting/check_diagram_pngs.py origin/main`. Render with `java -jar docs/plantuml/plantuml.jar docs/plantuml/<name>.puml`.

## Adding or changing a workflow

- Pin every action to a full commit SHA, with the version in a comment, as the existing files do.
- Keep `permissions` read-only at the top of the file, and grant more per job only when a step needs it.
- Add the `harden-runner` step to new jobs.
- If the change alters a step listed here, update this page in the same change.
