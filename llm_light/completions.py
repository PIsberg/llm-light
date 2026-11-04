"""`--completion <shell>`: a tab-completion script for the flags, built from the parser, so
a new flag needs no change here.

    bash        eval "$(llm-light --completion bash)"
    zsh         eval "$(llm-light --completion zsh)"
    fish        llm-light --completion fish | source
    powershell  llm-light --completion powershell | Out-String | Invoke-Expression
"""

SHELLS = ("bash", "zsh", "fish", "powershell")
PROGRAM = "llm-light"


def _options(parser) -> list:
    """(option strings, value choices or None) for each option, without --completion's own help."""
    found = []
    for action in parser._actions:
        if action.option_strings:
            found.append((list(action.option_strings), [str(c) for c in action.choices] if action.choices else None))
    return found


def _bash(options) -> str:
    flags = " ".join(flag for names, _ in options for flag in names)
    cases = "".join(f'        {"|".join(names)}) COMPREPLY=($(compgen -W "{" ".join(values)}" -- "$cur")); return;;\n'
                    for names, values in options if values)
    return (
        "_llm_light_complete() {\n"
        '    local cur="${COMP_WORDS[COMP_CWORD]}" prev="${COMP_WORDS[COMP_CWORD-1]}"\n'
        '    case "$prev" in\n'
        f"{cases}"
        "    esac\n"
        f'    COMPREPLY=($(compgen -W "{flags}" -- "$cur"))\n'
        "}\n"
        f"complete -F _llm_light_complete {PROGRAM}\n"
    )


def _zsh(options) -> str:
    return "autoload -U +X bashcompinit && bashcompinit\n" + _bash(options)


def _fish(options) -> str:
    lines = []
    for names, values in options:
        parts = [f"complete -c {PROGRAM}"]
        for name in names:
            parts.append(f"-l {name[2:]}" if name.startswith("--") else f"-s {name[1:]}")
        if values:
            parts.append(f'-x -a "{" ".join(values)}"')
        lines.append(" ".join(parts))
    return "\n".join(lines) + "\n"


def _powershell(options) -> str:
    flags = ", ".join(f"'{flag}'" for names, _ in options for flag in names)
    cases = "".join(f"        '{name}' {{ $values = @({', '.join(repr(v) for v in values)}) }}\n"
                    for names, values in options if values for name in names)
    return (
        f"Register-ArgumentCompleter -Native -CommandName {PROGRAM} -ScriptBlock {{\n"
        "    param($wordToComplete, $commandAst, $cursorPosition)\n"
        f"    $values = @({flags})\n"
        "    $previous = @($commandAst.CommandElements)[-2].ToString()\n"
        "    switch -Exact ($previous) {\n"
        f"{cases}"
        "    }\n"
        "    $values | Where-Object { $_ -like \"$wordToComplete*\" } |\n"
        "        ForEach-Object { [System.Management.Automation.CompletionResult]::new($_) }\n"
        "}\n"
    )


def script(shell: str, parser) -> str:
    return {"bash": _bash, "zsh": _zsh, "fish": _fish, "powershell": _powershell}[shell](_options(parser))
