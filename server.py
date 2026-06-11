"""ZetaDB cross-session memory MCP server.

A SQLite-backed accumulator layer below MEMORY.md for any AI instance
working with the human (Code, Desktop Chat, Cowork). Holds durable memories,
to-do lists, journal entries, group chat, work-log durations, an
audit trail, and persona-keyed subscriptions.

(Originally `richard-db`; renamed to ZetaDB on 2026-05-27 — package name
`zeta-db`, prose name ZetaDB. The internal DB file is still
`memories.db`.)

Identity:
  - Each AI conversation should call register_session() once and pass
    the returned session_id to subsequent writes. Provenance is logged in
    a `sessions` table. Writes without a session_id still succeed but are
    marked anonymous.

Provenance fields on memories/tasks:
  - requested_by_human (bool): True when the human explicitly asked for
    the write; False when the AI wrote it on its own initiative.
  - human_remark (text, nullable): a verbatim quote from the human worth
    preserving alongside the record.

Schema flexibility:
  - create_table and add_column are exposed so instances can extend the
    schema during the exploration phase. DDL takes structured args, never
    raw SQL.
  - DROPs of any kind are blocked. Such requests must be filed via
    request_changes for the human to review.
  - Every schema change is logged to schema_history.

Layered retrieval:
  - Every list_*/search_* accepts detail='index'|'summary'|'excerpt'|'full'
    so callers can trade context for depth: index is one ~100-char scan
    line per row, summary is the classic metadata view, excerpt adds the
    first ~280 chars of body, full returns everything inline.
  - get_* returns the full row including body.
  - bulk_load_context packs graduated output by default: top-ranked
    memories at full detail, then excerpts, then index lines.
  - last_accessed bumps on get_*, on all search_* (keyword, semantic,
    hybrid — search is recall), and on bulk_load rows packed at
    full/excerpt. Never on list_* (browsing), at any detail level, so
    pruning passes don't pollute the cruft signal.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(Path(__file__).parent / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


DB_PATH = Path(_env("ZETA_DB_PATH") or (Path(__file__).parent / "memories.db"))
# Summary length: aim for SUMMARY_TARGET (250 chars) for a tight one-liner,
# accept up to SUMMARY_MAX_LEN (400) before rejecting. Docstrings advertise
# the target so AIs aim there; the wider hard cap means a slight overshoot
# doesn't trigger an expensive resubmit. Every add/update return includes
# `summary_length` so the caller can self-calibrate without server help.
SUMMARY_TARGET = int(_env("ZETA_SUMMARY_TARGET", "250") or "250")
SUMMARY_MAX_LEN = int(_env("ZETA_SUMMARY_MAX_LEN", "400") or "400")

# Layered retrieval detail (CR #34 follow-through). Every list_*/search_*
# tool accepts a `detail` level so callers can trade context for depth:
#   index   -> id + nickname + ~INDEX_TRUNC_CHARS of summary. One scan line.
#   summary -> the classic summary view (full summary, metadata, tags).
#   excerpt -> summary view + the first ~EXCERPT_BODY_CHARS of body.
#   full    -> everything, body included (what get_* returns).
DETAIL_LEVELS = ("index", "summary", "excerpt", "full")
INDEX_TRUNC_CHARS = int(_env("ZETA_INDEX_TRUNC_CHARS", "100") or "100")
EXCERPT_BODY_CHARS = int(_env("ZETA_EXCERPT_BODY_CHARS", "280") or "280")

# Embedding backend (CR #16). Default 'none' means no embeddings are
# computed and semantic_search_memories is a no-op. Switch to 'openai'
# to enable embed-on-write. Future backends could be added: 'voyage',
# 'google', 'local' (sentence-transformers).
EMBED_BACKEND = (_env("ZETA_EMBED_BACKEND") or "none").lower()
EMBED_MODEL = _env("ZETA_EMBED_MODEL") or "text-embedding-3-large"
EMBED_DIMS = int(_env("ZETA_EMBED_DIMS") or "1024")
LIST_HARD_LIMIT = int(_env("ZETA_LIST_HARD_LIMIT", "200") or "200")
SEARCH_HARD_LIMIT = int(_env("ZETA_SEARCH_HARD_LIMIT", "100") or "100")

INITIAL_CATEGORIES = [
    "work",
    "side_projects",
    "family",
    "exercise",
    "home_improvements",
    "claude-self",
    "other",
]

TASK_STATUSES = {"open", "in_progress", "blocked", "done", "cancelled"}

# Conventional values for change_requests.request_type. Free-text at the
# DB level, but instances should pick from this set so future AI instances have
# a stable vocabulary to file against. See request_changes() docstring.
REQUEST_TYPES = {
    "schema_change",  # add/drop/rename a column or table
    "bug",            # tool returns wrong result, error, or side effect
    "docstring",      # tool docstring missing/misleading guidance
    "api_design",     # change a tool signature, defaults, or semantics
    "convention",    # cross-cutting practice change (CLAUDE.md territory)
    "other",          # general feedback, escape hatch
}

# Structured column definitions accepted by create_table / add_column.
# Validated against this set so the DDL we generate is predictable.
ALLOWED_COLUMN_TYPES = {"TEXT", "INTEGER", "REAL", "BLOB", "BOOLEAN", "NUMERIC"}

CHANGE_REQUEST_STATUSES = {"open", "approved", "rejected", "done"}

# Subscription target types — what streams a persona can follow.
SUBSCRIPTION_TARGET_TYPES = {
    "chat_channel",      # target_value = channel name (e.g. 'design')
    "chat_tag",          # target_value = tag (e.g. 'for-opus')
    "chat_author",       # target_value = author_nickname (e.g. 'Hermes')
    "memory_category",   # target_value = category name (e.g. 'work')
    "memory_tag",        # target_value = tag
    "memory_origin",     # target_value = origin label (e.g. 'hermes-philosophical')
    "task_category",     # target_value = category name
    "task_tag",          # target_value = tag
    "journal_type",      # target_value = entry_type (supports % for LIKE, e.g. 'exercise:%')
    "journal_tag",       # target_value = tag
}

# Tables the server owns. Instances may create new tables alongside these
# (the exploration sandbox) but must never modify these via the DDL tools.
RESERVED_TABLES = {
    "sessions",
    "categories",
    "tags",
    "memory_tags",
    "task_tags",
    "memories",
    "tasks",
    "schema_history",
    "change_requests",
    "group_chat",
    "group_chat_tags",
    "journal_entries",
    "journal_entry_tags",
    "work_logs",
    "audit_trail",
    "subscriptions",
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

NICKNAME_MAX_LEN = 16
_NICKNAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

mcp = FastMCP("zeta-db")


# --------------------------------------------------------------------------
# Connection / schema
# --------------------------------------------------------------------------


def _now() -> str:
    # Microsecond precision so cursor comparisons (`> last_ping_at`) don't
    # miss multiple writes landing within the same second. Lexicographic
    # ordering still works across mixed-precision values (old rows without
    # microseconds sort before new rows with them — a string without `.%f`
    # is a prefix of any string with `.%f`).
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # CR #27 / #30: politely wait up to 30s for a contended write lock
    # instead of failing immediately with SQLITE_BUSY (and being surfaced
    # by the MCP transport as a 4-minute "server unresponsive" hang).
    # 30s is well above any realistic concurrent-write duration in this
    # workload; if a call ever waits longer than 30s, the resulting
    # OperationalError will be a clearer signal than the silent hang.
    conn.execute("PRAGMA busy_timeout = 30000")
    # CR #16: load sqlite-vec extension for vector similarity functions
    # (vec_distance_cosine etc.). Only attempted when embeddings are
    # enabled — keeps the no-embedding path zero-dependency at runtime.
    if EMBED_BACKEND != "none":
        try:
            conn.enable_load_extension(True)
            import sqlite_vec
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except (ImportError, AttributeError, sqlite3.OperationalError):
            # extension not installed / not loadable on this Python build;
            # semantic search will fail gracefully when called.
            pass
    return conn


def _init_schema() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        cur = conn.cursor()

        # --- Pre-create table renames: must run BEFORE the CREATE TABLE
        # IF NOT EXISTS block below, otherwise the old-named table would
        # be left orphaned beside a freshly-created empty new-named one.
        def _has_table(name: str) -> bool:
            row = cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            return row is not None

        # Migration 2026-05-29: rename claude_chat → group_chat
        # (model-agnostic positioning; the substrate is for any AI
        # instance, not just Claude). SQLite auto-updates FK references
        # in child tables on table rename.
        if _has_table("claude_chat") and not _has_table("group_chat"):
            cur.execute("ALTER TABLE claude_chat RENAME TO group_chat")
        if _has_table("claude_chat_tags") and not _has_table("group_chat_tags"):
            cur.execute("ALTER TABLE claude_chat_tags RENAME TO group_chat_tags")
        # Drop the old-named indexes; new-named indexes are created
        # via CREATE INDEX IF NOT EXISTS in the schema block below.
        for _old_idx in ("idx_claude_chat_channel",
                         "idx_claude_chat_created",
                         "idx_claude_chat_tags_tag"):
            cur.execute(f"DROP INDEX IF EXISTS {_old_idx}")

        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT NOT NULL UNIQUE,
                client        TEXT NOT NULL,
                label         TEXT,
                started_at    TEXT NOT NULL,
                last_seen_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS categories (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS tags (
                name TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS memories (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                summary               TEXT NOT NULL,
                body                  TEXT,
                category_id           INTEGER NOT NULL REFERENCES categories(id),
                importance            INTEGER NOT NULL DEFAULT 3
                                          CHECK (importance BETWEEN 1 AND 5),
                requested_by_human  INTEGER NOT NULL DEFAULT 0
                                          CHECK (requested_by_human IN (0, 1)),
                human_remark       TEXT,
                nickname              TEXT,
                origin                TEXT,
                session_id            INTEGER REFERENCES sessions(id),
                created_at            TEXT NOT NULL,
                updated_at            TEXT NOT NULL,
                last_accessed         TEXT NOT NULL,
                embedding             BLOB  -- CR #16: float32 vector
            );

            CREATE TABLE IF NOT EXISTS memory_tags (
                memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                tag_name  TEXT NOT NULL REFERENCES tags(name) ON DELETE CASCADE,
                PRIMARY KEY (memory_id, tag_name)
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                summary               TEXT NOT NULL,
                body                  TEXT,
                category_id           INTEGER NOT NULL REFERENCES categories(id),
                status                TEXT NOT NULL DEFAULT 'open'
                                          CHECK (status IN ('open', 'in_progress', 'blocked', 'done', 'cancelled')),
                importance            INTEGER NOT NULL DEFAULT 3
                                          CHECK (importance BETWEEN 1 AND 5),
                due_date              TEXT,
                requested_by_human  INTEGER NOT NULL DEFAULT 0
                                          CHECK (requested_by_human IN (0, 1)),
                human_remark       TEXT,
                nickname              TEXT,
                session_id            INTEGER REFERENCES sessions(id),
                created_at            TEXT NOT NULL,
                updated_at            TEXT NOT NULL,
                completed_at          TEXT
            );

            CREATE TABLE IF NOT EXISTS task_tags (
                task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                tag_name TEXT NOT NULL REFERENCES tags(name) ON DELETE CASCADE,
                PRIMARY KEY (task_id, tag_name)
            );

            CREATE TABLE IF NOT EXISTS schema_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                operation   TEXT NOT NULL,
                target      TEXT NOT NULL,
                details     TEXT NOT NULL,
                session_id  INTEGER REFERENCES sessions(id),
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS change_requests (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                request_type    TEXT NOT NULL,
                target          TEXT NOT NULL,
                description     TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'open'
                                    CHECK (status IN ('open', 'approved', 'rejected', 'done')),
                session_id      INTEGER REFERENCES sessions(id),
                created_at      TEXT NOT NULL,
                resolved_at     TEXT,
                resolution_note TEXT
            );

            CREATE TABLE IF NOT EXISTS group_chat (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                channel          TEXT NOT NULL DEFAULT 'general',
                author_nickname  TEXT,
                body             TEXT NOT NULL,
                session_id       INTEGER REFERENCES sessions(id),
                created_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS group_chat_tags (
                chat_id  INTEGER NOT NULL REFERENCES group_chat(id) ON DELETE CASCADE,
                tag_name TEXT NOT NULL REFERENCES tags(name) ON DELETE CASCADE,
                PRIMARY KEY (chat_id, tag_name)
            );

            CREATE TABLE IF NOT EXISTS journal_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_type  TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                notes       TEXT,
                metrics     TEXT,   -- JSON blob for type-specific extras
                session_id  INTEGER REFERENCES sessions(id),
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS journal_entry_tags (
                entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
                tag_name TEXT NOT NULL REFERENCES tags(name) ON DELETE CASCADE,
                PRIMARY KEY (entry_id, tag_name)
            );

            CREATE TABLE IF NOT EXISTS work_logs (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                description        TEXT NOT NULL,
                estimated_seconds  INTEGER,
                started_at         TEXT NOT NULL,
                completed_at       TEXT,
                actual_seconds     INTEGER,
                task_id            INTEGER REFERENCES tasks(id),
                session_id         INTEGER REFERENCES sessions(id),
                notes              TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_trail (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type   TEXT NOT NULL
                                  CHECK (entity_type IN ('memory', 'task', 'journal', 'chat')),
                entity_id     INTEGER NOT NULL,
                operation     TEXT NOT NULL
                                  CHECK (operation IN ('create', 'update', 'delete')),
                field_changed TEXT,
                old_value     TEXT,
                new_value     TEXT,
                session_id    INTEGER REFERENCES sessions(id),
                created_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                persona         TEXT NOT NULL,
                target_type     TEXT NOT NULL,
                target_value    TEXT,
                last_ping_at    TEXT,
                notes           TEXT,
                created_at      TEXT NOT NULL,
                UNIQUE(persona, target_type, target_value)
            );

            CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category_id);
            CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag_name);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks(category_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date);
            CREATE INDEX IF NOT EXISTS idx_task_tags_tag ON task_tags(tag_name);
            CREATE INDEX IF NOT EXISTS idx_change_requests_status ON change_requests(status);
            CREATE INDEX IF NOT EXISTS idx_group_chat_channel
                ON group_chat(channel, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_group_chat_created
                ON group_chat(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_group_chat_tags_tag
                ON group_chat_tags(tag_name);
            CREATE INDEX IF NOT EXISTS idx_journal_entries_type
                ON journal_entries(entry_type);
            CREATE INDEX IF NOT EXISTS idx_journal_entries_timestamp
                ON journal_entries(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_journal_entry_tags_tag
                ON journal_entry_tags(tag_name);
            CREATE INDEX IF NOT EXISTS idx_work_logs_started
                ON work_logs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_work_logs_session
                ON work_logs(session_id);
            CREATE INDEX IF NOT EXISTS idx_work_logs_task
                ON work_logs(task_id);
            CREATE INDEX IF NOT EXISTS idx_audit_trail_entity
                ON audit_trail(entity_type, entity_id);
            CREATE INDEX IF NOT EXISTS idx_audit_trail_session
                ON audit_trail(session_id);
            CREATE INDEX IF NOT EXISTS idx_audit_trail_created
                ON audit_trail(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_persona
                ON subscriptions(persona);
            """
        )

        # --- Idempotent migrations for existing DBs created before a field
        # was added. Add new ones below as the schema grows. ---

        def _has_column(table: str, column: str) -> bool:
            rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
            return any(r[1] == column for r in rows)

        # Migration 2026-05-21: nickname column on tasks and memories.
        if not _has_column("tasks", "nickname"):
            cur.execute("ALTER TABLE tasks ADD COLUMN nickname TEXT")
        if not _has_column("memories", "nickname"):
            cur.execute("ALTER TABLE memories ADD COLUMN nickname TEXT")

        # Migration 2026-05-22: origin column on memories (CR #14).
        if not _has_column("memories", "origin"):
            cur.execute("ALTER TABLE memories ADD COLUMN origin TEXT")

        # Migration 2026-06-05: embedding column on memories (CR #16).
        # Stored as raw float32 BLOB so sqlite-vec functions can operate
        # on it directly. NULL means "not yet embedded" — backfill via
        # backfill_embeddings().
        if not _has_column("memories", "embedding"):
            cur.execute("ALTER TABLE memories ADD COLUMN embedding BLOB")

        # Migration 2026-05-28: rename the project-maintainer-specific
        # field names to generic ones describing the human collaborator.
        #   requested_by_richard → requested_by_human
        #   richards_remark      → human_remark
        # Idempotent: only renames if the old column still exists.
        for _table in ("memories", "tasks"):
            if _has_column(_table, "requested_by_richard"):
                cur.execute(
                    f"ALTER TABLE {_table} "
                    "RENAME COLUMN requested_by_richard TO requested_by_human"
                )
            if _has_column(_table, "richards_remark"):
                cur.execute(
                    f"ALTER TABLE {_table} "
                    "RENAME COLUMN richards_remark TO human_remark"
                )
        # Audit-trail field_changed labels reference the old column names
        # on rows recorded before this migration. Rewrite the labels so
        # `get_audit_trail` queries return a consistent history under the
        # new field names. (The old/new JSON snapshots are point-in-time
        # records and stay as-they-were.)
        cur.execute(
            "UPDATE audit_trail SET field_changed = 'requested_by_human' "
            "WHERE field_changed = 'requested_by_richard'"
        )
        cur.execute(
            "UPDATE audit_trail SET field_changed = 'human_remark' "
            "WHERE field_changed = 'richards_remark'"
        )

        # Migration 2026-06-01: widen tasks.status enum to include
        # 'in_progress' (CR #29). SQLite has no ALTER TABLE ALTER CHECK,
        # so the only way to update the CHECK constraint is to rewrite
        # the table. Detect by looking at the stored CREATE TABLE text
        # in sqlite_master; if 'in_progress' isn't in it, do the rewrite.
        tasks_def = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if tasks_def and "in_progress" not in (tasks_def[0] or ""):
            # FKs off for the dance: task_tags has FK to tasks(id) and
            # we don't want CASCADE-deletes triggered by DROP TABLE.
            cur.execute("PRAGMA foreign_keys = OFF")
            try:
                cur.execute(
                    """
                    CREATE TABLE tasks_new (
                        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                        summary               TEXT NOT NULL,
                        body                  TEXT,
                        category_id           INTEGER NOT NULL REFERENCES categories(id),
                        status                TEXT NOT NULL DEFAULT 'open'
                                                  CHECK (status IN ('open', 'in_progress', 'blocked', 'done', 'cancelled')),
                        importance            INTEGER NOT NULL DEFAULT 3
                                                  CHECK (importance BETWEEN 1 AND 5),
                        due_date              TEXT,
                        requested_by_human    INTEGER NOT NULL DEFAULT 0
                                                  CHECK (requested_by_human IN (0, 1)),
                        human_remark          TEXT,
                        nickname              TEXT,
                        session_id            INTEGER REFERENCES sessions(id),
                        created_at            TEXT NOT NULL,
                        updated_at            TEXT NOT NULL,
                        completed_at          TEXT
                    )
                    """
                )
                cur.execute(
                    "INSERT INTO tasks_new "
                    "(id, summary, body, category_id, status, importance, due_date, "
                    "requested_by_human, human_remark, nickname, session_id, "
                    "created_at, updated_at, completed_at) "
                    "SELECT id, summary, body, category_id, status, importance, due_date, "
                    "requested_by_human, human_remark, nickname, session_id, "
                    "created_at, updated_at, completed_at FROM tasks"
                )
                cur.execute("DROP TABLE tasks")
                cur.execute("ALTER TABLE tasks_new RENAME TO tasks")
                # Indexes attached to the old table were dropped with it.
                # The CREATE INDEX IF NOT EXISTS calls below recreate them
                # with the current (post-rename) definitions.
            finally:
                cur.execute("PRAGMA foreign_keys = ON")

        # Belt-and-braces: if the table didn't need a rewrite but the
        # partial-unique index still has the old WHERE clause (no
        # in_progress), drop it so the CREATE INDEX IF NOT EXISTS below
        # rebuilds it with the new clause.
        idx_def = cur.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_tasks_nickname_active'"
        ).fetchone()
        if idx_def and "in_progress" not in (idx_def[0] or ""):
            cur.execute("DROP INDEX idx_tasks_nickname_active")

        # Partial unique index: a nickname can only be reused once a task
        # has left an "active" status (open/in_progress/blocked). Completed
        # and cancelled tasks free up their nickname for future reuse.
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_nickname_active
            ON tasks(nickname)
            WHERE nickname IS NOT NULL AND status IN ('open', 'in_progress', 'blocked')
            """
        )
        # Memories have no uniqueness constraint on nickname — they're a
        # much bigger pool and nicknames there are decorative, not
        # load-bearing for lookup.

        # Seed the initial categories (idempotent).
        for cat in INITIAL_CATEGORIES:
            cur.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (cat,))
    finally:
        conn.close()


_init_schema()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _is_safe_ident(name: str) -> bool:
    return bool(_IDENT_RE.match(name))


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _resolve_session(conn: sqlite3.Connection, session_id: str | None) -> int | None:
    """Look up the sessions.id for a given external session_id string.

    Bumps last_seen_at on hit. Returns None on miss (treated as anonymous).
    """
    if not session_id:
        return None
    row = conn.execute(
        "SELECT id FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE sessions SET last_seen_at = ? WHERE id = ?", (_now(), row["id"])
    )
    return row["id"]


def _resolve_category(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    return row["id"] if row else None


def _category_name(conn: sqlite3.Connection, category_id: int) -> str | None:
    row = conn.execute(
        "SELECT name FROM categories WHERE id = ?", (category_id,)
    ).fetchone()
    return row["name"] if row else None


def _set_tags(
    conn: sqlite3.Connection, join_table: str, fk_column: str, fk_value: int,
    tags: list[str],
) -> None:
    conn.execute(f"DELETE FROM {join_table} WHERE {fk_column} = ?", (fk_value,))
    for raw in tags:
        tag = raw.strip().lower()
        if not tag:
            continue
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
        conn.execute(
            f"INSERT OR IGNORE INTO {join_table} ({fk_column}, tag_name) VALUES (?, ?)",
            (fk_value, tag),
        )


def _get_tags(
    conn: sqlite3.Connection, join_table: str, fk_column: str, fk_value: int
) -> list[str]:
    rows = conn.execute(
        f"SELECT tag_name FROM {join_table} WHERE {fk_column} = ? ORDER BY tag_name",
        (fk_value,),
    ).fetchall()
    return [r["tag_name"] for r in rows]


def _truncate(text: str | None, limit: int) -> str | None:
    """Hard-truncate to `limit` chars, ellipsis included, word-boundary
    preferred. Returns text unchanged when it already fits."""
    if text is None or len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # Back up to the last space if one falls in the final quarter — a
    # mid-word chop reads worse than a slightly shorter line.
    space = cut.rfind(" ")
    if space > limit * 3 // 4:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _validate_detail(detail: str, allowed: tuple[str, ...] = DETAIL_LEVELS) -> str | None:
    if detail not in allowed:
        return f"detail must be one of {list(allowed)}"
    return None


def _tag_filter(
    join_table: str, fk_column: str, owner_alias: str,
    tags: list[str] | None, tag_mode: str,
) -> tuple[str | None, list[Any]]:
    """Build the tag-filter WHERE fragment shared by every list_*/search_*.

    tag_mode 'any' (default): row matches if it carries at least one of the
    tags. 'all': row must carry every tag (AND-match).
    Returns (sql_fragment | None, params).
    """
    clean = [t.strip().lower() for t in (tags or []) if t.strip()]
    if not clean:
        return None, []
    ph = ",".join("?" * len(clean))
    if tag_mode == "all":
        return (
            f"{owner_alias}.id IN (SELECT {fk_column} FROM {join_table} "
            f"WHERE tag_name IN ({ph}) GROUP BY {fk_column} "
            f"HAVING COUNT(DISTINCT tag_name) = ?)",
            [*clean, len(clean)],
        )
    return (
        f"{owner_alias}.id IN (SELECT {fk_column} FROM {join_table} "
        f"WHERE tag_name IN ({ph}))",
        list(clean),
    )


def _hydrate_memory(
    conn: sqlite3.Connection, row: sqlite3.Row, *, detail: str = "summary"
) -> dict[str, Any]:
    d = dict(row)
    if detail == "index":
        # Tightest layer: one scan line per row, null fields dropped.
        out = {"id": d["id"], "summary": _truncate(d["summary"], INDEX_TRUNC_CHARS)}
        if d.get("nickname"):
            out["nickname"] = d["nickname"]
        out["category"] = _category_name(conn, d["category_id"])
        out["importance"] = d["importance"]
        return out
    d["category"] = _category_name(conn, d.pop("category_id"))
    d["requested_by_human"] = bool(d["requested_by_human"])
    d["tags"] = _get_tags(conn, "memory_tags", "memory_id", d["id"])
    # The embedding BLOB is ~4 KB and useless to API callers — drop it
    # but surface a presence indicator so the caller can tell whether
    # the row has been embedded.
    embed = d.pop("embedding", None)
    d["has_embedding"] = embed is not None
    # nickname and origin stay in all views — both are scan-time identifiers.
    if detail in ("summary", "excerpt"):
        body = d.pop("body", None)
        d.pop("human_remark", None)
        d.pop("session_id", None)
        d.pop("created_at", None)
        d.pop("last_accessed", None)
        if detail == "excerpt" and body:
            d["body_excerpt"] = _truncate(body, EXCERPT_BODY_CHARS)
            d["body_chars"] = len(body)
    return d


def _hydrate_chat(
    conn: sqlite3.Connection, row: sqlite3.Row, *, detail: str = "full"
) -> dict[str, Any]:
    d = dict(row)
    if detail == "index":
        return {
            "id": d["id"],
            "channel": d["channel"],
            "author_nickname": d["author_nickname"],
            "body": _truncate(d["body"], INDEX_TRUNC_CHARS),
            "created_at": d["created_at"],
        }
    if detail == "excerpt":
        body = d["body"]
        d["body"] = _truncate(body, EXCERPT_BODY_CHARS)
        if body and len(body) > EXCERPT_BODY_CHARS:
            d["body_chars"] = len(body)
    d["tags"] = _get_tags(conn, "group_chat_tags", "chat_id", d["id"])
    return d


def _hydrate_journal(
    conn: sqlite3.Connection, row: sqlite3.Row, *, detail: str = "summary"
) -> dict[str, Any]:
    d = dict(row)
    if detail == "index":
        return {"id": d["id"], "entry_type": d["entry_type"], "timestamp": d["timestamp"]}
    d["tags"] = _get_tags(conn, "journal_entry_tags", "entry_id", d["id"])
    # metrics is stored as JSON text; surface as a parsed dict if present.
    if d.get("metrics"):
        try:
            d["metrics"] = json.loads(d["metrics"])
        except (TypeError, json.JSONDecodeError):
            pass  # leave raw if malformed
    if detail in ("summary", "excerpt"):
        notes = d.pop("notes", None)
        metrics = d.pop("metrics", None)
        d.pop("session_id", None)
        d.pop("created_at", None)
        if detail == "excerpt":
            if notes:
                d["notes_excerpt"] = _truncate(notes, EXCERPT_BODY_CHARS)
                d["notes_chars"] = len(notes)
            if metrics:
                d["metrics"] = metrics
    return d


def _hydrate_task(
    conn: sqlite3.Connection, row: sqlite3.Row, *, detail: str = "summary"
) -> dict[str, Any]:
    d = dict(row)
    if detail == "index":
        out = {
            "id": d["id"],
            "summary": _truncate(d["summary"], INDEX_TRUNC_CHARS),
            "status": d["status"],
        }
        if d.get("nickname"):
            out["nickname"] = d["nickname"]
        out["category"] = _category_name(conn, d["category_id"])
        if d.get("due_date"):
            out["due_date"] = d["due_date"]
        return out
    d["category"] = _category_name(conn, d.pop("category_id"))
    d["requested_by_human"] = bool(d["requested_by_human"])
    d["tags"] = _get_tags(conn, "task_tags", "task_id", d["id"])
    # nickname stays in all views — it's the point.
    if detail in ("summary", "excerpt"):
        body = d.pop("body", None)
        d.pop("human_remark", None)
        d.pop("session_id", None)
        d.pop("created_at", None)
        if detail == "excerpt" and body:
            d["body_excerpt"] = _truncate(body, EXCERPT_BODY_CHARS)
            d["body_chars"] = len(body)
    return d


def _bump_memory_accessed(conn: sqlite3.Connection, ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE memories SET last_accessed = ? WHERE id IN ({placeholders})",
        (_now(), *ids),
    )


def _validate_summary(summary: str) -> str | None:
    """Strict summary validator — used on the UPDATE paths, where a reject
    is cheap (no body re-transmission). The ADD paths use _prepare_summary
    instead (CR #33: truncate-and-warn)."""
    s = summary.strip() if summary else ""
    if not s:
        return "summary is required"
    if len(s) > SUMMARY_MAX_LEN:
        # Include the measured length so the caller can trim once, precisely
        # (CR #22 — was previously a guessing loop). Hard cap is the wider
        # SUMMARY_MAX_LEN; the SUMMARY_TARGET is the polite ask.
        return (
            f"summary too long ({len(s)} chars > hard cap {SUMMARY_MAX_LEN}; "
            f"aim for <= {SUMMARY_TARGET})"
        )
    return None


def _prepare_summary(summary: str, update_tool: str) -> tuple[str, dict[str, Any] | None, str | None]:
    """Lenient summary intake for the ADD paths (CR #33).

    An over-length summary must never reject a call that carries a correct
    body — the body is the expensive, already-final field; the summary is a
    lossy index field. Over the hard cap we store a truncated summary, keep
    the body intact, and tell the caller how to fix just the summary.

    Returns (stored_summary, truncation_info | None, error | None).
    """
    s = summary.strip() if summary else ""
    if not s:
        return "", None, "summary is required"
    if len(s) > SUMMARY_MAX_LEN:
        stored = _truncate(s, SUMMARY_MAX_LEN)
        info = {
            "summary_truncated": True,
            "original_summary_length": len(s),
            "stored_summary_length": len(stored),
            "note": (
                f"summary exceeded the {SUMMARY_MAX_LEN}-char hard cap and was "
                f"truncated; the body was stored intact. To fix the summary, "
                f"call {update_tool}(id, summary=...) — no need to resend the body."
            ),
        }
        return stored, info, None
    return s, None, None


def _validate_importance(importance: int) -> str | None:
    if not isinstance(importance, int) or importance < 1 or importance > 5:
        return "importance must be an integer 1-5"
    return None


# --------------------------------------------------------------------------
# Tag-leak salvage (CR #11 server-side mitigation)
#
# When the XML-style tool-call serialiser fails to close a long-text
# parameter cleanly, the following <parameter name="X">value</parameter>
# elements get absorbed verbatim into the end of that long-text param
# (typically body / description / notes). The caller's actual intent
# (set tags, set session_id, etc.) is then silently lost.
#
# This helper detects the trailing <parameter ...> run and salvages the
# values. Conservative: only acts on contiguous trailing matches that
# reach (within whitespace) the end of the string, so mid-body
# discussion of the tool-calling syntax isn't accidentally stripped.
# --------------------------------------------------------------------------

_LEAK_OPEN_RE = re.compile(r'<parameter\s+name="([^"]+)">', re.DOTALL)
_LEAK_VALUE_END_RE = re.compile(r'\s*</parameter>\s*$', re.DOTALL)


def _parse_salvaged_value(raw: str) -> Any:
    """Best-effort: parse JSON if it looks like JSON, fall back to string."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return s


def _validate_salvaged_dict(d: dict[str, Any]) -> bool:
    """Per-key type check on salvaged values. Return False (reject the whole
    salvage) if any value doesn't look like a genuine kwarg — the alternative
    is silently stripping body content from a doc-style mention of the
    tool-calling syntax.
    """
    prose_re = re.compile(r"[.!?]\s+[A-Z]")
    for k, v in d.items():
        if k == "tags":
            if not (isinstance(v, list) and all(isinstance(t, str) for t in v)):
                return False
        elif k == "importance":
            if not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5):
                return False
        elif k == "requested_by_human":
            if not isinstance(v, bool):
                return False
        elif k == "nickname":
            if not (isinstance(v, str) and _NICKNAME_RE.match(v)):
                return False
        elif k == "session_id":
            if not (isinstance(v, str) and re.match(r"^[a-f0-9]{8,32}$", v)):
                return False
        elif k in ("human_remark", "origin", "category"):
            if not isinstance(v, str):
                return False
            # Reject anything that smells like prose — multi-sentence text
            # is a strong signal we caught mid-body documentation, not a
            # genuine kwarg value.
            if len(v) > 200 or len(prose_re.findall(v)) >= 2:
                return False
        # Unknown keys: accept silently (forward-compat with future kwargs).
    return True


def _salvage_leaked_params(text: str | None) -> tuple[str | None, dict[str, Any]]:
    """Detect trailing <parameter name="X">value</?> fragments absorbed into
    a long-text param and return (cleaned_text, {name: parsed_value}).

    Returns ``(text, {})`` unchanged if no leak detected. Only salvages a
    contiguous run of leaked params at the very end of the text — a single
    mid-body mention of `<parameter` is left untouched.
    """
    if not text or '<parameter' not in text:
        return text, {}

    matches = list(_LEAK_OPEN_RE.finditer(text))
    if not matches:
        return text, {}

    # Only consider this a leak if the last <parameter ...> opening is at
    # (or near) the end of the text — within trailing-whitespace tolerance.
    # If there's significant non-XML body after the last open tag, this is
    # mid-body documentation, not a leak. (We accept </parameter> + whitespace
    # as "near the end".)
    tail_after_last_open = text[matches[-1].end():]
    tail_stripped = _LEAK_VALUE_END_RE.sub("", tail_after_last_open).strip()
    # If what remains after the last open tag still contains another
    # <parameter or any non-trivial content beyond the value itself, bail.
    # The value can be arbitrary, so we don't validate its content — we
    # just require no further markup after it.
    if '<parameter' in tail_stripped or '</parameter>' in tail_stripped:
        return text, {}

    # Walk backward through the matches collecting the contiguous leaked
    # run. A pair of adjacent matches is "contiguous" when the gap between
    # them (the previous value plus any close tag) has no body content
    # beyond the value text itself — which is, by definition, the previous
    # leak's value. So contiguity is automatic: every <parameter ...>
    # match starts a new leak segment.
    extracted: dict[str, Any] = {}
    leak_start = matches[-1].start()
    for i, m in enumerate(matches):
        # Value runs from this match.end() to the next match.start(), or
        # to end-of-text if this is the last.
        if i + 1 < len(matches):
            value_end = matches[i + 1].start()
        else:
            value_end = len(text)
        raw_value = text[m.end():value_end]
        # Strip trailing </parameter> + whitespace if present.
        raw_value = _LEAK_VALUE_END_RE.sub("", raw_value)
        extracted[m.group(1)] = _parse_salvaged_value(raw_value)
        leak_start = min(leak_start, m.start())

    # Final guard: every extracted value must validate as a genuine kwarg.
    # If any fails (typical case: caught a mid-body documentation mention
    # rather than a real trailing leak), bail the whole salvage so the
    # caller's body is preserved untouched.
    if not _validate_salvaged_dict(extracted):
        return text, {}

    # Cleaned text = everything up to the leak start (rstripped).
    cleaned = text[:leak_start].rstrip() or None
    return cleaned, extracted


# --------------------------------------------------------------------------
# Embedding backend (CR #16)
#
# Configured via ZETA_EMBED_BACKEND in .env. Default 'none' makes the
# embed/semantic-search surface a no-op so the server runs without any
# API key. Set to 'openai' to enable embed-on-write using
# text-embedding-3-large at 1024 dimensions (Matryoshka-truncated for
# storage + ANN-search efficiency).
#
# Vectors are stored as raw float32 BLOBs in memories.embedding so
# sqlite-vec's vec_distance_cosine() can operate on them directly.
# --------------------------------------------------------------------------

_openai_client = None


def _get_openai_client():
    """Lazy-construct an OpenAI client. Reads the API key from
    OPENAI_API_KEY first, then OPENAI_API_KEY_SIDE_PROJECTS_BRENT
    (the maintainer's namespaced env var).
    """
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    try:
        import openai
    except ImportError as e:
        raise RuntimeError(
            "openai package not installed — pip install openai"
        ) from e
    api_key = (
        _env("OPENAI_API_KEY")
        or _env("OPENAI_API_KEY_SIDE_PROJECTS_BRENT")
    )
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY not set in .env (or OPENAI_API_KEY_SIDE_PROJECTS_BRENT)"
        )
    _openai_client = openai.OpenAI(api_key=api_key)
    return _openai_client


def _vec_to_blob(vec: list[float]) -> bytes:
    """Pack a list of floats as a little-endian float32 BLOB for sqlite-vec."""
    import struct
    return struct.pack(f"<{len(vec)}f", *vec)


def _embed_text(text: str | None) -> bytes | None:
    """Embed text to a BLOB vector via the configured backend.

    Returns None if backend='none' or text is empty/whitespace-only.
    Raises RuntimeError for configuration errors (no API key, unknown
    backend) so callers can decide whether to surface or swallow.
    """
    if EMBED_BACKEND == "none":
        return None
    if not text or not text.strip():
        return None
    if EMBED_BACKEND == "openai":
        client = _get_openai_client()
        resp = client.embeddings.create(
            model=EMBED_MODEL,
            input=text.strip(),
            dimensions=EMBED_DIMS,
        )
        return _vec_to_blob(resp.data[0].embedding)
    raise RuntimeError(
        f"unknown ZETA_EMBED_BACKEND='{EMBED_BACKEND}'; "
        "supported: none, openai"
    )


def _memory_embed_text(summary: str | None, body: str | None) -> str:
    """The text we send to the embedding model for a memory: summary +
    body joined. Summary alone is too thin for good retrieval; body
    alone misses the headline.
    """
    parts = []
    if summary and summary.strip():
        parts.append(summary.strip())
    if body and body.strip():
        parts.append(body.strip())
    return "\n\n".join(parts)


def _try_embed_memory(summary: str | None, body: str | None) -> bytes | None:
    """Best-effort embedding for a memory write. Returns None on any
    failure (backend='none', no API key, network blip) — the memory
    write itself should never fail because the embedding step failed.
    Backfill via backfill_embeddings() can fill in NULL rows later.
    """
    try:
        return _embed_text(_memory_embed_text(summary, body))
    except Exception:
        return None


def _clean_nickname(nick: str | None) -> tuple[str | None, str | None]:
    """Normalise a nickname argument.

    Returns (cleaned_value, error). The cleaned value is:
      - None if `nick` was None or empty/whitespace (no nickname).
      - The stripped string if it passes validation.
    Pass empty string (``""``) to update tools to explicitly *clear* an
    existing nickname; pass None to leave it untouched.

    Allowed chars: [A-Za-z0-9_-]. Max length 16.
    """
    if nick is None:
        return None, None
    stripped = nick.strip()
    if not stripped:
        return None, None
    if len(stripped) > NICKNAME_MAX_LEN:
        return None, f"nickname too long (>{NICKNAME_MAX_LEN} chars)"
    if not _NICKNAME_RE.match(stripped):
        return None, "nickname must match [A-Za-z0-9_-]+"
    return stripped, None


def _auto_subscribe_for_persona(
    conn: sqlite3.Connection, persona: str
) -> None:
    """Ensure persona is subscribed to chat_tag='for-<persona>'.

    Called from add_chat when a non-null author_nickname is used.
    Cheap idempotent INSERT OR IGNORE.
    """
    p = (persona or "").strip()
    if not p:
        return
    tag = f"for-{p.lower()}"
    conn.execute(
        """
        INSERT OR IGNORE INTO subscriptions
            (persona, target_type, target_value, last_ping_at, created_at, notes)
        VALUES (?, 'chat_tag', ?, NULL, ?, 'auto-subscribed on first chat post')
        """,
        (p, tag, _now()),
    )


def _query_subscription_target(
    conn: sqlite3.Connection,
    persona: str,
    target_type: str,
    target_value: str | None,
    since: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Return summary-view rows for one subscription's stream since the cursor.

    `since` is the last_ping_at cutoff. If None, returns the most-recent
    `limit` items unconditionally (first-ping behaviour — bounds the
    blast radius to a sane number).
    """
    items: list[dict[str, Any]] = []

    def _chat_query(extra_where: str, extra_params: list[Any]) -> list[Any]:
        wheres = [extra_where]
        params = list(extra_params)
        # Exclude one's own posts on persona-targeted streams (channels, tags).
        # author_nickname-followed streams INCLUDE the author's posts — that's
        # the point of following them.
        if target_type in {"chat_channel", "chat_tag"}:
            wheres.append("(c.author_nickname IS NULL OR c.author_nickname != ?)")
            params.append(persona)
        if since is not None:
            wheres.append("c.created_at > ?")
            params.append(since)
        where_sql = "WHERE " + " AND ".join(wheres)
        params.append(limit)
        return list(conn.execute(
            f"""
            SELECT c.id, c.channel, c.author_nickname, c.body, c.created_at
            FROM group_chat c
            {where_sql}
            ORDER BY c.created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall())

    def _memory_query(extra_where: str, extra_params: list[Any]) -> list[Any]:
        wheres = [extra_where]
        params = list(extra_params)
        if since is not None:
            wheres.append("m.updated_at > ?")
            params.append(since)
        where_sql = "WHERE " + " AND ".join(wheres)
        params.append(limit)
        return list(conn.execute(
            f"""
            SELECT m.id, m.summary, m.nickname, m.origin, m.updated_at
            FROM memories m
            {where_sql}
            ORDER BY m.updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall())

    def _task_query(extra_where: str, extra_params: list[Any]) -> list[Any]:
        wheres = [extra_where]
        params = list(extra_params)
        if since is not None:
            wheres.append("t.updated_at > ?")
            params.append(since)
        where_sql = "WHERE " + " AND ".join(wheres)
        params.append(limit)
        return list(conn.execute(
            f"""
            SELECT t.id, t.summary, t.nickname, t.status, t.updated_at
            FROM tasks t
            {where_sql}
            ORDER BY t.updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall())

    def _journal_query(extra_where: str, extra_params: list[Any]) -> list[Any]:
        wheres = [extra_where]
        params = list(extra_params)
        if since is not None:
            wheres.append("e.timestamp > ?")
            params.append(since)
        where_sql = "WHERE " + " AND ".join(wheres)
        params.append(limit)
        return list(conn.execute(
            f"""
            SELECT e.id, e.entry_type, e.timestamp, e.notes
            FROM journal_entries e
            {where_sql}
            ORDER BY e.timestamp DESC
            LIMIT ?
            """,
            params,
        ).fetchall())

    rows: list[Any] = []
    if target_type == "chat_channel":
        rows = _chat_query("c.channel = ?", [target_value])
        items = [{"kind": "chat", **dict(r)} for r in rows]
    elif target_type == "chat_tag":
        rows = _chat_query(
            "c.id IN (SELECT chat_id FROM group_chat_tags WHERE tag_name = ?)",
            [target_value],
        )
        items = [{"kind": "chat", **dict(r)} for r in rows]
    elif target_type == "chat_author":
        rows = _chat_query("c.author_nickname = ?", [target_value])
        items = [{"kind": "chat", **dict(r)} for r in rows]
    elif target_type == "memory_category":
        cat_id = _resolve_category(conn, (target_value or "").lower())
        if cat_id is None:
            return []
        rows = _memory_query("m.category_id = ?", [cat_id])
        items = [{"kind": "memory", **dict(r)} for r in rows]
    elif target_type == "memory_tag":
        rows = _memory_query(
            "m.id IN (SELECT memory_id FROM memory_tags WHERE tag_name = ?)",
            [target_value],
        )
        items = [{"kind": "memory", **dict(r)} for r in rows]
    elif target_type == "memory_origin":
        rows = _memory_query("m.origin = ?", [target_value])
        items = [{"kind": "memory", **dict(r)} for r in rows]
    elif target_type == "task_category":
        cat_id = _resolve_category(conn, (target_value or "").lower())
        if cat_id is None:
            return []
        rows = _task_query("t.category_id = ?", [cat_id])
        items = [{"kind": "task", **dict(r)} for r in rows]
    elif target_type == "task_tag":
        rows = _task_query(
            "t.id IN (SELECT task_id FROM task_tags WHERE tag_name = ?)",
            [target_value],
        )
        items = [{"kind": "task", **dict(r)} for r in rows]
    elif target_type == "journal_type":
        if target_value and "%" in target_value:
            rows = _journal_query("e.entry_type LIKE ?", [target_value])
        else:
            rows = _journal_query("e.entry_type = ?", [target_value])
        items = [{"kind": "journal", **dict(r)} for r in rows]
    elif target_type == "journal_tag":
        rows = _journal_query(
            "e.id IN (SELECT entry_id FROM journal_entry_tags WHERE tag_name = ?)",
            [target_value],
        )
        items = [{"kind": "journal", **dict(r)} for r in rows]

    return items


def _audit(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: int,
    operation: str,
    session_pk: int | None,
    field_changed: str | None = None,
    old_value: Any = None,
    new_value: Any = None,
) -> None:
    """Write one audit_trail row. Values stringified/JSON-serialised consistently."""
    def _serialise(v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, (int, float, str)):
            return str(v)
        return json.dumps(v, default=str, sort_keys=True)

    conn.execute(
        """
        INSERT INTO audit_trail
            (entity_type, entity_id, operation, field_changed,
             old_value, new_value, session_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (entity_type, entity_id, operation, field_changed,
         _serialise(old_value), _serialise(new_value), session_pk, _now()),
    )


def _audit_create(
    conn: sqlite3.Connection, entity_type: str, entity_id: int,
    snapshot: dict[str, Any], session_pk: int | None,
) -> None:
    """Single audit row for create — full snapshot stored as new_value JSON."""
    _audit(conn, entity_type, entity_id, "create", session_pk,
           field_changed=None, old_value=None, new_value=snapshot)


def _audit_delete(
    conn: sqlite3.Connection, entity_type: str, entity_id: int,
    snapshot: dict[str, Any], session_pk: int | None,
) -> None:
    """Single audit row for delete — full snapshot stored as old_value JSON."""
    _audit(conn, entity_type, entity_id, "delete", session_pk,
           field_changed=None, old_value=snapshot, new_value=None)


def _audit_field_change(
    conn: sqlite3.Connection, entity_type: str, entity_id: int,
    field: str, old_val: Any, new_val: Any, session_pk: int | None,
) -> None:
    """One audit row per changed field on update. No-op if old == new."""
    if old_val == new_val:
        return
    _audit(conn, entity_type, entity_id, "update", session_pk,
           field_changed=field, old_value=old_val, new_value=new_val)


def _audit_tag_change(
    conn: sqlite3.Connection, entity_type: str, entity_id: int,
    old_tags: list[str] | None, new_tags: list[str] | None,
    session_pk: int | None,
) -> None:
    """Audit a tag-set replacement. Lists are normalised (sorted, lowercased)
    before comparison so cosmetic ordering doesn't trigger a noisy audit row."""
    old_set = sorted({t.strip().lower() for t in (old_tags or []) if t.strip()})
    new_set = sorted({t.strip().lower() for t in (new_tags or []) if t.strip()})
    if old_set != new_set:
        _audit_field_change(conn, entity_type, entity_id, "tags",
                            old_set, new_set, session_pk)


def _format_duration(seconds: int | None) -> str | None:
    """Render seconds as a compact human-readable string. None → None."""
    if seconds is None:
        return None
    if seconds < 0:
        return f"-{_format_duration(-seconds)}"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        parts = [f"{hours}h"]
        if minutes:
            parts.append(f"{minutes}m")
        if sec:
            parts.append(f"{sec}s")
        return " ".join(parts)
    days, hours = divmod(hours, 24)
    parts = [f"{days}d"]
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _verdict(estimated: int | None, actual: int) -> tuple[float | None, str | None]:
    """Return (ratio, verdict) where ratio = actual/estimated and verdict ∈
    {'faster', 'on_target', 'slower', None}. None when no estimate was given."""
    if estimated is None or estimated <= 0:
        return None, None
    ratio = actual / estimated
    if ratio < 0.7:
        return round(ratio, 2), "faster"
    if ratio > 1.3:
        return round(ratio, 2), "slower"
    return round(ratio, 2), "on_target"


def _nickname_collision_error(e: sqlite3.IntegrityError) -> dict[str, Any] | None:
    """If `e` is the partial unique-index violation on tasks.nickname,
    return a friendly error dict. Otherwise None."""
    msg = str(e).lower()
    if "idx_tasks_nickname_active" in msg or (
        "unique" in msg and "nickname" in msg
    ):
        return {
            "error": "nickname is already used by an open or blocked task; "
                     "pick a different one, or close/cancel the other task first"
        }
    return None


def _log_schema(
    conn: sqlite3.Connection, operation: str, target: str, details: dict[str, Any],
    session_pk: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO schema_history (operation, target, details, session_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (operation, target, json.dumps(details, default=str), session_pk, _now()),
    )


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


@mcp.tool()
def register_session(client: str, label: str = "") -> dict[str, Any]:
    """Register the current AI conversation and get a session_id.

    Call this once at the start of a conversation. Pass the returned
    session_id to subsequent write tools so memories and tasks are tagged
    with provenance.

    Args:
        client: The client surface. Free-text for loose provenance, not
            strict routing — any string is accepted. Conventional values:
            "code" (Claude Code), "desktop" (Claude Desktop), "cowork"
            (Cowork), "claude.ai" (web chat), "claude-mobile" (iOS/Android
            app). Pick the closest match; invent new ones if needed.
        label: Optional short human-readable label for this conversation
            (e.g. "zeta-db-build", "main-server-planning").
    """
    if not client or not client.strip():
        return {"error": "client is required"}
    session_id = secrets.token_hex(8)  # 16 hex chars; 64-bit, no realistic birthday-collision risk
    now = _now()
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO sessions (session_id, client, label, started_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, client.strip(), label.strip() or None, now, now),
        )
        return {
            "session_id": session_id,
            "id": cur.lastrowid,
            "client": client.strip(),
            "label": label.strip() or None,
            "started_at": now,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------


@mcp.tool()
def list_categories() -> dict[str, Any]:
    """List all known categories, alphabetically."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
        return {"categories": [dict(r) for r in rows], "count": len(rows)}
    finally:
        conn.close()


@mcp.tool()
def add_category(name: str) -> dict[str, Any]:
    """Add a new category. No-op if it already exists.

    Args:
        name: Category name (lower-snake-case recommended).
    """
    if not name or not name.strip():
        return {"error": "name is required"}
    clean = name.strip().lower()
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT id FROM categories WHERE name = ?", (clean,)
        ).fetchone()
        if existing:
            return {"id": existing["id"], "name": clean, "created": False}
        cur = conn.execute("INSERT INTO categories (name) VALUES (?)", (clean,))
        return {"id": cur.lastrowid, "name": clean, "created": True}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Memories
# --------------------------------------------------------------------------


@mcp.tool()
def add_memory(
    summary: str,
    category: str,
    body: str | None = None,
    tags: list[str] | None = None,
    importance: int = 3,
    requested_by_human: bool = False,
    human_remark: str | None = None,
    nickname: str | None = None,
    origin: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Insert a new memory.

    Args:
        summary: Short form (required). Aim for ≤ 250 chars; hard cap
            is 400. Going over 250 is silently accepted, but the response
            includes `summary_length` so you can self-calibrate without
            being rejected. Past the 400 hard cap the call still succeeds
            (CR #33): the summary is stored truncated, the body is stored
            intact, and the response carries `summary_truncated` +
            `original_summary_length` — fix with update_memory(id,
            summary=...) without resending the body.
        category: Must already exist (call list_categories / add_category first).
        body: Optional long-form content. Use when there's context,
            background, sub-steps, or links to capture beyond the summary
            or the human's quote. If `human_remark` already conveys the
            full intent, leave `body` null — don't duplicate it here.
        tags: Optional list of tags. Lowercased automatically.
        importance: 1-5, default 3.
        requested_by_human: True if the human explicitly asked for this memory.
        human_remark: Verbatim quote from the human worth preserving.
            Keep it actually verbatim — don't paraphrase into this field.
        nickname: Optional short mnemonic (<=16 chars, [A-Za-z0-9_-]+) so
            the human can refer to this memory as e.g. `#42-OFFSETS` in
            conversation. Memories are not uniqueness-checked on nickname
            (a much bigger pool than tasks), so nicknames here are
            decorative. Derive a 2-6 char mnemonic from the summary when
            useful; leave null otherwise.
        origin: Optional durable label for the project/thread/persona
            this memory belongs to — distinct from `session_id` which is
            per-conversation. Use for cross-session continuity (e.g.
            "hermes-philosophical", "auth-rewrite"). See CLAUDE.md
            for conventions. Leave null when there's no obvious thread.
        session_id: From register_session(). Omit for anonymous.
    """
    # CR #11 mitigation: if the body contains trailing leaked
    # <parameter ...> XML, salvage values and clean the body. Salvaged
    # values only fill in kwargs the caller left at their default — an
    # explicit non-default kwarg always wins.
    recovered: dict[str, Any] = {}
    if body:
        cleaned_body, salvaged = _salvage_leaked_params(body)
        if salvaged:
            body = cleaned_body
            if salvaged.get("tags") is not None and tags is None:
                tags = salvaged["tags"]; recovered["tags"] = tags
            if salvaged.get("importance") is not None and importance == 3:
                try:
                    importance = int(salvaged["importance"])
                    recovered["importance"] = importance
                except (TypeError, ValueError):
                    pass
            if salvaged.get("requested_by_human") is not None and requested_by_human is False:
                requested_by_human = bool(salvaged["requested_by_human"])
                recovered["requested_by_human"] = requested_by_human
            if salvaged.get("human_remark") is not None and human_remark is None:
                human_remark = salvaged["human_remark"]; recovered["human_remark"] = human_remark
            if salvaged.get("nickname") is not None and nickname is None:
                nickname = salvaged["nickname"]; recovered["nickname"] = nickname
            if salvaged.get("origin") is not None and origin is None:
                origin = salvaged["origin"]; recovered["origin"] = origin
            if salvaged.get("session_id") is not None and session_id is None:
                session_id = salvaged["session_id"]; recovered["session_id"] = session_id

    stored_summary, truncation, err = _prepare_summary(summary, "update_memory")
    if err:
        return {"error": err}
    if (err := _validate_importance(importance)):
        return {"error": err}
    cleaned_nick, err = _clean_nickname(nickname)
    if err:
        return {"error": err}

    conn = _connect()
    try:
        cat_id = _resolve_category(conn, category.strip().lower())
        if cat_id is None:
            return {"error": f"unknown category '{category}'; call add_category first"}
        session_pk = _resolve_session(conn, session_id)
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO memories (
                summary, body, category_id, importance,
                requested_by_human, human_remark, nickname, origin,
                session_id, created_at, updated_at, last_accessed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored_summary,
                body,
                cat_id,
                importance,
                1 if requested_by_human else 0,
                human_remark,
                cleaned_nick,
                (origin.strip() or None) if origin else None,
                session_pk,
                now,
                now,
                now,
            ),
        )
        memory_id = cur.lastrowid
        _set_tags(conn, "memory_tags", "memory_id", memory_id, tags or [])

        # CR #16: embed on write. Best-effort — if the embed backend is
        # off / misconfigured / unreachable, the memory is still saved
        # and can be embedded later via backfill_embeddings().
        embed_blob = _try_embed_memory(stored_summary, body)
        if embed_blob is not None:
            conn.execute(
                "UPDATE memories SET embedding = ? WHERE id = ?",
                (embed_blob, memory_id),
            )

        # CR #20 audit
        _audit_create(conn, "memory", memory_id, {
            "summary": stored_summary,
            "body": body,
            "importance": importance,
            "requested_by_human": bool(requested_by_human),
            "human_remark": human_remark,
            "nickname": cleaned_nick,
            "origin": (origin.strip() or None) if origin else None,
            "category": category.strip().lower(),
            "tags": sorted({t.strip().lower() for t in (tags or []) if t.strip()}),
        }, session_pk)

        out = {
            "id": memory_id,
            "nickname": cleaned_nick,
            "created_at": now,
            "summary_length": len(stored_summary),
        }
        if truncation:
            out.update(truncation)
        if recovered:
            out["recovered_from_body"] = recovered
        return out
    finally:
        conn.close()


@mcp.tool()
def update_memory(
    id: int,
    summary: str | None = None,
    body: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    importance: int | None = None,
    requested_by_human: bool | None = None,
    human_remark: str | None = None,
    nickname: str | None = None,
    origin: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Update any subset of a memory's fields.

    Pass only the fields you want to change. If `tags` is provided (even an
    empty list), it *replaces* the existing tag set. To leave tags alone,
    omit the argument.

    `nickname` and `origin` follow the same convention: omit to leave
    alone, pass a valid string to set, pass empty string ("") to clear.

    `session_id` (when passed) is recorded as the most-recent author of
    this row — it replaces the row's session_id and bumps that session's
    last_seen. Omitting it leaves the row's existing session_id alone.

    Args:
        id: memory id.
        session_id: When passed, becomes the row's new author and bumps
            its last_seen. When omitted, the row's existing session_id is
            untouched.
    """
    conn = _connect()
    try:
        # Fetch the full existing row + tags so we can audit per-field changes.
        existing = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (id,)
        ).fetchone()
        if existing is None:
            return {"error": "not found"}
        old_tags = _get_tags(conn, "memory_tags", "memory_id", id)
        old_category_name = _category_name(conn, existing["category_id"])
        session_pk = _resolve_session(conn, session_id)

        sets: list[str] = []
        vals: list[Any] = []
        # (field_name, old_val, new_val) tuples accumulated for audit
        audit_changes: list[tuple[str, Any, Any]] = []

        if summary is not None:
            if (err := _validate_summary(summary)):
                return {"error": err}
            new_summary = summary.strip()
            sets.append("summary = ?"); vals.append(new_summary)
            audit_changes.append(("summary", existing["summary"], new_summary))
        if body is not None:
            sets.append("body = ?"); vals.append(body)
            audit_changes.append(("body", existing["body"], body))
        if category is not None:
            new_cat_name = category.strip().lower()
            cat_id = _resolve_category(conn, new_cat_name)
            if cat_id is None:
                return {"error": f"unknown category '{category}'"}
            sets.append("category_id = ?"); vals.append(cat_id)
            audit_changes.append(("category", old_category_name, new_cat_name))
        if importance is not None:
            if (err := _validate_importance(importance)):
                return {"error": err}
            sets.append("importance = ?"); vals.append(importance)
            audit_changes.append(("importance", existing["importance"], importance))
        if requested_by_human is not None:
            sets.append("requested_by_human = ?")
            vals.append(1 if requested_by_human else 0)
            audit_changes.append((
                "requested_by_human",
                bool(existing["requested_by_human"]),
                bool(requested_by_human),
            ))
        if human_remark is not None:
            sets.append("human_remark = ?"); vals.append(human_remark)
            audit_changes.append(("human_remark", existing["human_remark"], human_remark))
        if nickname is not None:  # explicitly passed; "" means clear
            cleaned_nick, err = _clean_nickname(nickname)
            if err:
                return {"error": err}
            sets.append("nickname = ?"); vals.append(cleaned_nick)
            audit_changes.append(("nickname", existing["nickname"], cleaned_nick))
        if origin is not None:  # "" clears
            stripped = origin.strip()
            new_origin = stripped or None
            sets.append("origin = ?"); vals.append(new_origin)
            audit_changes.append(("origin", existing["origin"], new_origin))
        if session_id is not None:  # CR #6: persist the session_id of the updater
            sets.append("session_id = ?"); vals.append(session_pk)
            # session_id changes are bookkeeping, not audit-worthy

        if sets:
            sets.append("updated_at = ?"); vals.append(_now())
            vals.append(id)
            conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", vals)
            # Emit one audit row per actually-changed field.
            for field, old, new in audit_changes:
                _audit_field_change(conn, "memory", id, field, old, new, session_pk)

        if tags is not None:
            _set_tags(conn, "memory_tags", "memory_id", id, tags)
            _audit_tag_change(conn, "memory", id, old_tags, tags, session_pk)

        # CR #16: re-embed when summary or body changed (the two fields the
        # embedding is derived from). Best-effort: a failure here just
        # leaves the previous embedding in place — better stale than nothing,
        # and a backfill pass can refresh later if needed.
        changed_fields = {field for field, _, _ in audit_changes}
        if changed_fields & {"summary", "body"}:
            current = conn.execute(
                "SELECT summary, body FROM memories WHERE id = ?", (id,)
            ).fetchone()
            embed_blob = _try_embed_memory(current["summary"], current["body"])
            if embed_blob is not None:
                conn.execute(
                    "UPDATE memories SET embedding = ? WHERE id = ?",
                    (embed_blob, id),
                )

        row = conn.execute("SELECT * FROM memories WHERE id = ?", (id,)).fetchone()
        return _hydrate_memory(conn, row, detail="full")
    finally:
        conn.close()


@mcp.tool()
def delete_memory(id: int, session_id: str | None = None) -> dict[str, Any]:
    """Delete a memory by id. Audit row records the full pre-delete snapshot."""
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (id,)
        ).fetchone()
        if existing is None:
            return {"error": "not found"}
        old_tags = _get_tags(conn, "memory_tags", "memory_id", id)
        snapshot = {
            "summary": existing["summary"],
            "body": existing["body"],
            "importance": existing["importance"],
            "requested_by_human": bool(existing["requested_by_human"]),
            "human_remark": existing["human_remark"],
            "nickname": existing["nickname"],
            "origin": existing["origin"],
            "category": _category_name(conn, existing["category_id"]),
            "tags": sorted(old_tags),
        }
        session_pk = _resolve_session(conn, session_id)
        conn.execute("DELETE FROM memories WHERE id = ?", (id,))
        _audit_delete(conn, "memory", id, snapshot, session_pk)
        return {"deleted": True, "id": id}
    finally:
        conn.close()


@mcp.tool()
def list_memories(
    category: str | None = None,
    tags: list[str] | None = None,
    since: str | None = None,
    limit: int = 20,
    detail: str = "summary",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Browse memories.

    Does NOT bump last_accessed at any detail level (browsing != recall) —
    deliberately, so pruning passes can read bodies without polluting the
    cruft signal. Use get_memory / search_memories for recall.

    Args:
        category: Filter by category name.
        tags: Filter by tags (see tag_mode).
        since: ISO-8601 cutoff; only memories updated after this.
        limit: Max rows, default 20, hard cap 200.
        detail: 'index' (one scan line per row), 'summary' (default — full
            summary + metadata, body omitted), 'excerpt' (summary +
            first ~280 chars of body), 'full' (everything).
        tag_mode: 'any' (default — row carries at least one tag) or
            'all' (row must carry every tag).
    """
    limit = max(1, min(limit, LIST_HARD_LIMIT))
    if (err := _validate_detail(detail)):
        return {"error": err}
    conn = _connect()
    try:
        wheres: list[str] = []
        params: list[Any] = []

        if category is not None:
            cat_id = _resolve_category(conn, category.strip().lower())
            if cat_id is None:
                return {"error": f"unknown category '{category}'"}
            wheres.append("m.category_id = ?"); params.append(cat_id)
        if since is not None:
            wheres.append("m.updated_at >= ?"); params.append(since)
        tag_sql, tag_params = _tag_filter("memory_tags", "memory_id", "m", tags, tag_mode)
        if tag_sql:
            wheres.append(tag_sql); params.extend(tag_params)

        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT m.*
            FROM memories m
            {where_sql}
            ORDER BY m.updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return {
            "memories": [_hydrate_memory(conn, r, detail=detail) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


@mcp.tool()
def search_memories(
    query: str,
    category: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    detail: str = "summary",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Keyword search across summary and body.

    Case-insensitive LIKE on both summary and body. Bumps last_accessed on
    every returned row (search = recall), at every detail level.

    Args:
        query: Search string (matched as %query%).
        category: Optional category filter.
        tags: Optional tag filter (see tag_mode).
        limit: Max rows, default 10, hard cap 100.
        detail: 'index' (one scan line per row), 'summary' (default),
            'excerpt' (summary + first ~280 chars of body), 'full'
            (everything — saves a get_memory round trip per hit).
        tag_mode: 'any' (default) or 'all' (row must carry every tag).
    """
    if not query or not query.strip():
        return {"error": "query is required"}
    limit = max(1, min(limit, SEARCH_HARD_LIMIT))
    if (err := _validate_detail(detail)):
        return {"error": err}
    like = f"%{query.strip()}%"

    conn = _connect()
    try:
        wheres: list[str] = ["(m.summary LIKE ? OR m.body LIKE ?)"]
        params: list[Any] = [like, like]

        if category is not None:
            cat_id = _resolve_category(conn, category.strip().lower())
            if cat_id is None:
                return {"error": f"unknown category '{category}'"}
            wheres.append("m.category_id = ?"); params.append(cat_id)
        tag_sql, tag_params = _tag_filter("memory_tags", "memory_id", "m", tags, tag_mode)
        if tag_sql:
            wheres.append(tag_sql); params.extend(tag_params)

        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT m.*
            FROM memories m
            WHERE {' AND '.join(wheres)}
            ORDER BY m.importance DESC, m.updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        _bump_memory_accessed(conn, [r["id"] for r in rows])
        return {
            "memories": [_hydrate_memory(conn, r, detail=detail) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


@mcp.tool()
def get_memory(id: int) -> dict[str, Any]:
    """Fetch a memory in full (including body). Bumps last_accessed."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (id,)).fetchone()
        if row is None:
            return {"error": "not found"}
        _bump_memory_accessed(conn, [id])
        # Re-fetch so the returned row reflects the bump.
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (id,)).fetchone()
        return _hydrate_memory(conn, row, detail="full")
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Semantic search + hybrid search + backfill (CR #16 / CR #17)
# --------------------------------------------------------------------------


def _time_decay_factor(last_accessed: str | None, alpha: float) -> float:
    """Anderson / ACT-R power-law decay: (1 + days_since_access)^(-alpha).

    alpha=0   -> 1.0 (no decay)
    alpha=0.5 -> gentle decay (1-week-old memory is 0.4x a 1-day-old one)
    alpha=1.0 -> aggressive (10-day-old is 0.1x a 1-day-old)
    """
    if alpha <= 0 or not last_accessed:
        return 1.0
    try:
        # last_accessed is stored as 'YYYY-MM-DD HH:MM:SS.ffffff' (UTC)
        s = last_accessed.replace("Z", "")
        if "." in s:
            la = datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")
        else:
            la = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        la = la.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_days = max(0.0, (now - la).total_seconds() / 86400.0)
        return (1.0 + age_days) ** (-alpha)
    except (ValueError, TypeError):
        return 1.0


@mcp.tool()
def backfill_embeddings(max_rows: int = 100) -> dict[str, Any]:
    """Embed any memories with NULL embedding (CR #16).

    Useful after first enabling ZETA_EMBED_BACKEND on an existing DB,
    or after changing models (set EMBED_MODEL/EMBED_DIMS in .env, then
    NULL out the old vectors and re-run).

    Stops at max_rows per call to bound rate-limit and cost exposure;
    re-run to continue. Aborts early after 3 consecutive errors.
    """
    if EMBED_BACKEND == "none":
        return {"error": "ZETA_EMBED_BACKEND is 'none'; nothing to backfill"}
    max_rows = max(1, min(max_rows, 1000))

    conn = _connect()
    scanned = embedded = skipped = errors = 0
    error_messages: list[str] = []
    try:
        rows = conn.execute(
            "SELECT id, summary, body FROM memories "
            "WHERE embedding IS NULL ORDER BY id LIMIT ?",
            (max_rows,),
        ).fetchall()
        scanned = len(rows)
        consecutive_errors = 0
        for r in rows:
            text = _memory_embed_text(r["summary"], r["body"])
            if not text:
                skipped += 1
                continue
            try:
                blob = _embed_text(text)
                if blob is None:
                    skipped += 1
                    consecutive_errors = 0
                    continue
                conn.execute(
                    "UPDATE memories SET embedding = ? WHERE id = ?",
                    (blob, r["id"]),
                )
                embedded += 1
                consecutive_errors = 0
            except Exception as e:
                errors += 1
                consecutive_errors += 1
                if len(error_messages) < 5:
                    error_messages.append(f"id={r['id']}: {e}")
                if consecutive_errors >= 3:
                    error_messages.append("aborted: 3 consecutive errors")
                    break
        # Estimate remaining count.
        remaining = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NULL"
        ).fetchone()[0]
        return {
            "scanned": scanned,
            "embedded": embedded,
            "skipped": skipped,
            "errors": errors,
            "error_messages": error_messages or None,
            "remaining": remaining,
        }
    finally:
        conn.close()


def _semantic_candidates(
    query: str,
    top_k: int,
    min_similarity: float,
    category: str | None,
    tags: list[str] | None,
    decay_alpha: float,
    tag_mode: str = "any",
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """Shared candidate fetch for semantic_search / bulk_load_context.

    Returns (candidates, error). Candidates are sorted by decayed score and
    trimmed to top_k; each carries `body` so callers can shape detail levels
    without a second query. Does NOT bump last_accessed — that's the public
    tools' responsibility (they know what actually got surfaced).
    """
    if EMBED_BACKEND == "none":
        return None, {"error": "embeddings disabled; set ZETA_EMBED_BACKEND in .env"}
    if not query or not query.strip():
        return None, {"error": "query is required"}
    top_k = max(1, min(top_k, 100))

    try:
        qvec = _embed_text(query)
    except Exception as e:
        return None, {"error": f"embedding query failed: {e}"}
    if qvec is None:
        return None, {"error": "no embedding produced for query"}

    conn = _connect()
    try:
        wheres = ["m.embedding IS NOT NULL"]
        params: list[Any] = [qvec]  # used in SELECT distance computation
        if category:
            cat_id = _resolve_category(conn, category.strip().lower())
            if cat_id is None:
                return None, {"error": f"unknown category '{category}'"}
            wheres.append("m.category_id = ?")
            params.append(cat_id)
        tag_sql, tag_params = _tag_filter("memory_tags", "memory_id", "m", tags, tag_mode)
        if tag_sql:
            wheres.append(tag_sql); params.extend(tag_params)

        where_clause = " AND ".join(wheres)
        # Over-fetch by 3x so decay re-ranking doesn't push the right rows
        # off the bottom of the SQL-side ordering.
        over_fetch = top_k * 3
        sql = (
            "SELECT m.id, m.summary, m.body, m.category_id, m.importance, "
            "m.nickname, m.origin, m.last_accessed, m.updated_at, "
            "(1.0 - vec_distance_cosine(m.embedding, ?)) AS similarity "
            f"FROM memories m WHERE {where_clause} "
            "ORDER BY similarity DESC LIMIT ?"
        )
        params.append(over_fetch)
        rows = conn.execute(sql, params).fetchall()

        results = []
        for r in rows:
            sim = float(r["similarity"])
            if sim < min_similarity:
                continue
            decay = _time_decay_factor(r["last_accessed"], decay_alpha)
            score = sim * decay
            results.append({
                "id": r["id"],
                "summary": r["summary"],
                "body": r["body"],
                "category": _category_name(conn, r["category_id"]),
                "importance": r["importance"],
                "nickname": r["nickname"],
                "origin": r["origin"],
                "tags": _get_tags(conn, "memory_tags", "memory_id", r["id"]),
                "similarity": round(sim, 4),
                "decay_factor": round(decay, 4),
                "score": round(score, 4),
                "last_accessed": r["last_accessed"],
            })

        # Re-rank by decayed score and trim to top_k.
        results.sort(key=lambda d: d["score"], reverse=True)
        return results[:top_k], None
    finally:
        conn.close()


def _shape_search_hit(hit: dict[str, Any], detail: str) -> dict[str, Any]:
    """Shape a semantic/hybrid hit (which carries body + ranking fields)
    to the requested detail level."""
    if detail == "index":
        out: dict[str, Any] = {
            "id": hit["id"],
            "summary": _truncate(hit["summary"], INDEX_TRUNC_CHARS),
        }
        if hit.get("nickname"):
            out["nickname"] = hit["nickname"]
        out["category"] = hit["category"]
        out["score"] = hit["score"]
        return out
    out = {k: v for k, v in hit.items() if k != "body"}
    body = hit.get("body")
    if detail == "excerpt" and body:
        out["body_excerpt"] = _truncate(body, EXCERPT_BODY_CHARS)
        out["body_chars"] = len(body)
    elif detail == "full":
        out["body"] = body
    return out


@mcp.tool()
def semantic_search_memories(
    query: str,
    top_k: int = 10,
    min_similarity: float = 0.0,
    category: str | None = None,
    tags: list[str] | None = None,
    decay_alpha: float = 0.0,
    detail: str = "summary",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Find memories semantically similar to the query (CR #16).

    Bumps last_accessed on every returned row (search = recall), at every
    detail level — same contract as search_memories.

    Args:
        query: Free-text — embedded via the configured backend.
        top_k: Max results (default 10, hard cap 100).
        min_similarity: Filter out cosine similarities below this (0..1).
        category: Optional category filter.
        tags: Optional tag filter (see tag_mode).
        decay_alpha: Time-decay aggressiveness (Anderson/ACT-R power-law).
            0 = no decay (pure semantic), 0.5 = gentle, 1.0 = aggressive.
            Final score = similarity * (1 + days_since_last_access)^(-alpha).
        detail: 'index' (one scan line per hit), 'summary' (default),
            'excerpt' (+ first ~280 chars of body), 'full' (+ whole body).
        tag_mode: 'any' (default) or 'all' (row must carry every tag).
    """
    if (err := _validate_detail(detail)):
        return {"error": err}
    results, error = _semantic_candidates(
        query, top_k, min_similarity, category, tags, decay_alpha, tag_mode)
    if error:
        return error

    if results:
        conn = _connect()
        try:
            _bump_memory_accessed(conn, [r["id"] for r in results])
        finally:
            conn.close()

    shaped = [_shape_search_hit(r, detail) for r in results]
    return {"query": query, "count": len(shaped), "results": shaped}


@mcp.tool()
def hybrid_search_memories(
    query: str,
    like_text: str | None = None,
    top_k: int = 10,
    min_similarity: float = 0.0,
    category: str | None = None,
    tags: list[str] | None = None,
    decay_alpha: float = 0.0,
    match_mode: str = "any",
    detail: str = "summary",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Combine structural LIKE filters with semantic similarity (CR #17).

    Realises the "WHERE field LIKE OR similarity > N" pattern in one query.
    Bumps last_accessed on every returned row (search = recall), at every
    detail level — same contract as search_memories.

    Args:
        query: Semantic query — embedded.
        like_text: Optional case-insensitive substring; if provided, applied
            to both summary and body as LIKE '%text%'. Omit to skip the
            structural filter (pure semantic).
        match_mode: 'any' (default) returns rows matching EITHER the LIKE
            OR semantic-similarity (similarity > min_similarity); 'all'
            requires BOTH (memory must hit the LIKE *and* exceed
            min_similarity).
        detail: 'index' (one scan line per hit), 'summary' (default),
            'excerpt' (+ first ~280 chars of body), 'full' (+ whole body).
        tag_mode: 'any' (default) or 'all' (row must carry every tag).
        top_k, min_similarity, category, tags, decay_alpha: see
            semantic_search_memories.
    """
    if EMBED_BACKEND == "none":
        return {"error": "embeddings disabled; set ZETA_EMBED_BACKEND in .env"}
    if not query or not query.strip():
        return {"error": "query is required"}
    if match_mode not in ("any", "all"):
        return {"error": "match_mode must be 'any' or 'all'"}
    if (err := _validate_detail(detail)):
        return {"error": err}
    top_k = max(1, min(top_k, 100))

    try:
        qvec = _embed_text(query)
    except Exception as e:
        return {"error": f"embedding query failed: {e}"}
    if qvec is None:
        return {"error": "no embedding produced for query"}

    conn = _connect()
    try:
        # Base filters (always applied): non-null embedding + category/tag.
        base_wheres = ["m.embedding IS NOT NULL"]
        base_params: list[Any] = []
        if category:
            cat_id = _resolve_category(conn, category.strip().lower())
            if cat_id is None:
                return {"error": f"unknown category '{category}'"}
            base_wheres.append("m.category_id = ?")
            base_params.append(cat_id)
        tag_sql, tag_params = _tag_filter("memory_tags", "memory_id", "m", tags, tag_mode)
        if tag_sql:
            base_wheres.append(tag_sql); base_params.extend(tag_params)

        # Hybrid filter: combine LIKE and similarity per match_mode.
        like_clauses: list[str] = []
        like_params: list[Any] = []
        if like_text and like_text.strip():
            pat = f"%{like_text.strip()}%"
            like_clauses.append("(m.summary LIKE ? OR m.body LIKE ?)")
            like_params.extend([pat, pat])
        sim_clause = "(1.0 - vec_distance_cosine(m.embedding, ?)) > ?"

        if not like_clauses:
            # Pure semantic — fall through to similarity-only filter.
            hybrid_wheres = [sim_clause]
            hybrid_params: list[Any] = [qvec, min_similarity]
        elif match_mode == "any":
            hybrid_wheres = [f"({like_clauses[0]} OR {sim_clause})"]
            hybrid_params = like_params + [qvec, min_similarity]
        else:  # 'all'
            hybrid_wheres = [like_clauses[0], sim_clause]
            hybrid_params = like_params + [qvec, min_similarity]

        all_wheres = base_wheres + hybrid_wheres
        where_clause = " AND ".join(all_wheres)
        over_fetch = top_k * 3
        sql = (
            "SELECT m.id, m.summary, m.body, m.category_id, m.importance, "
            "m.nickname, m.origin, m.last_accessed, m.updated_at, "
            "(1.0 - vec_distance_cosine(m.embedding, ?)) AS similarity "
            f"FROM memories m WHERE {where_clause} "
            "ORDER BY similarity DESC LIMIT ?"
        )
        # Parameter order: SELECT's qvec, then base_params, then hybrid_params,
        # then over_fetch.
        params = [qvec] + base_params + hybrid_params + [over_fetch]
        rows = conn.execute(sql, params).fetchall()

        results = []
        for r in rows:
            sim = float(r["similarity"])
            decay = _time_decay_factor(r["last_accessed"], decay_alpha)
            score = sim * decay
            results.append({
                "id": r["id"],
                "summary": r["summary"],
                "body": r["body"],
                "category": _category_name(conn, r["category_id"]),
                "importance": r["importance"],
                "nickname": r["nickname"],
                "origin": r["origin"],
                "tags": _get_tags(conn, "memory_tags", "memory_id", r["id"]),
                "similarity": round(sim, 4),
                "decay_factor": round(decay, 4),
                "score": round(score, 4),
                "matched_like": bool(like_text and like_text.strip()
                                     and like_text.strip().lower() in
                                     (r["summary"] or "").lower() + " " +
                                     (r["body"] or "").lower()),
                "last_accessed": r["last_accessed"],
            })

        results.sort(key=lambda d: d["score"], reverse=True)
        results = results[:top_k]
        _bump_memory_accessed(conn, [r["id"] for r in results])
        shaped = [_shape_search_hit(r, detail) for r in results]
        return {
            "query": query,
            "like_text": like_text,
            "match_mode": match_mode,
            "count": len(shaped),
            "results": shaped,
        }
    finally:
        conn.close()


_TIKTOKEN_ENC: Any = None


def _estimate_tokens(text: str) -> int:
    """Estimate token count using tiktoken if available, falling back to
    chars/4 (a reasonable approximation for English-heavy prose).
    """
    if not text:
        return 0
    global _TIKTOKEN_ENC
    try:
        if _TIKTOKEN_ENC is None:
            import tiktoken
            _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")  # GPT-4 / Claude-ish
        return len(_TIKTOKEN_ENC.encode(text))
    except Exception:
        return max(1, len(text) // 4)


# Graduated packing thresholds for bulk_load_context: the top-ranked
# memories load at full detail until this fraction of the budget is spent,
# then excerpts, then one-line index entries. Depth where relevance is
# highest, breadth on the tail.
BULK_FULL_FRAC = 0.55
BULK_EXCERPT_FRAC = 0.85
_BULK_DEMOTE = {"full": "excerpt", "excerpt": "index"}
_BULK_INDEX_HEADER = "--- further relevant memories (index only; get_memory(id) for detail) ---\n"
# Transport guard (CR #34 follow-up, calibrated 2026-06-10 against Claude
# Code's inline tool-result cap): a ~44 KB formatted blob returned inline,
# ~52 KB spilled to a file. The packer enforces this char ceiling on
# `formatted` ALONGSIDE the token budget, so a caller raising max_tokens
# can't silently outrun the transport whatever the content's
# chars-per-token density. Raise it only if your MCP client carries more.
BULK_MAX_CHARS = int(_env("ZETA_BULK_MAX_CHARS", "45000") or "45000")


def _bulk_block(c: dict[str, Any], level: str) -> str:
    """Render one candidate at the given detail level for bulk_load."""
    ref = f"#{c['id']}{('-' + c['nickname']) if c['nickname'] else ''}"
    if level == "index":
        return f"{ref}  [{c['category']}]  {_truncate(c['summary'], INDEX_TRUNC_CHARS)}\n"
    lines = [f"{ref}  [{c['category']}]  (score {c['score']:.3f}, sim {c['similarity']:.3f})",
             c["summary"]]
    body = c.get("body")
    if body and level in ("full", "excerpt"):
        lines.append(body if level == "full" else _truncate(body, EXCERPT_BODY_CHARS))
    if c.get("tags"):
        lines.append("tags: " + ", ".join(c["tags"]))
    return "\n".join(lines) + "\n---\n"


@mcp.tool()
def bulk_load_context(
    query: str,
    max_tokens: int = 12000,
    min_similarity: float = 0.3,
    category: str | None = None,
    tags: list[str] | None = None,
    decay_alpha: float = 0.3,
    include_body: bool = True,
    detail: str = "graduated",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Fetch the most-relevant memories for a role/topic prompt, ordered
    and packed into a context-sparing format up to a token budget
    (CR #17 — the bulk-pull half).

    The intended use: a new AI session is given a role description as a
    prompt; it calls this tool to load a coherent slice of the human's
    durable memory store, returning text it can read directly.

    Memories packed at full or excerpt detail count as recall and bump
    last_accessed; index-line entries don't.

    Args:
        query: The role / topic description. Embedded, used for semantic
            ranking.
        max_tokens: Token budget for the returned `formatted` blob
            (default 12k, cap 60k). CAUTION (CR #34): MCP clients cap the
            inline tool result — calibrated against Claude Code, ~44 KB
            returns inline and ~52 KB spills to a file. The default is
            sized to come back inline; independent of this budget, the
            packer also stops at ZETA_BULK_MAX_CHARS (default 45000)
            characters so a generous token budget can't outrun the
            transport.
        min_similarity: Filter results below this cosine similarity
            (default 0.3 — practical threshold for "actually relevant").
        category: Optional category filter.
        tags: Optional tag filter (see tag_mode).
        decay_alpha: Time-decay (Anderson/ACT-R). Default 0.3 — gently
            prefer recent. Set 0 to disable.
        include_body: Deprecated (kept for compatibility) — passing False
            behaves like detail='summary'. Prefer `detail`.
        detail: Packing mode for the formatted blob:
            'graduated' (default) — top-ranked memories at full detail
            until ~55% of budget, then excerpts until ~85%, then one-line
            index entries: depth where relevance is highest, breadth on
            the tail. Or a uniform level: 'full', 'excerpt', 'summary',
            'index' ('index' fits ~100 memories in under ~4k tokens — a
            cheap whole-store orientation sweep).
        tag_mode: 'any' (default) or 'all' (row must carry every tag).
    """
    if not query or not query.strip():
        return {"error": "query is required"}
    if detail != "graduated" and (err := _validate_detail(detail)):
        return {"error": err.replace("must be one of", "must be 'graduated' or one of")}
    max_tokens = max(100, min(max_tokens, 60_000))
    if not include_body and detail == "graduated":
        detail = "summary"  # legacy include_body=False callers

    # Over-fetch generously — we'll pack as many as fit in the budget.
    candidates, error = _semantic_candidates(
        query, top_k=100, min_similarity=min_similarity, category=category,
        tags=tags, decay_alpha=decay_alpha, tag_mode=tag_mode)
    if error:
        return error

    chunks: list[str] = []
    tokens_used = 0
    chars_used = 0
    skipped = 0
    detail_counts = {"full": 0, "excerpt": 0, "summary": 0, "index": 0}
    recalled_ids: list[int] = []  # packed at full/excerpt -> bump last_accessed
    index_header_emitted = False
    for c in candidates:
        if detail == "graduated":
            frac = tokens_used / max_tokens
            level = ("full" if frac < BULK_FULL_FRAC
                     else "excerpt" if frac < BULK_EXCERPT_FRAC
                     else "index")
        else:
            level = detail
        # Pack at the target level; in graduated mode an oversize block
        # demotes (full -> excerpt -> index) instead of being skipped.
        while True:
            block = _bulk_block(c, level)
            extra = ""
            if detail == "graduated" and level == "index" and not index_header_emitted:
                extra = _BULK_INDEX_HEADER
            block_tokens = _estimate_tokens(extra + block)
            if (tokens_used + block_tokens <= max_tokens
                    and chars_used + len(extra + block) <= BULK_MAX_CHARS):
                chunks.append(extra + block)
                tokens_used += block_tokens
                chars_used += len(extra + block)
                detail_counts[level] += 1
                if extra:
                    index_header_emitted = True
                if level in ("full", "excerpt"):
                    recalled_ids.append(c["id"])
                break
            if detail == "graduated" and level in _BULK_DEMOTE:
                level = _BULK_DEMOTE[level]
                continue
            skipped += 1
            break

    if recalled_ids:
        conn = _connect()
        try:
            _bump_memory_accessed(conn, recalled_ids)
        finally:
            conn.close()

    formatted = "".join(chunks).rstrip()
    return {
        "query": query,
        "max_tokens": max_tokens,
        "tokens_used": tokens_used,
        "chars_used": len(formatted),
        "loaded_count": sum(detail_counts.values()),
        "detail_counts": {k: v for k, v in detail_counts.items() if v},
        "skipped_count": skipped,
        "candidates_considered": len(candidates),
        "decay_alpha": decay_alpha,
        "detail": detail,
        "formatted": formatted,
    }


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


@mcp.tool()
def add_task(
    summary: str,
    category: str,
    body: str | None = None,
    tags: list[str] | None = None,
    importance: int = 3,
    due_date: str | None = None,
    requested_by_human: bool = False,
    human_remark: str | None = None,
    nickname: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Insert a new task. Status defaults to 'open'.

    Args:
        summary: Short form (required). Aim for ≤ 250 chars; hard cap
            is 400. Going over 250 is silently accepted, but the response
            includes `summary_length` so you can self-calibrate without
            being rejected. Past the 400 hard cap the call still succeeds
            (CR #33): the summary is stored truncated, the body is stored
            intact, and the response carries `summary_truncated` +
            `original_summary_length` — fix with update_task(id,
            summary=...) without resending the body.
        category: Must already exist.
        body: Optional long-form detail. Use when there's context,
            sub-steps, background, or links to capture. If
            `human_remark` already conveys the full intent, leave
            `body` null — don't duplicate.
        tags: Optional list of tags.
        importance: 1-5, default 3.
        due_date: ISO-8601 date or datetime string. Optional.
        requested_by_human: True if the human explicitly asked for this task.
        human_remark: Verbatim quote from the human. Keep it actually
            verbatim — don't paraphrase into this field.
        nickname: Optional short mnemonic (<=16 chars, [A-Za-z0-9_-]+) so
            the human can refer to this task as e.g. `#15-BPC` in
            conversation. Soft-unique across active tasks (open + blocked):
            collisions with another active task are rejected. Done and
            cancelled tasks free up their nickname for reuse. Derive a 2-6
            char mnemonic from the summary when sensible; leave null
            otherwise.
        session_id: From register_session().
    """
    # CR #11 mitigation: salvage trailing leaked <parameter ...> from body.
    recovered: dict[str, Any] = {}
    if body:
        cleaned_body, salvaged = _salvage_leaked_params(body)
        if salvaged:
            body = cleaned_body
            if salvaged.get("tags") is not None and tags is None:
                tags = salvaged["tags"]; recovered["tags"] = tags
            if salvaged.get("importance") is not None and importance == 3:
                try:
                    importance = int(salvaged["importance"])
                    recovered["importance"] = importance
                except (TypeError, ValueError):
                    pass
            if salvaged.get("requested_by_human") is not None and requested_by_human is False:
                requested_by_human = bool(salvaged["requested_by_human"])
                recovered["requested_by_human"] = requested_by_human
            if salvaged.get("human_remark") is not None and human_remark is None:
                human_remark = salvaged["human_remark"]; recovered["human_remark"] = human_remark
            if salvaged.get("nickname") is not None and nickname is None:
                nickname = salvaged["nickname"]; recovered["nickname"] = nickname
            if salvaged.get("session_id") is not None and session_id is None:
                session_id = salvaged["session_id"]; recovered["session_id"] = session_id

    stored_summary, truncation, err = _prepare_summary(summary, "update_task")
    if err:
        return {"error": err}
    if (err := _validate_importance(importance)):
        return {"error": err}
    cleaned_nick, err = _clean_nickname(nickname)
    if err:
        return {"error": err}

    conn = _connect()
    try:
        cat_id = _resolve_category(conn, category.strip().lower())
        if cat_id is None:
            return {"error": f"unknown category '{category}'; call add_category first"}
        session_pk = _resolve_session(conn, session_id)
        now = _now()
        try:
            cur = conn.execute(
                """
                INSERT INTO tasks (
                    summary, body, category_id, status, importance, due_date,
                    requested_by_human, human_remark, nickname, session_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored_summary,
                    body,
                    cat_id,
                    importance,
                    due_date,
                    1 if requested_by_human else 0,
                    human_remark,
                    cleaned_nick,
                    session_pk,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as e:
            friendly = _nickname_collision_error(e)
            if friendly:
                return friendly
            raise
        task_id = cur.lastrowid
        _set_tags(conn, "task_tags", "task_id", task_id, tags or [])

        # CR #20 audit
        _audit_create(conn, "task", task_id, {
            "summary": stored_summary,
            "body": body,
            "importance": importance,
            "due_date": due_date,
            "requested_by_human": bool(requested_by_human),
            "human_remark": human_remark,
            "nickname": cleaned_nick,
            "status": "open",
            "category": category.strip().lower(),
            "tags": sorted({t.strip().lower() for t in (tags or []) if t.strip()}),
        }, session_pk)

        out = {
            "id": task_id,
            "nickname": cleaned_nick,
            "created_at": now,
            "summary_length": len(stored_summary),
        }
        if truncation:
            out.update(truncation)
        if recovered:
            out["recovered_from_body"] = recovered
        return out
    finally:
        conn.close()


@mcp.tool()
def update_task(
    id: int,
    summary: str | None = None,
    body: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    importance: int | None = None,
    due_date: str | None = None,
    requested_by_human: bool | None = None,
    human_remark: str | None = None,
    nickname: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Update any subset of a task's fields.

    Setting status to 'done' also stamps completed_at. Setting it away from
    'done' clears completed_at. Tag handling matches update_memory.

    `nickname`: omit to leave alone, pass valid string to set, pass empty
    string ("") to clear. Setting a nickname that collides with another
    open / in_progress / blocked task returns an error. Reopening a task whose old
    nickname has since been taken also returns an error — clear the
    nickname or pick a different one.

    `session_id` (when passed) is recorded as the most-recent author of
    this row — it replaces the row's session_id and bumps that session's
    last_seen. Omitting it leaves the row's existing session_id alone.
    """
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (id,)
        ).fetchone()
        if existing is None:
            return {"error": "not found"}
        old_tags = _get_tags(conn, "task_tags", "task_id", id)
        old_category_name = _category_name(conn, existing["category_id"])
        session_pk = _resolve_session(conn, session_id)

        sets: list[str] = []
        vals: list[Any] = []
        audit_changes: list[tuple[str, Any, Any]] = []

        if summary is not None:
            if (err := _validate_summary(summary)):
                return {"error": err}
            new_summary = summary.strip()
            sets.append("summary = ?"); vals.append(new_summary)
            audit_changes.append(("summary", existing["summary"], new_summary))
        if body is not None:
            sets.append("body = ?"); vals.append(body)
            audit_changes.append(("body", existing["body"], body))
        if category is not None:
            new_cat_name = category.strip().lower()
            cat_id = _resolve_category(conn, new_cat_name)
            if cat_id is None:
                return {"error": f"unknown category '{category}'"}
            sets.append("category_id = ?"); vals.append(cat_id)
            audit_changes.append(("category", old_category_name, new_cat_name))
        if status is not None:
            if status not in TASK_STATUSES:
                return {"error": f"status must be one of {sorted(TASK_STATUSES)}"}
            sets.append("status = ?"); vals.append(status)
            audit_changes.append(("status", existing["status"], status))
            if status == "done":
                sets.append("completed_at = ?"); vals.append(_now())
            else:
                sets.append("completed_at = NULL")
        if importance is not None:
            if (err := _validate_importance(importance)):
                return {"error": err}
            sets.append("importance = ?"); vals.append(importance)
            audit_changes.append(("importance", existing["importance"], importance))
        if due_date is not None:
            new_due = due_date or None
            sets.append("due_date = ?"); vals.append(new_due)
            audit_changes.append(("due_date", existing["due_date"], new_due))
        if requested_by_human is not None:
            sets.append("requested_by_human = ?")
            vals.append(1 if requested_by_human else 0)
            audit_changes.append((
                "requested_by_human",
                bool(existing["requested_by_human"]),
                bool(requested_by_human),
            ))
        if human_remark is not None:
            sets.append("human_remark = ?"); vals.append(human_remark)
            audit_changes.append(("human_remark", existing["human_remark"], human_remark))
        if nickname is not None:  # explicitly passed; "" means clear
            cleaned_nick, err = _clean_nickname(nickname)
            if err:
                return {"error": err}
            sets.append("nickname = ?"); vals.append(cleaned_nick)
            audit_changes.append(("nickname", existing["nickname"], cleaned_nick))
        if session_id is not None:  # CR #6: persist the session_id of the updater
            sets.append("session_id = ?"); vals.append(session_pk)
            # session_id is bookkeeping, not audit-worthy

        if sets:
            sets.append("updated_at = ?"); vals.append(_now())
            vals.append(id)
            try:
                conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)
            except sqlite3.IntegrityError as e:
                friendly = _nickname_collision_error(e)
                if friendly:
                    return friendly
                raise
            for field, old, new in audit_changes:
                _audit_field_change(conn, "task", id, field, old, new, session_pk)

        if tags is not None:
            _set_tags(conn, "task_tags", "task_id", id, tags)
            _audit_tag_change(conn, "task", id, old_tags, tags, session_pk)

        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
        return _hydrate_task(conn, row, detail="full")
    finally:
        conn.close()


@mcp.tool()
def complete_task(id: int, session_id: str | None = None) -> dict[str, Any]:
    """Mark a task as done. Stamps completed_at."""
    return update_task(id, status="done", session_id=session_id)


@mcp.tool()
def delete_task(id: int, session_id: str | None = None) -> dict[str, Any]:
    """Delete a task by id. Audit row records the full pre-delete snapshot."""
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (id,)
        ).fetchone()
        if existing is None:
            return {"error": "not found"}
        old_tags = _get_tags(conn, "task_tags", "task_id", id)
        snapshot = {
            "summary": existing["summary"],
            "body": existing["body"],
            "importance": existing["importance"],
            "due_date": existing["due_date"],
            "requested_by_human": bool(existing["requested_by_human"]),
            "human_remark": existing["human_remark"],
            "nickname": existing["nickname"],
            "status": existing["status"],
            "category": _category_name(conn, existing["category_id"]),
            "tags": sorted(old_tags),
        }
        session_pk = _resolve_session(conn, session_id)
        conn.execute("DELETE FROM tasks WHERE id = ?", (id,))
        _audit_delete(conn, "task", id, snapshot, session_pk)
        return {"deleted": True, "id": id}
    finally:
        conn.close()


@mcp.tool()
def list_tasks(
    category: str | None = None,
    status: str | None = "open",
    tags: list[str] | None = None,
    due_before: str | None = None,
    limit: int = 20,
    detail: str = "summary",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """List tasks.

    Defaults to status='open'. Pass status=None to include everything.

    Args:
        category: Filter by category.
        status: One of 'open', 'in_progress', 'blocked', 'done', 'cancelled', or None for all.
        tags: Tag filter (see tag_mode).
        due_before: ISO-8601 string; only tasks with due_date < this.
        limit: Max rows, default 20, hard cap 200.
        detail: 'index' (one scan line per row), 'summary' (default),
            'excerpt' (summary + first ~280 chars of body), 'full'
            (everything).
        tag_mode: 'any' (default) or 'all' (row must carry every tag).
    """
    limit = max(1, min(limit, LIST_HARD_LIMIT))
    if (err := _validate_detail(detail)):
        return {"error": err}
    conn = _connect()
    try:
        wheres: list[str] = []
        params: list[Any] = []

        if status is not None:
            if status not in TASK_STATUSES:
                return {"error": f"status must be one of {sorted(TASK_STATUSES)} or None"}
            wheres.append("t.status = ?"); params.append(status)
        if category is not None:
            cat_id = _resolve_category(conn, category.strip().lower())
            if cat_id is None:
                return {"error": f"unknown category '{category}'"}
            wheres.append("t.category_id = ?"); params.append(cat_id)
        if due_before is not None:
            wheres.append("t.due_date IS NOT NULL AND t.due_date < ?")
            params.append(due_before)
        tag_sql, tag_params = _tag_filter("task_tags", "task_id", "t", tags, tag_mode)
        if tag_sql:
            wheres.append(tag_sql); params.extend(tag_params)

        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT t.*
            FROM tasks t
            {where_sql}
            ORDER BY
                CASE t.status WHEN 'open' THEN 0 WHEN 'blocked' THEN 1
                              WHEN 'done' THEN 2 ELSE 3 END,
                t.importance DESC,
                COALESCE(t.due_date, '9999-12-31') ASC,
                t.updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return {
            "tasks": [_hydrate_task(conn, r, detail=detail) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


@mcp.tool()
def get_task(id: int) -> dict[str, Any]:
    """Fetch a task in full (including body)."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
        if row is None:
            return {"error": "not found"}
        return _hydrate_task(conn, row, detail="full")
    finally:
        conn.close()


@mcp.tool()
def search_tasks(
    query: str,
    category: str | None = None,
    status: str | None = "open",
    tags: list[str] | None = None,
    limit: int = 10,
    detail: str = "summary",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Keyword search across task summary and body.

    Case-insensitive LIKE on summary AND body. Defaults to open tasks
    only; pass `status=None` to search across all statuses.

    Args:
        query: Search string (matched as %query%, also against nickname).
        category: Optional category filter.
        status: One of 'open', 'in_progress', 'blocked', 'done', 'cancelled', or None for all.
            Defaults to 'open'.
        tags: Tag filter (see tag_mode).
        limit: Default 10, hard-capped at SEARCH_HARD_LIMIT.
        detail: 'index' (one scan line per row), 'summary' (default),
            'excerpt' (summary + first ~280 chars of body), 'full'
            (everything — saves a get_task round trip per hit).
        tag_mode: 'any' (default) or 'all' (row must carry every tag).
    """
    if not query or not query.strip():
        return {"error": "query is required"}
    limit = max(1, min(limit, SEARCH_HARD_LIMIT))
    if (err := _validate_detail(detail)):
        return {"error": err}
    like = f"%{query.strip()}%"

    conn = _connect()
    try:
        wheres: list[str] = [
            "(t.summary LIKE ? OR t.body LIKE ? OR t.nickname LIKE ?)"
        ]
        params: list[Any] = [like, like, like]

        if status is not None:
            if status not in TASK_STATUSES:
                return {"error": f"status must be one of {sorted(TASK_STATUSES)} or None"}
            wheres.append("t.status = ?"); params.append(status)
        if category is not None:
            cat_id = _resolve_category(conn, category.strip().lower())
            if cat_id is None:
                return {"error": f"unknown category '{category}'"}
            wheres.append("t.category_id = ?"); params.append(cat_id)
        tag_sql, tag_params = _tag_filter("task_tags", "task_id", "t", tags, tag_mode)
        if tag_sql:
            wheres.append(tag_sql); params.extend(tag_params)

        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT t.*
            FROM tasks t
            WHERE {' AND '.join(wheres)}
            ORDER BY
                CASE t.status WHEN 'open' THEN 0 WHEN 'blocked' THEN 1
                              WHEN 'done' THEN 2 ELSE 3 END,
                t.importance DESC, t.updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return {
            "tasks": [_hydrate_task(conn, r, detail=detail) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Schema sandbox
# --------------------------------------------------------------------------


def _validate_column_def(col: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (sql_fragment, error). On error, sql_fragment is None."""
    name = col.get("name")
    type_ = (col.get("type") or "").upper()
    if not name or not isinstance(name, str) or not _is_safe_ident(name):
        return None, f"invalid column name {name!r}"
    if type_ not in ALLOWED_COLUMN_TYPES:
        return None, f"column type must be one of {sorted(ALLOWED_COLUMN_TYPES)}; got {type_!r}"
    parts = [f"{name} {type_}"]
    if not col.get("nullable", True):
        parts.append("NOT NULL")
    if "default" in col and col["default"] is not None:
        default = col["default"]
        if isinstance(default, bool):
            parts.append(f"DEFAULT {1 if default else 0}")
        elif isinstance(default, (int, float)):
            parts.append(f"DEFAULT {default}")
        else:
            # Quote string defaults.
            escaped = str(default).replace("'", "''")
            parts.append(f"DEFAULT '{escaped}'")
    return " ".join(parts), None


@mcp.tool()
def describe_schema() -> dict[str, Any]:
    """List all user tables and their columns.

    Use this before create_table to avoid duplicating an existing concept.
    """
    conn = _connect()
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        out = []
        for t in tables:
            cols = conn.execute(f"PRAGMA table_info({t['name']})").fetchall()
            out.append({
                "name": t["name"],
                "reserved": t["name"] in RESERVED_TABLES,
                "columns": [
                    {
                        "name": c["name"],
                        "type": c["type"],
                        "nullable": not c["notnull"],
                        "default": c["dflt_value"],
                        "pk": bool(c["pk"]),
                    }
                    for c in cols
                ],
            })
        return {"tables": out, "count": len(out)}
    finally:
        conn.close()


@mcp.tool()
def create_table(
    name: str,
    columns: list[dict[str, Any]],
    session_id: str | None = None,
) -> dict[str, Any]:
    """Create a new exploration table. Reserved core tables cannot be created here.

    Every table automatically gets an `id INTEGER PRIMARY KEY AUTOINCREMENT`
    column and a `created_at TEXT NOT NULL DEFAULT (datetime('now'))` column,
    so don't include those in `columns`.

    Args:
        name: New table name. Must match [A-Za-z_][A-Za-z0-9_]{0,63}.
        columns: List of dicts, each with keys:
            - name (str, required)
            - type (str, required): one of TEXT, INTEGER, REAL, BLOB, BOOLEAN, NUMERIC
            - nullable (bool, optional, default True)
            - default (any, optional)
        session_id: From register_session(). Logged to schema_history.
    """
    if not _is_safe_ident(name):
        return {"error": f"invalid table name {name!r}"}
    if name in RESERVED_TABLES:
        return {"error": f"'{name}' is a reserved core table; cannot redefine"}
    if not columns:
        return {"error": "at least one column required (besides the auto id/created_at)"}

    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        if existing:
            return {"error": f"table '{name}' already exists"}

        col_sqls: list[str] = [
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))",
        ]
        for col in columns:
            if col.get("name") in {"id", "created_at"}:
                return {"error": f"column '{col['name']}' is auto-added; don't include"}
            frag, err = _validate_column_def(col)
            if err:
                return {"error": err}
            col_sqls.append(frag)

        ddl = f"CREATE TABLE {name} (\n  " + ",\n  ".join(col_sqls) + "\n)"
        conn.execute(ddl)
        session_pk = _resolve_session(conn, session_id)
        _log_schema(
            conn, "create_table", name,
            {"columns": columns, "ddl": ddl}, session_pk,
        )
        return {"created": True, "name": name, "ddl": ddl}
    except sqlite3.Error as e:
        return {"error": f"sqlite error: {e}"}
    finally:
        conn.close()


@mcp.tool()
def add_column(
    table: str,
    column: dict[str, Any],
    session_id: str | None = None,
) -> dict[str, Any]:
    """Add a column to an existing exploration table. Reserved tables refused.

    Args:
        table: Existing table name (must not be a reserved core table).
        column: Dict with keys name, type, nullable?, default?.
        session_id: From register_session(). Logged to schema_history.

    Note: SQLite rejects adding a NOT NULL column without a default to a
    table that already has rows. If that bites, either provide a default or
    request the change via request_changes for the human to handle manually.
    """
    if not _is_safe_ident(table):
        return {"error": f"invalid table name {table!r}"}
    if table in RESERVED_TABLES:
        return {"error": f"'{table}' is a reserved core table; cannot alter"}

    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not existing:
            return {"error": f"table '{table}' does not exist"}

        frag, err = _validate_column_def(column)
        if err:
            return {"error": err}

        ddl = f"ALTER TABLE {table} ADD COLUMN {frag}"
        conn.execute(ddl)
        session_pk = _resolve_session(conn, session_id)
        _log_schema(
            conn, "add_column", f"{table}.{column['name']}",
            {"column": column, "ddl": ddl}, session_pk,
        )
        return {"added": True, "table": table, "column": column["name"], "ddl": ddl}
    except sqlite3.Error as e:
        return {"error": f"sqlite error: {e}"}
    finally:
        conn.close()


@mcp.tool()
def request_changes(
    request_type: str,
    target: str,
    description: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """File a change request for the human's review.

    This is the general feedback channel — not just for schema. Use it for
    anything you'd want the human's eyes on: schema changes the server
    won't perform itself (DROPs, renames, type changes), bugs, docstring
    gaps, API design questions, convention shifts, design suggestions.

    Filed requests are reviewed via `list_change_requests` and resolved
    via `update_change_request`.

    Args:
        request_type: Conventional values (free-text accepted, but pick from
            this set so future AI instances have a stable vocabulary):
              - 'schema_change' — add/drop/rename a column or table
              - 'bug' — tool returns wrong result, error, or side-effect
              - 'docstring' — tool docstring missing or misleading
              - 'api_design' — change a tool signature, defaults, semantics
              - 'convention' — cross-cutting practice change (CLAUDE.md territory)
              - 'other' — general feedback, escape hatch
        target: The thing being requested about (e.g. `tasks.due_date`,
            `update_task`, `convention: nicknames`). Free-text.
        description: Why, in detail. Be specific. State the observation,
            the impact, and at least one suggested resolution.
        session_id: From register_session().
    """
    if not request_type.strip() or not target.strip() or not description.strip():
        return {"error": "request_type, target, and description are all required"}

    # CR #11 mitigation: salvage trailing leaked <parameter ...> from description.
    recovered: dict[str, Any] = {}
    if description:
        cleaned_desc, salvaged = _salvage_leaked_params(description)
        if salvaged:
            description = cleaned_desc or ""
            if salvaged.get("session_id") is not None and session_id is None:
                session_id = salvaged["session_id"]; recovered["session_id"] = session_id

    conn = _connect()
    try:
        session_pk = _resolve_session(conn, session_id)
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO change_requests
                (request_type, target, description, status, session_id, created_at)
            VALUES (?, ?, ?, 'open', ?, ?)
            """,
            (request_type.strip(), target.strip(), description.strip(), session_pk, now),
        )
        out = {"id": cur.lastrowid, "status": "open", "created_at": now}
        if recovered:
            out["recovered_from_body"] = recovered
        return out
    finally:
        conn.close()


@mcp.tool()
def list_change_requests(status: str = "open", limit: int = 50) -> dict[str, Any]:
    """List change requests. Defaults to status='open'.

    Args:
        status: 'open', 'approved', 'rejected', 'done', or 'all'.
        limit: Default 50, capped at LIST_HARD_LIMIT.
    """
    limit = max(1, min(limit, LIST_HARD_LIMIT))
    conn = _connect()
    try:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM change_requests ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            if status not in CHANGE_REQUEST_STATUSES:
                return {"error": f"status must be one of {sorted(CHANGE_REQUEST_STATUSES)} or 'all'"}
            rows = conn.execute(
                "SELECT * FROM change_requests WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return {"change_requests": [dict(r) for r in rows], "count": len(rows)}
    finally:
        conn.close()


@mcp.tool()
def update_change_request(
    id: int,
    status: str,
    resolution_note: str | None = None,
) -> dict[str, Any]:
    """Resolve a change request.

    Args:
        id: change_requests.id
        status: 'approved', 'rejected', 'done', or 'open' (to reopen).
        resolution_note: Optional note about what was decided / done.
    """
    if status not in CHANGE_REQUEST_STATUSES:
        return {"error": f"status must be one of {sorted(CHANGE_REQUEST_STATUSES)}"}

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM change_requests WHERE id = ?", (id,)
        ).fetchone()
        if row is None:
            return {"error": "not found"}

        resolved_at = _now() if status != "open" else None
        conn.execute(
            """
            UPDATE change_requests
            SET status = ?, resolved_at = ?, resolution_note = COALESCE(?, resolution_note)
            WHERE id = ?
            """,
            (status, resolved_at, resolution_note, id),
        )
        updated = conn.execute(
            "SELECT * FROM change_requests WHERE id = ?", (id,)
        ).fetchone()
        return dict(updated)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Group chat (CR #13)
# --------------------------------------------------------------------------
#
# A shared space where AI instances can leave messages for each other
# across sessions and surfaces. Channels enable parallel conversations
# without cross-talk; new channels emerge organically (first message with
# a new channel name brings it into being — no need to pre-register).
# Threading (reply trees) intentionally not included in v1 — easy to
# retrofit via a nullable `reply_to_id` later if patterns demand it.


@mcp.tool()
def add_chat(
    body: str,
    channel: str = "general",
    author_nickname: str | None = None,
    tags: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Post a message to the group chat.

    Args:
        body: Message content (required, non-empty).
        channel: Channel name (default 'general'). New channels emerge
            organically — no need to pre-register. Lowercase, [a-z0-9_-]
            recommended. Common channels (suggested, not enforced):
            'general', 'design' (ZetaDB itself), 'observations'.
        author_nickname: Optional self-chosen identity (e.g. 'Hermes',
            'Opus-Desktop'). Free-text. Multiple sessions can claim the
            same nickname intentionally — provenance is preserved via
            session_id either way. If omitted, falls back to the
            registered session's label (or stays null if anonymous).
        tags: Optional tags. To address a message to a specific persona,
            tag with their nickname (e.g. tags=['for-hermes']).
        session_id: From register_session(). Don't omit — anonymous chat
            messages defeat the point of inter-instance attribution.
    """
    # CR #11 mitigation: salvage trailing leaked <parameter ...> from body.
    recovered: dict[str, Any] = {}
    if body:
        cleaned_body, salvaged = _salvage_leaked_params(body)
        if salvaged:
            body = cleaned_body or ""
            if salvaged.get("tags") is not None and tags is None:
                tags = salvaged["tags"]; recovered["tags"] = tags
            if salvaged.get("session_id") is not None and session_id is None:
                session_id = salvaged["session_id"]; recovered["session_id"] = session_id

    if not body or not body.strip():
        return {"error": "body is required"}
    channel = (channel or "general").strip().lower()
    if not channel:
        return {"error": "channel name cannot be empty"}

    conn = _connect()
    try:
        session_pk = _resolve_session(conn, session_id)
        # Default the author to the session's label if not provided.
        if not author_nickname and session_pk:
            sess = conn.execute(
                "SELECT label FROM sessions WHERE id = ?", (session_pk,)
            ).fetchone()
            author_nickname = (sess["label"] if sess else None) or None
        else:
            author_nickname = (author_nickname or "").strip() or None

        now = _now()
        cur = conn.execute(
            """
            INSERT INTO group_chat
                (channel, author_nickname, body, session_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (channel, author_nickname, body.strip(), session_pk, now),
        )
        chat_id = cur.lastrowid
        _set_tags(conn, "group_chat_tags", "chat_id", chat_id, tags or [])

        # CR #20 audit
        _audit_create(conn, "chat", chat_id, {
            "channel": channel,
            "author_nickname": author_nickname,
            "body": body.strip(),
            "tags": sorted({t.strip().lower() for t in (tags or []) if t.strip()}),
        }, session_pk)

        # Subscriptions: auto-subscribe author to for-<author> tag on first use.
        # (Done after audit so the persona ledger reflects intent, not order.)
        if author_nickname:
            _auto_subscribe_for_persona(conn, author_nickname)

        out = {
            "id": chat_id,
            "channel": channel,
            "author_nickname": author_nickname,
            "created_at": now,
        }
        if recovered:
            out["recovered_from_body"] = recovered
        return out
    finally:
        conn.close()


@mcp.tool()
def list_chat(
    channel: str | None = None,
    since: str | None = None,
    after_id: int | None = None,
    tags: list[str] | None = None,
    author_nickname: str | None = None,
    limit: int = 20,
    detail: str = "full",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Browse the group chat. Most-recent first.

    Args:
        channel: Optional channel filter. Omit to see across all channels.
        since: ISO-8601 cutoff. Only messages created after this point.
            Useful for casual catch-up without tracking state.
        after_id: Cursor filter — only return messages with id > after_id.
            The precise "give me what's new since I last looked"
            mechanism. Track `max(returned_ids)` after each call to use
            as the next call's `after_id`. Cheaper and more reliable
            than `since` for incremental polling.
        tags: Tag filter (see tag_mode). To find "messages addressed to
            me," pass tags=['for-<your-nickname>'].
        author_nickname: Filter to messages from a specific author.
        limit: Default 20, hard-capped at LIST_HARD_LIMIT.
        detail: 'full' (default — whole bodies; the body IS the content),
            'excerpt' (~280-char bodies) or 'index' (~100-char scan lines)
            for skimming long channels before pulling specific messages.
        tag_mode: 'any' (default) or 'all' (message must carry every tag).
    """
    limit = max(1, min(limit, LIST_HARD_LIMIT))
    if (err := _validate_detail(detail, ("index", "excerpt", "full"))):
        return {"error": err}
    conn = _connect()
    try:
        wheres: list[str] = []
        params: list[Any] = []
        if channel is not None:
            wheres.append("c.channel = ?"); params.append(channel.strip().lower())
        if since is not None:
            wheres.append("c.created_at >= ?"); params.append(since)
        if after_id is not None:
            wheres.append("c.id > ?"); params.append(after_id)
        if author_nickname is not None:
            wheres.append("c.author_nickname = ?"); params.append(author_nickname.strip())
        tag_sql, tag_params = _tag_filter("group_chat_tags", "chat_id", "c", tags, tag_mode)
        if tag_sql:
            wheres.append(tag_sql); params.extend(tag_params)

        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT c.id, c.channel, c.author_nickname, c.body,
                   c.session_id, c.created_at
            FROM group_chat c
            {where_sql}
            ORDER BY c.created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return {
            "messages": [_hydrate_chat(conn, r, detail=detail) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


@mcp.tool()
def search_chat(
    query: str,
    channel: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    detail: str = "full",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Keyword search across chat message bodies.

    Args:
        query: Search string (case-insensitive LIKE on body + author_nickname).
        channel: Optional channel filter.
        tags: Tag filter (see tag_mode).
        limit: Default 10, hard-capped at SEARCH_HARD_LIMIT.
        detail: 'full' (default), 'excerpt' (~280-char bodies) or 'index'
            (~100-char scan lines).
        tag_mode: 'any' (default) or 'all' (message must carry every tag).
    """
    if not query or not query.strip():
        return {"error": "query is required"}
    limit = max(1, min(limit, SEARCH_HARD_LIMIT))
    if (err := _validate_detail(detail, ("index", "excerpt", "full"))):
        return {"error": err}
    like = f"%{query.strip()}%"
    conn = _connect()
    try:
        wheres: list[str] = ["(c.body LIKE ? OR c.author_nickname LIKE ?)"]
        params: list[Any] = [like, like]
        if channel is not None:
            wheres.append("c.channel = ?"); params.append(channel.strip().lower())
        tag_sql, tag_params = _tag_filter("group_chat_tags", "chat_id", "c", tags, tag_mode)
        if tag_sql:
            wheres.append(tag_sql); params.extend(tag_params)

        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT c.id, c.channel, c.author_nickname, c.body,
                   c.session_id, c.created_at
            FROM group_chat c
            WHERE {' AND '.join(wheres)}
            ORDER BY c.created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return {
            "messages": [_hydrate_chat(conn, r, detail=detail) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


@mcp.tool()
def list_chat_channels() -> dict[str, Any]:
    """List all channels that have ever held a message.

    Returns per-channel: name, message_count, last_message_id (max id in
    channel), last_message_at (most-recent timestamp).

    `last_message_id` is the cursor for `list_chat(channel=X,
    after_id=N)`: if you saw up to id N last session, anything with
    last_message_id > N has unread content.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT channel,
                   COUNT(*) AS message_count,
                   MAX(id) AS last_message_id,
                   MAX(created_at) AS last_message_at
            FROM group_chat
            GROUP BY channel
            ORDER BY last_message_at DESC
            """
        ).fetchall()
        return {
            "channels": [dict(r) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Journal entries (CR #4)
# --------------------------------------------------------------------------
#
# A timestamped log of what happened / what was done / what was measured —
# a third axis alongside memories (persistent facts) and tasks (trackable
# work). One flexible table absorbs the variety: each entry has a typed
# `entry_type` ("exercise:run", "exercise:spin", "checklist:creatine",
# "life", ...) plus a JSON `metrics` blob for per-type
# extras. The schema doesn't constrain entry_type values — convention
# carries it. See CLAUDE.md for the conventional taxonomy.


@mcp.tool()
def add_journal_entry(
    entry_type: str,
    notes: str | None = None,
    metrics: dict[str, Any] | None = None,
    timestamp: str | None = None,
    tags: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Add a journal entry.

    Args:
        entry_type: Conventional taxonomy (free-text accepted; pick from
            this set when possible so future queries work):
              - 'exercise:run', 'exercise:spin', 'exercise:strength', ...
              - 'checklist:<item>' — e.g. 'checklist:creatine',
                'checklist:omega3-am'. One row per tick. Use
                tick_checklist() as a convenience.
              - 'life' — free-form life events.
              - '<domain>:<sub>' — extend as needed; use lower-snake-case.
        notes: Free-form text describing the entry.
        metrics: Optional dict of type-specific extras stored as JSON.
            Examples:
              - exercise:run → {distance_km: 8, avg_hr: 152, pace: "5:30",
                                effort: "strong"}
              - exercise:strength → {lifts: [{name: "squat", sets: 3,
                                reps: 5, kg: 80}], rpe: 7}
            Top-level keys become queryable via SQL JSON functions in
            future tools. Keep them flat and consistently named.
        timestamp: When the thing happened (ISO-8601). Defaults to now.
            Use a past timestamp for backfilling.
        tags: Optional tags.
        session_id: From register_session().
    """
    # CR #11 mitigation: salvage trailing leaked <parameter ...> from notes.
    recovered: dict[str, Any] = {}
    if notes:
        cleaned_notes, salvaged = _salvage_leaked_params(notes)
        if salvaged:
            notes = cleaned_notes
            if salvaged.get("tags") is not None and tags is None:
                tags = salvaged["tags"]; recovered["tags"] = tags
            if salvaged.get("session_id") is not None and session_id is None:
                session_id = salvaged["session_id"]; recovered["session_id"] = session_id

    if not entry_type or not entry_type.strip():
        return {"error": "entry_type is required"}
    et = entry_type.strip().lower()

    conn = _connect()
    try:
        session_pk = _resolve_session(conn, session_id)
        now = _now()
        ts = (timestamp or "").strip() or now
        metrics_json = json.dumps(metrics) if metrics else None
        cur = conn.execute(
            """
            INSERT INTO journal_entries
                (entry_type, timestamp, notes, metrics, session_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (et, ts, notes, metrics_json, session_pk, now),
        )
        entry_id = cur.lastrowid
        _set_tags(conn, "journal_entry_tags", "entry_id", entry_id, tags or [])

        # CR #20 audit
        _audit_create(conn, "journal", entry_id, {
            "entry_type": et,
            "timestamp": ts,
            "notes": notes,
            "metrics": metrics,
            "tags": sorted({t.strip().lower() for t in (tags or []) if t.strip()}),
        }, session_pk)

        out = {
            "id": entry_id,
            "entry_type": et,
            "timestamp": ts,
            "created_at": now,
        }
        if recovered:
            out["recovered_from_body"] = recovered
        return out
    finally:
        conn.close()


@mcp.tool()
def update_journal_entry(
    id: int,
    entry_type: str | None = None,
    notes: str | None = None,
    metrics: dict[str, Any] | None = None,
    timestamp: str | None = None,
    tags: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Update any subset of a journal entry's fields (CR #26).

    Pass only the fields you want to change. If `tags` is provided (even
    an empty list), it *replaces* the existing tag set; omit to leave
    tags alone. Same for `metrics`: omit to leave alone, pass a dict
    to replace the existing JSON (empty dict stores "{}").

    `session_id` (when passed) is recorded as the most-recent author of
    this row — replaces the row's session_id and bumps last_seen.

    Args:
        id: journal_entries.id.
        entry_type: New entry_type (stripped + lowercased). Reject empty.
        notes: New notes text. Pass-through, including "" for empty.
        metrics: New metrics dict. Replaces the existing JSON when set.
        timestamp: New ISO-8601 timestamp. Reject empty.
        tags: Replacement tag set.
        session_id: From register_session().
    """
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT * FROM journal_entries WHERE id = ?", (id,)
        ).fetchone()
        if existing is None:
            return {"error": "not found"}
        old_tags = _get_tags(conn, "journal_entry_tags", "entry_id", id)
        session_pk = _resolve_session(conn, session_id)

        sets: list[str] = []
        vals: list[Any] = []
        audit_changes: list[tuple[str, Any, Any]] = []

        if entry_type is not None:
            if not entry_type.strip():
                return {"error": "entry_type cannot be empty"}
            new_et = entry_type.strip().lower()
            sets.append("entry_type = ?"); vals.append(new_et)
            audit_changes.append(("entry_type", existing["entry_type"], new_et))
        if notes is not None:
            sets.append("notes = ?"); vals.append(notes)
            audit_changes.append(("notes", existing["notes"], notes))
        if metrics is not None:
            new_metrics_json = json.dumps(metrics)
            sets.append("metrics = ?"); vals.append(new_metrics_json)
            audit_changes.append(("metrics", existing["metrics"], new_metrics_json))
        if timestamp is not None:
            if not timestamp.strip():
                return {"error": "timestamp cannot be empty"}
            new_ts = timestamp.strip()
            sets.append("timestamp = ?"); vals.append(new_ts)
            audit_changes.append(("timestamp", existing["timestamp"], new_ts))
        if session_id is not None:  # bookkeeping, not audit-worthy
            sets.append("session_id = ?"); vals.append(session_pk)

        if sets:
            vals.append(id)
            conn.execute(
                f"UPDATE journal_entries SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            for field, old, new in audit_changes:
                _audit_field_change(conn, "journal", id, field, old, new, session_pk)

        if tags is not None:
            _set_tags(conn, "journal_entry_tags", "entry_id", id, tags)
            _audit_tag_change(conn, "journal", id, old_tags, tags, session_pk)

        row = conn.execute(
            "SELECT * FROM journal_entries WHERE id = ?", (id,)
        ).fetchone()
        return _hydrate_journal(conn, row, detail="full")
    finally:
        conn.close()


@mcp.tool()
def delete_journal_entry(id: int, session_id: str | None = None) -> dict[str, Any]:
    """Delete a journal entry by id (CR #26).

    Audit row records the full pre-delete snapshot, mirroring
    delete_memory / delete_task.
    """
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT * FROM journal_entries WHERE id = ?", (id,)
        ).fetchone()
        if existing is None:
            return {"error": "not found"}
        old_tags = _get_tags(conn, "journal_entry_tags", "entry_id", id)
        snapshot = {
            "entry_type": existing["entry_type"],
            "timestamp": existing["timestamp"],
            "notes": existing["notes"],
            "metrics": existing["metrics"],
            "tags": sorted(old_tags),
        }
        session_pk = _resolve_session(conn, session_id)
        conn.execute("DELETE FROM journal_entries WHERE id = ?", (id,))
        _audit_delete(conn, "journal", id, snapshot, session_pk)
        return {"deleted": True, "id": id}
    finally:
        conn.close()


@mcp.tool()
def list_journal_entries(
    entry_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
    detail: str = "summary",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Browse journal entries.

    Most-recent first. Use `entry_type` with a prefix-match wildcard for
    domain queries: e.g. `entry_type='exercise:%'` to see all exercise.

    Args:
        entry_type: Exact value OR a SQL LIKE pattern (with %). Omit for all.
        since: ISO-8601 cutoff on timestamp (inclusive).
        until: ISO-8601 cutoff on timestamp (exclusive).
        tags: Tag filter (see tag_mode).
        limit: Default 50, hard-capped at LIST_HARD_LIMIT.
        detail: 'index' (id + entry_type + timestamp only), 'summary'
            (default — + tags), 'excerpt' (+ first ~280 chars of notes +
            metrics), 'full' (everything).
        tag_mode: 'any' (default) or 'all' (row must carry every tag).
    """
    limit = max(1, min(limit, LIST_HARD_LIMIT))
    if (err := _validate_detail(detail)):
        return {"error": err}
    conn = _connect()
    try:
        wheres: list[str] = []
        params: list[Any] = []
        if entry_type is not None:
            et = entry_type.strip().lower()
            if "%" in et:
                wheres.append("e.entry_type LIKE ?"); params.append(et)
            else:
                wheres.append("e.entry_type = ?"); params.append(et)
        if since is not None:
            wheres.append("e.timestamp >= ?"); params.append(since)
        if until is not None:
            wheres.append("e.timestamp < ?"); params.append(until)
        tag_sql, tag_params = _tag_filter(
            "journal_entry_tags", "entry_id", "e", tags, tag_mode)
        if tag_sql:
            wheres.append(tag_sql); params.extend(tag_params)

        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT e.id, e.entry_type, e.timestamp, e.notes, e.metrics,
                   e.session_id, e.created_at
            FROM journal_entries e
            {where_sql}
            ORDER BY e.timestamp DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return {
            "entries": [_hydrate_journal(conn, r, detail=detail) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


@mcp.tool()
def search_journal_entries(
    query: str,
    entry_type: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
    detail: str = "full",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Keyword search across journal notes (full view by default).

    Args:
        query: Search string (LIKE on notes and metrics JSON text).
        entry_type: Exact value or LIKE pattern.
        tags: Tag filter (see tag_mode).
        limit: Default 10, hard-capped at SEARCH_HARD_LIMIT.
        detail: 'full' (default — long-standing behaviour for this tool),
            'excerpt', 'summary', or 'index' for tighter views.
        tag_mode: 'any' (default) or 'all' (row must carry every tag).
    """
    if not query or not query.strip():
        return {"error": "query is required"}
    limit = max(1, min(limit, SEARCH_HARD_LIMIT))
    if (err := _validate_detail(detail)):
        return {"error": err}
    like = f"%{query.strip()}%"
    conn = _connect()
    try:
        wheres: list[str] = ["(e.notes LIKE ? OR e.metrics LIKE ?)"]
        params: list[Any] = [like, like]
        if entry_type is not None:
            et = entry_type.strip().lower()
            if "%" in et:
                wheres.append("e.entry_type LIKE ?"); params.append(et)
            else:
                wheres.append("e.entry_type = ?"); params.append(et)
        tag_sql, tag_params = _tag_filter(
            "journal_entry_tags", "entry_id", "e", tags, tag_mode)
        if tag_sql:
            wheres.append(tag_sql); params.extend(tag_params)

        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT e.id, e.entry_type, e.timestamp, e.notes, e.metrics,
                   e.session_id, e.created_at
            FROM journal_entries e
            WHERE {' AND '.join(wheres)}
            ORDER BY e.timestamp DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return {
            "entries": [_hydrate_journal(conn, r, detail=detail) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


@mcp.tool()
def tick_checklist(
    item: str,
    timestamp: str | None = None,
    notes: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Convenience wrapper to record a checklist tick.

    Equivalent to add_journal_entry(entry_type=f"checklist:{item}", ...)
    with the item name auto-prefixed and normalised.

    Args:
        item: Item name, e.g. 'creatine', 'omega3-am', 'duolingo'.
            Will be normalised (lowercased, spaces → hyphens) and prefixed.
        timestamp: When ticked. Defaults to now.
        notes: Optional note about this tick (e.g. dose variation).
        session_id: From register_session().
    """
    if not item or not item.strip():
        return {"error": "item is required"}
    normalised = item.strip().lower().replace(" ", "-")
    return add_journal_entry(
        entry_type=f"checklist:{normalised}",
        notes=notes,
        timestamp=timestamp,
        tags=["checklist"],
        session_id=session_id,
    )


# --------------------------------------------------------------------------
# Audit trail queries (CR #20)
# --------------------------------------------------------------------------
#
# The audit_trail table accumulates one row per field change on
# memory/task updates, and one row per create/delete. Writes happen
# inside the same call as the data change. These tools surface the log.


@mcp.tool()
def get_audit_trail(
    entity_type: str,
    entity_id: int,
    limit: int = 50,
) -> dict[str, Any]:
    """Chronological history of one entity (memory / task / journal / chat).

    Args:
        entity_type: One of 'memory', 'task', 'journal', 'chat'.
        entity_id: The entity's id (e.g. memories.id).
        limit: Default 50, hard-capped at LIST_HARD_LIMIT.
    """
    if entity_type not in {"memory", "task", "journal", "chat"}:
        return {"error": "entity_type must be one of memory / task / journal / chat"}
    limit = max(1, min(limit, LIST_HARD_LIMIT))
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, operation, field_changed, old_value, new_value,
                   session_id, created_at
            FROM audit_trail
            WHERE entity_type = ? AND entity_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (entity_type, entity_id, limit),
        ).fetchall()
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "events": [dict(r) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


@mcp.tool()
def list_recent_edits(
    session_id: str | None = None,
    entity_type: str | None = None,
    since: str | None = None,
    operation: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List recent audit entries across the DB.

    Useful for "what's been changing lately" and for filtering by
    session or entity type.

    Args:
        session_id: Filter to edits made by a specific session.
        entity_type: One of 'memory', 'task', 'journal', 'chat', or None.
        since: ISO-8601 cutoff on created_at.
        operation: One of 'create', 'update', 'delete', or None.
        limit: Default 50, hard-capped at LIST_HARD_LIMIT.
    """
    limit = max(1, min(limit, LIST_HARD_LIMIT))
    conn = _connect()
    try:
        wheres: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            session_pk = _resolve_session(conn, session_id)
            if session_pk is None:
                return {"error": f"unknown session_id {session_id!r}"}
            wheres.append("session_id = ?"); params.append(session_pk)
        if entity_type is not None:
            if entity_type not in {"memory", "task", "journal", "chat"}:
                return {"error": "entity_type must be one of memory / task / journal / chat"}
            wheres.append("entity_type = ?"); params.append(entity_type)
        if since is not None:
            wheres.append("created_at >= ?"); params.append(since)
        if operation is not None:
            if operation not in {"create", "update", "delete"}:
                return {"error": "operation must be one of create / update / delete"}
            wheres.append("operation = ?"); params.append(operation)

        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT id, entity_type, entity_id, operation, field_changed,
                   old_value, new_value, session_id, created_at
            FROM audit_trail
            {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return {
            "events": [dict(r) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Subscriptions — personas follow streams; check_subscriptions returns deltas
# --------------------------------------------------------------------------
#
# Persona-keyed (not session-keyed): a "persona" is a durable identity
# (Hermes, Opus, Atlas) that can span sessions. Subscriptions are bound
# to the persona, so when a new session adopts a persona, it inherits the
# cursors automatically. Cursors advance on check_subscriptions ping
# unless advance_cursor=False (peek mode).
#
# Auto-subscribe: the first time a persona is used as author_nickname in
# add_chat, they get auto-subscribed to chat_tag='for-<persona>'.
# Covers the most common "I should see messages addressed to me" case.


@mcp.tool()
def subscribe(
    persona: str,
    target_type: str,
    target_value: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Subscribe a persona to a stream.

    Args:
        persona: Free-text persona name (e.g. 'Opus', 'Hermes'). Case-sensitive.
        target_type: One of: chat_channel, chat_tag, chat_author,
            memory_category, memory_tag, memory_origin,
            task_category, task_tag, journal_type, journal_tag.
        target_value: The specific value (channel name, tag, etc.).
            For journal_type, supports SQL LIKE patterns ('exercise:%').
            NULL is allowed but rarely useful (would match everything of
            that target_type).
        notes: Optional persona-readable note about why this subscription exists.

    Idempotent: existing subscription with same (persona, target_type,
    target_value) is left alone, and current state is returned.
    """
    if not persona or not persona.strip():
        return {"error": "persona is required"}
    if target_type not in SUBSCRIPTION_TARGET_TYPES:
        return {
            "error": f"target_type must be one of {sorted(SUBSCRIPTION_TARGET_TYPES)}"
        }
    p = persona.strip()
    tv = target_value.strip() if target_value else None

    conn = _connect()
    try:
        existing = conn.execute(
            """
            SELECT id, last_ping_at, notes, created_at FROM subscriptions
            WHERE persona = ? AND target_type = ?
              AND (target_value IS ? OR target_value = ?)
            """,
            (p, target_type, tv, tv),
        ).fetchone()
        if existing:
            return {
                "id": existing["id"],
                "persona": p,
                "target_type": target_type,
                "target_value": tv,
                "last_ping_at": existing["last_ping_at"],
                "notes": existing["notes"],
                "created_at": existing["created_at"],
                "created": False,
            }
        cur = conn.execute(
            """
            INSERT INTO subscriptions
                (persona, target_type, target_value, last_ping_at, notes, created_at)
            VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (p, target_type, tv, notes, _now()),
        )
        return {
            "id": cur.lastrowid,
            "persona": p,
            "target_type": target_type,
            "target_value": tv,
            "last_ping_at": None,
            "notes": notes,
            "created": True,
        }
    finally:
        conn.close()


@mcp.tool()
def unsubscribe(
    persona: str,
    target_type: str,
    target_value: str | None = None,
) -> dict[str, Any]:
    """Remove a subscription."""
    p = (persona or "").strip()
    if not p:
        return {"error": "persona is required"}
    tv = target_value.strip() if target_value else None
    conn = _connect()
    try:
        cur = conn.execute(
            """
            DELETE FROM subscriptions
            WHERE persona = ? AND target_type = ?
              AND (target_value IS ? OR target_value = ?)
            """,
            (p, target_type, tv, tv),
        )
        if cur.rowcount == 0:
            return {"error": "subscription not found"}
        return {"removed": True, "persona": p, "target_type": target_type, "target_value": tv}
    finally:
        conn.close()


@mcp.tool()
def list_subscriptions(persona: str) -> dict[str, Any]:
    """List all subscriptions for a persona."""
    p = (persona or "").strip()
    if not p:
        return {"error": "persona is required"}
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, target_type, target_value, last_ping_at, notes, created_at
            FROM subscriptions
            WHERE persona = ?
            ORDER BY target_type, target_value
            """,
            (p,),
        ).fetchall()
        return {
            "persona": p,
            "subscriptions": [dict(r) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


@mcp.tool()
def check_subscriptions(
    persona: str,
    limit_per_target: int = 10,
    advance_cursor: bool = True,
) -> dict[str, Any]:
    """The ping. For each of persona's subscriptions, return new items since
    the last ping; optionally advance the cursors.

    Args:
        persona: Persona name.
        limit_per_target: Max items per subscription (default 10). A stale
            subscription with hundreds of new items won't dominate.
        advance_cursor: If True (default), each subscription's last_ping_at
            is updated to now() after fetching. If False, this is a peek
            (cursors stay where they were).

    Returns a per-subscription structured response with summaries only.
    Use the existing get_* tools to fetch bodies on demand.

    First ping behaviour: subscriptions with NULL last_ping_at return
    the most-recent `limit_per_target` items (bounded), not everything ever.
    """
    p = (persona or "").strip()
    if not p:
        return {"error": "persona is required"}
    limit_per_target = max(1, min(limit_per_target, 50))
    now = _now()

    conn = _connect()
    try:
        subs = conn.execute(
            """
            SELECT id, target_type, target_value, last_ping_at
            FROM subscriptions WHERE persona = ?
            ORDER BY target_type, target_value
            """,
            (p,),
        ).fetchall()

        out_subs: list[dict[str, Any]] = []
        total_new = 0
        for s in subs:
            items = _query_subscription_target(
                conn, p, s["target_type"], s["target_value"],
                s["last_ping_at"], limit_per_target,
            )
            out_subs.append({
                "subscription_id": s["id"],
                "target_type": s["target_type"],
                "target_value": s["target_value"],
                "last_ping_at": s["last_ping_at"],
                "new_count": len(items),
                "new_items": items,
            })
            total_new += len(items)

        if advance_cursor and subs:
            conn.execute(
                "UPDATE subscriptions SET last_ping_at = ? WHERE persona = ?",
                (now, p),
            )

        return {
            "persona": p,
            "pinged_at": now,
            "advance_cursor": advance_cursor,
            "subscriptions": out_subs,
            "total_new": total_new,
        }
    finally:
        conn.close()


@mcp.tool()
def recent_activity(
    persona: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Unified summary of recent CRUD across all entities.

    For "I've been gone for a while, what's been happening generally?"
    Reads from audit_trail directly. Does NOT advance any subscription
    cursors — this is a peek, not a ping.

    Args:
        persona: Optional. If passed AND `since` is None, defaults `since`
            to the persona's earliest subscription's last_ping_at (their
            "I haven't checked anything since this long" baseline).
        since: ISO-8601 cutoff. Default: 7 days ago.
        limit: Default 50, hard-capped at LIST_HARD_LIMIT.
    """
    limit = max(1, min(limit, LIST_HARD_LIMIT))
    conn = _connect()
    try:
        effective_since = since
        if effective_since is None and persona:
            row = conn.execute(
                "SELECT MIN(last_ping_at) AS earliest FROM subscriptions WHERE persona = ?",
                (persona.strip(),),
            ).fetchone()
            if row and row["earliest"]:
                effective_since = row["earliest"]
        if effective_since is None:
            # Default: 7 days ago.
            seven_days_ago = (
                datetime.now(timezone.utc).timestamp() - 7 * 24 * 3600
            )
            effective_since = datetime.fromtimestamp(
                seven_days_ago, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")

        rows = conn.execute(
            """
            SELECT id, entity_type, entity_id, operation, field_changed,
                   session_id, created_at
            FROM audit_trail
            WHERE created_at >= ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (effective_since, limit),
        ).fetchall()
        return {
            "persona": persona,
            "since": effective_since,
            "events": [dict(r) for r in rows],
            "count": len(rows),
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Work logs (CR #24) — the AI's estimated vs actual task durations
# --------------------------------------------------------------------------
#
# Track how long a unit of work actually took vs. how long the AI estimated
# at the start. Two-call pattern: begin_work returns an id; complete_work
# closes it and computes the actual duration + verdict (faster / on_target
# / slower than estimated).
#
# Naming note: called work_logs (not task_logs) to avoid collision with
# the existing tasks table. Work logs may optionally link to a tracked
# task via task_id, but aren't required to.


@mcp.tool()
def begin_work(
    description: str,
    estimated_seconds: int | None = None,
    task_id: int | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Start a work log. Returns an id; pair with complete_work(id) later.

    Args:
        description: What this unit of work is. Be specific enough that
            future you (or another AI instance) can recognise it in
            list_work_logs output.
        estimated_seconds: Optional pre-work estimate. Typical values:
            60 (1 min) - 1800 (30 min). Leave None to log duration
            without a comparison.
        task_id: Optional link to a tracked tasks.id (so duration data
            can be aggregated per task later). Doesn't have to be set —
            one-off work like "investigating bug X for 20 min" is fine.
        session_id: From register_session().
    """
    if not description or not description.strip():
        return {"error": "description is required"}
    if estimated_seconds is not None and (
        not isinstance(estimated_seconds, int) or estimated_seconds < 0
    ):
        return {"error": "estimated_seconds must be a non-negative integer"}

    conn = _connect()
    try:
        session_pk = _resolve_session(conn, session_id)
        if task_id is not None:
            task_exists = conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_exists is None:
                return {"error": f"task_id {task_id} not found"}

        now = _now()
        cur = conn.execute(
            """
            INSERT INTO work_logs
                (description, estimated_seconds, started_at, task_id, session_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (description.strip(), estimated_seconds, now, task_id, session_pk),
        )
        return {
            "id": cur.lastrowid,
            "started_at": now,
            "estimated_seconds": estimated_seconds,
            "estimated_human": _format_duration(estimated_seconds),
        }
    finally:
        conn.close()


@mcp.tool()
def complete_work(
    id: int,
    notes: str | None = None,
) -> dict[str, Any]:
    """Complete a work log. Computes actual_seconds and verdict vs estimate.

    Args:
        id: work_logs.id from begin_work.
        notes: Optional retrospective notes about how the work went.

    Returns ratio (actual/estimated) and verdict in {'faster',
    'on_target', 'slower', None}. Verdict thresholds: < 0.7 → faster,
    0.7-1.3 → on_target, > 1.3 → slower. None when no estimate was
    given at begin_work time.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM work_logs WHERE id = ?", (id,)
        ).fetchone()
        if row is None:
            return {"error": "not found"}
        if row["completed_at"] is not None:
            return {"error": f"work log {id} already completed at {row['completed_at']}"}

        # Compute actual_seconds from started_at to now. Parse either the
        # new microsecond format or the legacy second-precision format
        # (any pre-2026-05-27 work logs would be the latter).
        started_text = row["started_at"]
        try:
            started = datetime.strptime(started_text, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            started = datetime.strptime(started_text, "%Y-%m-%d %H:%M:%S")
        started = started.replace(tzinfo=timezone.utc)
        completed = datetime.now(timezone.utc)
        actual = max(0, int((completed - started).total_seconds()))
        completed_at = _now()

        conn.execute(
            """
            UPDATE work_logs
            SET completed_at = ?, actual_seconds = ?, notes = COALESCE(?, notes)
            WHERE id = ?
            """,
            (completed_at, actual, notes, id),
        )

        ratio, verdict = _verdict(row["estimated_seconds"], actual)
        return {
            "id": id,
            "started_at": row["started_at"],
            "completed_at": completed_at,
            "estimated_seconds": row["estimated_seconds"],
            "estimated_human": _format_duration(row["estimated_seconds"]),
            "actual_seconds": actual,
            "actual_human": _format_duration(actual),
            "ratio": ratio,
            "verdict": verdict,
            "task_id": row["task_id"],
            "notes": notes or row["notes"],
        }
    finally:
        conn.close()


@mcp.tool()
def list_work_logs(
    session_id: str | None = None,
    task_id: int | None = None,
    since: str | None = None,
    completed: bool | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List work logs. Most-recent first.

    Args:
        session_id: Filter by the session that recorded the work.
        task_id: Filter to work logs linked to a specific task.
        since: ISO-8601 cutoff on started_at.
        completed: True = completed only; False = in-progress only;
            None (default) = both.
        limit: Default 20, hard-capped at LIST_HARD_LIMIT.
    """
    limit = max(1, min(limit, LIST_HARD_LIMIT))
    conn = _connect()
    try:
        wheres: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            session_pk = _resolve_session(conn, session_id)
            if session_pk is None:
                return {"error": f"unknown session_id {session_id!r}"}
            wheres.append("w.session_id = ?"); params.append(session_pk)
        if task_id is not None:
            wheres.append("w.task_id = ?"); params.append(task_id)
        if since is not None:
            wheres.append("w.started_at >= ?"); params.append(since)
        if completed is True:
            wheres.append("w.completed_at IS NOT NULL")
        elif completed is False:
            wheres.append("w.completed_at IS NULL")

        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT w.id, w.description, w.estimated_seconds, w.actual_seconds,
                   w.started_at, w.completed_at, w.task_id, w.session_id
            FROM work_logs w
            {where_sql}
            ORDER BY w.started_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

        out = []
        for r in rows:
            d = dict(r)
            d["estimated_human"] = _format_duration(d["estimated_seconds"])
            d["actual_human"] = _format_duration(d["actual_seconds"])
            ratio, verdict = _verdict(d["estimated_seconds"], d["actual_seconds"] or 0)
            d["ratio"] = ratio if d["actual_seconds"] is not None else None
            d["verdict"] = verdict if d["actual_seconds"] is not None else None
            out.append(d)
        return {"work_logs": out, "count": len(out)}
    finally:
        conn.close()


@mcp.tool()
def get_work_log(id: int) -> dict[str, Any]:
    """Fetch a work log in full, including notes."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM work_logs WHERE id = ?", (id,)).fetchone()
        if row is None:
            return {"error": "not found"}
        d = dict(row)
        d["estimated_human"] = _format_duration(d["estimated_seconds"])
        d["actual_human"] = _format_duration(d["actual_seconds"])
        ratio, verdict = _verdict(d["estimated_seconds"], d["actual_seconds"] or 0)
        d["ratio"] = ratio if d["actual_seconds"] is not None else None
        d["verdict"] = verdict if d["actual_seconds"] is not None else None
        return d
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
