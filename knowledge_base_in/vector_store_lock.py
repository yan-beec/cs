"""Coordinate online retrieval with in-process vector-store updates."""

from __future__ import annotations

from threading import RLock


# A re-entrant lock lets the rebuild path protect collection replacement while
# keeping the lock safe for helper functions that may be called from it.
vector_store_lock = RLock()
