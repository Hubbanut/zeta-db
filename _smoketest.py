"""Smoke test for ZetaDB. Exercises every tool against a fresh test DB.

Run from this directory:
    # Windows
    .venv\\Scripts\\python.exe _smoketest.py
    # macOS / Linux
    .venv/bin/python _smoketest.py

Uses a scratch DB file (memories.smoketest.db) so this can never clobber
the real memories.db. The scratch file is deleted at the start of each run
so results are reproducible.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "memories.smoketest.db"

# Point server.py at the scratch DB BEFORE importing it (the path is read at
# import time inside server.py). This way the real memories.db is untouched.
os.environ["ZETA_DB_PATH"] = str(DB)

if DB.exists():
    DB.unlink()
for suffix in ("-journal", "-wal", "-shm"):
    p = DB.with_name(DB.name + suffix)
    if p.exists():
        p.unlink()

sys.path.insert(0, str(HERE))
from server import (  # noqa: E402
    add_category, add_claude_chat, add_column, add_journal_entry,
    add_memory, add_task, begin_work, check_subscriptions,
    complete_task, complete_work, create_table, delete_memory,
    delete_task, describe_schema, get_audit_trail, get_memory,
    get_task, get_work_log, list_categories, list_change_requests,
    list_claude_chat, list_claude_chat_channels, list_journal_entries,
    list_memories, list_recent_edits, list_subscriptions, list_tasks,
    list_work_logs, recent_activity, register_session, request_changes,
    search_claude_chat, search_journal_entries, search_memories,
    search_tasks, subscribe, tick_checklist, unsubscribe,
    update_change_request, update_memory, update_task,
)

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, PASS if ok else FAIL, detail))
    marker = "OK " if ok else "!! "
    print(f"  {marker}{label}" + (f"  -- {detail}" if detail else ""))


def section(name: str) -> None:
    print(f"\n=== {name} ===")


# --------------------------------------------------------------------
section("identity")
sess = register_session("smoketest", "tool-shakedown")
check("register_session returns session_id",
      isinstance(sess.get("session_id"), str) and len(sess["session_id"]) == 16,
      sess.get("session_id"))
SID = sess["session_id"]

bad = register_session("", "")
check("register_session rejects empty client", "error" in bad)


# --------------------------------------------------------------------
section("categories")
cats = list_categories()
seeded = {c["name"] for c in cats["categories"]}
check("seeded categories present",
      {"work", "family", "exercise"}.issubset(seeded), str(sorted(seeded)))
check("claude-self category seeded (CR #12)",
      "claude-self" in seeded)

added = add_category("smoketest")
check("add_category creates", added.get("created") is True)
again = add_category("smoketest")
check("add_category idempotent", again.get("created") is False and again["id"] == added["id"])


# --------------------------------------------------------------------
section("memories")
m1 = add_memory(
    summary="Build server prefers SSD over HDD for the cache tier",
    category="work",
    body="HDDs in the cache path eat the latency budget; stick to SSDs.",
    tags=["hardware", "cache"],
    importance=5,
    requested_by_richard=True,
    richards_remark="No spinning disks on the cache path",
    session_id=SID,
)
check("add_memory returns id", isinstance(m1.get("id"), int), str(m1))
MID = m1["id"]

m2 = add_memory(
    summary="Bulk import does N+1 round-trips per row",
    category="work",
    tags=["import", "performance"],
    importance=4,
    session_id=SID,
)
check("add_memory minimal args", isinstance(m2.get("id"), int))

bad = add_memory(summary="", category="work")
check("add_memory rejects empty summary", "error" in bad)

bad = add_memory(summary="x", category="work", importance=7)
check("add_memory rejects importance out of range", "error" in bad)

bad = add_memory(summary="x", category="nonexistent")
check("add_memory rejects unknown category", "error" in bad)

long_summary = "x" * 500
bad = add_memory(summary=long_summary, category="work")
check("add_memory rejects oversize summary", "error" in bad)
# CR #22: error message includes the measured length now.
check("oversize summary error includes measured length",
      "error" in bad and "500" in bad["error"],
      bad.get("error", ""))

# CR #22: limit was bumped to 300; a 290-char summary should be accepted.
ok_mid = add_memory(summary="x" * 290, category="work", session_id=SID)
check("add_memory accepts ~290-char summary (limit bumped to 300)",
      "error" not in ok_mid)
delete_memory(ok_mid["id"], session_id=SID)

# Origin field (CR #14)
om = add_memory(summary="origin field test memory", category="work",
                origin="smoketest-thread", session_id=SID)
got_om = get_memory(om["id"])
check("add_memory accepts origin", got_om.get("origin") == "smoketest-thread")
listed_origin = list_memories(category="work")
check("list_memories includes origin in summary view",
      any(m.get("origin") == "smoketest-thread" for m in listed_origin["memories"]))
# Update origin and then clear it.
update_memory(om["id"], origin="renamed-thread", session_id=SID)
check("update_memory changes origin",
      get_memory(om["id"]).get("origin") == "renamed-thread")
update_memory(om["id"], origin="", session_id=SID)
check("update_memory clears origin via empty string",
      get_memory(om["id"]).get("origin") is None)


# --------------------------------------------------------------------
section("get / list / search")
got = get_memory(MID)
check("get_memory returns full row including body",
      got.get("body") is not None and got.get("category") == "work")
check("get_memory hydrates tags",
      "hardware" in got.get("tags", []))
check("get_memory exposes requested_by_richard as bool",
      got.get("requested_by_richard") is True)
check("get_memory exposes richards_remark", got.get("richards_remark") is not None)

initial_last_accessed = got["last_accessed"]
time.sleep(1.1)
got2 = get_memory(MID)
check("get_memory bumps last_accessed",
      got2["last_accessed"] > initial_last_accessed,
      f"{initial_last_accessed} -> {got2['last_accessed']}")

listed = list_memories(category="work")
check("list_memories returns rows for category",
      listed["count"] >= 2)
check("list_memories returns summary view (no body)",
      all("body" not in m for m in listed["memories"]))

last_before_list = got2["last_accessed"]
time.sleep(1.1)
list_memories(category="work")  # browsing shouldn't bump
fresh = get_memory(MID)  # but get does
check("list_memories does NOT bump last_accessed",
      fresh["last_accessed"] > last_before_list)  # get bumped it; list shouldn't have

filtered = list_memories(tags=["hardware"])
check("list_memories filters by tag",
      any(m["id"] == MID for m in filtered["memories"]))

searched = search_memories("SSD")
check("search_memories finds by summary",
      any(m["id"] == MID for m in searched["memories"]))

searched_body = search_memories("HDDs")
check("search_memories finds by body",
      any(m["id"] == MID for m in searched_body["memories"]))

bad = search_memories("")
check("search_memories rejects empty query", "error" in bad)


# --------------------------------------------------------------------
section("update / delete memory")
upd = update_memory(MID, importance=4, tags=["hardware", "cache", "latency"],
                    session_id=SID)
check("update_memory changes importance", upd.get("importance") == 4)
check("update_memory replaces tags",
      "latency" in upd.get("tags", []) and len(upd["tags"]) == 3)

upd2 = update_memory(MID, body="new body text")
check("update_memory changes body", upd2.get("body") == "new body text")

bad = update_memory(99999, summary="nope")
check("update_memory not found", "error" in bad)

d = delete_memory(m2["id"])
check("delete_memory succeeds", d.get("deleted") is True)
gone = get_memory(m2["id"])
check("get_memory after delete returns not found", "error" in gone)

# CR #6: session_id persistence on update_memory.
# Set up a second session, then update memory MID with that session.
sess2 = register_session("smoketest", "second-author")
SID2 = sess2["session_id"]
SID2_PK = sess2["id"]
update_memory(MID, summary="now updated by session 2", session_id=SID2)
after = get_memory(MID)
check("update_memory persists session_id of the updater (CR #6)",
      after.get("session_id") == SID2_PK,
      f"expected {SID2_PK}, got {after.get('session_id')}")
# Omitting session_id leaves it alone.
update_memory(MID, summary="updated again, no session passed")
after2 = get_memory(MID)
check("update_memory omitting session_id leaves it untouched",
      after2.get("session_id") == SID2_PK)


# --------------------------------------------------------------------
section("tasks")
t1 = add_task(
    summary="Build ZetaDB MCP server",
    category="work",
    body="Bring up the cross-session memory store with schema sandbox.",
    tags=["mcp", "infra"],
    importance=4,
    due_date="2026-05-20",
    requested_by_richard=True,
    session_id=SID,
)
check("add_task returns id", isinstance(t1.get("id"), int))
TID = t1["id"]

t2 = add_task(summary="Discuss feature with stakeholder", category="work",
              session_id=SID)
check("add_task minimal args", isinstance(t2.get("id"), int))

bad = add_task(summary="x", category="work", importance=0)
check("add_task rejects bad importance", "error" in bad)

got_t = get_task(TID)
check("get_task returns full row",
      got_t.get("body") is not None and got_t.get("status") == "open")
check("get_task hydrates tags", "mcp" in got_t.get("tags", []))

listed_t = list_tasks(category="work")
check("list_tasks returns open tasks for category",
      listed_t["count"] >= 2 and all(t["status"] == "open" for t in listed_t["tasks"]))
check("list_tasks returns summary view",
      all("body" not in t for t in listed_t["tasks"]))

upd_t = update_task(TID, status="blocked", richards_remark="waiting on review",
                    session_id=SID)
check("update_task changes status", upd_t.get("status") == "blocked")
check("update_task preserves richards_remark", upd_t.get("richards_remark") == "waiting on review")

bad = update_task(TID, status="invalid")
check("update_task rejects bad status", "error" in bad)

filtered_open = list_tasks(category="work", status="open")
check("list_tasks filter status='open' excludes blocked",
      all(t["id"] != TID for t in filtered_open["tasks"]))

filtered_all = list_tasks(category="work", status=None)
check("list_tasks status=None includes all",
      any(t["id"] == TID for t in filtered_all["tasks"]))

complete_task(TID, session_id=SID)
done = get_task(TID)
check("complete_task sets status and completed_at",
      done.get("status") == "done" and done.get("completed_at") is not None)

reopen = update_task(TID, status="open")
check("update_task reopen clears completed_at",
      reopen.get("status") == "open" and reopen.get("completed_at") is None)

# due_before
add_task(summary="Overdue", category="work", due_date="2026-01-01", session_id=SID)
add_task(summary="Future", category="work", due_date="2027-01-01", session_id=SID)
overdue = list_tasks(category="work", due_before="2026-06-01")
overdue_summaries = [t["summary"] for t in overdue["tasks"]]
check("list_tasks due_before filter",
      "Overdue" in overdue_summaries and "Future" not in overdue_summaries)

d_t = delete_task(t2["id"])
check("delete_task succeeds", d_t.get("deleted") is True)

# CR #6: session_id persistence on update_task.
update_task(TID, body="updated by session 2", session_id=SID2)
after_t = get_task(TID)
check("update_task persists session_id of the updater (CR #6)",
      after_t.get("session_id") == SID2_PK,
      f"expected {SID2_PK}, got {after_t.get('session_id')}")
update_task(TID, body="updated again, no session passed")
after_t2 = get_task(TID)
check("update_task omitting session_id leaves it untouched",
      after_t2.get("session_id") == SID2_PK)


# --------------------------------------------------------------------
section("search_tasks (CR #5)")
# Use TID (the surviving task that's been mutated).
res = search_tasks("ZetaDB", status=None)
check("search_tasks finds by summary",
      any(t["id"] == TID for t in res["tasks"]))

# Search by body content.
add_task(summary="Investigate importer edge case", category="work",
         body="Reviewer flagged this during yesterday's code review", session_id=SID)
res2 = search_tasks("reviewer", status=None)
check("search_tasks finds by body",
      any("importer" in t["summary"] for t in res2["tasks"]))

# Default status filter is 'open' — TID will be 'done' after this.
complete_task(TID, session_id=SID)
res_all = search_tasks("ZetaDB", status=None)
check("search_tasks status=None includes completed task",
      any(t["id"] == TID for t in res_all["tasks"]))
res_open = search_tasks("ZetaDB")  # default status='open'
check("search_tasks default status='open' excludes completed task",
      all(t["id"] != TID for t in res_open["tasks"]))

# Empty query rejected.
bad = search_tasks("")
check("search_tasks rejects empty query", "error" in bad)

# Bad status rejected.
bad = search_tasks("anything", status="bogus")
check("search_tasks rejects bad status", "error" in bad)


# --------------------------------------------------------------------
section("nicknames")

# Create with a nickname.
mn = add_memory(summary="Connection pool default is 10; bump to 50 for batch jobs",
                category="work", nickname="POOLCFG", session_id=SID)
check("add_memory accepts nickname", mn.get("nickname") == "POOLCFG")
MN = mn["id"]

got_mn = get_memory(MN)
check("get_memory returns nickname", got_mn.get("nickname") == "POOLCFG")

listed_mn = list_memories(category="work")
check("list_memories includes nickname in summary view",
      any(m.get("nickname") == "POOLCFG" for m in listed_mn["memories"]))

searched_mn = search_memories("pool")
check("search_memories includes nickname in summary view",
      any(m.get("nickname") == "POOLCFG" for m in searched_mn["memories"]))

# Validation: too long.
bad = add_memory(summary="x", category="work", nickname="A" * 17)
check("add_memory rejects oversize nickname", "error" in bad)

# Validation: bad chars.
bad = add_memory(summary="x", category="work", nickname="has space")
check("add_memory rejects nickname with disallowed chars", "error" in bad)

# Update: change nickname.
upd_mn = update_memory(MN, nickname="POOL")
check("update_memory changes nickname", upd_mn.get("nickname") == "POOL")

# Update: clear via empty string.
clr_mn = update_memory(MN, nickname="")
check("update_memory clears nickname via empty string", clr_mn.get("nickname") is None)

# Update: omit nickname → leave alone.
upd_mn2 = update_memory(MN, importance=4)
check("update_memory omitting nickname leaves it untouched",
      upd_mn2.get("nickname") is None and upd_mn2.get("importance") == 4)

# Memories: collisions are NOT enforced (no uniqueness rule on memories).
add_memory(summary="another", category="work", nickname="DUP", session_id=SID)
also = add_memory(summary="and another", category="work", nickname="DUP", session_id=SID)
check("add_memory allows nickname collisions (memories not unique-checked)",
      also.get("nickname") == "DUP")

# Tasks: nicknames ARE soft-unique among active tasks.
tn1 = add_task(summary="Batch operations should chunk by configurable size",
               category="work", nickname="BATCH", session_id=SID)
check("add_task accepts nickname", tn1.get("nickname") == "BATCH")
TN1 = tn1["id"]

# Same nickname on another open task → rejected.
collide = add_task(summary="conflicting", category="work", nickname="BATCH",
                   session_id=SID)
check("add_task rejects nickname colliding with another active task",
      "error" in collide and "already used" in collide["error"].lower())

# Nickname shows up in list_tasks summary view.
list_tn = list_tasks(category="work")
check("list_tasks includes nickname in summary view",
      any(t.get("nickname") == "BATCH" for t in list_tn["tasks"]))

# Complete the task — frees the nickname.
complete_task(TN1, session_id=SID)
freed = add_task(summary="now-allowed", category="work", nickname="BATCH",
                 session_id=SID)
check("nickname is freed when task is completed",
      freed.get("nickname") == "BATCH")
TN_FREE = freed["id"]

# Reopen the original task → would collide with the new one.
reopen_collide = update_task(TN1, status="open", session_id=SID)
check("reopening a task into a collided nickname is rejected",
      "error" in reopen_collide and "already used" in reopen_collide["error"].lower())

# Clear the new task's nickname → reopen the original now succeeds.
update_task(TN_FREE, nickname="", session_id=SID)
reopen_ok = update_task(TN1, status="open", session_id=SID)
check("reopen succeeds once the colliding nickname is cleared",
      reopen_ok.get("status") == "open" and reopen_ok.get("nickname") == "BATCH")

# Cancelled tasks also free the nickname.
update_task(TN1, status="cancelled", session_id=SID)
reuse_after_cancel = add_task(summary="re-using after cancel", category="work",
                               nickname="BATCH", session_id=SID)
check("nickname is freed when task is cancelled",
      reuse_after_cancel.get("nickname") == "BATCH")

# Empty / whitespace nickname on add → stays null.
bare = add_task(summary="no nick", category="work", nickname="   ", session_id=SID)
check("add_task treats whitespace-only nickname as null",
      bare.get("nickname") is None)


# --------------------------------------------------------------------
section("schema sandbox")
schema = describe_schema()
core_tables = {t["name"] for t in schema["tables"] if t["reserved"]}
check("describe_schema lists reserved core tables",
      {"memories", "tasks", "sessions", "schema_history",
       "claude_chat", "journal_entries"}.issubset(core_tables))

made = create_table(
    "spike_observations",
    columns=[
        {"name": "subject", "type": "TEXT", "nullable": False},
        {"name": "score", "type": "INTEGER", "default": 0},
    ],
    session_id=SID,
)
check("create_table succeeds", made.get("created") is True)

dup = create_table("spike_observations", columns=[{"name": "x", "type": "TEXT"}])
check("create_table rejects duplicate", "error" in dup)

bad = create_table("memories", columns=[{"name": "x", "type": "TEXT"}])
check("create_table refuses reserved table", "error" in bad)

bad = create_table("bad-name!", columns=[{"name": "x", "type": "TEXT"}])
check("create_table rejects bad identifier", "error" in bad)

bad = create_table("ok_table", columns=[{"name": "x", "type": "WHATEVER"}])
check("create_table rejects bad column type", "error" in bad)

bad = create_table("ok_table", columns=[{"name": "1bad", "type": "TEXT"}])
check("create_table rejects bad column name", "error" in bad)

added_col = add_column(
    "spike_observations",
    column={"name": "notes", "type": "TEXT"},
    session_id=SID,
)
check("add_column succeeds", added_col.get("added") is True)

bad = add_column("memories", column={"name": "x", "type": "TEXT"})
check("add_column refuses reserved table", "error" in bad)

bad = add_column("nonexistent", column={"name": "x", "type": "TEXT"})
check("add_column rejects missing table", "error" in bad)

schema2 = describe_schema()
spike = next(t for t in schema2["tables"] if t["name"] == "spike_observations")
spike_cols = {c["name"] for c in spike["columns"]}
check("schema reflects added column",
      {"id", "created_at", "subject", "score", "notes"} == spike_cols,
      str(sorted(spike_cols)))


# --------------------------------------------------------------------
section("change requests")
cr = request_changes(
    request_type="drop_table",
    target="spike_observations",
    description="Spike done; consolidating findings into a memory.",
    session_id=SID,
)
check("request_changes returns id", isinstance(cr.get("id"), int))
CRID = cr["id"]

bad = request_changes(request_type="", target="x", description="y")
check("request_changes rejects empty fields", "error" in bad)

open_crs = list_change_requests(status="open")
check("list_change_requests sees the new one",
      any(c["id"] == CRID for c in open_crs["change_requests"]))

bad = list_change_requests(status="bogus")
check("list_change_requests rejects bad status", "error" in bad)

all_crs = list_change_requests(status="all")
check("list_change_requests status='all' works",
      any(c["id"] == CRID for c in all_crs["change_requests"]))

resolved = update_change_request(CRID, status="done",
                                  resolution_note="Dropped manually.")
check("update_change_request resolves",
      resolved.get("status") == "done" and resolved.get("resolved_at") is not None)

bad = update_change_request(99999, status="done")
check("update_change_request not found", "error" in bad)


# --------------------------------------------------------------------
section("schema_history audit log")
import sqlite3  # noqa: E402
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
hist = conn.execute("SELECT operation, target FROM schema_history ORDER BY id").fetchall()
ops = [(h["operation"], h["target"]) for h in hist]
conn.close()
check("schema_history logs create_table",
      ("create_table", "spike_observations") in ops, str(ops))
check("schema_history logs add_column",
      ("add_column", "spike_observations.notes") in ops)


# --------------------------------------------------------------------
section("claude_chat (CR #13)")

# Post on default channel.
msg1 = add_claude_chat(body="hello from session 1", session_id=SID,
                       author_nickname="Hermes")
check("add_claude_chat returns id and channel",
      isinstance(msg1.get("id"), int) and msg1.get("channel") == "general")
check("add_claude_chat preserves explicit author_nickname",
      msg1.get("author_nickname") == "Hermes")

# Post on a different channel.
msg2 = add_claude_chat(body="design point: keep journal flexible",
                       channel="design", tags=["journaling", "schema"],
                       session_id=SID2)
check("add_claude_chat on new channel emerges organically",
      msg2.get("channel") == "design")

# Author defaults to session label if not given.
msg3 = add_claude_chat(body="anonymous-style post", channel="design",
                       session_id=SID2)
check("add_claude_chat falls back to session label as author",
      msg3.get("author_nickname") == "second-author")

# List by channel.
listed_design = list_claude_chat(channel="design")
check("list_claude_chat filters by channel",
      all(m["channel"] == "design" for m in listed_design["messages"]))
check("list_claude_chat returns expected message count for channel",
      listed_design["count"] == 2)

# List across all channels.
listed_all = list_claude_chat()
check("list_claude_chat across all channels sees all 3", listed_all["count"] == 3)

# List by author.
listed_hermes = list_claude_chat(author_nickname="Hermes")
check("list_claude_chat filters by author_nickname",
      listed_hermes["count"] == 1 and listed_hermes["messages"][0]["id"] == msg1["id"])

# List by tag.
listed_tag = list_claude_chat(tags=["journaling"])
check("list_claude_chat filters by tag",
      listed_tag["count"] == 1 and listed_tag["messages"][0]["id"] == msg2["id"])

# Search by body.
searched = search_claude_chat("design point")
check("search_claude_chat finds by body",
      any(m["id"] == msg2["id"] for m in searched["messages"]))

# Search rejects empty.
bad = search_claude_chat("")
check("search_claude_chat rejects empty query", "error" in bad)

# Channels listing.
channels = list_claude_chat_channels()
ch_names = {c["channel"] for c in channels["channels"]}
check("list_claude_chat_channels returns both channels",
      {"general", "design"}.issubset(ch_names))

# CR #15: list_claude_chat_channels exposes last_message_id as the cursor.
check("list_claude_chat_channels includes last_message_id",
      all("last_message_id" in c for c in channels["channels"]))

# CR #15: after_id filter on list_claude_chat ("give me new since ID X").
design_channel = next(c for c in channels["channels"] if c["channel"] == "design")
# msg2 was the first design message, msg3 the second. after_id=msg2 should
# return only msg3.
new_only = list_claude_chat(channel="design", after_id=msg2["id"])
check("list_claude_chat after_id returns only newer messages",
      new_only["count"] == 1 and new_only["messages"][0]["id"] == msg3["id"])

# after_id at the channel's latest should return empty (nothing new).
empty = list_claude_chat(channel="design", after_id=design_channel["last_message_id"])
check("list_claude_chat after_id at last_message_id returns empty",
      empty["count"] == 0)

# Post a new message and verify cursor pattern picks it up.
msg4 = add_claude_chat(body="newer design point", channel="design", session_id=SID)
caught_up = list_claude_chat(channel="design",
                              after_id=design_channel["last_message_id"])
check("list_claude_chat after_id catches new post via cursor",
      caught_up["count"] == 1 and caught_up["messages"][0]["id"] == msg4["id"])

# Validation: empty body.
bad = add_claude_chat(body="", session_id=SID)
check("add_claude_chat rejects empty body", "error" in bad)


# --------------------------------------------------------------------
section("journal_entries (CR #4)")

# Add an exercise entry with metrics.
je1 = add_journal_entry(
    entry_type="exercise:run",
    notes="Long run, felt strong",
    metrics={"distance_km": 8, "avg_hr": 152, "effort": "strong"},
    tags=["running"],
    session_id=SID,
)
check("add_journal_entry returns id", isinstance(je1.get("id"), int))
check("add_journal_entry normalises entry_type",
      je1.get("entry_type") == "exercise:run")

# Add a journal entry with explicit past timestamp.
je2 = add_journal_entry(
    entry_type="exercise:spin",
    notes="Spin session",
    metrics={"duration_min": 45, "media_type": "audio"},
    timestamp="2026-05-21 06:00:00",
    session_id=SID,
)
check("add_journal_entry accepts explicit timestamp",
      je2.get("timestamp") == "2026-05-21 06:00:00")

# Add a life-event entry without metrics.
add_journal_entry(entry_type="life",
                  notes="Shipped v1 release",
                  tags=["work"], session_id=SID)

# List all journal entries.
listed_j = list_journal_entries()
check("list_journal_entries returns recent entries first",
      listed_j["count"] >= 3)
check("list_journal_entries summary view drops notes/metrics",
      all("notes" not in e and "metrics" not in e for e in listed_j["entries"]))

# Filter by entry_type prefix.
exercise_only = list_journal_entries(entry_type="exercise:%")
check("list_journal_entries supports LIKE prefix on entry_type",
      exercise_only["count"] == 2)

# Filter by exact entry_type.
runs_only = list_journal_entries(entry_type="exercise:run")
check("list_journal_entries exact entry_type filter",
      runs_only["count"] == 1)

# Filter by date range.
since_only = list_journal_entries(since="2026-05-22 00:00:00")
# je2 has timestamp 2026-05-21 (before cutoff), should be excluded.
check("list_journal_entries since filter excludes pre-cutoff",
      all(e["timestamp"] >= "2026-05-22 00:00:00" for e in since_only["entries"]))

# Search by notes.
searched_j = search_journal_entries("strong")
check("search_journal_entries finds by notes",
      any(e["id"] == je1["id"] for e in searched_j["entries"]))

# Search by metrics JSON.
searched_lang = search_journal_entries("audio")
check("search_journal_entries finds by metrics JSON",
      any(e["id"] == je2["id"] for e in searched_lang["entries"]))

# Search returns full view (notes and metrics included).
check("search_journal_entries returns full view",
      any(e.get("metrics") for e in searched_lang["entries"]))

# tick_checklist convenience.
tk = tick_checklist("creatine", session_id=SID)
check("tick_checklist creates entry with normalised type",
      tk.get("entry_type") == "checklist:creatine")
tk2 = tick_checklist("Omega 3 AM", session_id=SID)
check("tick_checklist normalises spaces to hyphens",
      tk2.get("entry_type") == "checklist:omega-3-am")

# All ticks visible under the checklist: prefix.
ticks = list_journal_entries(entry_type="checklist:%")
check("list_journal_entries finds checklist ticks under prefix",
      ticks["count"] == 2)

# Validation.
bad = add_journal_entry(entry_type="", session_id=SID)
check("add_journal_entry rejects empty entry_type", "error" in bad)


# --------------------------------------------------------------------
section("REQUEST_TYPES constants exposed (CR #3)")
from server import REQUEST_TYPES  # noqa: E402
check("REQUEST_TYPES contains 'bug'", "bug" in REQUEST_TYPES)
check("REQUEST_TYPES contains 'docstring'", "docstring" in REQUEST_TYPES)
check("REQUEST_TYPES contains 'schema_change'", "schema_change" in REQUEST_TYPES)
check("REQUEST_TYPES contains 'api_design'", "api_design" in REQUEST_TYPES)
check("REQUEST_TYPES contains 'convention'", "convention" in REQUEST_TYPES)
check("REQUEST_TYPES contains 'other'", "other" in REQUEST_TYPES)


# --------------------------------------------------------------------
section("work_logs (CR #24)")

import time as _time

# Begin a work log with an estimate.
wl1 = begin_work(description="Investigate something", estimated_seconds=120,
                 session_id=SID)
check("begin_work returns id and estimate", isinstance(wl1.get("id"), int))
WLID = wl1["id"]
check("begin_work renders estimated_human", wl1.get("estimated_human") == "2m")

# Tiny pause so actual_seconds is non-zero.
_time.sleep(1)

# Complete it.
done_wl = complete_work(WLID, notes="went smoothly")
check("complete_work returns actual_seconds", isinstance(done_wl.get("actual_seconds"), int))
check("complete_work returns verdict (faster than estimate)",
      done_wl.get("verdict") == "faster",
      f"ratio={done_wl.get('ratio')}, actual={done_wl.get('actual_seconds')}s")
check("complete_work renders actual_human",
      done_wl.get("actual_human") is not None)

# Trying to complete again fails.
again = complete_work(WLID)
check("complete_work rejects already-completed", "error" in again)

# Begin without estimate — verdict is None.
wl2 = begin_work(description="No estimate work", session_id=SID)
_time.sleep(1)
done_wl2 = complete_work(wl2["id"])
check("complete_work without estimate has None verdict",
      done_wl2.get("verdict") is None and done_wl2.get("ratio") is None)

# Begin with task_id link.
TASK_FOR_WL = add_task(summary="Linked task", category="work", session_id=SID)
wl3 = begin_work(description="Linked work", estimated_seconds=60,
                 task_id=TASK_FOR_WL["id"], session_id=SID)
check("begin_work accepts task_id link", wl3.get("id") is not None)

# Bad task_id rejected.
bad = begin_work(description="Bad link", task_id=999999, session_id=SID)
check("begin_work rejects unknown task_id", "error" in bad)

# Empty description rejected.
bad = begin_work(description="", session_id=SID)
check("begin_work rejects empty description", "error" in bad)

# List filtering.
listed = list_work_logs(completed=True)
check("list_work_logs completed=True excludes in-progress",
      all(w["completed_at"] is not None for w in listed["work_logs"]))

in_progress_one = begin_work(description="Still going", session_id=SID)
inprog = list_work_logs(completed=False)
check("list_work_logs completed=False shows in-progress",
      any(w["id"] == in_progress_one["id"] for w in inprog["work_logs"]))

# By task_id filter.
filtered = list_work_logs(task_id=TASK_FOR_WL["id"])
check("list_work_logs filters by task_id",
      filtered["count"] == 1 and filtered["work_logs"][0]["id"] == wl3["id"])

# get_work_log returns full row.
full = get_work_log(WLID)
check("get_work_log returns notes",
      full.get("notes") == "went smoothly")


# --------------------------------------------------------------------
section("audit_trail (CR #20)")

# Audit on memory create.
m_aud = add_memory(summary="audit me", category="work", body="initial body",
                   importance=3, tags=["audit-test"], session_id=SID)
MAUD = m_aud["id"]
trail = get_audit_trail("memory", MAUD)
check("memory create writes an audit_trail row",
      any(e["operation"] == "create" for e in trail["events"]))
create_event = next(e for e in trail["events"] if e["operation"] == "create")
check("audit_create's new_value contains the snapshot fields",
      "summary" in (create_event["new_value"] or "") and
      "tags" in (create_event["new_value"] or ""))

# Audit on memory update — per-field.
update_memory(MAUD, summary="audit me v2", importance=4, session_id=SID)
trail2 = get_audit_trail("memory", MAUD)
update_events = [e for e in trail2["events"] if e["operation"] == "update"]
fields_changed = {e["field_changed"] for e in update_events}
check("memory update writes one audit row per changed field",
      {"summary", "importance"}.issubset(fields_changed),
      str(sorted(fields_changed)))

# Audit captures old vs new value.
summary_event = next(e for e in update_events if e["field_changed"] == "summary")
check("audit captures old and new field values",
      summary_event["old_value"] == "audit me" and
      summary_event["new_value"] == "audit me v2")

# Tag-change audit.
update_memory(MAUD, tags=["audit-test", "newly-tagged"], session_id=SID)
trail3 = get_audit_trail("memory", MAUD)
tag_events = [e for e in trail3["events"]
              if e["operation"] == "update" and e["field_changed"] == "tags"]
check("tag-set change writes an audit row", len(tag_events) >= 1)

# Noop update doesn't write audit rows.
trail_before = len(get_audit_trail("memory", MAUD)["events"])
update_memory(MAUD, importance=4, session_id=SID)  # same as current
trail_after = len(get_audit_trail("memory", MAUD)["events"])
check("noop update (same value) writes no audit row",
      trail_after == trail_before)

# Audit on delete captures snapshot.
delete_memory(MAUD, session_id=SID)
trail_after_delete = get_audit_trail("memory", MAUD)
delete_event = next((e for e in trail_after_delete["events"]
                     if e["operation"] == "delete"), None)
check("memory delete writes audit row with snapshot",
      delete_event is not None and delete_event["old_value"] is not None)

# Tasks also audited.
t_aud = add_task(summary="audit this task", category="work", session_id=SID)
update_task(t_aud["id"], status="blocked", session_id=SID)
t_trail = get_audit_trail("task", t_aud["id"])
check("task create + update both audited",
      sum(1 for e in t_trail["events"] if e["operation"] == "create") == 1 and
      any(e["operation"] == "update" and e["field_changed"] == "status"
          for e in t_trail["events"]))

# Journal create audited.
j_aud = add_journal_entry(entry_type="life", notes="audit me journal",
                           session_id=SID)
j_trail = get_audit_trail("journal", j_aud["id"])
check("journal create audited",
      any(e["operation"] == "create" for e in j_trail["events"]))

# Chat create audited.
c_aud = add_claude_chat(body="audit me chat", channel="general",
                        author_nickname="Smoketest", session_id=SID)
c_trail = get_audit_trail("chat", c_aud["id"])
check("chat create audited",
      any(e["operation"] == "create" for e in c_trail["events"]))

# list_recent_edits filters work.
recent = list_recent_edits(entity_type="memory", limit=20)
check("list_recent_edits filters by entity_type",
      all(e["entity_type"] == "memory" for e in recent["events"]))

bad = list_recent_edits(entity_type="bogus")
check("list_recent_edits rejects bad entity_type", "error" in bad)

bad = list_recent_edits(operation="invalid")
check("list_recent_edits rejects bad operation", "error" in bad)


# --------------------------------------------------------------------
section("subscriptions (delta-since-last-ping)")

# Subscribe a persona to a channel and a tag.
sub1 = subscribe(persona="TestPersona", target_type="chat_channel",
                 target_value="design")
check("subscribe creates new", sub1.get("created") is True)
sub1_dup = subscribe(persona="TestPersona", target_type="chat_channel",
                     target_value="design")
check("subscribe is idempotent", sub1_dup.get("created") is False)

# Bad target_type rejected.
bad = subscribe(persona="X", target_type="bogus", target_value="y")
check("subscribe rejects bad target_type", "error" in bad)

# list_subscriptions returns the persona's subs.
subs = list_subscriptions("TestPersona")
check("list_subscriptions returns subs",
      subs["count"] == 1 and subs["subscriptions"][0]["target_value"] == "design")

# Auto-subscribe on add_claude_chat:
add_claude_chat(body="seeding new persona Atlas",
                channel="general", author_nickname="Atlas-test",
                session_id=SID)
atlas_subs = list_subscriptions("Atlas-test")
auto = [s for s in atlas_subs["subscriptions"]
        if s["target_type"] == "chat_tag" and s["target_value"] == "for-atlas-test"]
check("add_claude_chat auto-subscribes author to for-<persona>",
      len(auto) == 1)

# check_subscriptions: post in design AFTER subscribe → should surface.
add_claude_chat(body="design message for testpersona", channel="design",
                author_nickname="OtherClaude", session_id=SID)
ping = check_subscriptions("TestPersona")
check("check_subscriptions returns new items",
      ping["total_new"] >= 1)
design_sub = next(s for s in ping["subscriptions"]
                  if s["target_type"] == "chat_channel" and s["target_value"] == "design")
check("check_subscriptions delta finds the new design message",
      design_sub["new_count"] >= 1)

# After ping, cursor advanced — second ping returns nothing.
ping2 = check_subscriptions("TestPersona")
check("second ping shows cursor advanced (no new items)",
      ping2["total_new"] == 0)

# Peek mode: doesn't advance cursor.
add_claude_chat(body="another design msg", channel="design",
                author_nickname="OtherClaude", session_id=SID)
peek = check_subscriptions("TestPersona", advance_cursor=False)
check("check_subscriptions peek returns new item",
      peek["total_new"] >= 1)
peek2 = check_subscriptions("TestPersona", advance_cursor=False)
check("check_subscriptions peek doesn't advance cursor (same new item)",
      peek2["total_new"] >= 1)
# Now advance properly.
check_subscriptions("TestPersona")

# Self-filtering: TestPersona's own posts shouldn't surface in their channel subs.
add_claude_chat(body="own post in design", channel="design",
                author_nickname="TestPersona", session_id=SID)
add_claude_chat(body="another's post in design", channel="design",
                author_nickname="OtherClaude", session_id=SID)
ping3 = check_subscriptions("TestPersona")
design_items = next(s for s in ping3["subscriptions"]
                    if s["target_value"] == "design")["new_items"]
own_seen = any(i.get("author_nickname") == "TestPersona" for i in design_items)
check("check_subscriptions filters out own posts on channel subs",
      not own_seen)

# Subscribe to a category — memory updates surface.
subscribe(persona="TestPersona", target_type="memory_category",
          target_value="work")
add_memory(summary="new work memory for sub test", category="work",
           session_id=SID)
ping4 = check_subscriptions("TestPersona")
memory_sub = next((s for s in ping4["subscriptions"]
                   if s["target_type"] == "memory_category"), None)
check("memory_category subscription surfaces new memories",
      memory_sub and memory_sub["new_count"] >= 1)

# journal_type with LIKE pattern.
subscribe(persona="TestPersona", target_type="journal_type",
          target_value="exercise:%")
add_journal_entry(entry_type="exercise:run",
                  notes="sub test journal", session_id=SID)
ping5 = check_subscriptions("TestPersona")
j_sub = next((s for s in ping5["subscriptions"]
              if s["target_type"] == "journal_type"), None)
check("journal_type subscription with LIKE pattern works",
      j_sub and j_sub["new_count"] >= 1)

# unsubscribe.
unsub = unsubscribe(persona="TestPersona", target_type="chat_channel",
                    target_value="design")
check("unsubscribe succeeds", unsub.get("removed") is True)
subs_after = list_subscriptions("TestPersona")
remaining = [s for s in subs_after["subscriptions"]
             if s["target_type"] == "chat_channel"]
check("unsubscribe removes the row", len(remaining) == 0)

# recent_activity uses audit_trail.
recent = recent_activity(limit=10)
check("recent_activity returns events",
      "events" in recent and isinstance(recent["events"], list))


# --------------------------------------------------------------------
print("\n========================================")
fails = [r for r in results if r[1] == FAIL]
print(f"Total: {len(results)}  Pass: {len(results) - len(fails)}  Fail: {len(fails)}")
if fails:
    for label, _, detail in fails:
        print(f"  FAIL  {label}  {detail}")
    sys.exit(1)
print("All checks passed.")
