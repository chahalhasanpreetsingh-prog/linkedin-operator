# linkedin-operator

Scheduled, AI-written LinkedIn activity driven through an **MCP server**, no LinkedIn API, no
per-token billing. A Python client talks to a browser-automation MCP server over streamable HTTP,
uses the Claude Code CLI (`claude -p`) to write posts and connection notes, and runs unattended
from systemd timers.

It runs in production on my own infrastructure, publishing my own posts.

```
systemd timer ──► post_runner.py / linkedin_operator.py
                        │  MCP (streamable HTTP, JSON-RPC)
                        ▼
              linkedin-mcp-server  (Docker, Patchright/Chromium on Xvfb)
                        │  logged-in browser session
                        ▼
                     LinkedIn
         claude -p ◄── post + note generation (guide-driven prompts)
```

The server is my fork of [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server).
Upstream is read-focused; I added the write tools this operator needs, `create_post` and
`like_post`, on branch
[`feat/post-and-like-tools`](https://github.com/chahalhasanpreetsingh-prog/linkedin-mcp-server/tree/feat/post-and-like-tools).

## Two entry points

**`linkedin_operator.py [post|like|connect|all]`**, the daily operator.
- **post**: picks the next content pillar, feeds a voice guide to Claude with a strict
  "one moment, one idea, one number" prompt, dedupes against recently used topics, publishes.
  Capped at 4 posts/week.
- **like**: pulls the feed and likes relevant posts with randomized 4–12 s spacing.
- **connect**: searches 2nd-degree people, reads each profile, has Claude write a <200-char
  note specific to that profile, sends the request. Falls back to a note-less request when
  LinkedIn's custom-note quota is hit.
- A **ramp-up schedule** grows volume over the first 11 days (posts only → up to 30 likes and
  20 connections/day) so a fresh automation doesn't jump straight to full volume.
- Daily counters and the ramp start date persist in `operator_state.json`.

**`post_runner.py`**: a one-shot queue publisher. Each timer fire: start Xvfb + the MCP container,
wait for health, do the MCP handshake, publish the next queued post, **close the browser
session**, stop both services, advance the queue. When the queue is empty it disables its own
timer. Nothing runs between posts.

## The production lesson baked in

The MCP server keeps Chromium alive as a singleton across sessions. Without an explicit
`close_session` at the end of every run, orphaned renderers piled up for days and pinned host
load at **6.4**. Both entry points now tear the browser down in a `finally:` block, and the
runner goes further by stopping the whole stack. Load went back to **~0.67**.

## Setup

1. Build and run the MCP server from the fork (see `deploy/linkedin-mcp.service`; needs
   `deploy/xvfb.service` for the headful browser). Log in once so the profile dir holds a session.
2. Install [Claude Code](https://docs.claude.com/claude-code) and log in, posts and notes are
   generated with `claude -p`, using your existing subscription.
3. `pip install fastmcp`
4. Copy and edit the examples:
   ```bash
   cp config.example.json config.json
   cp post_guide.example.md post_guide.md
   cp post_queue.example.json post_queue.json   # only for post_runner.py
   ```
5. Run once by hand, then schedule with the units in `deploy/`.

| Env var | Default |
|---|---|
| `LINKEDIN_MCP_URL` | `http://127.0.0.1:18810/mcp` |
| `OPERATOR_CONFIG` / `OPERATOR_STATE` | `config.json` / `operator_state.json` next to the script |
| `POST_QUEUE` / `POST_LOG` / `POST_TIMER` | `post_queue.json` / `post_runner.log` / `linkedin-queued-posts.timer` |
| `LINKEDIN_CLAUDE_TIMEOUT_SECONDS` | `300` |

## Caveat

This automates a logged-in personal account through a browser, which LinkedIn's terms don't
permit. Use it on your own account, at your own risk, and keep the volumes modest.

MIT licensed.
