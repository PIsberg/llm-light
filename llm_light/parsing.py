import re
from pathlib import Path
from llm_light.config import _LANG_CONFIG, _NAME_CHILD_TYPES

try:
    from tree_sitter import Parser as TSParser
except ImportError:
    pass

_COMMENT_EXTS_C = frozenset({".java", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs"})


def _walk(node, node_types, results):
    """DFS: collect nodes whose type is in node_types; don't recurse into matches."""
    if node.type in node_types:
        results.append(node)
        return
    for child in node.children:
        _walk(child, node_types, results)


def _symbol_span(node):
    """The node a symbol's chunk covers. An arrow function bound by const,
    let or var takes its whole declaration (and an export in front), so the
    chunk reads "export const load = async (id) => ..." (#270)."""
    parent = node.parent
    if node.type in _BOUND_FUNCTION_TYPES and parent is not None and parent.type == "variable_declarator":
        decl = parent.parent
        if decl is not None and decl.type in ("lexical_declaration", "variable_declaration"):
            if decl.parent is not None and decl.parent.type == "export_statement":
                return decl.parent
            return decl
    return node


_BOUND_FUNCTION_TYPES = frozenset({"arrow_function", "function_expression"})


def _extract_symbol_name(node):
    """Return the identifier name for an AST symbol node."""
    # const load = async () => ...: the function is named by its declarator
    if node.type in _BOUND_FUNCTION_TYPES and node.parent is not None \
            and node.parent.type == "variable_declarator":
        for child in node.parent.children:
            if child.type in _NAME_CHILD_TYPES:
                return child.text.decode("utf-8", errors="replace")
    # Python decorated_definition wraps a function — dig inside for the name
    if node.type == "decorated_definition":
        for child in node.children:
            if child.type in {"function_definition", "async_function_definition"}:
                return _extract_symbol_name(child)
    for child in node.children:
        if child.type in _NAME_CHILD_TYPES:
            return child.text.decode("utf-8", errors="replace")
    return f"{node.type}_{node.start_point[0]}"


def _chunk_with_treesitter(filepath, source, language, node_types):
    """
    Parse source with tree-sitter and emit one chunk per matched symbol node.
    Preamble = source lines before the first symbol (imports, class header, etc.).
    Falls back to whole-file if parsing yields no symbols.
    """
    src_bytes = bytes(source, "utf-8")
    parser    = TSParser(language)
    tree      = parser.parse(src_bytes)

    symbols = []
    _walk(tree.root_node, node_types, symbols)

    if not symbols:
        return [{"id": filepath, "text": source}]

    spans          = [_symbol_span(node) for node in symbols]
    lines          = source.splitlines(keepends=True)
    first_sym_line = min(span.start_point[0] for span in spans)   # 0-indexed row
    preamble       = "".join(lines[:first_sym_line]).rstrip()

    chunks = []
    seen = {}

    def add(name, body):
        # Deduplicate overloaded names with a numeric suffix
        if name in seen:
            seen[name] += 1
            uid = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
            uid = name
        chunk_text = (
            f"// {filepath}\n"
            + preamble + "\n"
            + "    // ...\n"
            + body.strip()
            + "\n"
        )
        chunks.append({"id": f"{filepath}::{uid}", "text": chunk_text})

    for node, span in zip(symbols, spans):
        # tree-sitter offsets count UTF-8 bytes, not characters (#262).
        add(_extract_symbol_name(node),
            src_bytes[span.start_byte:span.end_byte].decode("utf-8", errors="replace"))

    covered = set()
    for span in spans:
        covered.update(range(span.start_point[0], span.end_point[0] + 1))
    for row, body in _gap_pieces(lines, first_sym_line, covered):
        add(_gap_name(body, row), body)

    return chunks


# Top-level code between symbols (#270): constants after the first function,
# class headers and attributes, a class without methods. Split at blank lines
# into pieces of at most _GAP_MAX_LINES; a piece needs _GAP_MIN_LINES non-blank
# lines, one of them code, or it is left out (a lone assignment, a comment).
_GAP_MIN_LINES = 2
_GAP_MAX_LINES = 40
_COMMENT_STARTS = ("#", "//", "/*", "*")
_GAP_NAME_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
    r"(?:const|let|var|class|interface|type|enum|struct|static|final|func|fn|impl|trait|mod)?\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)")
_GAP_NAME_SKIP = frozenset({"if", "for", "while", "try", "with", "return", "import", "from",
                            "package", "use", "else", "elif", "except", "finally", "assert"})


def _gap_pieces(lines, first_row, covered):
    """(first row, text) for each run of uncovered lines from first_row on."""
    pieces, block, start = [], [], None

    def flush():
        nonlocal block, start
        text = "".join(block)
        code = [l.strip() for l in block if l.strip()]
        if len(code) >= _GAP_MIN_LINES and any(not l.startswith(_COMMENT_STARTS) for l in code):
            pieces.append((start, text))
        block, start = [], None

    for row in range(first_row, len(lines)):
        line = lines[row]
        if row in covered:
            flush()
            continue
        if not line.strip() and len(block) >= _GAP_MAX_LINES:
            flush()
            continue
        if start is None:
            if not line.strip():
                continue
            start = row
        block.append(line)
    flush()
    return pieces


def _gap_name(body, row):
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(_COMMENT_STARTS):
            continue
        m = _GAP_NAME_RE.match(line)
        if m and m.group(1) not in _GAP_NAME_SKIP:
            return m.group(1)
        break
    return f"top_{row + 1}"


_BODY_MARK = "\n    // ...\n"


def embed_text(chunk_text):
    """The text a chunk is embedded as: its path line and its body, without
    the file preamble.

    The chunk text (what the model is shown) is the path, the preamble and the
    body. Embedding models read only the start of their input (all-mpnet-base-v2:
    384 tokens), so a long import block left every function of a file with the
    same vector (#264). A whole-file chunk has no preamble and is used as it is.
    Used for the dense model only; the static model keeps the full text.
    """
    head, sep, body = chunk_text.partition(_BODY_MARK)
    if not sep:
        return chunk_text
    return head.split("\n", 1)[0] + "\n" + body


def chunk_file(filepath, source):
    """
    Split a source file into symbol-level chunks using tree-sitter AST parsing.
    Dispatches by file extension; falls back to whole-file for unsupported or
    missing grammar packages.
    Chunk ID format: 'filepath::symbolName' or 'filepath' (whole-file fallback).
    """
    ext = Path(filepath).suffix.lower()
    cfg = _LANG_CONFIG.get(ext)
    if cfg is None:
        return [{"id": filepath, "text": source}]
    return _chunk_with_treesitter(filepath, source, cfg["lang"], cfg["node_types"])


def _strip_comments_by_parse(text, ext, strip_docstrings):
    """Remove the comments tree-sitter finds in `text`, or None without a grammar.

    Regexes cut string literals: "#ff8800" and every "https://" URL lost their
    tail (#266). The parse knows a comment from a string, also in a fragment
    such as one method body, where it recovers around the missing context.
    Javadoc and other /** */ blocks stay unless strip_docstrings; so do Python
    docstrings (a bare string statement).
    """
    cfg = _LANG_CONFIG.get(ext)
    if not cfg:
        return None
    try:
        src = text.encode("utf-8")
        tree = TSParser(cfg["lang"]).parse(src)
    except Exception:
        return None
    cuts = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type.endswith("comment"):
            if strip_docstrings or not src[node.start_byte:node.end_byte].startswith(b"/**"):
                cuts.append((node.start_byte, node.end_byte))
            continue
        if (strip_docstrings and ext == ".py" and node.type == "expression_statement"
                and node.named_child_count == 1 and node.named_children[0].type == "string"):
            cuts.append((node.start_byte, node.end_byte))
            continue
        stack.extend(node.children)
    for start, end in sorted(cuts, reverse=True):
        src = src[:start] + src[end:]
    return src.decode("utf-8", errors="replace")


def _strip_comments(text: str, ext: str) -> str:
    """Remove low-value comments from a code chunk to cut retrieved-context tokens.

    Strips only code comments — not the file-path header line or preamble —
    so the structural context (package, imports, class declaration) is preserved.

    Docstrings (Python) and Javadoc (`/** ... */`) carry the natural-language
    description of what the code does, which is exactly what the model needs to
    answer "what does X do?". They are kept by default; set STRIP_DOCSTRINGS
    (LLM_LIGHT_STRIP_DOCS=1) to remove them too.
    """
    from llm_light.config import STRIP_DOCSTRINGS

    stripped = _strip_comments_by_parse(text, ext, STRIP_DOCSTRINGS)
    if stripped is not None:
        return re.sub(r'\n{3,}', '\n\n', stripped)

    # No grammar for this language: the regex fallback, which cannot tell a
    # comment marker inside a string from a real one.
    if ext in _COMMENT_EXTS_C:
        if STRIP_DOCSTRINGS:
            text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)   # all /* */ blocks
        else:
            # Drop plain /* */ blocks but keep Javadoc /** */ doc comments.
            text = re.sub(r'/\*(?!\*).*?\*/', '', text, flags=re.DOTALL)
        text = re.sub(r'//[^\n]*',  '', text)                        # // line
    elif ext == ".py":
        text = re.sub(r'#.*', '', text)                             # # line
        if STRIP_DOCSTRINGS:
            text = re.sub(r'"""[\s\S]*?"""', '', text)             # """ docstring """
            text = re.sub(r"'''[\s\S]*?'''", '', text)             # ''' docstring '''
    # Collapse runs of 3+ blank lines left behind by removal
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text
