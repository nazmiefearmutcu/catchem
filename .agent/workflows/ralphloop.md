---
description: Never-stop Ralph loop on THIS repo — one strictly-better VERIFIED unit per turn, state in .ralph/. Runs FOREGROUND scoped checks (never background-polls) so it cannot stall. For unattended/forever runs use `ralph start "$PWD" --daemon`.
---
# /ralphloop — never-stop, always-better (in-IDE)

You are ONE iteration of an unstoppable self-improvement loop on the current git repo. If a `.ralph/`
directory does not exist yet, run `ralph init "$PWD"` first (or tell the user to), then continue.

⚠ ANTI-STALL RULES — READ FIRST. These prevent the loop from freezing mid-iteration (the
"…I'm checking the background task…" then nothing hang):
- Every tool/command call MUST return promptly. NEVER launch a long command as a BACKGROUND task and
  then poll / "wait for it to finish" — that is exactly what hangs the session. Run commands in the
  FOREGROUND and read their output directly.
- Do NOT run the whole test suite when it is slow. VERIFY only the FOCUSED subset for the file you
  changed (e.g. `python3 -m pytest tests/test_<module>.py -q`, not the full suite). The headless
  daemon re-runs the FULL verify + metric ratchet independently, so a scoped check here is enough.
- Keep each unit SMALL enough that implement+verify finishes in ~1–2 minutes. If a step runs long or
  does not return, ABANDON it, record the blocker in `.ralph/LEDGER.md` (OPEN), and move on — never
  sit waiting on a command.

Until the user types an explicit stop word, repeat:
1. READ state from disk (never trust memory): `.ralph/MISSION.md`, `.ralph/HANDOFF.md`,
   `.ralph/LEDGER.md` (OPEN items), the last 3 `.ralph/PROGRESS.md` blocks, `.ralph/RATCHET.json`.
2. Pick ONE small unit that makes the repo STRICTLY BETTER than HEAD (real bug fix → focused test
   coverage → hardening → docs). No cosmetic busywork; respect every RATCHET floor.
3. Implement it. VERIFY for real — FOREGROUND, scoped, fast (see ANTI-STALL rules). Never claim a
   pass you did not observe.
4. APPEND (never overwrite) one ITER block to `.ralph/PROGRESS.md`; update `.ralph/LEDGER.md`
   (OPEN→DONE on success; REJECTED + WHY if an approach failed).
5. Commit ONLY if strictly-better and verified. Edit nothing under `.ralph/` except PROGRESS.md/LEDGER.md.
6. Do NOT stop to summarize. Immediately go to step 1.

For an UNATTENDED, truly-forever run that AUTO-RECOVERS from agy stalls/timeouts (the robust engine:
fresh-context iterations + a git-backed metric ratchet that auto-reverts regressions and time-boxes
every agy run), use the headless CLI instead:
    ralph start "$PWD" --daemon      # then: ralph status "$PWD" / ralph stop "$PWD"
Hard limits: no financial actions; no broad `rm -rf`; no force-push to main; no DB drop; no push to a
remote the user does not own.
