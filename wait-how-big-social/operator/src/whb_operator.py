#!/usr/bin/env python3
"""Fail-closed Buffer operator for the Wait, How Big? launch queue.

The only secret is BUFFER_API_KEY, supplied through the environment. The script
never prints it or writes it to disk. It discovers the Buffer organization and
connected X, Instagram, and TikTok channels, rebuilds idempotency from Buffer's
own post history, and keeps no more than ten scheduled posts per channel.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

API_URL = "https://api.buffer.com"
ROOT = Path(__file__).resolve().parent
QUEUE_PATH = ROOT / "queue.json"
STATE_PATH = ROOT / "state.json"
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
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Buffer HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Buffer network error: {exc.reason}") from exc
    if payload.get("errors"):
        raise RuntimeError(f"Buffer GraphQL error: {payload['errors']}")
    return payload.get("data") or {}


def get_org_and_channels(api_key: str) -> tuple[str, dict[str, dict[str, Any]]]:
    account = gql(
        api_key,
        """query GetOrganizations {
          account { organizations { id name ownerEmail } }
        }""",
    )
    organizations = ((account.get("account") or {}).get("organizations") or [])
    if not organizations:
        raise RuntimeError("No Buffer organization is available for this API key.")
    org = organizations[0]
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
            if raw in aliases and canonical not in selected:
                selected[canonical] = channel
    return org_id, selected


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
        raise RuntimeError(f"Buffer rejected post: {result['message']}")
    post = result.get("post")
    if not post:
        raise RuntimeError(f"Buffer returned no post object: {result}")
    return post


def infer_anchor(queue: list[dict[str, Any]], posts_by_service: dict[str, list[dict[str, Any]]]) -> datetime | None:
    by_caption = {
        item["captions"][service]: item
        for item in queue
        for service in REQUIRED
        if service in item.get("captions", {})
    }
    candidates: list[datetime] = []
    for posts in posts_by_service.values():
        for post in posts:
            item = by_caption.get(post.get("text"))
            if not item or not post.get("dueAt"):
                continue
            candidates.append(parse_iso(post["dueAt"]) - timedelta(hours=float(item["relative_hours"])))
    return min(candidates) if candidates else None


def main() -> int:
    api_key = os.environ.get("BUFFER_API_KEY", "").strip()
    if not api_key:
        print("WAIT_HOW_BIG_NOT_CONFIGURED: BUFFER_API_KEY is absent; no action taken.")
        return 0

    state = load_json(
        STATE_PATH,
        {
            "version": 1,
            "paused": False,
            "anchor_utc": None,
            "last_run_utc": None,
            "last_result": "never_run",
            "posts": {},
            "errors": [],
        },
    )
    if state.get("paused") or os.environ.get("WHB_KILL_SWITCH", "").lower() in {"1", "true", "yes", "on"}:
        state.update({"last_run_utc": iso(utc_now()), "last_result": "paused"})
        write_json(STATE_PATH, state)
        print("WAIT_HOW_BIG_PAUSED: kill switch is active; no action taken.")
        return 0

    queue_doc = load_json(QUEUE_PATH, {})
    queue = queue_doc.get("queue") or []
    if not queue:
        raise RuntimeError("Publishing queue is empty.")

    org_id, channels = get_org_and_channels(api_key)
    missing = [service for service in REQUIRED if service not in channels]
    paused = [service for service, channel in channels.items() if channel.get("isQueuePaused")]
    if missing or paused:
        message = f"Missing channels: {missing}; paused channels: {paused}"
        state.update({"last_run_utc": iso(utc_now()), "last_result": "channel_gate_failed"})
        state.setdefault("errors", []).append({"at": iso(utc_now()), "message": message})
        state["errors"] = state["errors"][-20:]
        write_json(STATE_PATH, state)
        raise RuntimeError(message)

    posts_by_service = {
        service: get_channel_posts(api_key, org_id, channel["id"])
        for service, channel in channels.items()
        if service in REQUIRED
    }

    anchor = parse_iso(state["anchor_utc"]) if state.get("anchor_utc") else infer_anchor(queue, posts_by_service)
    if anchor is None:
        now = utc_now()
        rounded = now.replace(minute=(now.minute // 15 + 1) * 15 % 60, second=0, microsecond=0)
        if rounded <= now:
            rounded += timedelta(hours=1)
        anchor = rounded + timedelta(minutes=15)
        state["anchor_utc"] = iso(anchor)

    existing_by_service: dict[str, dict[str, dict[str, Any]]] = {}
    scheduled_counts: dict[str, int] = {}
    error_posts: list[dict[str, Any]] = []
    for service, posts in posts_by_service.items():
        mapping: dict[str, dict[str, Any]] = {}
        for post in posts:
            text = post.get("text")
            if text:
                mapping[text] = post
            for asset in post.get("assets") or []:
                source = asset.get("source")
                if source:
                    mapping[source] = post
            if post.get("status") == "error":
                error_posts.append({"service": service, "post_id": post.get("id"), "text": text})
        existing_by_service[service] = mapping
        scheduled_counts[service] = sum(
            1 for post in posts if post.get("status") in {"scheduled", "sending", "needs_approval"}
        )

    if error_posts:
        state.update({"last_run_utc": iso(utc_now()), "last_result": "account_health_error"})
        state.setdefault("errors", []).append({"at": iso(utc_now()), "message": "Existing Buffer post errors", "posts": error_posts})
        state["errors"] = state["errors"][-20:]
        write_json(STATE_PATH, state)
        raise RuntimeError(f"Existing Buffer post errors require review: {error_posts}")

    created: list[dict[str, Any]] = []
    media_cache: dict[str, bool] = {}
    fallback_due = utc_now() + timedelta(minutes=30)

    for service in REQUIRED:
        capacity = max(0, MAX_SCHEDULED_PER_CHANNEL - scheduled_counts.get(service, 0))
        if capacity == 0:
            continue
        channel = channels[service]
        for item in queue:
            if capacity == 0:
                break
            caption = item["captions"][service]
            media_url = item["media_url"]
            existing = existing_by_service[service]
            if caption in existing or media_url in existing:
                post = existing.get(caption) or existing.get(media_url) or {}
                state.setdefault("posts", {}).setdefault(item["content_id"], {})[service] = {
                    "post_id": post.get("id"),
                    "status": post.get("status"),
                    "due_at": post.get("dueAt"),
                    "external_link": post.get("externalLink"),
                }
                continue

            if media_url not in media_cache:
                media_cache[media_url] = media_available(media_url)
            if not media_cache[media_url]:
                state.update({"last_run_utc": iso(utc_now()), "last_result": "media_not_ready"})
                state.setdefault("errors", []).append({"at": iso(utc_now()), "message": f"Media unavailable: {media_url}"})
                state["errors"] = state["errors"][-20:]
                write_json(STATE_PATH, state)
                print(f"WAIT_HOW_BIG_MEDIA_NOT_READY: {media_url}; no duplicate or partial retry attempted.")
                return 0

            planned_due = anchor + timedelta(hours=float(item["relative_hours"]))
            due_at = planned_due
            if due_at <= utc_now() + timedelta(minutes=10):
                due_at = max(fallback_due, utc_now() + timedelta(minutes=20))
                fallback_due = due_at + timedelta(hours=2)

            if os.environ.get("WHB_DRY_RUN", "").lower() in {"1", "true", "yes", "on"}:
                post = {"id": "dry-run", "status": "scheduled", "dueAt": iso(due_at), "externalLink": None}
            else:
                post = create_video_post(
                    api_key,
                    service,
                    channel["id"],
                    caption,
                    media_url,
                    due_at,
                    int(item.get("thumbnail_offset_ms", 2000)),
                )

            state.setdefault("posts", {}).setdefault(item["content_id"], {})[service] = {
                "post_id": post.get("id"),
                "status": post.get("status"),
                "due_at": post.get("dueAt") or iso(due_at),
                "external_link": post.get("externalLink"),
            }
            created.append({"content_id": item["content_id"], "service": service, "post_id": post.get("id"), "due_at": post.get("dueAt") or iso(due_at)})
            existing_by_service[service][caption] = post
            existing_by_service[service][media_url] = post
            capacity -= 1

    state.update(
        {
            "last_run_utc": iso(utc_now()),
            "last_result": "scheduled" if created else "no_change",
            "organization_id": org_id,
            "channels": {
                service: {
                    "id": channel.get("id"),
                    "name": channel.get("name"),
                    "display_name": channel.get("displayName"),
                    "service": channel.get("service"),
                }
                for service, channel in channels.items()
                if service in REQUIRED
            },
            "last_created": created,
        }
    )
    write_json(STATE_PATH, state)
    print(json.dumps({"result": state["last_result"], "anchor_utc": state["anchor_utc"], "created": created}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        state = load_json(STATE_PATH, {"version": 1, "paused": False, "errors": []})
        state.update({"last_run_utc": iso(utc_now()), "last_result": "failed"})
        state.setdefault("errors", []).append({"at": iso(utc_now()), "message": str(exc)})
        state["errors"] = state["errors"][-20:]
        write_json(STATE_PATH, state)
        print(f"WAIT_HOW_BIG_OPERATOR_FAILED: {exc}", file=sys.stderr)
        raise
