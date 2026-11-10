"""MCP servers the agent may use, from ~/.llm_light/mcp.json.

The file has the shape of Claude Code's `.mcp.json`:

    {"mcpServers": {
        "docs":  {"command": "npx", "args": ["-y", "some-mcp-server"], "env": {"TOKEN": "${DOCS_TOKEN}"}},
        "issues": {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${T}"}}
    }}

Only that file is read, never the project's `.mcp.json`: a cloned repo must not start
programs or add tools. LLM_LIGHT_MCP_CONFIG moves it (the test suite points it at a temp
file).

Two ways a server is used, both with the `claude` provider only:

- Subscription: the claude CLI agent starts the servers itself on every turn. The turn
  gets `--mcp-config` with this file's servers (still with `--strict-mcp-config`, so the
  repo's own `.mcp.json` stays out) and an `mcp__<server>` allow rule for each, since a -p
  run otherwise denies every call.
- API key: llm_light connects at startup through the `mcp` SDK (the optional
  `llm-light-cli[mcp]` extra), on one background event loop, and offers each server tool
  as `mcp__<server>__<tool>` after the built-in tools. The list is fixed for the session,
  because tools render first in the cached prompt prefix: a server that changes its tools
  later is not re-listed, and one that dies keeps its tools, whose calls then fail.

Plan mode uses no server: an MCP tool can change things outside the repo.
"""

import asyncio
import atexit
import concurrent.futures
import copy
import json
import os
import re
import threading
from pathlib import Path

from llm_light import config

PREFIX = "mcp__"
TYPES = ("stdio", "http", "sse")
_SERVER_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
_TOOL_NAME_MAX = 64  # the Messages API's limit on a tool name
_DESCRIPTION_MAX = 2048
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
INSTALL_HINT = ("pipx install 'llm-light-cli[mcp]' (or pipx inject llm-light-cli mcp, "
                "or pip install mcp in a checkout)")


def config_file() -> Path:
    return Path(config.env_get("MCP_CONFIG") or Path.home() / ".llm_light" / "mcp.json")


# --- the config file ----------------------------------------------------------------------

def _str_map(value, what):
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                              for k, v in value.items()):
        raise ValueError(f"{what} must map names to strings")
    return dict(value)


def _entry(raw) -> dict:
    """The validated, normalised form of one server entry; ValueError says what is wrong."""
    if not isinstance(raw, dict):
        raise ValueError("must be an object")
    kind = raw.get("type", "stdio")
    if kind not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    if kind == "stdio":
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("needs a command")
        args = raw.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError("args must be a list of strings")
        return {"type": "stdio", "command": command, "args": list(args), "env": _str_map(raw.get("env"), "env")}
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("needs an http:// or https:// url")
    return {"type": kind, "url": url, "headers": _str_map(raw.get("headers"), "headers")}


def load() -> tuple:
    """(servers, problems): the valid servers by name, in file order, and one line for
    each entry, or the whole file, that was skipped."""
    path = config_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, []
    except OSError as e:
        return {}, [f"{path}: cannot read it: {e}"]
    try:
        data = json.loads(text)
    except ValueError as e:
        return {}, [f"{path}: not valid JSON: {e}"]
    raw_servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(raw_servers, dict):
        return {}, [f"{path}: needs an \"mcpServers\" object"]
    servers, problems = {}, []
    for name, raw in raw_servers.items():
        if not _SERVER_NAME.fullmatch(name):
            problems.append(f"{path}: server {name!r} skipped: a name is 1 to 32 letters, "
                            "digits, _ or -")
            continue
        try:
            servers[name] = _entry(raw)
        except ValueError as e:
            problems.append(f"{path}: server {name!r} skipped: {e}")
    return servers, problems


_loaded = None


def configured() -> dict:
    """load()'s servers, read once per process."""
    global _loaded
    if _loaded is None:
        _loaded = load()
    return _loaded[0]


def problems() -> list:
    configured()
    return _loaded[1]


def reset() -> None:
    """Forget the loaded file (tests)."""
    global _loaded
    _loaded = None


def expand(value: str) -> str:
    """${VAR} and ${VAR:-default} from the environment, as the claude CLI expands them."""
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)


def _expanded(entry: dict) -> dict:
    if entry["type"] == "stdio":
        return {**entry, "command": expand(entry["command"]),
                "args": [expand(a) for a in entry["args"]],
                "env": {k: expand(v) for k, v in entry["env"].items()}}
    return {**entry, "url": expand(entry["url"]),
            "headers": {k: expand(v) for k, v in entry["headers"].items()}}


# --- the claude CLI agent -------------------------------------------------------------------

def cli_config(servers: dict) -> dict:
    """The --mcp-config content for the CLI agent. Its stdio servers get the real home
    directory back: the CLI runs with HOME pointed at an empty temp dir, and a server
    started there would lose its own settings and caches."""
    home = os.path.expanduser("~")
    out = {}
    for name, entry in servers.items():
        entry = copy.deepcopy(entry)
        if entry["type"] == "stdio":
            entry["env"] = {"HOME": home, "USERPROFILE": home, **entry["env"]}
        out[name] = entry
    return {"mcpServers": out}


def allow_rules(servers: dict) -> list:
    """One CLI allow rule per server, covering all of its tools."""
    return [PREFIX + name for name in servers]


def cli_status_lines(servers: dict) -> list:
    names = ", ".join(servers)
    return [f"MCP servers for the agent, which starts them itself each turn: {names}"]


# --- API-key mode ---------------------------------------------------------------------------

def exposed_name(server: str, tool: str):
    """mcp__<server>__<tool> with characters the API refuses replaced, or None if too long."""
    name = f"{PREFIX}{server}__{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}"
    return name if len(name) <= _TOOL_NAME_MAX else None


def result_text(result) -> tuple:
    """(text, is_error) for a CallToolResult. Text goes through; an image or a binary
    resource is named, not sent, so a tool cannot flood the context with base64."""
    from llm_light.tools import _cap
    parts = []
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append(block.text)
        elif kind in ("image", "audio"):
            parts.append(f"[{getattr(block, 'mime_type', kind)} {kind} omitted]")
        elif kind == "resource":
            resource = block.resource
            text = getattr(resource, "text", None)
            parts.append(text if isinstance(text, str) else
                         f"[binary resource {getattr(resource, 'uri', '')} omitted]")
        elif kind == "resource_link":
            parts.append(f"[resource {getattr(block, 'uri', '')}]")
    structured = getattr(result, "structured_content", None)
    if not parts and structured is not None:
        parts.append(json.dumps(structured))
    return _cap("\n".join(parts)), bool(getattr(result, "is_error", False))


def _describe(exc: BaseException) -> str:
    """The innermost message of an exception, unwrapping the groups anyio raises."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _secrets(entry: dict) -> list:
    """Values in an entry that may be credentials: env and header values, a URL's query
    and password, after ${VAR} expansion. Longest first, so a value inside another is
    not left half-masked."""
    from urllib.parse import urlsplit
    entry = _expanded(entry)
    found = list(entry.get("env", {}).values()) + list(entry.get("headers", {}).values())
    for value in list(entry.get("headers", {}).values()):
        found += value.split()  # "Bearer <token>": the token alone may be quoted
    if entry.get("url"):
        parts = urlsplit(entry["url"])
        found += [parts.query, parts.password or ""]
    return sorted({v for v in found if len(v) >= 4}, key=len, reverse=True)


def _redact(text: str, entry: dict) -> str:
    """`text` with the entry's secret values replaced by ***. An error goes to the model
    and to the screen; an HTTP error can quote the URL and the request headers."""
    for secret in _secrets(entry):
        text = text.replace(secret, "***")
    return text


def _log_path(name: str) -> Path:
    return Path(config.SESSION_DIR) / "mcp" / f"{name}.log"


class _Server:
    def __init__(self, name, entry):
        self.name = name
        self.entry = entry
        self.client = None
        self.listed = []
        self.error = None
        self.log = None
        self.ready = None
        self.task = None


class _Manager:
    """The servers of one session, on an event loop in a daemon thread.

    Each server lives in one task that holds its client open until stop(): anyio needs a
    context entered and left in the same task. Nothing here prints; start() and
    status_lines() return lines for the caller to show.
    """

    def __init__(self):
        self.loop = None
        self.thread = None
        self.servers = {}
        self.definitions = []
        self.routes = {}
        self._closing = None

    def start(self, servers: dict, timeout=None) -> list:
        timeout = config.MCP_START_TIMEOUT if timeout is None else timeout
        self.servers = {name: _Server(name, entry) for name, entry in servers.items()}
        try:
            import mcp  # noqa: F401
        except ImportError:
            for srv in self.servers.values():
                srv.error = "the mcp package is not installed"
            return [f"MCP servers not started: the mcp package is not installed. {INSTALL_HINT}"]
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="llm_light-mcp", daemon=True)
        self.thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._boot(timeout), self.loop).result(timeout + 15)
        except concurrent.futures.TimeoutError:
            for srv in self.servers.values():
                if srv.client is None and srv.error is None:
                    srv.error = f"did not start within {timeout:g}s"
        self._build_definitions()
        return self.status_lines()

    async def _boot(self, timeout):
        self._closing = asyncio.Event()
        for srv in self.servers.values():
            srv.ready = asyncio.Event()
            srv.task = asyncio.create_task(self._serve(srv))
        results = await asyncio.gather(
            *(asyncio.wait_for(srv.ready.wait(), timeout) for srv in self.servers.values()),
            return_exceptions=True)
        for srv, outcome in zip(self.servers.values(), results):
            if isinstance(outcome, (asyncio.TimeoutError, TimeoutError)):
                srv.error = f"did not start within {timeout:g}s"
                srv.task.cancel()

    async def _serve(self, srv):
        from contextlib import AsyncExitStack
        from mcp import Client
        from mcp_types import Implementation
        from llm_light.commands import version
        try:
            async with AsyncExitStack() as stack:
                transport = await self._transport(srv, stack)
                client = await stack.enter_async_context(
                    Client(transport, client_info=Implementation(name="llm-light", version=version())))
                listed, cursor = [], None
                while True:
                    page = await client.list_tools(cursor=cursor)
                    listed += page.tools
                    cursor = page.next_cursor
                    if not cursor or len(listed) >= 1000:
                        break
                srv.listed = listed
                srv.client = client
                srv.ready.set()
                await self._closing.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001  anyio raises exception groups
            if srv.error is None:
                srv.error = _redact(_describe(e), srv.entry)
        finally:
            srv.client = None
            srv.ready.set()
            if srv.log is not None:
                srv.log.close()

    async def _transport(self, srv, stack):
        entry = _expanded(srv.entry)
        if entry["type"] == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            path = _log_path(srv.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            # The server's stderr goes to a file: on the terminal it would land in a
            # streamed reply, and a child holding our stderr pipe kept a run from exiting.
            srv.log = open(path, "w", encoding="utf-8", errors="replace")
            params = StdioServerParameters(command=entry["command"], args=entry["args"],
                                           env=entry["env"] or None, cwd=os.getcwd())
            return stdio_client(params, errlog=srv.log)
        if entry["type"] == "http":
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared._httpx_utils import create_mcp_http_client
            http = await stack.enter_async_context(create_mcp_http_client(headers=entry["headers"]))
            return streamable_http_client(entry["url"], http_client=http)
        from mcp.client.sse import sse_client
        return sse_client(entry["url"], headers=entry["headers"])

    def _build_definitions(self):
        self.definitions, self.routes = [], {}
        for srv in self.servers.values():
            for tool in srv.listed:
                name = exposed_name(srv.name, tool.name)
                if name is None or name in self.routes:
                    continue
                schema = dict(tool.input_schema or {})
                schema.setdefault("type", "object")
                description = (tool.description or tool.title or "").strip()
                self.definitions.append({
                    "name": name,
                    "description": description[:_DESCRIPTION_MAX] or f"{tool.name} from the {srv.name} MCP server",
                    "input_schema": schema,
                })
                self.routes[name] = (srv, tool.name)

    def call(self, name: str, arguments: dict) -> tuple:
        route = self.routes.get(name)
        if route is None:
            return f"Error: no MCP tool {name!r} is connected", True
        srv, tool = route
        client = srv.client
        if client is None:
            return f"Error: the MCP server {srv.name!r} is not connected: {srv.error or 'stopped'}", True
        future = asyncio.run_coroutine_threadsafe(
            client.call_tool(tool, arguments, read_timeout_seconds=config.MCP_CALL_TIMEOUT), self.loop)
        try:
            result = future.result(config.MCP_CALL_TIMEOUT + 5)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return f"Error: {name} did not answer within {config.MCP_CALL_TIMEOUT}s", True
        except BaseException as e:
            future.cancel()
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            return f"Error: {name} failed: {_redact(_describe(e), srv.entry)}", True
        return result_text(result)

    def status_lines(self) -> list:
        lines = []
        for srv in self.servers.values():
            names = [t.name for t in srv.listed]
            if srv.client is not None:
                defs = [d for d in self.definitions if self.routes[d["name"]][0] is srv]
                tokens = len(json.dumps(defs)) // 4
                shown = ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
                lines.append(f"MCP {srv.name}: {len(defs)} tools ({shown}), about {tokens:,} tokens a turn")
            else:
                where = f"; log: {_log_path(srv.name)}" if srv.entry["type"] == "stdio" else ""
                lines.append(f"MCP {srv.name}: not connected: {srv.error or 'stopped'}{where}")
        return lines

    def stop(self) -> None:
        if self.loop is None:
            return
        loop, self.loop = self.loop, None

        async def close():
            if self._closing is not None:
                self._closing.set()
            tasks = [s.task for s in self.servers.values() if s.task is not None]
            if tasks:
                await asyncio.wait(tasks, timeout=5)
        try:
            asyncio.run_coroutine_threadsafe(close(), loop).result(10)
        except Exception:  # noqa: BLE001  shutting down: nothing to report to
            pass
        loop.call_soon_threadsafe(loop.stop)
        self.thread.join(timeout=5)


_manager = None


def start(servers: dict, timeout=None) -> list:
    """Connect to the servers (API-key mode). Returns a line per server for the caller
    to show. Blocks until each has listed its tools, failed, or timed out."""
    global _manager
    stop()
    _manager = _Manager()
    atexit.register(stop)
    return _manager.start(servers, timeout)


def tool_definitions() -> list:
    return _manager.definitions if _manager is not None else []


def call(name: str, arguments: dict) -> tuple:
    if _manager is None:
        return f"Error: MCP servers are not connected, so {name} cannot run", True
    return _manager.call(name, arguments)


def status_lines() -> list:
    return _manager.status_lines() if _manager is not None else []


def stop() -> None:
    global _manager
    manager, _manager = _manager, None
    if manager is not None:
        manager.stop()


# --- startup, /mcp and /doctor --------------------------------------------------------------

def startup_lines(provider) -> list:
    """Load the file and start what the provider uses; one line each for the user."""
    from llm_light import providers
    from llm_light.providers.base import MCP
    lines = list(problems())
    servers = configured()
    if not servers:
        return lines
    if not provider.has(MCP):
        return lines + [f"MCP servers in {config_file()} are not used: {provider.name} runs without "
                        f"tools. They work with --provider {providers.DEFAULT}."]
    return lines + provider.start_mcp(servers)


def command_lines(provider) -> list:
    """What /mcp shows."""
    from llm_light import permissions
    from llm_light.providers.base import MCP
    servers = configured()
    if not servers:
        return [f"No MCP servers. Add them to {config_file()}, in the shape of Claude Code's "
                ".mcp.json, and restart."] + list(problems())
    if not provider.has(MCP):
        return [f"{provider.name} runs without tools, so the MCP servers in {config_file()} are not used."]
    lines = status_lines() or cli_status_lines(servers)
    if permissions.plan():
        lines.append("Plan mode is on: no MCP tool runs until you leave it.")
    return lines + list(problems())
