"""Token/cost accounting (V3 6.4): the persisted, honest record of what each
turn actually cost.

Per-turn usage (real token counts from api:/local runners, harvested by
_call_agent_once off the traces side-channel) is appended as one JSON line to
<app_dir>/costs.jsonl, then rolled up per session / project / section.
Honesty rules (R2/R3/§13.2) shape everything here:

  - CLI-subscription turns report no tokens -> recorded UNMETERED (tokens
    null), never $0.00, never an estimate. The denominator (total turns)
    stays complete so coverage can't be overstated.
  - A metered turn on a model with no verified price records its tokens with
    cost_micro_usd null -> UNPRICED. We never guess a price.
  - All money is integer micro-USD (§6.6); rounding happens only at render.

This file is a stdlib LEAF (like docs.py/events.py): no orchestrator import,
no third-party deps. Callers inject anything discovery-shaped.

The costs.jsonl schema is a versioned CONTRACT — 7.5's budget termination
reads these records; bump SCHEMA_VERSION on any breaking field change.
"""
import json
import os

SCHEMA_VERSION = 1
COSTS_FILENAME = "costs.jsonl"

# Mirrors events.py's PIPE_BUF discipline: POSIX only guarantees atomic
# appends up to PIPE_BUF (>=512, 4096 on Linux); events.py uses 3500 and so
# do we. Cost records are numeric and tiny — the guard is belt-and-braces.
_MAX_LINE_BYTES = 3500

# Verified per-MTok prices in integer micro-USD, keyed (provider, model-id
# prefix); longest prefix wins. Anthropic prices verified 2026-07-19 against
# the API reference. sonnet-5 has an intro price ($2/$10 through 2026-08-31)
# — we pin the STANDARD price: over-stating spend is a tolerable error,
# under-stating is not. OpenAI/Google prices are deliberately ABSENT until
# verified against a source: their turns record tokens with cost null
# (unpriced) rather than a guessed number (R3).
PRICES = {
    ("anthropic", "claude-fable-5"): (10_000_000, 50_000_000),
    ("anthropic", "claude-mythos-5"): (10_000_000, 50_000_000),
    ("anthropic", "claude-opus-4"): (5_000_000, 25_000_000),
    ("anthropic", "claude-sonnet-5"): (3_000_000, 15_000_000),
    ("anthropic", "claude-sonnet-4"): (3_000_000, 15_000_000),
    ("anthropic", "claude-haiku-4-5"): (1_000_000, 5_000_000),
}

_MTOK = 1_000_000


def costs_path(app_dir):
    return os.path.join(app_dir, COSTS_FILENAME)


def parse_usage(tokens):
    """Normalize a traces-side-channel usage dict ({provider?, model?,
    prompt_tokens?, completion_tokens?}) into (provider, model, tokens_in,
    tokens_out) — or None when there's no usable token data (the unmetered
    case). Never raises: a corrupt stash must not take a turn down.

    run_local's stash has counts but no provider/model — that is still
    METERED (real tokens) but unpriced (provider None finds no price)."""
    if not isinstance(tokens, dict):
        return None
    tin = tokens.get("prompt_tokens")
    tout = tokens.get("completion_tokens")
    if not isinstance(tin, int) or not isinstance(tout, int) \
            or isinstance(tin, bool) or isinstance(tout, bool) \
            or tin < 0 or tout < 0:
        return None
    provider = tokens.get("provider")
    model = tokens.get("model")
    return (provider if isinstance(provider, str) and provider else None,
            model if isinstance(model, str) and model else None,
            tin, tout)


def price_for(provider, model):
    """(input, output) micro-USD per MTok for the longest matching model-id
    prefix under this provider, or None. Never guesses."""
    if not provider or not model:
        return None
    best = None
    for (p, prefix), rates in PRICES.items():
        if p == provider and model.startswith(prefix):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), rates)
    return best[1] if best else None


def cost_micro_usd(provider, model, tokens_in, tokens_out):
    """Integer micro-USD for a turn, or None when the model has no verified
    price. Integer math end-to-end: round-half-up at the final division so
    a full-precision fixture reproduces hand-computed totals exactly."""
    rates = price_for(provider, model)
    if rates is None:
        return None
    rin, rout = rates
    return (tokens_in * rin + tokens_out * rout + _MTOK // 2) // _MTOK


def turn_record(agent, tokens, ts):
    """Build one costs.jsonl record for a completed turn. `tokens` is the
    traces stash (may be None -> unmetered). Caller supplies ts (now_str)."""
    parsed = parse_usage(tokens)
    rec = {"v": SCHEMA_VERSION, "ts": ts, "agent": str(agent)}
    if parsed is None:
        rec.update(provider=None, model=None, input_tokens=None,
                   output_tokens=None, cost_micro_usd=None, metered=False)
        return rec
    provider, model, tin, tout = parsed
    rec.update(provider=provider, model=model, input_tokens=tin,
               output_tokens=tout,
               cost_micro_usd=cost_micro_usd(provider, model, tin, tout),
               metered=True)
    return rec


def record_turn(app_dir, rec):
    """Append one record to <app_dir>/costs.jsonl. Best-effort (mirrors
    events.emit_event): disk problems return False, never raise, never take
    a run down. Returns True when the line hit disk."""
    try:
        line = json.dumps(rec, ensure_ascii=False)
        if len(line.encode("utf-8")) > _MAX_LINE_BYTES:
            # Numeric records can't get here; a bloated string field (e.g. a
            # pathological agent id) gets truncated rather than dropped.
            rec = dict(rec, agent=str(rec.get("agent", ""))[:200])
            line = json.dumps(rec, ensure_ascii=False)
        with open(costs_path(app_dir), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception:  # noqa: BLE001 - accounting must never crash a run
        return False


def read_records(app_dir):
    """All parseable records, oldest first. Malformed/alien lines are
    skipped (§: costs failures never take the GUI scan down)."""
    out = []
    try:
        with open(costs_path(app_dir), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


def _empty_totals():
    return {"metered_turns": 0, "unmetered_turns": 0, "unpriced_turns": 0,
            "tokens_in": 0, "tokens_out": 0, "cost_micro_usd": 0}


def _fold(totals, rec):
    if rec.get("metered") and isinstance(rec.get("input_tokens"), int) \
            and isinstance(rec.get("output_tokens"), int):
        totals["metered_turns"] += 1
        totals["tokens_in"] += rec["input_tokens"]
        totals["tokens_out"] += rec["output_tokens"]
        cost = rec.get("cost_micro_usd")
        if isinstance(cost, int) and not isinstance(cost, bool):
            totals["cost_micro_usd"] += cost
        else:
            # Metered but no verified price: counted separately so a mixed
            # rollup renders as '>= $X.XX · N unpriced', never a bare total.
            totals["unpriced_turns"] += 1
    else:
        totals["unmetered_turns"] += 1
    return totals


def rollup(app_dir):
    """Totals for one session dir. Honesty survives aggregation: the
    denominator (metered+unmetered) equals total recorded turns, and
    unpriced metered turns are counted, not silently folded."""
    totals = _empty_totals()
    for rec in read_records(app_dir):
        _fold(totals, rec)
    return totals


def rollup_workspace(root, app_ids):
    """Aggregate across sessions. `app_ids` comes from the caller (the
    engine's find_apps(root) — injected, not imported, to keep this module
    a leaf). Ids are 'project' (flat) or 'project/section/chat' (nested,
    V3 3.0 layout); legacy '--'-joined flat names degrade to session+project
    totals — their name shape is opaque here and that is fine."""
    per_session, per_project, per_section = {}, {}, {}
    grand = _empty_totals()
    for app_id in app_ids or []:
        totals = rollup(os.path.join(root, str(app_id)))
        per_session[str(app_id)] = totals
        parts = str(app_id).split("/")
        proj = parts[0]
        per_project.setdefault(proj, _empty_totals())
        for key, val in totals.items():
            per_project[proj][key] += val
            grand[key] += val
        if len(parts) >= 2:
            sec = parts[0] + "/" + parts[1]
            per_section.setdefault(sec, _empty_totals())
            for key, val in totals.items():
                per_section[sec][key] += val
    return {"schema_version": SCHEMA_VERSION, "total": grand,
            "per_session": per_session, "per_project": per_project,
            "per_section": per_section}
