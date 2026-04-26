"""Local documentation indexing & search (RAG backend).

`docs_index` avoids colliding with Python's built-in `docs/` naming in
projects while keeping the user-facing concept ("docs") clear.
"""
from dosm.docs_index.indexer import IndexStats, get_index_status, reindex
from dosm.docs_index.routes import router as docs_router
from dosm.docs_index.search import SearchHit, search

__all__ = [
    "IndexStats",
    "SearchHit",
    "docs_router",
    "get_index_status",
    "reindex",
    "search",
]
