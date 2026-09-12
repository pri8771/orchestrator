#!/usr/bin/env python3
"""Fail-closed Buffer operator for the Wait, How Big? launch queue (executable: whb_operator.py).

The only secret is BUFFER_API_KEY, supplied through the environment. The script
never prints it or writes it to disk. It discovers the Buffer organization and
connected X, Instagram, and TikTok channels, rebuilds idempotency from Buffer's
own post history, and keeps no more than ten scheduled posts per channel.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

API_URL = "https://api.buffer.com"
ROOT = Path(__file__).resolve().parent
QUEUE_PATH = ROOT / "queue.json"
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.json"
PLAN_PATH = ROOT / "plan.json"
MAX_SCHEDULED_PER_CHANNEL = 10
REQUIRED = ("twitter", "instagram", "tiktok")
SERVICE_ALIASES = {
    "twitter": {"twitter", "x", "x_twitter"},
    "instagram": {"instagram"},
    "tiktok": {"tiktok", "tik_tok"},
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise RuntimeError("Timezone is required")
    return result.astimezone(timezone.utc)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("Buffer redirect refused")


def gql(api_key: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "wait-how-big-operator/1.0",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise RuntimeError("Buffer response exceeds bounded size")
            payload = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Buffer HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError("Buffer network error") from None
    if payload.get("errors"):
        raise RuntimeError("Buffer GraphQL error; inspect through the official account")
    return payload.get("data") or {}


def get_org_and_channels(api_key: str) -> tuple[str, dict[str, dict[str, Any]]]:
    expected = load_json(CONFIG_PATH, {})["channels"]
    account = gql(
        api_key,
        """query GetOrganizations {
          account { organizations { id } }
        }""",
    )
    organizations = ((account.get("account") or {}).get("organizations") or [])
    if not organizations:
        raise RuntimeError("No Buffer organization is available for this API key.")
    if len(organizations) > 5:
        raise RuntimeError("Organization discovery exceeds bounded scope")
    matches = []
    for org in organizations:
        org_id = org["id"]
        data = gql(
            api_key,
        """query GetChannels($organizationId: OrganizationId!) {
          channels(input: { organizationId: $organizationId, filter: { isLocked: false } }) {
            id name displayName service isQueuePaused
          }
        }""",
            {"organizationId": org_id},
        )
        selected: dict[str, dict[str, Any]] = {}
        for channel in data.get("channels") or []:
            raw = str(channel.get("service") or "").lower()
            for canonical, aliases in SERVICE_ALIASES.items():
                if raw in aliases and channel.get("id") == expected[canonical]["id"]:
                    if canonical in selected:
                        raise RuntimeError("Duplicate expected channel")
                    selected[canonical] = channel
        if set(selected) == set(REQUIRED): matches.append((org_id, selected))
    if len(matches) != 1:
        raise RuntimeError("Exact three existing WHB channels not uniquely verified")
    return matches[0]


def get_channel_posts(api_key: str, org_id: str, channel_id: str) -> list[dict[str, Any]]:
    data = gql(
        api_key,
        """query GetPosts($organizationId: OrganizationId!, $channelId: ChannelId!) {
          posts(
            first: 100
            input: {
              organizationId: $organizationId
              sort: [{ field: createdAt, direction: desc }]
              filter: {
                channelIds: [$channelId]
                status: [scheduled, sending, needs_approval, sent, error]
              }
            }
          ) {
            edges {
              node {
                id text dueAt status channelId externalLink
                assets { source }
              }
            }
          }
        }""",
        {"organizationId": org_id, "channelId": channel_id},
    )
    return [edge.get("node") or {} for edge in ((data.get("posts") or {}).get("edges") or [])]


def media_available(url: str) -> bool:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "wait-how-big-operator/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            return response.status == 200 and ("video" in content_type or url.lower().endswith(".mp4"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return False


def create_video_post(
    api_key: str,
    service: str,
    channel_id: str,
    text: str,
    media_url: str,
    due_at: datetime,
    thumbnail_offset_ms: int,
) -> dict[str, Any]:
    video: dict[str, Any] = {"url": media_url}
    if service in {"instagram", "tiktok"}:
        video["metadata"] = {"thumbnailOffset": int(thumbnail_offset_ms)}

    post_input: dict[str, Any] = {
        "text": text,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": "customScheduled",
        "dueAt": iso(due_at),
        "aiAssisted": True,
        "needsApproval": False,
        "saveToDraft": False,
        "source": "wait-how-big-operator",
        "assets": [{"video": video}],
    }
    if service == "instagram":
        post_input["metadata"] = {
            "instagram": {
                "type": "reel",
                "shouldShareToFeed": True,
                "isAiGenerated": True,
            }
        }
    elif service == "tiktok":
        post_input["metadata"] = {"tiktok": {"isAiGenerated": True}}
    elif service == "twitter":
        post_input["metadata"] = {"twitter": {"isAiGenerated": True}}

    data = gql(
        api_key,
        """mutation CreatePost($input: CreatePostInput!) {
          createPost(input: $input) {
            ... on PostActionSuccess {
              post { id text dueAt status channelId externalLink assets { source } }
            }
            ... on MutationError { message }
          }
        }""",
        {"input": post_input},
    )
    result = data.get("createPost") or {}
    if result.get("message"):
        raise RuntimeError("Buffer rejected post")
    post = result.get("post")
    if not post:
        raise RuntimeError("Buffer returned no post object")
    return post


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def binding(org_id: str, channels: dict[str, Any]) -> dict[str, str]:
    return {
        "queue_sha256": sha(QUEUE_PATH.read_bytes()),
        "source_sha256": sha(Path(__file__).read_bytes()),
        "config_sha256": sha(CONFIG_PATH.read_bytes()),
        "channels_sha256": sha(canonical({"organization_id": org_id,
            "channels": {s: {"id": channels[s]["id"], "service": channels[s]["service"],
                              "isQueuePaused": channels[s].get("isQueuePaused")} for s in REQUIRED}})),
    }


def default_state() -> dict[str, Any]:
    return {"version": 2, "paused": False, "anchor_utc": None, "posts": {}, "effects": {}, "errors": []}


def exact_match(post: dict[str, Any], payload: dict[str, Any], require_due: bool = False) -> bool:
    if not post.get("id") or post.get("id") == "dry-run": return False
    if post.get("channelId") != payload["channel_id"] or post.get("text") != payload["caption"]:
        return False
    if not any(a.get("source") == payload["media_url"] for a in post.get("assets") or []):
        return False
    if require_due:
        try:
            if parse_iso(post["dueAt"]) != parse_iso(payload["due_at"]): return False
        except (ValueError, KeyError, TypeError): return False
    return True


def validate_queue(queue_doc: dict[str, Any]) -> list[dict[str, Any]]:
    queue = queue_doc.get("queue") or []
    if not 1 <= len(queue) <= 13: raise RuntimeError("Queue must contain 1 to 13 items")
    ids = [item.get("content_id") for item in queue]
    if any(not x for x in ids) or len(set(ids)) != len(ids): raise RuntimeError("Queue IDs must be unique")
    for item in queue:
        if not str(item.get("media_url", "")).startswith("https://raw.githubusercontent.com/pri8771/orchestrator/"):
            raise RuntimeError("Unexpected mission media origin")
        if set(item.get("captions", {})) != set(REQUIRED): raise RuntimeError("Three platform captions required")
        if not all(isinstance(c, str) and c.strip() for c in item["captions"].values()):
            raise RuntimeError("Nonempty captions required")
        if not isinstance(item.get("relative_hours"), (int, float)) or not 0 <= item["relative_hours"] <= 1000:
            raise RuntimeError("Relative schedule outside bounded queue")
    return queue


def build_plan(queue: list[dict[str, Any]], state: dict[str, Any], org_id: str,
               channels: dict[str, Any], posts_by_service: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    now = utc_now()
    # This is a proposed anchor only. Never write it into actual state during dry-run.
    proposed = os.environ.get("WHB_PROPOSED_ANCHOR_UTC")
    anchor = parse_iso(proposed) if proposed else (parse_iso(state["anchor_utc"]) if state.get("anchor_utc") else now + timedelta(minutes=30))
    planned, observed, conflicts = [], [], []
    media_cache = {}
    for service in REQUIRED:
        posts = posts_by_service[service]
        if any(p.get("status") == "error" for p in posts):
            raise RuntimeError("Existing Buffer errors require review")
        count = sum(p.get("status") in {"scheduled", "sending", "needs_approval"} for p in posts)
        capacity = max(0, MAX_SCHEDULED_PER_CHANNEL - count)
        for item in queue:
            payload = {"content_id": item["content_id"], "service": service,
                       "channel_id": channels[service]["id"], "caption": item["captions"][service],
                       "media_url": item["media_url"], "thumbnail_offset_ms": int(item.get("thumbnail_offset_ms", 2000))}
            matches = [p for p in posts if exact_match(p, payload)]
            partial = [p for p in posts if p.get("text") == payload["caption"] or
                       any(a.get("source") == payload["media_url"] for a in p.get("assets") or [])]
            key = item["content_id"] + ":" + service
            if len(matches) == 1:
                observed.append({"target": key, "post_id": matches[0]["id"], "status": matches[0].get("status")})
                continue
            if partial:
                conflicts.append({"target": key, "reason": "multiple_or_partial_payload_matches"})
                continue
            if capacity == 0: continue
            if payload["media_url"] not in media_cache:
                media_cache[payload["media_url"]] = media_available(payload["media_url"])
            if not media_cache[payload["media_url"]]: raise RuntimeError("Media unavailable; bootstrap not accepted")
            due = anchor + timedelta(hours=float(item["relative_hours"]))
            if due <= now + timedelta(minutes=10):
                raise RuntimeError("Past-due queue requires a reviewed new scheduling plan")
            payload["due_at"] = iso(due)
            planned.append({"target": key, "kind": "planned_only", "payload": payload,
                            "payload_sha256": sha(canonical(payload))})
            capacity -= 1
    if conflicts: raise RuntimeError("Conflicting historical posts require review")
    return {"version": 2, "jira_key": "BOTS-117", "result": "dry_run_ok",
            "created_at": iso(now), "binding": binding(org_id, channels),
            "organization_id": org_id, "proposed_anchor_utc": iso(anchor),
            "planned": planned, "observed_existing": observed,
            "publication_held": True, "actual_mutations": 0,
            "history_limit": "latest_100_per_channel_is_not_proof_of_lifetime_absence",
            "normal_github_publishing": "disabled_ephemeral_runner_state"}


@contextlib.contextmanager
def state_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "operator.lock"
    try: fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError: raise RuntimeError("State lock exists; review host/process and intents before recovery") from None
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "created_at": iso(utc_now())}))
            stream.flush(); os.fsync(stream.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


def record_post(state: dict[str, Any], target: str, effect: dict[str, Any], post: dict[str, Any]) -> None:
    payload = effect["payload"]
    state.setdefault("posts", {}).setdefault(payload["content_id"], {})[payload["service"]] = {
        "post_id": post["id"], "status": post.get("status"), "due_at": post.get("dueAt"),
        "external_link": post.get("externalLink"), "channel_id": payload["channel_id"],
        "verification": "exact_Buffer_history_readback", "verified_at": iso(utc_now())}
    effect.update({"state": "verified_buffer", "post_id": post["id"], "verified_at": iso(utc_now())})
    if not state.get("anchor_utc") and effect.get("proposed_anchor_utc"):
        state["anchor_utc"] = effect["proposed_anchor_utc"]
    state["last_result"] = "buffer_receipt_verified_not_publication_claim"


def reconcile(api_key: str, state_path: Path, state: dict[str, Any], org_id: str,
              channels: dict[str, Any], histories: dict[str, list[dict[str, Any]]]) -> int:
    changed = False
    for target, effect in state.get("effects", {}).items():
        if effect["state"] == "verified_buffer": continue
        payload = effect["payload"]
        if effect["organization_id"] != org_id or payload["channel_id"] != channels[payload["service"]]["id"]:
            raise RuntimeError("Reconciliation account mismatch")
        found = [p for p in histories[payload["service"]] if exact_match(p, payload, True)]
        if effect.get("post_id"):
            found = [p for p in found if p["id"] == effect["post_id"]]
        effect["last_reconciliation_at"] = iso(utc_now())
        if len(found) == 1:
            record_post(state, target, effect, found[0])
            changed = True
        else:
            effect["state"] = "uncertain"
            effect["last_match_count"] = len(found)
    write_json(state_path, state)
    unresolved = any(e["state"] != "verified_buffer" for e in state.get("effects", {}).values())
    print(json.dumps({"result": "uncertain_hold" if unresolved else "reconciliation_complete",
                      "mutations": 0, "changed": changed, "automatic_resend": False}))
    return 2 if unresolved else 0


def publish_canary(api_key: str, state_path: Path, state: dict[str, Any], org_id: str,
                   channels: dict[str, Any], histories: dict[str, list[dict[str, Any]]]) -> int:
    plan = load_json(PLAN_PATH, {})
    grant_path = os.environ.get("WHB_CANARY_GRANT")
    if not grant_path: raise RuntimeError("Explicit canary grant path required")
    grant = load_json(Path(grant_path), {})
    current_binding = binding(org_id, channels)
    if plan.get("result") != "dry_run_ok" or plan.get("binding") != current_binding:
        raise RuntimeError("Exact queue/channel/source/config bootstrap is required")
    age = utc_now() - parse_iso(plan["created_at"])
    if age < timedelta(0) or age > timedelta(hours=24): raise RuntimeError("Bootstrap expired")
    if (grant.get("authorized") is not True or grant.get("mode") != "manual_canary" or
        grant.get("max_mutations") != 1 or not grant.get("authorization_ref") or
        not grant.get("history_review_ref") or grant.get("binding") != current_binding or
        grant.get("plan_sha256") != sha(canonical(plan)) or
        parse_iso(grant.get("expires_at", "2000-01-01T00:00:00Z")) <= utc_now()):
        raise RuntimeError("Exact expiring single-canary grant and history review required")
    target = grant.get("target")
    selected = [p for p in plan.get("planned", []) if p["target"] == target]
    if len(selected) != 1: raise RuntimeError("Grant target is not exactly one planned candidate")
    payload = selected[0]["payload"]
    if selected[0]["payload_sha256"] != sha(canonical(payload)):
        raise RuntimeError("Planned payload hash mismatch")
    service = payload.get("service")
    items = [item for item in validate_queue(load_json(QUEUE_PATH, {}))
             if item["content_id"] == payload.get("content_id")]
    if service not in REQUIRED or len(items) != 1 or target != payload["content_id"] + ":" + service:
        raise RuntimeError("Canary must name one existing queue item and service")
    item = items[0]
    expected_due = parse_iso(plan["proposed_anchor_utc"]) + timedelta(hours=float(item["relative_hours"]))
    if (payload["channel_id"] != channels[service]["id"] or payload["caption"] != item["captions"][service] or
        payload["media_url"] != item["media_url"] or payload["thumbnail_offset_ms"] != int(item.get("thumbnail_offset_ms", 2000)) or
        parse_iso(payload["due_at"]) != expected_due):
        raise RuntimeError("Planned payload does not match exact queue/channel/schedule")
    prior = state.get("effects", {}).get(target)
    if prior:
        print(json.dumps({"result": "already_verified" if prior["state"] == "verified_buffer" else "uncertain_hold",
                          "target": target, "mutations": 0, "automatic_resend": False}))
        return 0 if prior["state"] == "verified_buffer" else 2
    if any(e["state"] != "verified_buffer" for e in state.get("effects", {}).values()):
        raise RuntimeError("Unresolved external effect requires read-only reconciliation")
    posts = histories[payload["service"]]
    if any(p.get("text") == payload["caption"] or any(a.get("source") == payload["media_url"]
            for a in p.get("assets") or []) for p in posts):
        raise RuntimeError("Canary payload appears in history; reconcile instead of sending")
    if any(p.get("status") == "error" for p in posts): raise RuntimeError("Existing Buffer error")
    if sum(p.get("status") in {"scheduled", "sending", "needs_approval"} for p in posts) >= MAX_SCHEDULED_PER_CHANNEL:
        raise RuntimeError("Channel scheduled capacity reached")
    due = parse_iso(payload["due_at"])
    if due <= utc_now() + timedelta(minutes=10): raise RuntimeError("Planned canary time expired; generate a new plan")
    if not media_available(payload["media_url"]): raise RuntimeError("Canary media unavailable")
    if parse_iso(grant["expires_at"]) <= utc_now(): raise RuntimeError("Grant expired before send")
    effect = {"state": "intent", "recorded_at": iso(utc_now()), "organization_id": org_id,
              "payload": payload, "payload_sha256": selected[0]["payload_sha256"],
              "binding": current_binding, "authorization_ref": grant["authorization_ref"],
              "plan_sha256": grant["plan_sha256"], "proposed_anchor_utc": plan["proposed_anchor_utc"]}
    state.setdefault("effects", {})[target] = effect
    # Durable local intent is committed and fsynced BEFORE the only external mutation.
    write_json(state_path, state)
    try:
        post = create_video_post(api_key, payload["service"], payload["channel_id"], payload["caption"],
                                 payload["media_url"], due, payload["thumbnail_offset_ms"])
        effect["state"] = "uncertain"
        if post.get("id") and post["id"] != "dry-run": effect["post_id"] = post["id"]
        write_json(state_path, state)
        if not effect.get("post_id"): raise RuntimeError("Provider ID unavailable")
        readback = get_channel_posts(api_key, org_id, payload["channel_id"])
        found = [p for p in readback if p.get("id") == effect["post_id"] and exact_match(p, payload, True)]
        if len(found) != 1: raise RuntimeError("Exact provider readback unavailable")
        record_post(state, target, effect, found[0])
        # An observed scheduled record supports the actual anchor; dry-run never does.
        if not state.get("anchor_utc"): state["anchor_utc"] = plan["proposed_anchor_utc"]
        write_json(state_path, state)
        print(json.dumps({"result": "buffer_receipt_verified_not_publication_claim",
                          "target": target, "post_id": post["id"], "mutations": 1}))
        return 0
    except Exception:
        effect["state"] = "uncertain"
        effect["failure_at"] = iso(utc_now())
        write_json(state_path, state)
        print(json.dumps({"result": "uncertain_hold", "target": target, "automatic_resend": False}))
        return 2


def main() -> int:
    api_key = os.environ.get("BUFFER_API_KEY", "").strip()
    if not api_key:
        print("WAIT_HOW_BIG_NOT_CONFIGURED: BUFFER_API_KEY is absent; no action taken.")
        return 0
    dry_run = enabled("WHB_DRY_RUN")
    is_reconcile = enabled("WHB_RECONCILE")
    state_dir = os.environ.get("WHB_DURABLE_STATE_DIR")
    state_path = Path(state_dir) / "state.json" if state_dir else STATE_PATH
    state = load_json(state_path, default_state())
    if any(p.get("post_id") == "dry-run" for services in state.get("posts", {}).values() for p in services.values()):
        raise RuntimeError("Legacy synthetic actuals require explicit evidence reconciliation")
    held = state.get("paused") or enabled("WHB_KILL_SWITCH")
    if not dry_run and not is_reconcile and held:
        print("WAIT_HOW_BIG_PAUSED: publication remains held; WHB_DRY_RUN=true permits read-only validation.")
        return 0
    if not dry_run:
        # GitHub always refuses effects. A post-POST git push/artifact is not durable intent.
        if enabled("GITHUB_ACTIONS") or not state_dir or not Path(state_dir).is_absolute():
            print("WAIT_HOW_BIG_DURABLE_HOST_REQUIRED: GitHub publishing is disabled; use an admitted local manual canary.")
            return 2
        if not is_reconcile and not enabled("WHB_MANUAL_CANARY"):
            print("WAIT_HOW_BIG_MANUAL_CANARY_REQUIRED: no automatic normal publishing.")
            return 2
    queue = validate_queue(load_json(QUEUE_PATH, {}))
    org_id, channels = get_org_and_channels(api_key)
    if any(channels[s].get("isQueuePaused") is not False for s in REQUIRED):
        if not dry_run and not is_reconcile: raise RuntimeError("Expected Buffer channel is paused")
    if dry_run:
        histories = {s: get_channel_posts(api_key, org_id, channels[s]["id"]) for s in REQUIRED}
        plan = build_plan(queue, state, org_id, channels, histories)
        plan["publication_held"] = bool(held or any(channels[s].get("isQueuePaused") is not False for s in REQUIRED))
        write_json(PLAN_PATH, plan)
        print(json.dumps({"result": "dry_run_ok", "planned_count": len(plan["planned"]),
                          "actual_mutations": 0, "plan_sha256": sha(canonical(plan)),
                          "binding": plan["binding"], "actual_state_changed": False}))
        return 0
    with state_lock(Path(state_dir)):
        # Reload after taking the sole-writer lock to prevent races with another local process.
        state = load_json(state_path, default_state())
        histories = {s: get_channel_posts(api_key, org_id, channels[s]["id"]) for s in REQUIRED}
        if is_reconcile: return reconcile(api_key, state_path, state, org_id, channels, histories)
        if state.get("paused") or enabled("WHB_KILL_SWITCH"):
            raise RuntimeError("Publication paused before send")
        return publish_canary(api_key, state_path, state, org_id, channels, histories)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Provider messages may contain private content. Never echo arbitrary exceptions.
        print("WAIT_HOW_BIG_OPERATOR_FAILED: inspect the preserved plan/intent and official account; no automatic retry.", file=sys.stderr)
        raise SystemExit(2)
