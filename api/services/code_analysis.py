"""Deterministic code-repository analysis for `llmwiki --code`.

Runs (synchronously, from the CLI) after a code workspace has been indexed. It:

  * extracts top-level symbols and imports from each indexed source file,
  * persists an import graph as 'imports' edges in document_references,
  * stores per-file symbols in documents.metadata,
  * writes wiki/codebase/structure.md (a deterministic facts page) and
    wiki/codebase/overview.md (a scaffold for Claude to flesh into prose).

Pure stdlib plus an optional tree-sitter fast-path is not required here — symbol
and import extraction are regex-based and language-agnostic, robust for Python
and JS/TS and best-effort for other languages. No async, no network.
"""

import datetime
import json
import os
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from pathlib import Path

# Imported lazily-safe: this module lives in api/ which is on sys.path when the
# CLI runs it (see ./llmwiki). CODE_TYPES gates which docs we analyze.
from domain.file_types import CODE_TYPES

# ── Language display names (for the structure page) ────────────────────────
_LANG_NAMES = {
    "py": "Python", "pyi": "Python", "js": "JavaScript", "jsx": "JavaScript",
    "mjs": "JavaScript", "cjs": "JavaScript", "ts": "TypeScript", "tsx": "TypeScript",
    "go": "Go", "rs": "Rust", "java": "Java", "kt": "Kotlin", "kts": "Kotlin",
    "scala": "Scala", "c": "C", "h": "C", "cc": "C++", "cpp": "C++", "cxx": "C++",
    "hpp": "C++", "hh": "C++", "cs": "C#", "rb": "Ruby", "php": "PHP",
    "swift": "Swift", "dart": "Dart", "lua": "Lua", "r": "R", "sh": "Shell",
    "bash": "Shell", "zsh": "Shell", "sql": "SQL", "css": "CSS", "scss": "SCSS",
    "vue": "Vue", "svelte": "Svelte", "ex": "Elixir", "exs": "Elixir",
    "clj": "Clojure", "hs": "Haskell", "ml": "OCaml", "proto": "Protobuf",
    "graphql": "GraphQL", "gql": "GraphQL",
}

# A line that opens a top-level definition (no leading whitespace = top level).
_SYMBOL_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?"
    r"(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|final\s+|abstract\s+|open\s+|pub(?:\([^)]*\))?\s+)*"
    r"(?:async\s+)?"
    r"(?P<kind>def|class|function|func|fn|interface|struct|enum|impl|trait|type|namespace|object|record)\b"
    r"\s+(?P<name>[A-Za-z_$][\w$]*)"
)

_KIND_NORMALIZE = {
    "def": "function", "function": "function", "func": "function", "fn": "function",
    "class": "class", "struct": "class", "record": "class", "object": "class",
}

_JS_EXTS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
            "/index.ts", "/index.tsx", "/index.js", "/index.jsx")


# ── Public entry point ─────────────────────────────────────────────────────

def build_codebase_pages(ws: Path, db_path: Path) -> str:
    """Analyze the indexed repo, persist the import graph, and write the
    wiki/codebase/ pages. Idempotent. Returns a short summary string."""
    ws = Path(ws)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        user_id = conn.execute("SELECT user_id FROM workspace LIMIT 1").fetchone()
        if not user_id:
            return "no workspace"
        user_id = user_id[0]

        files = _load_source_files(conn)
        all_paths = {f["relative_path"] for f in files}

        # Per-file symbols → metadata; collect for the structure page.
        for f in files:
            symbols = extract_symbols(f["content"] or "", f["file_type"])
            _store_symbols(conn, f["id"], f["file_type"], symbols)
            f["symbols"] = symbols

        # Import graph: resolve each file's imports to in-repo targets.
        module_index = _python_module_index(all_paths)
        edges = []  # (source_rel, target_rel)
        for f in files:
            targets = _resolve_file_imports(
                f["content"] or "", f["file_type"], f["relative_path"],
                all_paths, module_index,
            )
            for tgt in targets:
                if tgt != f["relative_path"]:
                    edges.append((f["relative_path"], tgt))

        n_edges = _rebuild_import_edges(conn, files, edges)

        # Structure + overview pages.
        structure_md = _build_structure_markdown(ws.name, files, edges)
        overview_md = _build_overview_scaffold(ws.name, files)
        _write_page(conn, ws, user_id, "wiki/codebase/structure.md", "Codebase Structure", structure_md)
        _write_page(conn, ws, user_id, "wiki/codebase/overview.md", "Codebase Overview", overview_md)

        conn.commit()
        n_symbols = sum(len(f["symbols"]) for f in files)
        return f"{len(files)} files, {n_symbols} symbols, {n_edges} import edges"
    finally:
        conn.close()


# ── DB helpers ─────────────────────────────────────────────────────────────

def _load_source_files(conn: sqlite3.Connection) -> list[dict]:
    placeholders = ",".join("?" for _ in CODE_TYPES)
    rows = conn.execute(
        f"SELECT id, relative_path, file_type, content FROM documents "
        f"WHERE source_kind = 'source' AND file_type IN ({placeholders})",
        tuple(CODE_TYPES),
    ).fetchall()
    return [dict(r) for r in rows]


def _store_symbols(conn: sqlite3.Connection, doc_id: str, file_type: str, symbols: list[dict]) -> None:
    row = conn.execute("SELECT metadata FROM documents WHERE id = ?", (doc_id,)).fetchone()
    meta = {}
    if row and row[0]:
        try:
            meta = json.loads(row[0]) or {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
    meta["language"] = _LANG_NAMES.get(file_type.lower(), file_type)
    meta["symbols"] = symbols
    conn.execute("UPDATE documents SET metadata = ? WHERE id = ?", (json.dumps(meta), doc_id))


def _rebuild_import_edges(conn: sqlite3.Connection, files: list[dict], edges: list[tuple[str, str]]) -> int:
    """Replace all 'imports' edges with the freshly computed set."""
    id_by_path = {f["relative_path"]: f["id"] for f in files}
    conn.execute("DELETE FROM document_references WHERE reference_type = 'imports'")
    seen = set()
    n = 0
    for src_rel, tgt_rel in edges:
        src_id, tgt_id = id_by_path.get(src_rel), id_by_path.get(tgt_rel)
        if not src_id or not tgt_id or (src_id, tgt_id) in seen:
            continue
        seen.add((src_id, tgt_id))
        conn.execute(
            "INSERT OR IGNORE INTO document_references "
            "(id, source_document_id, target_document_id, reference_type) "
            "VALUES (?, ?, ?, 'imports')",
            (str(uuid.uuid4()), src_id, tgt_id),
        )
        n += 1
    return n


def _write_page(conn: sqlite3.Connection, ws: Path, user_id: str,
                relative: str, title: str, content: str) -> None:
    """Write a wiki page to disk and upsert its index row (by relative_path)."""
    disk = ws / relative
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_text(content, encoding="utf-8")

    parts = relative.split("/")
    dir_path = "/" + "/".join(parts[:-1]) + "/"
    existing = conn.execute("SELECT id FROM documents WHERE relative_path = ?", (relative,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE documents SET content = ?, updated_at = datetime('now'), "
            "version = version + 1 WHERE id = ?",
            (content, existing[0]),
        )
    else:
        conn.execute(
            "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
            "source_kind, file_type, status, content, tags, version, document_number) "
            "VALUES (?, ?, ?, ?, ?, ?, 'wiki', 'md', 'ready', ?, '[]', 0, "
            "(SELECT COALESCE(MAX(document_number), 0) + 1 FROM documents))",
            (str(uuid.uuid4()), user_id, parts[-1], title, dir_path, relative, content),
        )


# ── Symbol extraction ──────────────────────────────────────────────────────

def extract_symbols(content: str, file_type: str) -> list[dict]:
    """Top-level definitions as [{name, kind, line}] (1-indexed lines)."""
    symbols = []
    for i, line in enumerate(content.split("\n"), 1):
        m = _SYMBOL_RE.match(line)
        if not m:
            continue
        kind = _KIND_NORMALIZE.get(m.group("kind"), m.group("kind"))
        symbols.append({"name": m.group("name"), "kind": kind, "line": i})
    return symbols


# ── Import extraction + resolution ─────────────────────────────────────────

_PY_IMPORT_RE = re.compile(r"^\s*import\s+([\w.]+)", re.MULTILINE)
_PY_FROM_RE = re.compile(r"^\s*from\s+(\.*)([\w.]*)\s+import\s+(.+)$", re.MULTILINE)
_JS_FROM_RE = re.compile(r"""(?:import|export)\s+[^;'"]*?from\s+['"]([^'"]+)['"]""")
_JS_REQUIRE_RE = re.compile(r"""(?:require|import)\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_JS_BARE_IMPORT_RE = re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""", re.MULTILINE)
_RUST_USE_RE = re.compile(r"^\s*use\s+([\w:]+)", re.MULTILINE)
_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)


def _resolve_file_imports(content, file_type, file_rel, all_paths, module_index):
    ft = file_type.lower()
    if ft in ("py", "pyi"):
        return _resolve_python(content, file_rel, all_paths, module_index)
    if ft in ("js", "jsx", "ts", "tsx", "mjs", "cjs"):
        return _resolve_js(content, file_rel, all_paths)
    if ft == "rs":
        return _resolve_suffix(content, _RUST_USE_RE, all_paths, sep="::", exts=(".rs", "/mod.rs"))
    if ft == "java":
        return _resolve_suffix(content, _JAVA_IMPORT_RE, all_paths, sep=".", exts=(".java",))
    return []


def _python_module_index(all_paths: set[str]) -> dict[str, str]:
    """Map dotted module path → relative file path for every indexed .py file."""
    index: dict[str, str] = {}
    for p in all_paths:
        if not (p.endswith(".py") or p.endswith(".pyi")):
            continue
        stem = p.rsplit(".", 1)[0]
        if stem.endswith("/__init__"):
            stem = stem[: -len("/__init__")]
        module = stem.replace("/", ".")
        index[module] = p
    return index


def _match_python_module(imp: str, module_index: dict[str, str]) -> str | None:
    """Find the file whose module path is (or ends with) the imported module.

    Suffix matching handles src/ layouts (src.pkg.mod matches import pkg.mod).
    Prefer the shortest matching module (closest to a package root).
    """
    if imp in module_index:
        return module_index[imp]
    candidates = [(m, p) for m, p in module_index.items() if m.endswith("." + imp)]
    if not candidates:
        return None
    return min(candidates, key=lambda mp: len(mp[0]))[1]


def _resolve_python(content, file_rel, all_paths, module_index) -> list[str]:
    targets: list[str] = []
    pkg_dir = os.path.dirname(file_rel)

    for imp in _PY_IMPORT_RE.findall(content):
        tgt = _match_python_module(imp, module_index)
        if tgt:
            targets.append(tgt)

    for dots, mod, names in _PY_FROM_RE.findall(content):
        if dots:  # relative import: from . / .. / .mod import ...
            base = pkg_dir
            for _ in range(len(dots) - 1):
                base = os.path.dirname(base)
            if mod:
                base = os.path.normpath(os.path.join(base, mod.replace(".", "/")))
            # `from .pkg import a, b` targets pkg; `from . import a, b` targets
            # each name as a submodule. Resolve whichever applies against base.
            stems = ([base] if mod else []) + [
                os.path.join(base, _import_name(n)) for n in names.split(",")
            ]
            for stem in stems:
                for cand in (f"{stem}.py", f"{stem}/__init__.py"):
                    cand = os.path.normpath(cand).lstrip("./")
                    if cand in all_paths:
                        targets.append(cand)
                        break
        elif mod:
            tgt = _match_python_module(mod, module_index)
            if tgt:
                targets.append(tgt)
    return targets


def _import_name(token: str) -> str:
    """Strip `as alias`, parens, and whitespace from one imported name."""
    return token.split(" as ")[0].strip(" ()\t").strip()


def _resolve_js(content, file_rel, all_paths) -> list[str]:
    specs = set(_JS_FROM_RE.findall(content))
    specs |= set(_JS_REQUIRE_RE.findall(content))
    specs |= set(_JS_BARE_IMPORT_RE.findall(content))

    targets: list[str] = []
    file_dir = os.path.dirname(file_rel)
    for spec in specs:
        if not (spec.startswith(".") or spec.startswith("/")):
            continue  # bare/external package
        base = os.path.normpath(os.path.join(file_dir, spec))
        for ext in _JS_EXTS:
            cand = (base + ext).lstrip("./")
            if cand in all_paths:
                targets.append(cand)
                break
    return targets


def _resolve_suffix(content, pattern, all_paths, sep, exts) -> list[str]:
    """Best-effort: turn a module ref (a::b::c / a.b.C) into path suffixes and
    match any indexed file whose path ends with one of them."""
    targets: list[str] = []
    for ref in pattern.findall(content):
        rel = ref.replace(sep, "/")
        for ext in exts:
            suffix = rel + ext
            match = next((p for p in all_paths if p == suffix or p.endswith("/" + suffix)), None)
            if match:
                targets.append(match)
                break
    return targets


# ── Page rendering ─────────────────────────────────────────────────────────

def _frontmatter(title: str, description: str, tags: list[str]) -> str:
    today = datetime.date.today().isoformat()
    tag_list = ", ".join(tags)
    return (
        "---\n"
        f"title: {title}\n"
        f"description: {description}\n"
        f"date: {today}\n"
        f"tags: [{tag_list}]\n"
        "---\n\n"
    )


def _build_structure_markdown(repo_name: str, files: list[dict], edges: list[tuple[str, str]]) -> str:
    lang_files = Counter()
    lang_lines = Counter()
    for f in files:
        lang = _LANG_NAMES.get(f["file_type"].lower(), f["file_type"])
        lang_files[lang] += 1
        lang_lines[lang] += (f["content"] or "").count("\n") + 1

    out = [_frontmatter(
        "Codebase Structure",
        f"Auto-generated structural map of the {repo_name} repository.",
        ["codebase", "structure", "reference"],
    )]
    out.append(f"# Codebase Structure — {repo_name}\n")
    out.append("> Auto-generated by `llmwiki --code`. Regenerated on every reindex; "
               "edit prose in [overview.md](overview.md) instead.\n")

    out.append("## Languages\n")
    out.append("| Language | Files | Lines |")
    out.append("|----------|------:|------:|")
    for lang, count in lang_files.most_common():
        out.append(f"| {lang} | {count} | {lang_lines[lang]:,} |")
    out.append("")

    out.append("## Top-level layout\n")
    out.append("```")
    for line in _dir_tree(files):
        out.append(line)
    out.append("```\n")

    entry_points = _entry_points(files)
    if entry_points:
        out.append("## Entry points\n")
        for ep in entry_points:
            out.append(f"- `{ep}`")
        out.append("")

    mermaid = _dependency_mermaid(edges)
    if mermaid:
        out.append("## Module dependencies\n")
        out.append("Import edges aggregated by module (directory).\n")
        out.append(mermaid)
        out.append("")

    return "\n".join(out) + "\n"


def _dir_tree(files: list[dict]) -> list[str]:
    """Render a two-level tree: top-level dirs, then their immediate children
    (subdirs collapsed with a file count, files listed by name)."""
    root_files: list[str] = []
    top_children: dict[str, Counter] = defaultdict(Counter)
    for f in files:
        parts = f["relative_path"].split("/")
        if len(parts) == 1:
            root_files.append(parts[0])
            continue
        # (child name, is_subdir)
        top_children[parts[0]][(parts[1], len(parts) > 2)] += 1

    lines = [f for f in sorted(root_files)]
    for top in sorted(top_children):
        total = sum(top_children[top].values())
        lines.append(f"{top}/  ({total} files)")
        for (name, is_dir), cnt in sorted(top_children[top].items()):
            lines.append(f"  {name}/  ({cnt} files)" if is_dir else f"  {name}")
    return lines


def _entry_points(files: list[dict]) -> list[str]:
    names = {"main", "index", "__main__", "app", "cli", "server", "manage"}
    found = []
    for f in files:
        stem = Path(f["relative_path"]).stem.lower()
        content = f["content"] or ""
        if stem in names or 'if __name__ == "__main__"' in content or "func main(" in content:
            found.append(f["relative_path"])
    return sorted(found)[:15]


def _dependency_mermaid(edges: list[tuple[str, str]], max_edges: int = 25) -> str:
    def module(rel: str) -> str:
        # The containing directory is the "module"; root files stand alone.
        return os.path.dirname(rel) or rel

    agg = set()
    for src, tgt in edges:
        a, b = module(src), module(tgt)
        if a != b:
            agg.add((a, b))
    if not agg:
        return ""

    def node(n: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", n) or "root"

    lines = ["```mermaid", "graph LR"]
    for a, b in sorted(agg)[:max_edges]:
        lines.append(f"    {node(a)}[{a}] --> {node(b)}[{b}]")
    lines.append("```")
    return "\n".join(lines)


def _build_overview_scaffold(repo_name: str, files: list[dict]) -> str:
    top_dirs = sorted({
        f["relative_path"].split("/")[0]
        for f in files if "/" in f["relative_path"]
    })

    out = [_frontmatter(
        "Codebase Overview",
        f"Human-readable overview of the {repo_name} codebase — what it does and how it's organized.",
        ["codebase", "overview"],
    )]
    out.append(f"# {repo_name}\n")
    out.append("<!-- SCAFFOLD: written by `llmwiki --code`. Replace the TODOs below "
               "with real prose. See [structure.md](structure.md) for the generated "
               "facts (languages, layout, dependency graph) to cite. -->\n")
    out.append("## What this is\n")
    out.append("_TODO: One or two paragraphs on what this codebase does and who uses it._\n")
    out.append("## Architecture\n")
    out.append("_TODO: How the pieces fit together. Reference `codebase/structure.md` for the "
               "module dependency graph, and link to the source files you describe._\n")

    if top_dirs:
        out.append("## Modules\n")
        out.append("_TODO: Summarize each top-level module. One subsection per package; "
                   "describe its responsibility and cite the key source files._\n")
        for d in top_dirs:
            out.append(f"### `{d}/`\n")
            out.append("_TODO_\n")

    return "\n".join(out) + "\n"
