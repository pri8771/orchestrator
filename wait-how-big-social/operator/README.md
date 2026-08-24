# Wait, How Big? social operator

The account-creation step requires a person to complete platform signup, CAPTCHA/2FA, age confirmation, and agreements. Everything after that is prepared here.

This operator remains inert until the three social accounts are connected to Buffer and the repository secret `WHB_BUFFER_API_KEY` exists.

## One-time owner bootstrap

1. Create public accounts for **Wait, How Big?** on X, Instagram, and TikTok using `pchordia@unsubscriber.me`.
2. Create a free Buffer account using that inbox and connect those exact three accounts.
3. In Buffer **Settings → API**, create a personal API key with posting permission.
4. In GitHub repository `pri8771/orchestrator`, add the key as the Actions secret `WHB_BUFFER_API_KEY`. Never place it in a file, issue, commit, or chat.
5. Run **Wait How Big social operator** once with `dry_run=true`. Review `state.json`, then run it normally.

## Behavior

- Discovers the Buffer organization and connected channels automatically.
- Requires one unlocked X, one Instagram, and one TikTok channel; otherwise it fails closed.
- Verifies every direct media URL before creating a post.
- Rebuilds idempotency from Buffer post history, so a state-write failure does not duplicate posts.
- Keeps at most ten scheduled posts per channel.
- Uses platform-specific captions and exact scheduled times.
- Stops when Buffer reports an existing post error.
- Supports an emergency repository variable: set `WHB_KILL_SWITCH=true` to stop all posting.

The operator source and queue are contained in `operator_bundle.zip`; the workflow verifies its SHA-256 before execution. No social credentials or API keys are stored in this directory.
