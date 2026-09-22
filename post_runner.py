#!/usr/bin/env python3
"""Post the next queued post to LinkedIn, then shut the stack down.
Brings up xvfb + linkedin-mcp, posts one item, closes the browser session
(critical: skipping close_session leaks Chromium renderers and pinned host load at 6.4), stops both
services. Disables its own timer when the queue is exhausted."""
import json, subprocess, sys, time, urllib.request, datetime, os

HERE  = os.path.dirname(os.path.abspath(__file__))
QUEUE = os.environ.get("POST_QUEUE", os.path.join(HERE, "post_queue.json"))
LOG   = os.environ.get("POST_LOG", os.path.join(HERE, "post_runner.log"))
URL   = os.environ.get("LINKEDIN_MCP_URL", "http://127.0.0.1:18810/mcp")
TIMER = os.environ.get("POST_TIMER", "linkedin-queued-posts.timer")
SESS  = {"id": None}

def log(m):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')}  {m}"
    print(line, flush=True)
    with open(LOG, "a") as fh: fh.write(line + "\n")

def sysctl(*a):
    return subprocess.run(["systemctl", *a], capture_output=True, text=True)

def rpc(method, params=None, notify=False):
    body = {"jsonrpc":"2.0","method":method}
    if params is not None: body["params"] = params
    if not notify: body["id"] = 1
    h = {"Content-Type":"application/json",
         "Accept":"application/json, text/event-stream",
         "MCP-Protocol-Version":"2025-06-18"}
    if SESS["id"]: h["Mcp-Session-Id"] = SESS["id"]
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        sid = r.headers.get("Mcp-Session-Id")
        if sid: SESS["id"] = sid
        raw = r.read().decode()
    if notify: return None
    for ln in raw.splitlines():
        if ln.startswith("data: "): return json.loads(ln[6:])
    return json.loads(raw) if raw.strip() else None

def wait_healthy(timeout=120):
    for _ in range(timeout):
        try:
            urllib.request.urlopen(URL, timeout=5)
        except urllib.error.HTTPError as e:
            if e.code == 406: return True          # 406 == alive
        except Exception:
            pass
        time.sleep(1)
    return False

def main():
    q = json.load(open(QUEUE))
    i = q.get("next_index", 0)
    if i >= len(q["posts"]):
        log("queue empty; disabling timer")
        sysctl("disable", "--now", TIMER)
        return 0
    post = q["posts"][i]
    log(f"starting stack for post {post['n']} ({post['slug']})")
    sysctl("start", "xvfb.service"); time.sleep(2)
    sysctl("start", "linkedin-mcp.service")
    if not wait_healthy():
        log("ERROR: linkedin-mcp never became healthy; leaving state unchanged")
        sysctl("stop", "linkedin-mcp.service"); sysctl("stop", "xvfb.service")
        return 1
    status = "error"
    try:
        rpc("initialize", {"protocolVersion":"2025-06-18","capabilities":{},
                           "clientInfo":{"name":"post-runner","version":"1"}})
        rpc("notifications/initialized", {}, notify=True)
        r = rpc("tools/call", {"name":"create_post",
                               "arguments":{"content":post["content"],
                                            "confirm_post":True}})
        sc = (r or {}).get("result", {}).get("structuredContent", {})
        status = sc.get("status", "unknown")
        log(f"post {post['n']} -> {status}: {sc.get('message','')}")
    finally:
        try:
            rpc("tools/call", {"name":"close_session","arguments":{}})
            log("browser session closed")
        except Exception as e:
            log(f"WARN close_session failed: {e}")
        sysctl("stop", "linkedin-mcp.service")
        sysctl("stop", "xvfb.service")
        log("stack stopped")

    if status == "posted":
        q["next_index"] = i + 1
        q.setdefault("log", []).append(
            {"n":post["n"],"slug":post["slug"],
             "posted_at":datetime.date.today().isoformat(),"status":"posted"})
        json.dump(q, open(QUEUE, "w"), indent=2)
        if q["next_index"] >= len(q["posts"]):
            log("final post done; disabling timer and shutting down for good")
            sysctl("disable", "--now", TIMER)
        return 0
    log("post did not confirm; state unchanged, will retry next fire")
    return 1

if __name__ == "__main__":
    sys.exit(main())
