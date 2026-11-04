"""
Pytest configuration and shared fixtures for llm_light tests.

This file:
- Configures pytest plugins and test discovery
- Provides shared fixtures used across test suites
- Sets up common test infrastructure (mocks, paths, environment)
"""

import os
import sys
from pathlib import Path

# Add the project root to sys.path so tests can import llm_light
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# Set dummy API key to prevent import errors in test mode
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-mock-key")

# Most tests were written for the single dense model; tests of the hybrid
# index (#61) switch it on themselves with patch.object(config, ...).
os.environ.setdefault("LLM_LIGHT_HYBRID", "0")

# Keep the suite's fake token usage out of the real ~/.llm_light_stats.json.
# Set before any llm_light import: config reads it at import time.
import tempfile as _tempfile
os.environ["LLM_LIGHT_STATS_FILE"] = os.path.join(
    _tempfile.mkdtemp(prefix="llm_light_stats_"), "stats.json")
# Same for the conversations saved for --continue (~/.llm_light_sessions).
os.environ["LLM_LIGHT_SESSION_DIR"] = _tempfile.mkdtemp(prefix="llm_light_sessions_")
# And the developer's own slash commands (~/.llm_light/commands) stay out of the suite.
os.environ["LLM_LIGHT_COMMANDS_DIR"] = _tempfile.mkdtemp(prefix="llm_light_commands_")
# And their settings, which would change the model, mode and keys under test.
os.environ["LLM_LIGHT_SETTINGS_FILE"] = os.path.join(_tempfile.mkdtemp(prefix="llm_light_settings_"), "settings.json")
# A developer's own --proxy setting (e.g. llm-fw) must not route the suite.
for _var in ("LLM_LIGHT_PROXY", "LLM_LIGHT_PROXY_CA"):
    os.environ.pop(_var, None)

import threading

import pytest


@pytest.fixture(autouse=True)
def _fresh_session_model():
    """ROUTE_ONCE is on by default: a model pinned by one test's first route
    must not carry into the next test."""
    import llm_light.state as state
    state.session_model = None
    yield
    state.session_model = None


@pytest.fixture(autouse=True)
def _restore_synced_bindings():
    """Put back every binding llm_light._sync_bindings() writes (#139).

    patch("llm_light.warm_cache") restores only the package attribute. A
    wrapped call made under the patch copies the mock into llm.warm_cache and
    main.warm_cache, and a later test calling those hit the mock and sent
    nothing. The package globals are snapshotted too, for tests that assign
    them by hand.
    """
    import llm_light
    missing = object()  # a binding the first sync has not created yet
    saved = [(module, name, getattr(module, name, missing))
             for module, names in llm_light._SYNC_TARGETS for name in names]
    saved += [(llm_light, name, getattr(llm_light, name))
              for name in {n for _, names in llm_light._SYNC_TARGETS for n in names}]
    yield
    for module, name, value in saved:
        if value is not missing:
            setattr(module, name, value)
        elif hasattr(module, name):
            delattr(module, name)


@pytest.fixture(autouse=True)
def _fresh_permissions_and_commands():
    """Permission mode, /model pin, /copy replies and pending !command output
    are session state: one test's must not leak into the next."""
    import llm_light.state as state

    def reset():
        state.permission_mode = "default"
        state.bypass_allowed = False
        state.pinned_model = None
        state.cli_session = None
        state.cli_tree = None
        state.cli_resume_failed = False
        state.pending_context.clear()
        state.replies.clear()
        state.checkpoints.clear()
    reset()
    history = list(state.conversation_history)
    yield
    reset()
    state.conversation_history[:] = history


@pytest.fixture(autouse=True)
def _no_real_llmlingua_loader():
    """A test that runs main() unpatched starts the real LLMLingua loader. It
    keeps loading after the test ends and sets the compressor singleton under
    a later test's feet (#74). Patch _start_llmlingua_load in such tests."""
    import llm_light.compressor as compressor
    before = compressor._load_thread
    yield
    thread = compressor._load_thread
    if thread is not before and isinstance(thread, threading.Thread):
        thread.join(timeout=300)  # leave nothing loading for the next test
        compressor._load_thread = None
        compressor._load_done.clear()
        compressor._load_attempted = False
        compressor._compressor = None
        compressor._load_error = None
        pytest.fail("test started a real LLMLingua loader thread; patch "
                    "llm_light.main._start_llmlingua_load", pytrace=False)


@pytest.fixture(scope="session")
def project_root_fixture():
    """Provide the project root directory."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def tests_dir():
    """Provide the tests directory."""
    return Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def fixtures_dir():
    """Provide the fixtures directory."""
    return Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temporary directory for test artifacts."""
    return tmp_path


@pytest.fixture(scope="session")
def llm_light_module():
    """Import and provide the llm_light module."""
    import llm_light
    return llm_light
