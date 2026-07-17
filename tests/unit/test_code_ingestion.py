"""Code-project ingestion: file-type recognition, ignore filtering, code
chunking, symbol/import extraction, structure pages, and import-edge survival."""

import uuid
from pathlib import Path

import aiosqlite
import pytest

import domain.watcher as _watcher
from domain.file_types import CODE_TYPES, SIMPLE_TEXT_TYPES, TEXT_INDEX_TYPES

SCHEMA_PATH = Path(__file__).parents[2] / "shared" / "sqlite_schema.sql"
USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _reset_watcher_ignore_cache():
    """The watcher caches ignore patterns in a module global; keep tests that
    exercise _should_ignore from leaking that cache into (or out of) the suite."""
    _watcher._ignore_patterns = None
    yield
    _watcher._ignore_patterns = None


async def _init_db(workspace: Path) -> aiosqlite.Connection:
    (workspace / ".llmwiki").mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(workspace / ".llmwiki" / "index.db"))
    await db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    await db.execute(
        "INSERT INTO workspace (id, name, description, user_id) VALUES (?, 'ws', '', ?)",
        (str(uuid.uuid4()), USER_ID),
    )
    await db.commit()
    return db


# ── File-type classification ───────────────────────────────────────────────

def test_code_types_are_text_index_types():
    assert CODE_TYPES <= TEXT_INDEX_TYPES
    assert SIMPLE_TEXT_TYPES <= TEXT_INDEX_TYPES
    for ext in ("py", "ts", "tsx", "go", "rs", "java", "rb", "sql"):
        assert ext in CODE_TYPES


def test_code_and_simple_types_are_disjoint():
    # Code files get code-aware chunking; keeping the sets disjoint means the
    # dispatch in chunk_text_document is unambiguous.
    assert CODE_TYPES.isdisjoint(SIMPLE_TEXT_TYPES)


# ── Ignore filtering (CLI ↔ watcher parity) ────────────────────────────────

def test_should_ignore_build_and_vendor_dirs(tmp_path):
    from domain.watcher import _should_ignore
    ws = tmp_path
    for p in ("node_modules/pkg/index.js", "dist/bundle.js", "target/debug/app",
              ".venv/lib/x.py", "build/out.o"):
        assert _should_ignore(ws / p, ws) is True, p
    assert _should_ignore(ws / "src/app.py", ws) is False


def test_should_ignore_lockfiles_and_minified(tmp_path):
    from domain.watcher import _should_ignore
    ws = tmp_path
    for p in ("package-lock.json", "yarn.lock", "web/vendor.min.js", "a.css.map"):
        assert _should_ignore(ws / p, ws) is True, p


def test_should_ignore_respects_gitignore(tmp_path):
    # The autouse fixture clears the watcher's ignore-pattern cache around this.
    (tmp_path / ".gitignore").write_text("secret.py\n*.log\n")
    assert _watcher._should_ignore(tmp_path / "secret.py", tmp_path) is True
    assert _watcher._should_ignore(tmp_path / "run.log", tmp_path) is True
    assert _watcher._should_ignore(tmp_path / "keep.py", tmp_path) is False


# ── Code-aware chunking ────────────────────────────────────────────────────

def test_chunk_code_splits_on_definitions():
    from services.chunker import chunk_code
    # Each function is large enough that they can't all pack into one chunk.
    code = "\n\n".join(
        f"def func_{i}():\n" + "\n".join(f"    x{j} = compute({j})" for j in range(120))
        for i in range(4)
    )
    chunks = chunk_code(code, "py")
    assert len(chunks) >= 2  # big funcs don't all collapse into one chunk
    # Each function's body stays contiguous within a chunk.
    for i in range(4):
        assert any(f"def func_{i}():" in c.content for c in chunks)


def test_chunk_code_keeps_tiny_files():
    from services.chunker import chunk_code
    chunks = chunk_code("def x():\n    return 1\n", "py")
    assert len(chunks) == 1
    assert "def x()" in chunks[0].content


def test_chunk_code_falls_back_to_line_windows_without_defs():
    from services.chunker import chunk_code, CODE_LINE_WINDOW
    code = "\n".join(f"value_{i} = {i}" for i in range(CODE_LINE_WINDOW * 2 + 5))
    chunks = chunk_code(code, "py")
    assert len(chunks) >= 2


def test_chunk_code_single_definition_gets_breadcrumb():
    from services.chunker import chunk_code
    code = "def only_one():\n" + "\n".join(f"    a{j} = {j}" for j in range(300))
    chunks = chunk_code(code, "py")
    assert any(c.header_breadcrumb == "only_one" for c in chunks)


# ── Symbol & import extraction ─────────────────────────────────────────────

def test_extract_symbols_python():
    from services.code_analysis import extract_symbols
    code = "import os\n\ndef foo():\n    pass\n\nclass Bar:\n    def method(self):\n        pass\n"
    syms = extract_symbols(code, "py")
    names = {(s["name"], s["kind"]) for s in syms}
    assert ("foo", "function") in names
    assert ("Bar", "class") in names
    # nested method is not a top-level symbol
    assert not any(s["name"] == "method" for s in syms)


def test_extract_symbols_typescript():
    from services.code_analysis import extract_symbols
    code = "export function add(a,b){return a+b}\nexport class Widget {}\ninterface Opts {}\n"
    syms = {(s["name"], s["kind"]) for s in extract_symbols(code, "ts")}
    assert ("add", "function") in syms
    assert ("Widget", "class") in syms
    assert ("Opts", "interface") in syms


def test_resolve_python_imports():
    from services.code_analysis import _python_module_index, _resolve_python
    paths = {"pkg/__init__.py", "pkg/models.py", "pkg/service.py"}
    idx = _python_module_index(paths)
    content = "from pkg.models import User\nfrom . import models\nimport os\n"
    targets = set(_resolve_python(content, "pkg/service.py", paths, idx))
    assert "pkg/models.py" in targets
    assert "os" not in targets  # external, unresolved


def test_resolve_js_relative_imports():
    from services.code_analysis import _resolve_js
    paths = {"web/util.ts", "web/app.ts"}
    content = "import { add } from './util'\nimport React from 'react'\n"
    targets = _resolve_js(content, "web/app.ts", paths)
    assert targets == ["web/util.ts"]  # react is external, skipped


# ── End-to-end structure pass + import graph ───────────────────────────────

async def _index_code_file(db, ws, rel, content):
    """Minimal stand-in for the CLI indexer: write + insert a source row."""
    (ws / rel).parent.mkdir(parents=True, exist_ok=True)
    (ws / rel).write_text(content)
    ext = rel.rsplit(".", 1)[-1]
    await db.execute(
        "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
        "source_kind, file_type, status, content, tags, version, document_number) "
        "VALUES (?, ?, ?, ?, '/', ?, 'source', ?, 'ready', ?, '[]', 0, "
        "(SELECT COALESCE(MAX(document_number),0)+1 FROM documents))",
        (str(uuid.uuid4()), USER_ID, Path(rel).name, Path(rel).name, rel, ext, content),
    )
    await db.commit()


async def test_build_codebase_pages_end_to_end(tmp_path):
    from services.code_analysis import build_codebase_pages

    db = await _init_db(tmp_path)
    await _index_code_file(db, tmp_path, "pkg/models.py", "class User:\n    pass\n")
    await _index_code_file(db, tmp_path, "pkg/service.py",
                           "from pkg.models import User\n\ndef get_user():\n    return User()\n")
    await db.close()

    summary = build_codebase_pages(tmp_path, tmp_path / ".llmwiki" / "index.db")
    assert "import edge" in summary

    # Pages written to disk.
    structure = (tmp_path / "wiki" / "codebase" / "structure.md").read_text()
    assert "Codebase Structure" in structure
    assert "Python" in structure
    assert (tmp_path / "wiki" / "codebase" / "overview.md").exists()

    # Import edge persisted.
    db = await aiosqlite.connect(str(tmp_path / ".llmwiki" / "index.db"))
    cur = await db.execute(
        "SELECT d1.relative_path, d2.relative_path FROM document_references r "
        "JOIN documents d1 ON d1.id = r.source_document_id "
        "JOIN documents d2 ON d2.id = r.target_document_id "
        "WHERE r.reference_type = 'imports'"
    )
    edges = await cur.fetchall()
    assert ("pkg/service.py", "pkg/models.py") in edges
    await db.close()


async def test_graph_rebuild_preserves_import_edges(tmp_path):
    """rebuild_local wipes wiki-derived edges but must keep 'imports' edges."""
    from services.code_analysis import build_codebase_pages
    from services.graph import rebuild_local

    db = await _init_db(tmp_path)
    await _index_code_file(db, tmp_path, "pkg/a.py", "class A:\n    pass\n")
    await _index_code_file(db, tmp_path, "pkg/b.py", "from pkg.a import A\n")
    await db.close()

    build_codebase_pages(tmp_path, tmp_path / ".llmwiki" / "index.db")

    db = await aiosqlite.connect(str(tmp_path / ".llmwiki" / "index.db"))

    async def _import_edge_count():
        cur = await db.execute(
            "SELECT COUNT(*) FROM document_references WHERE reference_type = 'imports'"
        )
        return (await cur.fetchone())[0]

    assert await _import_edge_count() == 1
    await rebuild_local(db, USER_ID)
    assert await _import_edge_count() == 1  # survived the wiki-edge rebuild
    await db.close()
