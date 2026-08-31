# Wait, How Big? launch handoff

Last updated: 2026-08-31

This directory is the durable handoff for the zero-budget **Wait, How Big?**
social launch. It records verified external state, safe login identities, operator
files, known blockers, and the exact launch sequence. It deliberately contains
no passwords, API keys, OAuth codes, passkeys, 2FA codes, or recovery data.

## Current verified state

| Area | Status | Evidence / detail |
| --- | --- | --- |
| Email alias | Complete | `waithowbig@unsubscriber.me` is an alias of `pchordia@unsubscriber.me`; mail arrives in the latter Gmail inbox. |
| X | Public and connected to Buffer | `@WaitHowBig` — <https://x.com/WaitHowBig> |
| Instagram | Public Creator account and connected to Buffer | `@waithowbig` — <https://www.instagram.com/waithowbig/> |
| TikTok | Public and connected to Buffer | `@waithowbignow` — <https://www.tiktok.com/@waithowbignow> |
| Buffer | Free account, email verified, exactly three channels | `WaitHowBig`, `waithowbig`, `waithowbignow` |
| Buffer personal key | Not generated | The prepared form used key name `Wait How Big Operator`, one-year expiry, and only `account:read`, `posts:read`, `posts:write`. Recreate the form because an unsubmitted browser form is not durable state. |
| GitHub secret | Missing | `gh secret list -R pri8771/orchestrator` returned no `WHB_BUFFER_API_KEY` on 2026-08-31. |
| Dry run | Not completed | No successful `workflow_dispatch` run with `dry_run=true` is recorded. |
| Posts | None verified | `state.json` still has no post receipts. Public baselines were zero posts/followers when checked during bootstrap; re-check live before reporting current metrics. |

Do not describe connected accounts, a configured key form, a schedule receipt,
or queued media as a launch. A launch requires a successful dry run followed by
a deliberate normal run and verified public post IDs/timestamps.

## Login identities and destinations

Use these identifiers only at the corresponding official site. Enter all
passwords, passkeys, CAPTCHA answers, 2FA codes, birth dates, and recovery data
personally in the live UI; never paste them into chat, Cursor, files, issues,
commits, terminals, or logs.

| Destination | Correct identity | Avoid / notes |
| --- | --- | --- |
| Gmail / Google Workspace | Sign in as `pchordia@unsubscriber.me`; receive alias mail for `waithowbig@unsubscriber.me` | The alias is not a separate Gmail mailbox or password. |
| Buffer | `waithowbig@unsubscriber.me` | Do not use the unrelated `onepersonops@unsubscriber.me` / `My Organization` workspace. |
| X | Username `WaitHowBig` | The browser may also offer unrelated `@OnePerson0ops` or `@OnePersonOpsMgr`; never authorize Buffer against them. |
| Instagram | Username `waithowbig` | Verify the OAuth page names `waithowbig` before approval. |
| TikTok | Username `waithowbignow`, display name `Wait, How Big?` | Do not authorize the unrelated `@wootworwoot` or `@appleuser535236940`. |
| GitHub | Repository owner `pri8771` | Repository: <https://github.com/pri8771/orchestrator> |

## Brand profile state

- Brand display name: `Wait, How Big?`
- Handle priority used during creation: `WaitHowBig` -> `WaitHowBigNow` ->
  `WaitHowBigHQ` -> `WaitHowBigDaily`.
- X accepted `@WaitHowBig`. Last verified profile work still needed: upload the
  brand avatar/banner, apply the canonical bio, and if the platform allows,
  change display name from `Wait How Big` to `Wait, How Big?`.
- Instagram is a free public Creator account in the Education category, with
  the brand avatar and bio published.
- TikTok accepted the next-priority handle `@waithowbignow`. The saved bio was
  the ASCII fallback `Big numbers into pictures. One scale check a day.`; the
  brand avatar still needed a live re-check/upload.

Canonical bios recovered during setup:

- X: `The world, finally in perspective. Original scale comparisons, strange numbers, and visual answers. New posts daily.`
- Instagram: `The world, finally in perspective. 📏 Original visual comparisons daily.`
- TikTok target: `Big numbers → pictures. One scale check a day. 📏`

Temporary downloaded assets used during bootstrap were:

- `/private/tmp/whb-assets.ea8bz6/avatar.png` (1024x1024)
- `/private/tmp/whb-assets.ea8bz6/x-banner.png` (1500x500)

Those are ephemeral paths and may no longer exist. Recover authoritative assets
from the project/Drive launch packet rather than inventing replacements.

## Repository and operator files

- Default branch used by GitHub Actions: `main`
- Historical media-preparation branch: `wait-how-big-social-media`
- Workflow: `.github/workflows/wait-how-big-operator.yml`
- Operator documentation: `wait-how-big-social/operator/README.md`
- Operator archive: `wait-how-big-social/operator/operator_bundle.zip`
- Persisted state: `wait-how-big-social/operator/state.json`
- Archive contents: `operator.py` and `queue.json`
- Canonical Drive references: `MANUAL_BOOTSTRAP.md` and
  `ACCOUNT_LAUNCH_PACKET.md` (not stored in this repository)
- Original ChatGPT-project mirror:
  `/Users/pchordia/.codex/.chatgpt-projects/g-p-6a8c5f876b00819199ed0d8bec3aab98`
- Older project-local attempt:
  `/Users/pchordia/Documents/Codex/2026-08-24/objective-create-and-configure-the-project`

The queue contains 13 verified 1080x1920 H.264/AAC launch videos. The first
three queue items are the brand trailer at relative hour 0, million-vs-billion
seconds at +2 hours, and 30 Earths to the Moon at +7 hours. The operator caps
each channel at ten scheduled posts, validates media URLs, and rebuilds
idempotency from Buffer post history.

## Current blockers and live failure evidence

1. **No Buffer key exists in GitHub.** `WHB_BUFFER_API_KEY` is absent, so the
   workflow environment currently receives an empty `BUFFER_API_KEY`.
2. **The packaged script has a Python module-name collision.** The archive
   extracts `operator.py` and the workflow executes it from the same directory.
   Python's standard library imports `operator`, resolves the local script, and
   fails with a circular import (`cannot import name 'eq' from partially
   initialized module 'operator'`). Rename the executable (for example,
   `whb_operator.py`), rebuild the archive deterministically, update the
   workflow command and SHA-256, and add a smoke test.
3. Scheduled runs currently execute every three hours at minute 17 and fail.
   The latest inspected failure was Actions run `33342049065` on 2026-08-30.
   No post was created by these failures.
4. `wait-how-big-social/operator/state.json` still reports
   `awaiting_accounts_and_buffer_key`, `anchor_utc: null`, no posts, and no
   successful run timestamp.

## Safe completion order

1. Fix and test the `operator.py` module-name collision before adding a secret.
2. Keep the operator unable to publish while bootstrapping. Set repository
   variable `WHB_KILL_SWITCH=true` or temporarily remove the cron trigger before
   adding the secret; otherwise the next scheduled run is a normal run.
3. In Buffer Settings -> API, recreate the key form with name
   `Wait How Big Operator`, expiry `1 year`, and only `account:read`,
   `posts:read`, `posts:write`.
4. Immediately before clicking **Generate API Key**, obtain explicit owner
   confirmation because this creates persistent posting access.
5. Transfer the generated value directly into GitHub Actions secret
   `WHB_BUFFER_API_KEY`. Do not echo it, copy it into shell history, save it to
   a file, commit it, or include it in logs/chat.
6. With publishing still blocked, dispatch **Wait How Big social operator**
   using `dry_run=true`. Verify the run, logs, three exact channels, media
   checks, and persisted state. A dry run must create no Buffer/public posts.
7. Review the dry-run diff/state. Only after a separate explicit owner decision,
   clear the kill switch/restore the cron and perform one normal run.
8. Verify resulting Buffer post IDs, public URLs, timestamps, captions, and
   media on all three platforms before calling the experiment launched.

## Constraints

- Spend $0: no card, paid plan, upgrade, or trial.
- Never bypass CAPTCHA, 2FA, age/birth-date checks, device verification, or
  platform agreements.
- Preserve unrelated social accounts and Buffer workspaces.
- Do not store credentials or tokens in chat, files, commits, issues, or logs.
- Pause for password/passkey entry, CAPTCHA, 2FA, private birth information,
  platform agreements, API-key generation, secret installation, and any live
  publish action.
