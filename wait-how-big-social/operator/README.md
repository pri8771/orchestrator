# Wait, How Big? operator — BOTS-117 release repair

The operator is runnable and fail-closed. This repair changes local source and tests only; it does not claim a current live key, successful Buffer run, scheduled job or public post. The account/channel observations in `../README.md` are historical and must be rechecked by the release owner.

## What changed

`whb_operator.py`, `queue.json` and `config.json` are now plain reviewable source beside their deterministic `operator_bundle.zip`. The executable no longer shadows Python's `operator` module. The thirteen queue items, captions and three established channel IDs are preserved. WHB-001 now references the [reviewed media correction](../assets/reviewed/README.md); the other twelve media URLs are unchanged.

A read-only dry run is explicitly allowed while `WHB_KILL_SWITCH=true` or local publication is paused. It queries the intended channels/history, checks media availability and writes only `plan.json`. It never writes fake post IDs/statuses, actual posts, or the accepted scheduling anchor. The proposed anchor and proposed payloads are labeled planned. Current-state input `state.json` remains byte-for-byte unchanged.

Bootstrap is an exact successful plan, valid for at most 24 hours, bound to queue bytes, operator source bytes, config bytes and the observed organization/channel identities. A successful timestamp alone is insufficient. A release also requires the exact canonical plan hash and selected payload. Source, captions, media URLs, IDs, configuration or channel changes invalidate the old bootstrap/grant.

The operator identifies the unique Buffer organization containing the three exact configured channel IDs; it never selects the first organization or first account of a platform. Discovery is bounded to five organizations. Original IDs remain:

- X `WaitHowBig`: `6a8f2926ccaf649a671fa86d`
- Instagram `waithowbig`: `6a8f1d89ccaf649a671f69fc`
- TikTok `waithowbignow`: `6a8fb214ccaf649a6724d002`

## Local checks without credentials

From the repository root:

```sh
python3 -m unittest discover -s wait-how-big-social/tests -v
python3 wait-how-big-social/operator/build_bundle.py
```

The tests substitute all provider/media calls and include a real child-process exit after durable intent. They verify exact channel selection, dry-run/actual separation, bootstrap/plan/grant drift, no automatic resend, payload readback, reproducible archive bytes, and clean packaged execution without a key. No API or media request occurs during these checks. Use Python 3.10+; the workflow selects Python 3.12.

The builder only packages the three named plain files with fixed metadata and updates the one workflow checksum. Rebuild after source/config/queue changes. Do not edit the archive by itself.

## GitHub stays read-only

The existing three-hour schedule is retained, but scheduled runs are always dry runs. Manual dispatch defaults `dry_run=true`. `dry_run=false` refuses publication on GitHub even if a secret, grant, or purported local-state path is supplied.

The workflow has `contents: read`, uploads only the planned validation artifact, and does not commit/push runtime state. A commit, cache or artifact after a POST cannot protect against runner loss before that save. This repair therefore **does not enable normal GitHub publishing**. The root release owner may integrate this safe workflow without enabling an unattended publishing loop.

A successful GitHub plan can be inspected and transferred to the same-byte local deployment, but its exact bindings and expiry still apply. The workflow's ten-minute execution timeout bounds read-only validation; no LLM remains running.

## One exact canary on a durable local host

The release owner supplies an already-authorized `BUFFER_API_KEY` securely through the environment. This code does not inspect/generate keys, purchase plans or change account/production settings. Key/entitlement and current Buffer schema acceptance remain live checks for root; tests do not prove them.

Choose one stable local host and absolute local-disk state directory, outside repository checkout, cloud sync and network shares. All invocations must share that directory; copied independent state is not a distributed lease. Keep a backup. A single-run external process timeout is appropriate; a killed process leaves a durable intent/lock for explicit recovery.

First, leave publishing held and make a read-only plan. Example PowerShell environment, with the existing key supplied externally:

```powershell
$env:WHB_DURABLE_STATE_DIR = 'D:\AstraState\wait-how-big'
$env:WHB_DRY_RUN = 'true'
$env:WHB_KILL_SWITCH = 'true'
python 'D:\DEPLOYMENT\wait-how-big-social\operator\whb_operator.py'
```

`plan.json` is written beside the source, separate from actual state. Its output includes `plan_sha256` and four binding hashes. Review its exact channel, caption, media, due time and current media/source acceptance. Read history through the official account: the API's latest 100 posts per status per channel is a bounded observation, **not proof of lifetime absence**. Preserve that actual review in `history_review_ref`. If a schedule is now past due, set `WHB_PROPOSED_ANCHOR_UTC` to an explicitly reviewed future UTC time during the next dry run; this changes the proposed plan without rewriting actual historical anchor.

Copy `canary-grant.example.json` to an untracked `*.grant.json`. Fill the actual authorization reference, history review reference, expiry, exact reviewed `plan_sha256`, four bindings, and one target such as `WHB-000:twitter`. Only the root's already-authorized reviewed release sets `authorized: true`. The included example is intentionally invalid/expired. A grant never selects more than one mutation.

When root admits that exact canary:

```powershell
$env:WHB_DRY_RUN = 'false'
$env:WHB_KILL_SWITCH = 'false'
$env:WHB_MANUAL_CANARY = 'true'
$env:WHB_CANARY_GRANT = 'D:\AstraConfig\WHB-000-twitter.grant.json'
python 'D:\DEPLOYMENT\wait-how-big-social\operator\whb_operator.py'
```

No production variable is changed by this document. The operator requires a local absolute state path, exact current bindings, fresh valid plan and grant, intended queue payload/channel, active channel, current capacity, available media and future due time. It takes a sole-writer lock and refreshes history after locking. It records and flushes an intent with exact payload/authority **before** the only mutation. Its existing limit of ten scheduled posts per channel remains enforced. No generic batch-release flag exists; root admits each subsequent target independently.

## Effect recovery and honest receipts

Actual state records are separate from the plan. Each target progresses from durable `intent` to `uncertain` to `verified_buffer`. Provider-accepted IDs are saved before readback. A Buffer receipt is accepted only when channel, caption, media URL, scheduled due time and provider ID match the intended payload. Matching only a caption or media URL is a conflict, not idempotency evidence. A verified scheduled Buffer record is not yet a public launch; root verifies the actual destination permalink, rendered media/caption, account and timestamp.

A timeout, rejected/malformed mutation, interrupted host process or missing readback cannot trigger an automatic resend. A replay finds the existing durable effect and exits. Other uncertain effects also block new mutations. No missing-history result resets an effect.

For a read-only recovery on the same durable host:

```powershell
$env:WHB_DRY_RUN = 'false'
$env:WHB_KILL_SWITCH = 'true'
$env:WHB_RECONCILE = 'true'
python 'D:\DEPLOYMENT\wait-how-big-social\operator\whb_operator.py'
```

Reconciliation is permitted while publishing is held. It accepts exactly one full payload/due-time match, and preserves a known provider-accepted ID. Missing/multiple matches remain uncertain. This version does not automatically clear or resend a genuinely failed uncertain target.

If the host was killed, `operator.lock` may remain. First establish that no publisher process is running and inspect the persisted state/effect with root. Preserve the lock and intent evidence, then root may remove the stale lock to allow **read-only reconciliation**. Do not delete the effect or restore a clean initial state to get around it. If the provider cannot resolve the outcome, keep that target blocked. Scheduled public content requires official-account moderation/withdrawal by the release owner; a local hold does not delete a public post.

## Scope and release limitation

[BOTS-117](https://priyanshchordia-1779372280524.atlassian.net/browse/BOTS-117) is the real source association. No native Jira edit or Done transition is performed. Historical account/launch directions and existing independent account grants remain authoritative for root's release; this repair creates none.

Usable now: reviewable source, deterministic bundle, offline tests, read-only bootstrap while held, and a narrowly granted local canary with durable intent/reconciliation. Remaining live inputs: independent final review/integration, current official account/API/media verification, exact release grant, durable host admission and destination readback. Continuous GitHub publishing remains unavailable until a separately reviewed durable remote intent mechanism exists; this is an explicit safe release boundary, not a claimed autonomous launch.

## Live history-filter correction — BOTS-117

On September 13, 2026, Buffer returned an empty collection for the combined status filter although the known WHB-001 post was returned by a single-status query and a direct-ID lookup. Sorting was checked independently and did not cause the omission. The adapter now sends one request with five aliased single-status queries, each bounded to the latest 100 posts for the exact channel. It returns at most 500 records per channel. Missing or malformed branches, invalid channel/status records and duplicate IDs fail closed. An explicit valid empty collection remains empty. This is bounded history, not proof of lifetime absence.

The first WHB-001 X post is already public. Keep its durable effect and publication receipt; do not recreate it as a test. This patch changes history lookup only, not credentials, media, authorization, mutation retry, or unattended publishing policy.
