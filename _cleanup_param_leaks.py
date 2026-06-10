"""One-off maintenance: scrub legacy leaked <parameter> XML from memory bodies.

Before the CR #11 server-side salvage landed (2026-06-05), some memories were
written with trailing leaked `<parameter name="...">` XML absorbed into their
bodies. The salvage code only runs at write time, so those rows still carry
the garbage — visible at the end of body blocks in bulk_load_context output.

This script runs the same salvage helper against every existing memory body:

    # Dry run (default) — report what would change, touch nothing:
    .venv\\Scripts\\python.exe _cleanup_param_leaks.py

    # Apply — rewrite bodies via update_memory (audit rows + re-embed free):
    .venv\\Scripts\\python.exe _cleanup_param_leaks.py --apply [--session-id ID]

Tags salvaged from a leak are applied only when the row currently has no
tags (the leak means they were never set; a tagged row was tagged on
purpose since then). Other salvaged values are reported but not applied —
too ambiguous to guess retroactively.

Safe to run while the MCP server is up (WAL + busy_timeout). Safe to re-run:
a cleaned body has no trailing <parameter> run, so it no longer matches.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402  (reads ZETA_DB_PATH / .env like the real server)
from server import _connect, _get_tags, _salvage_leaked_params, update_memory  # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    session_id = None
    if "--session-id" in sys.argv:
        session_id = sys.argv[sys.argv.index("--session-id") + 1]

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, summary, body FROM memories "
            "WHERE body LIKE '%<parameter %' ORDER BY id"
        ).fetchall()
        candidates = []
        for r in rows:
            cleaned, salvaged = _salvage_leaked_params(r["body"])
            if salvaged:
                tags = _get_tags(conn, "memory_tags", "memory_id", r["id"])
                candidates.append((r, cleaned, salvaged, tags))
    finally:
        conn.close()

    print(f"DB: {server.DB_PATH}")
    print(f"Bodies containing '<parameter ': {len(rows)}; "
          f"with a salvageable trailing leak: {len(candidates)}\n")

    for r, cleaned, salvaged, tags in candidates:
        print(f"#{r['id']}  {r['summary'][:80]}")
        tail = r["body"][len(cleaned or ""):].strip()
        print(f"  would strip {len(tail)} chars of trailing XML: "
              f"{tail[:120]!r}{'...' if len(tail) > 120 else ''}")
        print(f"  salvaged params: { {k: str(v)[:60] for k, v in salvaged.items()} }")
        will_tag = "tags" in salvaged and not tags
        if will_tag:
            print(f"  row has no tags -> would apply salvaged tags {salvaged['tags']}")
        if apply:
            kwargs = {"body": cleaned, "session_id": session_id}
            if will_tag:
                kwargs["tags"] = salvaged["tags"]
            result = update_memory(r["id"], **kwargs)
            ok = "error" not in result
            print(f"  APPLIED: {'ok' if ok else result}")
        print()

    if not apply and candidates:
        print("Dry run only. Re-run with --apply to rewrite.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
