# Cursor handoff prompt

Copy the block below into a new Cursor conversation from a clean clone of
`pri8771/orchestrator`.

```text
Continue the Wait, How Big? social-launch bootstrap in repository
https://github.com/pri8771/orchestrator.

Read these files first:
- wait-how-big-social/README.md
- wait-how-big-social/operator/README.md
- .github/workflows/wait-how-big-operator.yml
- wait-how-big-social/operator/state.json
- inspect wait-how-big-social/operator/operator_bundle.zip without exposing secrets

Verified external state:
- Email alias: waithowbig@unsubscriber.me, alias of pchordia@unsubscriber.me.
  Gmail login is pchordia@unsubscriber.me; the alias is not a separate mailbox.
- X: @WaitHowBig, https://x.com/WaitHowBig, connected to Buffer.
- Instagram: @waithowbig, https://www.instagram.com/waithowbig/, public free
  Creator/Education account, connected to Buffer.
- TikTok: @waithowbignow, https://www.tiktok.com/@waithowbignow, connected to Buffer.
- Buffer Free account login identity: waithowbig@unsubscriber.me. Its email is
  verified and it has exactly the three channels above.
- Buffer channel IDs for cross-checking only: X
  6a8f2926ccaf649a671fa86d, Instagram 6a8f1d89ccaf649a671f69fc,
  TikTok 6a8fb214ccaf649a6724d002.
- Never use unrelated Buffer onepersonops@unsubscriber.me, X @OnePerson0ops or
  @OnePersonOpsMgr, or TikTok @wootworwoot / @appleuser535236940.
- No passwords, passkeys, 2FA codes, API keys, OAuth codes, or recovery data are
  stored here. Stop and hand off whenever the owner must enter or approve them.

Current GitHub evidence as of 2026-08-31:
- gh secret list -R pri8771/orchestrator shows no WHB_BUFFER_API_KEY.
- wait-how-big-social/operator/state.json is still
  awaiting_accounts_and_buffer_key with no post receipts.
- Scheduled workflow runs are failing. Run 33342049065 shows two independent
  blockers: BUFFER_API_KEY is empty, and the archive's operator.py shadows
  Python's stdlib operator module, causing a circular-import ImportError before
  main() can fail closed.
- The workflow cron runs every three hours at minute 17 and scheduled runs use
  dry_run=false. Do not add a usable secret while the cron can publish.

Your tasks, in order:
1. Fetch origin/main, inspect the worktree, and preserve unrelated changes.
2. Fix the module-name collision safely. Prefer renaming operator.py inside the
   archive to whb_operator.py, update the workflow command, rebuild the zip
   deterministically, update its verified SHA-256, and add a regression/smoke
   test proving the packaged executable starts under Python 3.12 without
   shadowing stdlib operator. Do not weaken checksum verification.
3. Add a safe bootstrap gate so a newly installed secret cannot be consumed by
   the recurring normal schedule before a successful manual dry run. Preserve
   WHB_KILL_SWITCH support and fail-closed behavior. Explain the chosen guard.
4. Run focused tests plus the relevant repository verification checks. Commit
   and push code/doc changes with evidence.
5. Re-check Buffer's three exact connected channels and current public profile
   baselines. Do not infer current engagement from the old zero baseline.
6. In Buffer Settings -> API, recreate a personal key named "Wait How Big
   Operator", expiry one year, with only account:read, posts:read, posts:write.
   Stop immediately before Generate API Key and obtain explicit owner approval.
7. After approval, transfer the key directly to GitHub Actions secret
   WHB_BUFFER_API_KEY without printing it, placing it in shell history, saving
   it to disk, chat, logs, issues, or commits. Verify only the secret name exists.
8. Keep publishing blocked and dispatch the workflow once with dry_run=true.
   Verify success, exact three channels, media checks, no Buffer/public posts,
   and the resulting state.json receipt. If it fails, diagnose and fix; do not
   proceed to a normal run.
9. Stop before clearing the kill switch or doing a normal/publishing run. Report
   the dry-run evidence and ask for a separate explicit owner decision.

Constraints:
- Spend $0; no paid plan, card, trial, or upgrade.
- Do not bypass CAPTCHA, 2FA, device/age verification, or agreements.
- Preserve unrelated accounts/workspaces and user changes.
- Never reveal or persist credentials/tokens.
- Plans, connected channels, scheduled jobs, and dry runs are not publication.
  A launch requires verified public post IDs/URLs/timestamps on all three sites.

Brand/profile follow-up after the operator is safe:
- X still needs live re-check of avatar, banner, punctuation in display name,
  and canonical bio.
- TikTok still needs live re-check/upload of the brand avatar; the actual saved
  bio was "Big numbers into pictures. One scale check a day."
- Temporary bootstrap assets were
  /private/tmp/whb-assets.ea8bz6/avatar.png and
  /private/tmp/whb-assets.ea8bz6/x-banner.png, but they are ephemeral; recover
  authoritative assets from MANUAL_BOOTSTRAP.md / ACCOUNT_LAUNCH_PACKET.md in
  the project Drive if those files are gone.

Work autonomously through safe read-only and code changes. Pause only at real
owner gates: password/passkey, CAPTCHA, 2FA, private birth information,
agreements, API-key generation, secret installation, or any live publish action.
```
