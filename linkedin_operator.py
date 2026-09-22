#!/usr/bin/env python3
"""
LinkedIn Operator — scheduled AI posting, liking, and connecting through an MCP server.

Modes:
  post        — generate a post with Claude and publish it
  like        — like N relevant posts from the feed
  connect     — send N connection requests with personalized notes

The ramp-up schedule (days since start):
  1–2:   posts only
  3–4:   posts + 10 likes
  5–6:   posts + 20 likes + 5 connections
  7–8:   posts + 25 likes + 12 connections
  9–10:  posts + 30 likes + 18 connections
  11+:   posts + 30 likes + 20 connections (full volume)
"""

import asyncio
import json
import logging
import os
import random
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from fastmcp import Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("operator")

CONFIG_FILE = Path(os.environ.get("OPERATOR_CONFIG", Path(__file__).with_name("config.json")))
CONFIG = json.loads(CONFIG_FILE.read_text())

MCP_URL = os.environ.get("LINKEDIN_MCP_URL", "http://127.0.0.1:18810/mcp")
STATE_FILE = Path(os.environ.get("OPERATOR_STATE", Path(__file__).with_name("operator_state.json")))
GUIDE_FILE = Path(os.path.expanduser(CONFIG.get("guide_file", "post_guide.md")))
CLAUDE_BIN = shutil.which("claude") or "/usr/local/bin/claude"
CLAUDE_TIMEOUT_SECONDS = int(os.environ.get("LINKEDIN_CLAUDE_TIMEOUT_SECONDS", "300"))

AUTHOR_NAME = CONFIG["author_name"]
AUTHOR_ABOUT = CONFIG["author_about"]
LIKE_SEARCH_QUERIES = CONFIG["like_search_queries"]
CONNECT_SEARCH_QUERIES = CONFIG["connect_search_queries"]
CONTENT_PILLARS = CONFIG["content_pillars"]
CROSSOVER_PILLAR = CONFIG.get("crossover_pillar", "")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "ramp_start_date": date.today().isoformat(),
        "pillar_index": 0,
        "likes_today": 0,
        "connections_today": 0,
        "last_reset_date": date.today().isoformat(),
        "topics_used": [],
        "posts_this_week": [],
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def reset_daily_counters(state: dict) -> dict:
    today = date.today().isoformat()
    if state.get("last_reset_date") != today:
        state["likes_today"] = 0
        state["connections_today"] = 0
        state["last_reset_date"] = today
        # Clean up posts_this_week — keep only posts from the past 7 days
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        state["posts_this_week"] = [
            d for d in state.get("posts_this_week", []) if d >= cutoff
        ]
    return state


def get_ramp_limits(state: dict) -> dict:
    start = date.fromisoformat(state["ramp_start_date"])
    days_elapsed = (date.today() - start).days + 1  # day 1 on ramp_start_date
    if days_elapsed <= 2:
        return {"likes": 0, "connections": 0}
    elif days_elapsed <= 4:
        return {"likes": 10, "connections": 0}
    elif days_elapsed <= 6:
        return {"likes": 20, "connections": 5}
    elif days_elapsed <= 8:
        return {"likes": 25, "connections": 12}
    elif days_elapsed <= 10:
        return {"likes": 30, "connections": 18}
    else:
        return {"likes": 30, "connections": 20}


def read_guide() -> str:
    if GUIDE_FILE.exists():
        return GUIDE_FILE.read_text()
    return ""


async def call_tool(client: Client, name: str, args: dict) -> dict:
    result = await client.call_tool(name, args)
    # fastmcp returns a CallToolResult; content is in result.content list
    content = getattr(result, "content", None) or result
    items = content if isinstance(content, list) else [content]
    for item in items:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except Exception:
                return {"raw": text}
    return {}


def claude(prompt: str) -> str:
    """Call the claude CLI in non-interactive mode and return the response text."""
    result = subprocess.run(
        [CLAUDE_BIN, "-p", "--output-format", "text"],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr.strip()}")
    return result.stdout.strip()


async def generate_post(state: dict) -> str:
    guide = read_guide()
    pillar = CONTENT_PILLARS[state["pillar_index"] % len(CONTENT_PILLARS)]
    topics_used = state.get("topics_used", [])

    prompt = f"""You are {AUTHOR_NAME} writing a LinkedIn post for yourself. You have read and internalized the following guide completely:

---
{guide}
---

Before you write, silently do the pre-writing ritual from PART 0:
1. Pick ONE specific moment or incident within the pillar "{pillar}" — not a topic, a moment.
2. Pick ONE anchor number or fact you will use exactly once.
3. Decide the single takeaway the reader leaves with (one sentence).
Do NOT print any of this. It is your private planning.

Then write exactly ONE LinkedIn post that obeys THE ONE RULE: one moment, one idea, one number.

Hard requirements:
- First person as {AUTHOR_NAME} ("I", never third person).
- Stay inside the pillar: {pillar}.
- Use ONE project or venture only. (The single exception: if and only if the pillar is
  "{CROSSOVER_PILLAR}", connect exactly TWO of the author's worlds with a real
  thread — never three or more.)
- ABSOLUTELY NO biographical recap. Do not list the author's roles or ventures together.
  Anything not part of THIS one moment is banned from the post.
- Match the restraint and voice of the complete example posts in PART 5. Imitate how they
  stay on a single moment and never recite the résumé.
- Hook under 210 characters, on its own line, and it must NOT start with the word "I".
- New line every 1–2 sentences. End on a genuine question or a clean statement (not inspiration).
- Under 1300 characters total. No URLs. No hashtags. At most one emoji (prefer none).
- Do NOT reuse any of these recently used topics: {', '.join(topics_used[-10:]) if topics_used else 'none yet'}

Final self-check before you output: could a reader summarise this post in one sentence that is
NOT "{AUTHOR_NAME} has done a lot of impressive things"? If not, rewrite it around a single moment.

Output ONLY the post text. No preamble, no "Here is a post:", no explanation. Just the post."""

    return await asyncio.get_event_loop().run_in_executor(None, claude, prompt)


async def do_post(client: Client, state: dict) -> bool:
    posts_this_week = state.get("posts_this_week", [])
    if len(posts_this_week) >= 4:
        log.info("Already have 4 posts this week — skipping post")
        return False

    log.info("Generating post (pillar: %s)...", CONTENT_PILLARS[state["pillar_index"] % len(CONTENT_PILLARS)])
    post_text = await generate_post(state)
    log.info("Generated post (%d chars):\n%s", len(post_text), post_text)

    result = await call_tool(client, "create_post", {
        "content": post_text,
        "confirm_post": True,
    })
    log.info("Post result: %s", result)

    if result.get("status") == "posted":
        state["pillar_index"] = (state["pillar_index"] + 1) % len(CONTENT_PILLARS)
        state["posts_this_week"].append(date.today().isoformat())
        # Extract rough topic from first line of post for dedup tracking
        first_line = post_text.split("\n")[0][:60]
        state.setdefault("topics_used", []).append(first_line)
        log.info("Post published successfully")
        return True
    else:
        log.warning("Post failed: %s", result.get("message"))
        return False


async def do_likes(client: Client, state: dict, target: int) -> int:
    already_done = state.get("likes_today", 0)
    remaining = target - already_done
    if remaining <= 0:
        log.info("Already liked %d today (target %d) — skipping", already_done, target)
        return 0

    log.info("Liking up to %d more posts today (already done %d)", remaining, already_done)

    # Get feed posts
    feed_result = await call_tool(client, "get_feed", {"num_posts": min(50, max(20, remaining * 2))})
    refs = feed_result.get("references", {}).get("feed", [])
    post_urls = [r["url"] for r in refs if r.get("url")]

    if not post_urls:
        log.warning("No posts found in feed")
        return 0

    # Shuffle to vary which posts we like each run
    random.shuffle(post_urls)

    liked = 0
    for url in post_urls:
        if liked >= remaining:
            break
        try:
            result = await call_tool(client, "like_post", {
                "post_url": url,
                "confirm_like": True,
            })
            status = result.get("status", "")
            if status == "liked":
                liked += 1
                log.info("Liked post %d/%d: %s", liked, remaining, url[:70])
                await asyncio.sleep(random.uniform(4, 12))
            elif status == "already_liked":
                log.debug("Already liked: %s", url[:70])
            elif status == "not_found":
                log.debug("Like button not found on: %s", url[:70])
            else:
                log.debug("Like status=%s on %s", status, url[:70])
        except Exception as e:
            log.warning("Error liking %s: %s", url[:70], e)

    state["likes_today"] = already_done + liked
    log.info("Liked %d posts this run (%d total today)", liked, state["likes_today"])
    return liked


async def build_connection_note(client: Client, username: str, profile_text: str) -> str:
    prompt = f"""Write a personalized LinkedIn connection request note from {AUTHOR_NAME} to this person.

About {AUTHOR_NAME}: {AUTHOR_ABOUT}

Target profile text (excerpted):
{profile_text[:800]}

Rules:
- Under 200 characters (LinkedIn limit for free connection notes)
- Specific to something in their profile — don't be generic
- First person, casual but professional
- No flattery or "I came across your profile"
- Sound like a real person reaching out, not a template
- Output ONLY the note text, nothing else"""

    return await asyncio.get_event_loop().run_in_executor(None, claude, prompt)


async def do_connections(client: Client, state: dict, target: int) -> int:
    already_done = state.get("connections_today", 0)
    remaining = target - already_done
    if remaining <= 0:
        log.info("Already sent %d connections today (target %d) — skipping", already_done, target)
        return 0

    log.info("Sending up to %d connection requests today (already done %d)", remaining, already_done)

    # Gather candidate usernames from search
    candidates: list[str] = []
    query = random.choice(CONNECT_SEARCH_QUERIES)
    log.info("Searching people with query: %s", query)
    try:
        search_result = await call_tool(client, "search_people", {
            "keywords": query,
            "network": ["S"],  # 2nd-degree connections
        })
        refs = search_result.get("references", {}).get("search_results", [])
        for r in refs:
            url = r.get("url", "")
            if "/in/" in url:
                # Extract username from /in/username/ path
                parts = url.split("/in/")
                if len(parts) > 1:
                    username = parts[1].strip("/").split("/")[0].split("?")[0]
                    if username and username not in candidates:
                        candidates.append(username)
    except Exception as e:
        log.warning("Search error: %s", e)

    if not candidates:
        log.warning("No candidates found from search")
        return 0

    random.shuffle(candidates)
    sent = 0
    for username in candidates:
        if sent >= remaining:
            break
        try:
            # Get their profile for personalized note
            profile_result = await call_tool(client, "get_person_profile", {
                "linkedin_username": username,
            })
            profile_text = (
                profile_result.get("sections", {}).get("main_profile", "")
            )
            if not profile_text:
                continue

            note = await build_connection_note(client, username, profile_text)
            log.info("Connecting to %s with note: %s", username, note[:80])

            result = await call_tool(client, "connect_with_person", {
                "linkedin_username": username,
                "note": note,
            })
            status = result.get("status", "")
            if status == "connected":
                sent += 1
                log.info("Connected %d/%d: %s", sent, remaining, username)
                await asyncio.sleep(random.uniform(15, 35))
            elif status in ("already_connected", "pending"):
                log.debug("Skipping %s: %s", username, status)
            elif status == "custom_note_limit_reached":
                log.warning("Note limit reached — sending without note")
                result2 = await call_tool(client, "connect_with_person", {
                    "linkedin_username": username,
                })
                if result2.get("status") == "connected":
                    sent += 1
                    await asyncio.sleep(random.uniform(15, 35))
            else:
                log.debug("Connection status=%s for %s: %s", status, username, result.get("message"))
        except Exception as e:
            log.warning("Error connecting to %s: %s", username, e)

    state["connections_today"] = already_done + sent
    log.info("Sent %d connections this run (%d total today)", sent, state["connections_today"])
    return sent


async def main() -> None:
    if not shutil.which("claude"):
        log.error("claude CLI not found in PATH — is Claude Code installed?")
        sys.exit(1)

    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    state = load_state()
    state = reset_daily_counters(state)
    limits = get_ramp_limits(state)

    log.info("Mode: %s | Ramp limits: %s | State: likes_today=%d, connections_today=%d",
             mode, limits, state.get("likes_today", 0), state.get("connections_today", 0))

    async with Client(MCP_URL) as client:
        try:
            if mode in ("post", "all"):
                await do_post(client, state)

            if mode in ("like", "all") and limits["likes"] > 0:
                await do_likes(client, state, limits["likes"])

            if mode in ("connect", "all") and limits["connections"] > 0:
                await do_connections(client, state, limits["connections"])
        finally:
            # Tear down the MCP server's persistent browser at the end of every
            # run. The server keeps Chromium alive as a singleton across MCP
            # sessions; without this call the browser (and leaked page
            # renderers) accumulate CPU for days. close_session reaps all
            # Chromium/driver processes; the server re-inits the browser
            # lazily on the next run. Runs even if a do_* step raised.
            try:
                await call_tool(client, "close_session", {})
                log.info("Closed browser session")
            except Exception as e:
                log.warning("close_session failed: %s", e)

    save_state(state)
    log.info("Done. State saved.")


if __name__ == "__main__":
    asyncio.run(main())
