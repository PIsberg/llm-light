"""Permission modes: what llm_light and its agent may do without asking.

Named after Claude Code's modes, and switched the same way: a startup flag,
`/permissions <mode>`, or shift+tab at the prompt.

  default            ask before applying edits and before committing the CLI
                     agent's edits; the shell runs only --allow-command prefixes
  acceptEdits        apply edits and commit agent edits without asking (-y)
  plan               read-only: the model plans, nothing is written, no command runs
  bypassPermissions  ask nothing: edits apply and the shell runs any command
                     (--dangerously-skip-permissions, alias --yolo)

bypassPermissions is offered only when llm_light was started with
--dangerously-skip-permissions or --allow-dangerously-skip-permissions, as in
Claude Code, so a slip at the prompt cannot switch every safeguard off.

Spending questions (the top-tier model prompt, the /budget cap) are not
permissions and are asked in every mode.
"""

import llm_light.state as state

DEFAULT, ACCEPT_EDITS, PLAN, BYPASS = "default", "acceptEdits", "plan", "bypassPermissions"
MODES = (DEFAULT, ACCEPT_EDITS, PLAN, BYPASS)

_ALIASES = {
    "default": DEFAULT, "ask": DEFAULT, "normal": DEFAULT,
    "acceptedits": ACCEPT_EDITS, "accept-edits": ACCEPT_EDITS, "accept": ACCEPT_EDITS,
    "auto": ACCEPT_EDITS, "edits": ACCEPT_EDITS,
    "plan": PLAN,
    "bypasspermissions": BYPASS, "bypass": BYPASS, "yolo": BYPASS,
    "dangerously-skip-permissions": BYPASS,
}

LABELS = {
    DEFAULT: "ask before edits",
    ACCEPT_EDITS: "accept edits",
    PLAN: "plan mode",
    BYPASS: "bypass permissions",
}

DESCRIPTIONS = {
    DEFAULT: "asks before applying edits; the shell runs only --allow-command prefixes",
    ACCEPT_EDITS: "applies edits and commits the agent's edits without asking",
    PLAN: "read-only: the model plans, nothing is written and no command runs",
    BYPASS: "asks nothing: edits apply and the shell runs any command",
}

_PLAN_NOTE = ("[Plan mode: the user wants a plan, not changes. Investigate, then describe "
              "the steps and the files you would change. Do not write SEARCH/REPLACE or "
              "file blocks, edit files or run commands.]\n")
_BYPASS_NOTE = ("[Permissions bypassed: the shell tool runs any command from the project "
                "root this session; the allowlist in its description does not apply.]\n")


def parse(name: str) -> str | None:
    """The mode `name` stands for (any case, or an alias such as yolo), else None."""
    return _ALIASES.get((name or "").strip().lower())


def mode() -> str:
    return state.permission_mode


def set_mode(name: str) -> str:
    """Switch to the mode `name` names and return it. Raises ValueError for an
    unknown name, or for bypass when it was not enabled at startup."""
    new = parse(name)
    if new is None:
        raise ValueError(f"Unknown permission mode {name!r}. Modes: {', '.join(MODES)}.")
    if new == BYPASS and not state.bypass_allowed:
        raise ValueError("Bypass is off for this session. Restart with "
                         "--dangerously-skip-permissions (or --allow-dangerously-skip-permissions) "
                         "to allow it.")
    with state.lock:
        state.permission_mode = new
    return new


def cycle() -> str:
    """The next mode, as shift+tab steps through them: default, acceptEdits,
    plan, then bypass when allowed, then back to default."""
    order = [DEFAULT, ACCEPT_EDITS, PLAN] + ([BYPASS] if state.bypass_allowed else [])
    current = mode()
    nxt = order[(order.index(current) + 1) % len(order)] if current in order else DEFAULT
    with state.lock:
        state.permission_mode = nxt
    return nxt


def auto_edits() -> bool:
    """Edits apply, and agent edits commit, without asking."""
    return mode() in (ACCEPT_EDITS, BYPASS)


def plan() -> bool:
    return mode() == PLAN


def bypass() -> bool:
    return mode() == BYPASS


def question_note(api_shell: bool) -> str:
    """A line put before the question in plan and bypass modes; "" otherwise,
    so a default-mode request is byte-for-byte what it was. `api_shell`: the
    model has llm_light's own shell tool (API-key mode), whose description
    still names the allowlist."""
    if plan():
        return _PLAN_NOTE
    if bypass() and api_shell:
        return _BYPASS_NOTE
    return ""
