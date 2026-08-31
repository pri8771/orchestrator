# Wait, How Big? social operator

See [`../README.md`](../README.md) for the complete launch handoff and
[`../CURSOR_HANDOFF.md`](../CURSOR_HANDOFF.md) for the continuation prompt.

The owner-controlled account bootstrap is complete: Buffer Free has exactly X
`@WaitHowBig`, Instagram `@waithowbig`, and TikTok `@waithowbignow`, and the
Buffer email is verified. The operator is **not launched**: the personal API key
has not been generated, `WHB_BUFFER_API_KEY` is absent, no dry run has passed,
and no public post has been verified.

## Remaining owner bootstrap

1. Fix the packaged executable's Python module-name collision described below.
2. Keep scheduled publishing blocked with `WHB_KILL_SWITCH=true` (or disable the
   cron trigger) before installing a usable secret.
3. In Buffer **Settings -> API**, generate a personal key named
   `Wait How Big Operator`, expiring in one year, with only `account:read`,
   `posts:read`, and `posts:write`. Obtain explicit owner approval immediately
   before generation.
4. Add the value directly as repository Actions secret `WHB_BUFFER_API_KEY`.
   Never place it in a file, issue, commit, shell history, log, or chat.
5. Dispatch **Wait How Big social operator** once with `dry_run=true` while the
   publish block remains active. Review the run and `state.json` before asking
   for a separate decision to allow a normal run.

## Known packaged-executable blocker

`operator_bundle.zip` currently contains `operator.py`, and the workflow runs
`python /tmp/whb-operator/operator.py`. Python adds that directory to
`sys.path`; standard-library imports of `operator` resolve the local script and
fail with a circular import. Scheduled run `33342049065` demonstrated the
failure under Python 3.12.

Rename the executable (for example, `whb_operator.py`), rebuild the archive
deterministically, update the workflow command and SHA-256, and add a smoke test
that launches the packaged executable with no API key. The no-key path should
exit cleanly with `WAIT_HOW_BIG_NOT_CONFIGURED`, never an import traceback.

## Behavior

- Discovers the Buffer organization and connected channels automatically.
- Requires one unlocked X, one Instagram, and one TikTok channel; otherwise it fails closed.
- Verifies every direct media URL before creating a post.
- Rebuilds idempotency from Buffer post history, so a state-write failure does not duplicate posts.
- Keeps at most ten scheduled posts per channel.
- Uses platform-specific captions and exact scheduled times.
- Applies AI-assistance metadata to supported platform payloads.
- Stops when Buffer reports an existing post error.
- Supports an emergency repository variable: set `WHB_KILL_SWITCH=true` to stop all posting.

The operator source and queue are contained in `operator_bundle.zip`; the workflow verifies its SHA-256 before execution. No social credentials or API keys are stored in this directory.

## Current state snapshot

As of 2026-08-31, `state.json` still reports
`awaiting_accounts_and_buffer_key`, with `anchor_utc: null`, no run timestamp,
and no post receipts. The recurring workflow runs every three hours at minute
17 and currently fails. Connected channels and a queued schedule are not public
publication evidence.
