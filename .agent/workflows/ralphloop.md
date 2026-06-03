---
description: Never-stop Ralph loop on THIS repo — one strictly-better VERIFIED unit per turn, state externalized to .ralph/. For unattended/forever runs use `ralph start "$PWD" --daemon`.
---
# /ralphloop
Until the user says stop, repeat: read .ralph/{MISSION,HANDOFF,LEDGER,RATCHET}.* first; pick ONE unit
that makes the repo STRICTLY BETTER than HEAD (real fix → coverage → hardening → docs); implement it;
VERIFY for real; append to .ralph/PROGRESS.md + update .ralph/LEDGER.md; commit only if strictly-better
and verified; never stop to summarize — go again. Headless engine: `ralph start "$PWD" --daemon`.
Hard limits: no financial actions; no broad `rm -rf`; no force-push to main; no DB drop; no push to a
remote the user doesn't own.
