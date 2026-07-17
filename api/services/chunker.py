"""Text chunker with header breadcrumb tracking.

Splits document content into ~512 token chunks with ~128 token overlap.
Tracks markdown headers to build breadcrumb context per chunk.
"""

import re
import logging
from dataclasses import dataclass, field

import asyncpg

logger = logging.getLogger(__name__)

CHUNK_SIZE = 512
CHUNK_OVERLAP = 128
MIN_CHUNK_TOKENS = 32
MAX_CHUNK_CHARS = 10_000  # matches DB constraint chk_chunks_content_length

SENTENCE_RE = re.compile(r'(?<=[.!?。！？])\s+')
HEADER_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class Chunk:
    index: int
    content: str
    page: int | None
    start_char: int
    token_count: int
    header_breadcrumb: str = ""


def chunk_text(
    content: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
    page: int | None = None,
    start_char_offset: int = 0,
) -> list[Chunk]:
    """Chunk a text string into overlapping segments with header tracking."""
    if not content or not content.strip():
        return []

    paragraphs = _split_paragraphs(content)
    header_stack: list[tuple[int, str]] = []
    chunks: list[Chunk] = []
    current_blocks: list[str] = []
    current_tokens = 0
    current_start = start_char_offset
    char_pos = start_char_offset

    for para in paragraphs:
        para_tokens = _estimate_tokens(para)

        header_match = HEADER_RE.match(para)
        if header_match:
            level = len(header_match.group(1))
            heading = header_match.group(2).strip()
            header_stack = [(l, t) for l, t in header_stack if l < level]
            header_stack.append((level, heading))

        if current_tokens + para_tokens > chunk_size and current_blocks:
            chunk_text_str = "\n\n".join(current_blocks)
            if _estimate_tokens(chunk_text_str) >= MIN_CHUNK_TOKENS:
                breadcrumb = " > ".join(t for _, t in header_stack)
                chunks.append(Chunk(
                    index=len(chunks),
                    content=chunk_text_str,
                    page=page,
                    start_char=current_start,
                    token_count=_estimate_tokens(chunk_text_str),
                    header_breadcrumb=breadcrumb,
                ))

            overlap_blocks, overlap_tokens = _get_overlap(current_blocks, overlap)
            current_blocks = overlap_blocks
            current_tokens = overlap_tokens
            current_start = char_pos - sum(len(b) + 2 for b in overlap_blocks)

        current_blocks.append(para)
        current_tokens += para_tokens
        char_pos += len(para) + 2

    if current_blocks:
        chunk_text_str = "\n\n".join(current_blocks)
        if _estimate_tokens(chunk_text_str) >= MIN_CHUNK_TOKENS:
            breadcrumb = " > ".join(t for _, t in header_stack)
            chunks.append(Chunk(
                index=len(chunks),
                content=chunk_text_str,
                page=page,
                start_char=current_start,
                token_count=_estimate_tokens(chunk_text_str),
                header_breadcrumb=breadcrumb,
            ))

    return _enforce_max_chars(chunks)


def _enforce_max_chars(chunks: list[Chunk]) -> list[Chunk]:
    """Split any chunk whose content exceeds MAX_CHUNK_CHARS.

    The paragraph-based chunker emits one chunk per paragraph when a single
    paragraph is bigger than CHUNK_SIZE — fine for English wiki text, but CJK
    paragraphs and long code blocks routinely exceed the 10k-char DB limit.
    Split such chunks on sentence boundaries; fall back to fixed-size slices
    if no sentence break is available.
    """
    if not any(len(c.content) > MAX_CHUNK_CHARS for c in chunks):
        return chunks

    result: list[Chunk] = []
    for c in chunks:
        if len(c.content) <= MAX_CHUNK_CHARS:
            result.append(Chunk(
                index=len(result), content=c.content, page=c.page,
                start_char=c.start_char, token_count=c.token_count,
                header_breadcrumb=c.header_breadcrumb,
            ))
            continue
        # Each split piece gets its own start_char (base + cumulative offset)
        # so downstream consumers (e.g. text-anchor highlight mapping) can
        # derive each piece's end as start_char + len(content) without
        # adjacent pieces appearing to start at the same paragraph offset.
        base = c.start_char or 0
        offset = 0
        for piece in _split_oversized(c.content):
            result.append(Chunk(
                index=len(result), content=piece, page=c.page,
                start_char=base + offset, token_count=_estimate_tokens(piece),
                header_breadcrumb=c.header_breadcrumb,
            ))
            offset += len(piece)
    return result


def _split_oversized(text: str) -> list[str]:
    parts = SENTENCE_RE.split(text)
    pieces: list[str] = []
    current = ""
    for part in parts:
        candidate = (current + " " + part).strip() if current else part
        if len(candidate) <= MAX_CHUNK_CHARS:
            current = candidate
        else:
            if current:
                pieces.append(current)
            if len(part) <= MAX_CHUNK_CHARS:
                current = part
            else:
                # Sentence-split didn't help — hard-slice.
                for i in range(0, len(part), MAX_CHUNK_CHARS):
                    pieces.append(part[i:i + MAX_CHUNK_CHARS])
                current = ""
    if current:
        pieces.append(current)
    return pieces


# ── Code-aware chunking ───────────────────────────────────────────────────
#
# Prose chunking splits on blank lines and sentence boundaries, which mangles
# source code. chunk_code splits on *definition* boundaries (functions, classes)
# so a search hit returns a coherent unit and the breadcrumb names the symbol.
# It prefers tree-sitter when installed, else falls back to a language-agnostic
# regex/line-window splitter — local mode stays zero-config.

# ext → tree-sitter language name (for the optional tree-sitter path).
_TS_LANG_BY_EXT = {
    "py": "python", "pyi": "python",
    "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
    "ts": "typescript", "tsx": "tsx",
    "go": "go", "rs": "rust", "java": "java",
    "c": "c", "h": "c", "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "hpp": "cpp", "hh": "cpp",
    "rb": "ruby", "php": "php", "cs": "csharp", "swift": "swift",
    "kt": "kotlin", "scala": "scala", "lua": "lua",
}

# tree-sitter node types that name a top-level definition worth a boundary.
_TS_DEF_NODE_TYPES = frozenset({
    "function_definition", "function_declaration", "function_item", "method_definition",
    "class_definition", "class_declaration", "class_item",
    "impl_item", "struct_item", "enum_item", "trait_item", "type_item",
    "interface_declaration", "method_declaration", "constructor_declaration",
    "type_declaration", "enum_declaration", "struct_specifier", "namespace_definition",
    "module", "object_declaration", "trait_declaration",
})

# Fallback: a line that opens a definition in a C-like or scripting language.
_CODE_DEF_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?:export\s+)?(?:default\s+)?"
    r"(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|final\s+|abstract\s+|open\s+|override\s+|pub\s+)*"
    r"(?:async\s+)?"
    r"(?P<kw>def|class|function|func|fn|interface|struct|enum|impl|trait|type|namespace|module|object|record)\b"
    r"[ \t]*(?P<name>[A-Za-z_$][\w$]*)?"
)

CODE_LINE_WINDOW = 80   # lines per chunk when a file has no detectable defs
CODE_LINE_OVERLAP = 10


def chunk_code(content: str, file_type: str | None = None) -> list[Chunk]:
    """Chunk source code on definition boundaries, packing small defs together.

    Unlike chunk_text, this never drops short files below MIN_CHUNK_TOKENS — a
    tiny source file should still be searchable.
    """
    if not content or not content.strip():
        return []

    lines = content.split("\n")
    boundaries, names = _code_boundaries(content, lines, file_type)
    chunks = _chunk_by_boundaries(lines, boundaries, names)
    return _enforce_max_chars(chunks)


def _code_boundaries(content: str, lines: list[str], file_type: str | None):
    """Return (sorted boundary line indices, {line_index: symbol_name})."""
    ts = _treesitter_boundaries(content, file_type)
    if ts is not None:
        return ts

    boundaries: list[int] = []
    names: dict[int, str] = {}
    def_indents = []
    for i, line in enumerate(lines):
        m = _CODE_DEF_RE.match(line)
        if m:
            def_indents.append((i, len(m.group("indent")), m.group("name") or ""))
    if not def_indents:
        return [], {}
    # Only the outermost definitions are chunk boundaries; nested methods stay
    # inside their enclosing class/function.
    base_indent = min(ind for _, ind, _ in def_indents)
    for i, ind, name in def_indents:
        if ind <= base_indent:
            boundaries.append(i)
            names[i] = name
    return boundaries, names


def _treesitter_boundaries(content: str, file_type: str | None):
    """Boundaries from tree-sitter's top-level definition nodes, or None."""
    lang = _TS_LANG_BY_EXT.get((file_type or "").lower())
    if not lang:
        return None
    try:
        from tree_sitter_language_pack import get_parser
    except Exception:
        return None
    try:
        parser = get_parser(lang)
        tree = parser.parse(content.encode("utf-8", errors="replace"))
    except Exception:
        return None

    data = content.encode("utf-8", errors="replace")
    boundaries: list[int] = []
    names: dict[int, str] = {}
    for child in tree.root_node.children:
        if child.type not in _TS_DEF_NODE_TYPES:
            continue
        line_idx = data[: child.start_byte].count(b"\n")
        boundaries.append(line_idx)
        name_node = child.child_by_field_name("name")
        if name_node is not None:
            names[line_idx] = data[name_node.start_byte:name_node.end_byte].decode(
                "utf-8", errors="replace"
            )
    if not boundaries:
        return None
    boundaries = sorted(set(boundaries))
    return boundaries, names


def _chunk_by_boundaries(
    lines: list[str], boundaries: list[int], names: dict[int, str]
) -> list[Chunk]:
    """Build definition units from boundary lines, then pack them into chunks."""
    # Char offset of each line start (content was split on "\n").
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1

    if not boundaries:
        return _line_window_chunks(lines, offsets)

    # Segment starts: a preamble (imports etc.) before the first def, then one
    # segment per top-level definition.
    starts = list(boundaries)
    if starts[0] != 0:
        starts.insert(0, 0)

    units: list[tuple[str, int, str]] = []  # (text, start_char, symbol_name)
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        text = "\n".join(lines[start:end]).strip("\n")
        if not text.strip():
            continue
        units.append((text, offsets[start], names.get(start, "")))

    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_names: list[str] = []
    buf_tokens = 0
    buf_start = 0

    def flush():
        nonlocal buf, buf_names, buf_tokens
        if not buf:
            return
        text = "\n\n".join(buf)
        distinct = {n for n in buf_names if n}
        breadcrumb = next(iter(distinct)) if len(distinct) == 1 else ""
        chunks.append(Chunk(
            index=len(chunks), content=text, page=None,
            start_char=buf_start, token_count=_estimate_tokens(text),
            header_breadcrumb=breadcrumb,
        ))
        buf, buf_names, buf_tokens = [], [], 0

    for text, start_char, name in units:
        t = _estimate_tokens(text)
        if buf and buf_tokens + t > CHUNK_SIZE:
            flush()
        if not buf:
            buf_start = start_char
        buf.append(text)
        buf_names.append(name)
        buf_tokens += t
    flush()
    return chunks


def _line_window_chunks(lines: list[str], offsets: list[int]) -> list[Chunk]:
    """Fixed line-window chunks with overlap, for files with no detectable defs."""
    chunks: list[Chunk] = []
    i = 0
    n = len(lines)
    while i < n:
        window = lines[i:i + CODE_LINE_WINDOW]
        text = "\n".join(window).strip("\n")
        if text.strip():
            chunks.append(Chunk(
                index=len(chunks), content=text, page=None,
                start_char=offsets[i], token_count=_estimate_tokens(text),
                header_breadcrumb="",
            ))
        if i + CODE_LINE_WINDOW >= n:
            break
        i += CODE_LINE_WINDOW - CODE_LINE_OVERLAP
    return chunks


def chunk_pages(page_contents: list[tuple[int, str]]) -> list[Chunk]:
    """Chunk multiple pages, preserving page numbers. Each (page_number, content) tuple."""
    all_chunks: list[Chunk] = []
    for page_num, content in page_contents:
        page_chunks = chunk_text(content, page=page_num)
        for c in page_chunks:
            c.index = len(all_chunks)
            all_chunks.append(c)
    return all_chunks


async def store_chunks(
    pool_or_conn,
    document_id: str,
    user_id: str,
    knowledge_base_id: str,
    chunks: list[Chunk],
):
    if isinstance(pool_or_conn, asyncpg.Connection):
        await _store_chunks_on_conn(pool_or_conn, document_id, user_id, knowledge_base_id, chunks)
    else:
        conn = await pool_or_conn.acquire()
        try:
            await _store_chunks_on_conn(conn, document_id, user_id, knowledge_base_id, chunks)
        finally:
            await pool_or_conn.release(conn)


async def _store_chunks_on_conn(
    conn: asyncpg.Connection,
    document_id: str,
    user_id: str,
    knowledge_base_id: str,
    chunks: list[Chunk],
):
    await conn.execute("DELETE FROM document_chunks WHERE document_id = $1", document_id)

    if not chunks:
        return

    # source_content seeds the immutable raw text; content starts identical
    # but may diverge later when highlight CRUD writes annotations into the
    # chunk via api/services/highlight_chunks.
    await conn.executemany(
        "INSERT INTO document_chunks "
        "(document_id, user_id, knowledge_base_id, chunk_index, content, source_content, page, start_char, token_count, header_breadcrumb) "
        "VALUES ($1, $2, $3, $4, $5, $5, $6, $7, $8, $9)",
        [
            (document_id, user_id, knowledge_base_id, c.index, c.content, c.page, c.start_char, c.token_count, c.header_breadcrumb)
            for c in chunks
        ],
    )
    logger.info("Stored %d chunks for doc %s", len(chunks), document_id[:8])


def _split_paragraphs(text: str) -> list[str]:
    """Split on double newlines, preserving paragraph structure."""
    parts = re.split(r'\n\s*\n', text)
    return [p.strip() for p in parts if p.strip()]


def _get_overlap(blocks: list[str], target_tokens: int) -> tuple[list[str], int]:
    """Get trailing blocks that fit within target_tokens for overlap."""
    result: list[str] = []
    tokens = 0
    for block in reversed(blocks):
        block_tokens = _estimate_tokens(block)
        if tokens + block_tokens > target_tokens:
            break
        result.insert(0, block)
        tokens += block_tokens
    return result, tokens
