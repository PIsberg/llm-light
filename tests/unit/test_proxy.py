"""
Tests for routing LLM traffic through an HTTPS proxy such as llm-fw
(llm_light/proxy.py, --proxy, --proxy-ca, --llm-fw).

The proxy in these tests is a local socket that records the CONNECT line and
then stops the client, so no request leaves the machine.
"""

import io
import socket
import ssl
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import certifi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import llm_light  # noqa: F401  (loads the package before the submodules)
from llm_light import config, providers, proxy, state
from llm_light import main as main_mod
from llm_light.providers import base, codex, gemini
from llm_light.providers import claude as claude_mod

CA = Path(__file__).resolve().parent.parent / "fixtures" / "proxy_ca.pem"
CA_SUBJECT = ((("commonName", "llm_light test proxy CA"),),)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for var in ("LLM_LIGHT_PROXY", "LLM_LIGHT_PROXY_CA"):
        monkeypatch.delenv(var, raising=False)
    proxy.configure(None)
    yield
    proxy.configure(None)


# --- configure() --------------------------------------------------------------

@pytest.mark.parametrize("url", ["socks5://127.0.0.1:1080", "127.0.0.1:8080", "http://", "http://host:notaport"])
def test_configure_rejects_a_url_that_is_not_an_http_proxy(url):
    with pytest.raises(ValueError, match="http://host:port"):
        proxy.configure(url)
    assert not proxy.active()


def test_configure_rejects_a_missing_ca_file(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        proxy.configure("http://127.0.0.1:8080", str(tmp_path / "nope.crt"))
    assert not proxy.active()


def test_configure_rejects_a_ca_file_without_a_certificate(tmp_path):
    bad = tmp_path / "bad.crt"
    bad.write_text("not a certificate\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no PEM certificate"):
        proxy.configure("http://127.0.0.1:8080", str(bad))
    assert not proxy.active()


def test_configure_none_turns_the_proxy_off():
    proxy.configure("http://127.0.0.1:8080", str(CA))
    proxy.configure(None)
    assert not proxy.active()
    assert proxy.child_env(None) is None


def test_display_hides_the_proxy_password():
    proxy.configure("http://llm-fw:s3cret@10.0.0.5:8080")
    shown = proxy.display()
    assert "s3cret" not in shown
    assert shown == "http://llm-fw:***@10.0.0.5:8080"


def test_display_hides_a_token_given_as_the_user_name():
    proxy.configure("http://s3cret@10.0.0.5:8080")
    shown = proxy.display()
    assert "s3cret" not in shown
    assert shown == "http://***@10.0.0.5:8080"


def test_bad_url_error_hides_the_token():
    with pytest.raises(ValueError) as exc:
        proxy.configure("socks5://s3cret@10.0.0.5:1080")
    assert "s3cret" not in str(exc.value)


# --- trust ----------------------------------------------------------------------

def test_ssl_context_trusts_the_proxy_ca_and_the_public_roots():
    proxy.configure("http://127.0.0.1:8080", str(CA))
    certs = proxy.ssl_context().get_ca_certs()
    subjects = [c["subject"] for c in certs]
    assert CA_SUBJECT in subjects
    public = ssl.create_default_context(cafile=certifi.where()).get_ca_certs()
    # The CA is added to certifi's bundle, not swapped in for it: hosts the
    # proxy only tunnels still present their real certificate.
    assert len(certs) == len(public) + 1


# --- child_env() ------------------------------------------------------------------

def test_without_a_ca_the_client_keeps_its_own_default_trust():
    # anthropic 2.x trusts the OS store by default, where `llm-fw setup`
    # installs its CA: --proxy alone must not narrow that to certifi.
    proxy.configure("http://127.0.0.1:8080")
    assert proxy.ssl_context() is None


def test_child_env_is_untouched_without_a_proxy():
    assert proxy.child_env(None) is None
    env = {"PATH": "/bin"}
    assert proxy.child_env(env) == {"PATH": "/bin"}


def test_child_env_points_the_cli_at_the_proxy_and_its_ca():
    proxy.configure("http://127.0.0.1:8080", str(CA))
    env = proxy.child_env({"PATH": "/bin", "HOME": "/tmp/h"})
    assert env["PATH"] == "/bin" and env["HOME"] == "/tmp/h"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:8080"
    # llm-fw answers a plain proxied request with 501: HTTPS only.
    assert "HTTP_PROXY" not in env and "http_proxy" not in env
    assert Path(env["NODE_EXTRA_CA_CERTS"]) == CA.resolve()
    bundle = Path(env["SSL_CERT_FILE"]).read_text(encoding="utf-8")
    assert CA.read_text(encoding="utf-8").strip() in bundle
    assert Path(certifi.where()).read_text(encoding="utf-8").strip() in bundle


def test_child_env_without_a_ca_sets_only_the_proxy():
    proxy.configure("http://127.0.0.1:8080")
    env = proxy.child_env({})
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:8080"
    assert "NODE_EXTRA_CA_CERTS" not in env and "SSL_CERT_FILE" not in env


def test_child_env_never_changes_our_own_environment():
    import os
    proxy.configure("http://127.0.0.1:8080", str(CA))
    before = dict(os.environ)
    proxy.child_env(None)
    assert dict(os.environ) == before


# --- the Anthropic client goes through the proxy ------------------------------------

class _ConnectRecorder:
    """A proxy that records one CONNECT line, answers 200 and closes."""

    def __init__(self):
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.request = b""
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.server.settimeout(10)
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            while b"\r\n\r\n" not in self.request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                self.request += chunk
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

    def close(self):
        self.server.close()
        self.thread.join(timeout=5)


class _RecordingContext(ssl.SSLContext):
    """Records the host the client starts TLS with, then stops it."""
    hosts = []

    def wrap_socket(self, sock, *args, server_hostname=None, **kwargs):
        type(self).hosts.append(server_hostname)
        raise ssl.SSLError("stopped by test")


def test_anthropic_client_tunnels_through_the_proxy_with_the_proxy_trust(monkeypatch):
    import anthropic
    # The environment has its own proxy, as after `llm-fw setup`: --proxy
    # must win over it. It is a closed local port, so a client that ignores
    # --proxy fails here instead of reaching the real API.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    recorder = _ConnectRecorder()
    try:
        proxy.configure(f"http://127.0.0.1:{recorder.port}", str(CA))
        _RecordingContext.hosts = []
        monkeypatch.setattr(proxy, "ssl_context",
                            lambda: _RecordingContext(ssl.PROTOCOL_TLS_CLIENT))
        client = claude_mod._new_client().with_options(max_retries=0, timeout=10)
        with pytest.raises(anthropic.APIConnectionError):
            client.messages.create(model="claude-haiku-5-5", max_tokens=1,
                                   messages=[{"role": "user", "content": "hi"}])
    finally:
        recorder.close()
    assert recorder.request.startswith(b"CONNECT api.anthropic.com:443 ")
    assert _RecordingContext.hosts == ["api.anthropic.com"]


# --- the provider CLIs get the proxy in their environment -----------------------------

class _Stop(Exception):
    pass


def _capture_run_cli(seen):
    def fake(provider, argv, stdin_text, *a, **kw):
        seen["env"] = kw.get("env")
        raise _Stop
    return fake


def test_codex_inherits_our_environment_without_a_proxy():
    seen = {}
    with patch("llm_light.providers.codex.shutil.which", return_value="/bin/codex"), \
         patch("llm_light.providers.codex.run_cli", side_effect=_capture_run_cli(seen)):
        with pytest.raises(_Stop):
            codex.PROVIDER._run("q")
    assert seen["env"] is None


@pytest.mark.parametrize("module,binary", [(codex, "/bin/codex"), (gemini, "/bin/agy")])
def test_cli_providers_run_with_the_proxy_and_ca(module, binary):
    proxy.configure("http://127.0.0.1:8080", str(CA))
    seen = {}
    name = module.__name__
    with patch(f"{name}.shutil.which", return_value=binary), \
         patch(f"{name}.run_cli", side_effect=_capture_run_cli(seen)):
        with pytest.raises(_Stop):
            module.PROVIDER._run("q")
    env = seen["env"]
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:8080"
    assert "SSL_CERT_FILE" in env and "NODE_EXTRA_CA_CERTS" in env
    if module is gemini:
        assert env["HOME"] != str(Path.home())  # still the isolated home


def test_claude_cli_runs_with_the_proxy_and_ca():
    import json
    proxy.configure("http://127.0.0.1:8080", str(CA))
    proc = MagicMock()
    proc.stdout = iter([json.dumps({"type": "result", "subtype": "success", "result": "ok",
                                    "usage": {"input_tokens": 1, "output_tokens": 1}}) + "\n"])
    proc.stderr = io.StringIO("")
    proc.wait.return_value = 0
    proc.poll.return_value = 0
    proc.returncode = 0
    with patch("subprocess.Popen", return_value=proc) as popen, \
         patch("shutil.which", return_value="/fake/bin/claude"), \
         redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        claude_mod._make_cli_subprocess_call("hello")
    env = popen.call_args.kwargs["env"]
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:8080"
    assert Path(env["NODE_EXTRA_CA_CERTS"]) == CA.resolve()


# --- command line ----------------------------------------------------------------------

class _Bare(base.Provider):
    name = "barecli"
    capabilities = frozenset()

    def model_label(self, routed_model):
        return "m"

    def header(self):
        return ("m", "barecli")

    def require_auth(self):
        return None

    def complete(self, request):
        return "a", None, False

    def summarize(self, instructions):
        return "s", None


def _run_main(monkeypatch, argv):
    bare = _Bare()
    monkeypatch.setattr(sys, "argv", ["llm_light"] + argv)
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", [])
    monkeypatch.setattr(providers, "select", lambda name: bare)
    monkeypatch.setattr(state, "provider", bare, raising=False)
    monkeypatch.setattr(main_mod, "_start_llmlingua_load", lambda: None)
    monkeypatch.setattr(main_mod, "start_chat", lambda **k: None)
    monkeypatch.setattr(main_mod, "one_shot", lambda *a, **k: None)
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            main_mod.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue() + err.getvalue()


def test_proxy_flag_routes_and_says_so_without_the_password(monkeypatch):
    code, text = _run_main(monkeypatch, ["--proxy", "http://llm-fw:tok@10.0.0.5:8080",
                                         "--proxy-ca", str(CA), "hello"])
    assert code == 0
    assert proxy.active()
    assert proxy.child_env({})["HTTPS_PROXY"] == "http://llm-fw:tok@10.0.0.5:8080"
    assert "http://llm-fw:***@10.0.0.5:8080" in text
    assert "tok@" not in text


def test_proxy_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_LIGHT_PROXY", "http://127.0.0.1:9090")
    monkeypatch.setenv("LLM_LIGHT_PROXY_CA", str(CA))
    assert _run_main(monkeypatch, ["hello"])[0] == 0
    env = proxy.child_env({})
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9090"
    assert Path(env["NODE_EXTRA_CA_CERTS"]) == CA.resolve()


def test_llm_fw_flag_uses_the_firewalls_defaults(monkeypatch):
    monkeypatch.setattr(proxy, "LLM_FW_CA", CA)
    assert _run_main(monkeypatch, ["--llm-fw", "hello"])[0] == 0
    env = proxy.child_env({})
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:8080"
    assert Path(env["NODE_EXTRA_CA_CERTS"]) == CA.resolve()


def test_llm_fw_flag_without_its_ca_points_at_llm_fw_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "LLM_FW_CA", tmp_path / "ca.crt")
    code, text = _run_main(monkeypatch, ["--llm-fw", "hello"])
    assert code == 2
    assert "llm-fw setup" in text
    assert not proxy.active()


def test_bad_proxy_url_is_a_usage_error(monkeypatch):
    code, text = _run_main(monkeypatch, ["--proxy", "socks5://127.0.0.1:1080", "hello"])
    assert code == 2
    assert "http://host:port" in text


def test_proxy_ca_without_a_proxy_is_a_usage_error(monkeypatch):
    code, text = _run_main(monkeypatch, ["--proxy-ca", str(CA), "hello"])
    assert code == 2
    assert "--proxy-ca needs --proxy" in text


def test_no_proxy_flag_prints_no_proxy_line(monkeypatch):
    code, text = _run_main(monkeypatch, ["hello"])
    assert code == 0
    assert "proxy" not in text
    assert not proxy.active()
