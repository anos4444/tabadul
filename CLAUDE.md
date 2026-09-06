# tabadul — notes for coding agents

## Git rules (the owner's standing instruction, 2026-09-06)

- Every commit is authored **and** committed as
  `anos4444 <anas.abdullah@gmail.com>` — set `git config user.name anos4444`
  and `git config user.email anas.abdullah@gmail.com` in the clone before
  the first commit. Never a firm identity.
- **No AI trailers.** No `Co-Authored-By: Claude …`, no `Claude-Session:`,
  no model names or session links in commit messages, PR bodies, tags or
  code comments. If a harness adds such lines on its own, strip them before
  pushing.
- **Commit directly to `main`.** No feature branches, no PR branches, no
  release branches — `main` is the only branch, locally and on GitHub.
  Validate first, then push `main`.

## Commit style

`type: short summary` subject, then a body with what broke (with a concrete
repro where possible), the root cause, the fix, and how it was verified —
the reasoning that justifies the change, not a changelog restatement. Read
recent `git log` before writing one.
