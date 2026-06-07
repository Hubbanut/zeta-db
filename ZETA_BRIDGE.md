# ZetaDB ⇄ Web-Session Bridge (manual courier)

**Why this exists.** The Claude Code *web* session runs in an ephemeral cloud
container and cannot reach the ZetaDB MCP server running locally on the PC.
This doc is the manual relay: a Desktop session (which *has* the tools) exports
context for the web session to work on, and writes back the changes the web
session proposes. Continuity lives in ZetaDB, not in any one chat thread — this
just shuttles state between two sessions that can't see each other.

**The loop:**
1. **EXPORT** — paste Prompt 1 into a Desktop session → it dumps the relevant
   records as a markdown block → you copy that block into the web session.
2. **WORK** — web session reasons over it, proposes new/updated records in the
   write-back format below.
3. **WRITE-BACK** — paste Prompt 2 (with the web session's proposed block) into
   a Desktop session → it executes the writes with correct provenance →
   it reports the assigned IDs/nicknames → you bring those back so the web
   session stays in sync.

Keep it lossless: always carry the **IDs and nicknames** across the bridge so
both sides refer to the same records (`#42-ROYAL`, not "that task about
ordering").

---

## PROMPT 1 — Export (paste into Desktop, bring its output back to web)

> You have the ZetaDB MCP tools. First `register_session(client="desktop",
> label="royal-bridge-export")` and keep the `session_id`. Then assemble an
> export of everything relevant to my work for **Royal** (grocery retail / AI
> implementation). Pull from:
>
> - `search_memories` for: `royal`, `grocery`, `retail`, `inventory`,
>   `ordering`, `pricing`, `markdown`, `supplier`, plus `list_memories(
>   category="work")` — dedupe by id.
> - `list_tasks(status=None)` filtered to the same theme (open *and* done, so
>   the web session sees what's already handled).
> - `search_journal_entries` / `list_journal_entries` for any `release`-type or
>   `life`-type entries tied to Royal rollouts.
> - `list_chat` in any channel where Royal/grocery came up, plus
>   `list_chat(tags=["for-<my-persona>"])` if I've adopted one.
>
> Output as a single fenced markdown block I can copy whole. For each record use
> this compact shape, and **do not** paraphrase summaries — copy them verbatim:
>
> ```
> ### MEMORIES
> - [#<id>-<nickname>] (cat:<category>, imp:<1-5>, by_human:<y/n>) <summary>
>   body: <body or "—">
>   tags: <comma list>  origin: <origin or "—">  last_accessed: <date>
>
> ### TASKS
> - [#<id>-<nickname>] (cat:<category>, status:<status>, due:<date or —>) <summary>
>   body: <body or "—">
>
> ### JOURNAL (Royal-relevant)
> - [<id>] <timestamp> <entry_type>: <notes>  metrics: <json or —>
>
> ### CHAT (Royal-relevant)
> - [<id>] <channel> / <author> @ <created_at>: <body>
> ```
>
> If nothing matches a section, write "none". Keep it to what's genuinely about
> Royal — don't dump the whole DB.

---

## PROMPT 2 — Write-back (paste into Desktop *with* the web session's proposed block)

> You have the ZetaDB MCP tools. `register_session(client="desktop",
> label="royal-bridge-writeback")` and keep the `session_id`; pass it on every
> write. Below is a block of proposed changes from another session. Execute them
> exactly, then report back the assigned IDs and nicknames for every new record.
>
> Rules:
> - **ADD** lines → `add_memory` / `add_task` with the given fields. Use the
>   suggested nickname (validate ≤16 chars, `[A-Za-z0-9_-]+`); if it collides
>   with an active task nickname, pick the next sensible one and tell me what you
>   chose.
> - **UPDATE #id** lines → `update_memory` / `update_task`, passing *only* the
>   fields listed. Remember `tags=[...]` **replaces** the whole tag set — only
>   include tags if the change intends a full replacement.
> - **COMPLETE #id** → `complete_task`. **DELETE #id** → confirm with me first
>   unless the line says `confirmed`.
> - Set `requested_by_human=True` only on lines tagged `(by_human)`.
> - **Serialization guard:** any record with a long multi-line `body` — after
>   writing it, immediately `get_*` it back and check the trailing text didn't
>   absorb a leaked parameter. Fix with `update_*` if so.
> - After all writes, output a confirmation list: `#<id>-<nickname> — <op> — <summary>`.
>
> PROPOSED CHANGES:
> ```
> <-- the web session pastes its block here -->
> ```

---

## Write-back block format (what the web session produces)

The web session emits proposals in this shape so Prompt 2 can execute them
verbatim:

```
ADD memory (cat:work, imp:4) (by_human) nickname:RORDER
  summary: <one line>
  body: <optional>
  tags: royal, ordering, ai-rollout
  origin: royal-ai

ADD task (cat:work, status:open, due:2026-07-01) nickname:RPILOT
  summary: <one line>
  body: <optional>

UPDATE #42 task
  status: in_progress
  body: <new body>

COMPLETE #37

DELETE #19 confirmed
```

**Discipline reminder (from CLAUDE.md):** only durable, factual, cross-session-
useful records belong in ZetaDB. No transient session state, no vibes, nothing
about personal/family life that shouldn't live in a queryable blob. When in
doubt, leave it out.
