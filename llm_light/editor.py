import sys
import re
import difflib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from llm_light.ui import _T_EDIT, _T_ERR, _ANSI_CYAN, _ANSI_GREEN, _ANSI_RED, _ANSI_BOLD, _ANSI_DIM, _ANSI_RESET, show_diff, ToolExecutionManager, choose
import llm_light.state as state
from llm_light.linter import _lint_content
from llm_light import git_manager, permissions

_ANY_BLOCK = re.compile(r"```[a-zA-Z0-9_+-]*(?::([^\n`]+))?[ \t]*\n(.*?)```", re.DOTALL)


def _prompt_approval(n: int) -> bool:
    """Ask before applying edits, with the inline menu under the diff.

    "Don't ask again" holds for these edits for the rest of the session
    (state.auto_approve_edits); it does not cover agent commits.
    """
    if state.auto_approve_edits:
        return True
    choice = choose(
        f"Apply {n} change{'s' if n != 1 else ''}?",
        [("yes", "Yes"),
         ("always", "Yes, and don't ask again this session"),
         ("no", "No, discard")],
    )
    if choice == "always":
        state.auto_approve_edits = True
    return choice in ("yes", "always")
_SR_PATTERN = re.compile(
    r"^<<<<<<< SEARCH\n(.*?)=======\n(.*?)>>>>>>> REPLACE\n?$",
    re.DOTALL,
)

def parse_edit_blocks(text):
    edits = []
    for m in _ANY_BLOCK.finditer(text):
        raw_path = m.group(1)
        path = raw_path.strip() if raw_path else ""
        body = m.group(2)

        # Only treat a fenced block as a file edit when the fence explicitly
        # carries a "path:" marker (e.g. ```python:src/foo.py). Previously a
        # leading "# src/foo.py"-style comment inside the body was also
        # accepted as a filename hint, but that misclassifies any quoted code
        # example in an answer (book excerpts, docs snippets) as a new-file
        # creation — which then triggers spurious lint-error retries and
        # strips the response text. The system prompt already instructs
        # Claude to use the explicit "path:" form.
        if not path:
            continue

        path = path.lstrip("/\\")
        while path.startswith("./") or path.startswith(".\\"):
            path = path[2:]

        sr = _SR_PATTERN.match(body)
        if sr:
            edits.append({"type": "edit", "path": path,
                          "search": sr.group(1), "replace": sr.group(2)})
        else:
            edits.append({"type": "new", "path": path, "content": body})
    return edits

def _safe_target(path_str):
    """Resolve an edit target and confirm it stays inside the project root.

    Claude-authored paths are untrusted: content in indexed files can steer
    the model (prompt injection), so a reply could carry an absolute path or
    a '../' escape. parse_edit_blocks only strips leading separators — it does
    not stop traversal. Reject anything that resolves outside the cwd.
    """
    root = Path.cwd().resolve()
    candidate = (root / path_str).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(
            f"Refusing to touch '{path_str}': path resolves outside the project root"
        )
    return candidate


def _read_text(p):
    """A file's text as stored: its own line endings, and any byte that is not
    UTF-8 kept as a surrogate escape, so writing it back with _encode changes
    nothing the edit did not name (#265)."""
    return p.read_bytes().decode("utf-8", errors="surrogateescape")


def _encode(text):
    return text.encode("utf-8", errors="surrogateescape")


def _printable(text):
    """Text safe to print, lint or send: an undecodable byte becomes U+FFFD."""
    return _encode(text).decode("utf-8", errors="replace")


def _uses_crlf(text):
    crlf = text.count("\r\n")
    return crlf > text.count("\n") - crlf


def _resolve_new_content(edit, current=None):
    """Return (old, new) text for one edit block.

    `current` is the file's text after the earlier blocks of the same reply
    (apply_edits passes it); without it the file is read from disk. SEARCH
    and REPLACE come with LF endings; a file that uses CRLF keeps CRLF.
    """
    old, new = _resolve_raw(edit, current)
    if old and _uses_crlf(old):
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    return old, new


def _resolve_raw(edit, current):
    p = _safe_target(edit["path"])
    if edit["type"] == "new":
        if current is not None:
            return current, edit["content"]
        old = _read_text(p) if p.exists() else ""
        return old, edit["content"]

    if current is not None:
        old = current
    elif not p.exists():
        raise FileNotFoundError(f"File not found: {edit['path']}")
    else:
        old = _read_text(p)
    search = edit["search"]
    replace = edit["replace"]
    
    if search in old:
        return old, old.replace(search, replace, 1)
        
    s_strip = search.strip("\r\n")
    r_strip = replace.strip("\r\n")
    if s_strip and s_strip in old:
        return old, old.replace(s_strip, r_strip, 1)
        
    norm_old = old.replace("\r\n", "\n")
    norm_search = search.replace("\r\n", "\n")
    norm_replace = replace.replace("\r\n", "\n")
    
    if norm_search in norm_old:
        return old, norm_old.replace(norm_search, norm_replace, 1)
        
    norm_s_strip = norm_search.strip("\n")
    norm_r_strip = norm_replace.strip("\n")
    if norm_s_strip and norm_s_strip in norm_old:
        return old, norm_old.replace(norm_s_strip, norm_r_strip, 1)
        
    s_lines = norm_search.splitlines()
    o_lines = norm_old.splitlines()
    
    while s_lines and not s_lines[0].strip(): s_lines.pop(0)
    while s_lines and not s_lines[-1].strip(): s_lines.pop()
    
    if s_lines:
        s_stripped = [l.strip() for l in s_lines]
        o_stripped = [l.strip() for l in o_lines]
        
        slen = len(s_stripped)
        for i in range(len(o_stripped) - slen + 1):
            if o_stripped[i:i+slen] == s_stripped:
                orig_lead = len(o_lines[i]) - len(o_lines[i].lstrip(" \t"))
                search_lead = len(s_lines[0]) - len(s_lines[0].lstrip(" \t"))
                
                new_r_lines = []
                for rl in norm_replace.splitlines():
                    r_trim = rl.lstrip(" \t")
                    if not r_trim:
                        new_r_lines.append("")
                        continue
                    rel_indent = max(0, len(rl) - len(r_trim) - search_lead)
                    new_r_lines.append(" " * (orig_lead + rel_indent) + r_trim)
                
                prefix = "\n".join(o_lines[:i])
                suffix = "\n".join(o_lines[i+slen:])
                
                res = []
                if prefix: res.append(prefix)
                res.append("\n".join(new_r_lines))
                if suffix: res.append(suffix)
                
                final_str = "\n".join(res)
                if norm_old.endswith("\n") and not final_str.endswith("\n"):
                    final_str += "\n"
                return old, final_str
                
    # No guessing (#261): a near match used to be replaced too, which could
    # overwrite a different function or paste the whole REPLACE over a shorter
    # window. The retry gets the nearest text instead, to quote exactly.
    # The excerpt goes to the model: never from a file the index skips (a
    # dotfile such as .env, or anything under a hidden or vendored directory).
    from llm_light.skeleton import _is_skipped
    hint = "" if _is_skipped(p.relative_to(Path.cwd().resolve())) else _printable(_nearest_text(s_lines, o_lines))
    raise ValueError(
        f"SEARCH block not found in {edit['path']}: it must match the file exactly "
        f"(only indentation may differ).{hint}")


_HINT_MAX_LINES = 40


def _nearest_text(s_lines, o_lines):
    """The file's lines most like the SEARCH, as a hint for the retry message.

    Only ever quoted, never written. Windows of the SEARCH's line count plus or
    to plus 2 are compared with difflib (never shorter: a shorter window drops
    the lines the SEARCH got wrong); below a ratio of 0.5 nothing is named.
    """
    if not s_lines or not o_lines:
        return ""
    s_text = "\n".join(l.strip() for l in s_lines)
    o_stripped = [l.strip() for l in o_lines]
    slen = len(s_lines)
    best_ratio, best_idx, best_len = 0.0, -1, 0
    n = len(o_stripped)
    for w_size in range(min(slen, n), min(n, slen + 2) + 1):
        for i in range(len(o_stripped) - w_size + 1):
            matcher = difflib.SequenceMatcher(None, s_text, "\n".join(o_stripped[i:i + w_size]))
            if matcher.real_quick_ratio() <= best_ratio or matcher.quick_ratio() <= best_ratio:
                continue
            ratio = matcher.ratio()
            if ratio > best_ratio:
                best_ratio, best_idx, best_len = ratio, i, w_size
    if best_ratio < 0.5:
        return ""
    shown = o_lines[best_idx:best_idx + min(best_len, _HINT_MAX_LINES)]
    return (f"\nNearest text, lines {best_idx + 1}-{best_idx + best_len}:\n"
            + "\n".join(shown))


def apply_edits(edits, check_only=False, explanation="", auto_apply=False):
    """
    Apply file edits from Claude's response.
    
    Args:
        edits: List of edit dictionaries (from parse_edit_blocks).
        check_only: If True, only check edits for syntax errors, don't write files.
        explanation: Brief explanation of changes (used in auto-commit message).
        
    Returns:
        If check_only=True, returns list of lint errors (empty if no errors).
        If check_only=False, returns None after applying changes (or early on error).
    """
    if not edits:
        if not check_only:
            print(f"  {_T_EDIT}  No file blocks found in response.")
        return [] if check_only else None

    # The system prompt asks for one block per change, even within a file, so
    # a file's blocks apply in order, each to the result of the one before it.
    # Each file is then checked, shown, written and committed once (#260).
    files = {}
    lint_errors = []
    for e in edits:
        try:
            key = _safe_target(e["path"])
            f = files.get(key)
            old, new = _resolve_new_content(e, current=f["_new"] if f else None)
            if f is None:
                files[key] = {"path": e["path"], "_old": old, "_new": new}
            else:
                f["_new"] = new
        except Exception as ex:
            if check_only:
                lint_errors.append(f"Edit failed for {e['path']}: {ex}")
            else:
                print(f"  {_T_ERR}  {ex}")

    if check_only:
        for f in files.values():
            err = _lint_content(f["path"], _printable(f["_new"]))
            if err:
                lint_errors.append(f"SyntaxError in {f['path']}:\n{err}")
        return lint_errors

    applicable = list(files.values())
    if not applicable:
        print(f"  {_T_EDIT}  No applicable changes.\n")
        return None

    print()
    for r in applicable:
        is_new = not Path(r["path"]).exists()
        tag = f"{_ANSI_GREEN}new file{_ANSI_RESET}" if is_new else f"{_ANSI_CYAN}edited{_ANSI_RESET}"
        print(f"  {_T_EDIT}  {r['path']}  {_ANSI_DIM}({tag}){_ANSI_RESET}")
    print()

    for r in applicable:
        show_diff(r["path"], _printable(r["_new"]), old_content=_printable(r["_old"]))  # the panel is titled with the path

    if auto_apply or permissions.auto_edits():
        auto = True
    elif not sys.stdin.isatty():
        auto = True
    else:
        auto = _prompt_approval(len(applicable))

    if not auto:
        print(f"  {_T_EDIT}  Cancelled.\n")
        return

    n = len(applicable)
    label = f"Applying {n} change{'s' if n != 1 else ''}"
    created = {r["path"] for r in applicable if not Path(r["path"]).exists()}
    errors = []
    written_files = []

    def _write_one(r):
        p = Path(r["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        if r["path"] in created:
            p.write_text(r["_new"], encoding="utf-8")  # a new file: the platform's line ending
        else:
            p.write_bytes(_encode(r["_new"]))           # an existing one: its own endings and bytes
        return r["path"]

    with ToolExecutionManager(label):
        if len(applicable) > 1:
            with ThreadPoolExecutor(max_workers=min(len(applicable), 8)) as pool:
                futures = {pool.submit(_write_one, r): r for r in applicable}
                for fut in as_completed(futures):
                    try:
                        written_files.append(fut.result())
                    except Exception as ex:
                        errors.append(f"{futures[fut]['path']}: {ex}")
        else:
            try:
                written_files.append(_write_one(applicable[0]))
            except Exception as ex:
                errors.append(f"{applicable[0]['path']}: {ex}")

    for path in written_files:
        print(f"  {_T_EDIT}  {_ANSI_CYAN}{path}{_ANSI_RESET}")
    for err in errors:
        print(f"  {_T_ERR}  Failed to write {err}")

    # The user's PostToolUse hooks (a formatter, say) run before the commit, so it
    # holds what they made of the file.
    from llm_light import hooks
    if hooks.configured().get("PostToolUse"):
        for path in written_files:
            hooks.after_edit(path, new=path in created)

    if written_files and git_manager.is_git_repo():
        # Only offer the test run (and its revert) when the commit went in;
        # otherwise "revert the last commit" would target an older commit.
        if git_manager.auto_commit(written_files, explanation):
            from llm_light.affected_tests import check_after_edit
            check_after_edit(written_files)
