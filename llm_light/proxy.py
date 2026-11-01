"""Send the model traffic through an HTTPS proxy, such as the llm-fw firewall.

Only the model traffic goes through it: the Anthropic client, and the
environment of the provider CLIs that llm_light starts. os.environ is never
changed, so embedding-model downloads, git and the commands the agent runs
keep their own connection.

An intercepting proxy presents certificates signed by its own CA. The CA is
added to certifi's bundle rather than swapped in for it, because a proxy that
only tunnels a host (llm-fw does this for every non-LLM host) passes the real
certificate through.
"""
import atexit
import os
import ssl
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

# llm-fw's defaults: its forward proxy port and the CA `llm-fw setup` creates.
LLM_FW_URL = "http://127.0.0.1:8080"
LLM_FW_CA = Path.home() / ".llm-fw" / "ca.crt"

_url: str | None = None
_ca: str | None = None
_bundle: str | None = None


def configure(url: str | None, ca: str | None = None) -> None:
    """Route model traffic through url, trusting the CA certificate at ca.

    None turns the proxy off. Raises ValueError, naming the problem, for a
    URL that is not http(s)://host[:port] or a CA file that is missing or
    holds no PEM certificate; the previous setting is kept then.
    """
    global _url, _ca, _bundle
    if not url:
        _url = _ca = None
        _drop_bundle()
        return
    parts = urlsplit(url)
    try:
        parts.port
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"proxy {_redact(url)!r}: give the proxy as http://host:port")
    ca_path = bundle = None
    if ca:
        ca_path = Path(ca).expanduser().resolve()
        if not ca_path.is_file():
            raise ValueError(f"proxy CA certificate not found: {ca_path}")
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cafile=str(ca_path))
        except (ssl.SSLError, OSError):
            raise ValueError(f"proxy CA certificate {ca_path}: no PEM certificate in it")
        bundle = _write_bundle(ca_path)
    _drop_bundle()
    _url, _ca, _bundle = url, (str(ca_path) if ca_path else None), bundle


def active() -> bool:
    return _url is not None


def url() -> str | None:
    """The proxy URL, credentials included; None when no proxy is set."""
    return _url


def display() -> str:
    """The proxy URL with its password hidden, for messages."""
    return _redact(_url) if _url else ""


def ca_cert() -> str | None:
    """Absolute path of the proxy's CA certificate, if one was given."""
    return _ca


def ssl_context() -> ssl.SSLContext | None:
    """Verify servers against certifi's roots plus the proxy's CA.

    None without a CA: the client then keeps its own default trust, which for
    anthropic 2.x is the OS store, where `llm-fw setup` installs its CA.
    """
    if not _bundle:
        return None
    return ssl.create_default_context(cafile=_bundle)


def child_env(env: dict | None) -> dict | None:
    """env (None: ours) with the proxy and its CA set for a CLI child process.

    Without a proxy, env is returned as it is, so a None still means the child
    inherits our environment. HTTP_PROXY stays unset: llm-fw forwards CONNECT
    only. NODE_EXTRA_CA_CERTS adds the CA for Node clients; SSL_CERT_FILE
    replaces the bundle for Python and Rust clients, so it gets the combined
    one. Go clients on Windows and macOS read only the OS store.
    """
    if _url is None:
        return env
    out = dict(os.environ if env is None else env)
    out["HTTPS_PROXY"] = _url
    if os.name != "nt":  # Windows environment names are case-insensitive
        out["https_proxy"] = _url
    if _bundle:
        out["NODE_EXTRA_CA_CERTS"] = _ca
        out["SSL_CERT_FILE"] = _bundle
    return out


def _redact(url: str) -> str:
    """url with its credentials hidden. A user name is kept only beside a
    password (llm-fw:TOKEN@host); alone it may be the token (TOKEN@host)."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    userinfo = f"{parts.username}:***" if parts.password is not None else "***"
    return parts._replace(netloc=f"{userinfo}@{host}").geturl()


def _write_bundle(ca_path: Path) -> str:
    """certifi's roots plus the CA, in a temp file removed at exit."""
    import certifi
    roots = Path(certifi.where()).read_text(encoding="utf-8")
    ca = ca_path.read_text(encoding="utf-8")
    fd, path = tempfile.mkstemp(prefix="llm_light_proxy_ca_", suffix=".pem")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(roots.rstrip("\n") + "\n" + ca)
    return path


def _drop_bundle() -> None:
    global _bundle
    if _bundle:
        try:
            os.remove(_bundle)
        except OSError:
            pass
    _bundle = None


atexit.register(_drop_bundle)
