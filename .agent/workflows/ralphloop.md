---
description: Start the NEVER-STOPPING Ralph engine on THIS repo. /ralphloop launches the headless daemon (ralph start --daemon) that improves the repo FOREVER — fresh-context iterations, a metric ratchet that auto-reverts regressions, and automatic recovery from any agy stall/timeout. It does NOT stop until you run `ralph stop`.
---
# /ralphloop — start the never-stop engine

When the user types /ralphloop they want THIS repo to start improving itself FOREVER and to NOT stop
until they stop it. A single agy chat session cannot do that — it stalls or ends — so DO NOT try to
loop inside this conversation. Instead LAUNCH THE EXTERNAL DAEMON: it is immortal, runs in fresh
contexts, auto-reverts regressions via a metric ratchet, and AUTO-RECOVERS from any agy stall/timeout.

Do EXACTLY this, in order. Run each in the FOREGROUND; each returns immediately (never background-poll):

1. Scaffold if needed (no-op if already set up):
       ralph init "$PWD" 2>/dev/null || true
2. Start the never-stopping daemon (idempotent — if one is already running it just says so):
       ralph start "$PWD" --daemon
3. Confirm it is ALIVE and show pid + current iteration:
       ralph status "$PWD"
4. Show recent progress so the user sees it working:
       tail -n 20 "$PWD/.ralph/PROGRESS.md" 2>/dev/null || true

Then tell the user briefly:
  "✅ Never-stop Ralph engine is running (pid + iteration above). It keeps improving this repo FOREVER
   and auto-recovers from any agy stall/timeout — it will NOT stop until you stop it.
     • Watch:  ralph status \"$PWD\"   ·   tail -f .ralph/logs/supervisor.log   ·   .ralph/PROGRESS.md
     • Stop:   ralph stop \"$PWD\"      (or: touch .ralph/STOP)"

You are DONE after that. The daemon owns the loop and runs even after this chat closes — do NOT iterate
here, do NOT poll/wait, do NOT keep summarizing. If `ralph status` shows it is NOT alive (e.g. the
sandbox blocked the background launch), tell the user to run `ralph start "$PWD" --daemon` once in a
plain terminal — then it runs forever. (`ralph` = the antigravity-ralph repo's bin/ralph if not on PATH.)
