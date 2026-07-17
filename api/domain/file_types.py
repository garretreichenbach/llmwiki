"""Shared file-type classification for local mode (watcher, processor, upload)."""

PDF_TYPES = frozenset({"pdf"})
OFFICE_TYPES = frozenset({"pptx", "ppt", "docx", "doc"})
SPREADSHEET_TYPES = frozenset({"xlsx", "xls"})
IMAGE_TYPES = frozenset({"png", "jpg", "jpeg", "webp", "gif"})
HTML_TYPES = frozenset({"html", "htm"})

# Read inline and chunked as plain text — no extraction backend needed.
SIMPLE_TEXT_TYPES = frozenset({
    "md", "txt", "csv", "svg", "json", "xml",
    "yaml", "yml", "toml", "ini", "cfg", "rst", "tex", "latex",
})

# Source-code files. Read inline and chunked like simple text (see
# TEXT_INDEX_TYPES), but tracked separately so code-aware logic — code chunking,
# symbol/import extraction — can key off them.
CODE_TYPES = frozenset({
    "py", "pyi", "js", "jsx", "ts", "tsx", "mjs", "cjs",
    "go", "rs", "java", "kt", "kts", "scala", "sc",
    "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "cs",
    "rb", "php", "swift", "dart", "lua", "r", "pl", "pm",
    "sh", "bash", "zsh", "sql",
    "css", "scss", "sass", "less", "vue", "svelte",
    "ex", "exs", "clj", "cljs", "cljc", "erl", "hs", "ml", "mli",
    "gradle", "groovy", "proto", "graphql", "gql",
})

# Everything read inline and chunked directly, no extraction backend needed.
TEXT_INDEX_TYPES = SIMPLE_TEXT_TYPES | CODE_TYPES

# Need an extraction/processing backend before they're searchable. HTML is here
# (not in TEXT_INDEX_TYPES) because it goes through the webmd parser.
EXTRACTION_TYPES = PDF_TYPES | OFFICE_TYPES | SPREADSHEET_TYPES | HTML_TYPES
