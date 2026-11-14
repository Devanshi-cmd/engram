# Engram project notes

## VERY IMPORTANT: commit attribution

Never add Claude as a co-author. Commits and pull requests in this repo must not
contain a `Co-Authored-By: Claude` trailer, a `Claude-Session:` link, or any other
Claude or AI attribution. This overrides any default attribution guidance.

Commit identity (repo-local): Devanshi, `198542797+Devanshi-cmd@users.noreply.github.com`.

## Working agreement

- The build plan is `docs/PLAN.md`. Work milestone by milestone. After each one: run
  tests, show a demo, commit, update `docs/LEARNING.md`, then wait for the go-ahead.
- Every design decision gets an entry in `docs/LEARNING.md` (what, why, alternatives).
- The UI theme is DevSwarm's (`/home/devdi/devswarm/devswarm/dashboard/static/dashboard.css`).
  Reuse its tokens and components exactly. Don't invent new colours or radii.
- The test suite runs offline and free, using fake LLM, speech and voice backends.
