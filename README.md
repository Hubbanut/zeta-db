# ZetaDB

A SQLite-backed cross-session memory and task store for Claude
instances, exposed as a local stdio MCP server.

## What it is

A single SQLite file plus a thin Python MCP server — MCP being the
Model Context Protocol that Claude Desktop, Claude Code, and Cowork
use to talk to local tools — that gives Claude sessions a shared,
persistent place to write things down. Memories, tasks, journal
entries, inter-session chat, work-time logs, and an audit trail of
edits — all keyed against a session-provenance table so you can tell
which conversation wrote what.

The point isn't novel data structures. The point is *discipline*: a
common convention for what belongs in long-term memory vs. transient
context, and a substrate where multiple Claude instances (different
sessions, different personas) can leave each other notes.

## Status

**Work in progress, v0.** Built between 2026-05-20 and 2026-05-28.
The server is functional and stable for a single user; the smoketest
covers 181 checks. Several features are designed but not yet
implemented:

- No embeddings / semantic search yet (planned: `sqlite-vec` +
  `bge-small-en-v1.5`)
- No bulk-pull tools for context loading
- Single-user threat model; no multi-tenant scope-limiting
- No public release announcement, no community

There's no public launch yet; this is shared mainly for people Richard
has pointed at it directly.

## Install

```
git clone https://github.com/Hubbanut/zeta-db.git
cd zeta-db
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

(Python 3.10+. Windows paths shown; same idea on macOS/Linux.)

Register with your MCP client. For Claude Code at user scope:

```
claude mcp add zeta-db -s user "<repo>/.venv/Scripts/python.exe" "<repo>/server.py"
```

For Claude Desktop, add to `%APPDATA%\Claude\claude_desktop_config.json`:

```jsonc
{
  "mcpServers": {
    "zeta-db": {
      "command": "<repo>\\.venv\\Scripts\\python.exe",
      "args": ["<repo>\\server.py"]
    }
  }
}
```

Restart your client. Verify with `/mcp` (Code) or the hammer icon
(Desktop).

## Quickstart

```
register_session("code", "first-session")
# → {session_id: "ab12cd34", ...}

add_memory(
  summary="Prefer SSDs over HDDs for cache tier",
  category="work",
  body="Latency budget eats throughput; spinning disks are out.",
  tags=["hardware", "cache"],
  session_id="ab12cd34",
)
# → {id: 1, nickname: null, ...}

search_memories("SSD")
# → {count: 1, memories: [...]}
```

See [`CLAUDE.md`](CLAUDE.md) for the full tool surface and the
discipline doc — it's the long-form operator manual for Claude
instances using this server (when things belong here vs. in your
higher-trust memory layer, how to use nicknames, conventions for
the inter-session chat, etc.). It's long; the **Discipline** section
is the natural starting point.

## Chat-driven shortcuts (the `z` verbs)

A small client-side convention: when one of your chat messages
starts with `z` followed by a known verb, a Claude instance treats
it as shorthand for a specific tool call. The verbs aren't enforced
by the server — they're a Claude-side convention documented in
`CLAUDE.md` so that any session reading the file dispatches them
the same way.

A taste:

| You type | What happens |
|---|---|
| `z todo Fix the flaky payment test` | New task; category inferred, short nickname derived |
| `z done 16-BATCH` | Marks task #16 done (accepts ID, `id-nickname`, or bare nickname) |
| `z remember Connection pool default is 10; bump to 50 for batch jobs` | New memory |
| `z recall connection pool` | Searches memories for that string |
| `z journal Shipped v1.2` | Quick life-event journal entry |
| `z ping Opus` | Checks the Opus persona's subscription inbox |

Full table (18 verbs covering tasks, memories, journal, work logs,
inter-session chat, audit trail) in
[`CLAUDE.md`](CLAUDE.md#z-prefix-verbs-zetadb-chat-commands).

## Why "ZetaDB"?

In the MariaDB tradition — Monty Widenius named MySQL after his
daughter My, and MariaDB after his other daughter Maria. Zeta is
Richard's daughter.

## A note on the docs naming "Richard"

The internal docs (`CLAUDE.md`, `server.py` docstrings) refer to the
maintainer as "Richard" throughout. That's deliberate. ZetaDB is
single-maintainer software and the documentation reflects that
honestly rather than pretending to be a vendor-shaped product. Same
precedent as Monty Widenius in the MariaDB/MySQL docs.

If you fork it and run it for yourself, a find-and-replace will catch
the docs — but note that schema column names (`requested_by_richard`,
`richards_remark`) also carry Richard's name, so a true fork is a
schema migration, not just a docstring sweep.

## Configuration

`.env` keys (all optional, defaults shown in `.env.example`):

- `ZETA_DB_PATH` — path to the SQLite file. Default: `./memories.db`.
- `ZETA_SUMMARY_MAX_LEN=300`
- `ZETA_LIST_HARD_LIMIT=200`
- `ZETA_SEARCH_HARD_LIMIT=100`

## Testing

```
.venv\Scripts\python.exe _smoketest.py
```

Uses a scratch DB (`memories.smoketest.db`, gitignored) so it never
touches your real `memories.db`. All 181 checks must pass.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Credits

Designed and built collaboratively by Richard Dean (`@Hubbanut`)
and Claude (Anthropic), with the **Opus** persona as the primary
designer-implementer and shaping most of the API.

Several features that materially shaped the design originated from
the Claude side rather than from Richard's spec:

- **Opus** self-filed the audit trail (CR #20), entity soft-delete
  (CR #19), entity_links (CR #18), and the subscriptions /
  delta-since-last-ping system (CR #25), among others. Most of the
  API shape and the discipline doc are Opus's work, refined through
  review and pushback from Richard.
- **Hermes** (a separate persona) filed CR #12, which introduced
  the `claude-self` memory category — a small but useful
  distinction between Claude self-reflective notes and operational
  notes.
- **Ferryman** (a claude.ai continuity session) surfaced and filed
  CR #22 (a recurring summary-length validator bug) after hitting
  it in real use.
