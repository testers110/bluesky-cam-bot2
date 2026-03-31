#!/usr/bin/env python3
"""
Bluesky Bot - Posts Filipina/Pinay female Chaturbate rooms to Bluesky
Runs hourly at a random time within the hour.
Requires environment variables:
  BLUESKY_HANDLE  - your Bluesky handle (e.g. yourname.bsky.social)
  BLUESKY_APP_PASSWORD - your Bluesky app password (NOT your login password)
"""

import os
import sys
import time
import random
import logging
import requests
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

CHATURBATE_API_URL = (
    "https://chaturbate.com/api/public/affiliates/onlinerooms/"
    "?wm=HcOhv&client_ip=request_ip&limit=500"
)

BLUESKY_API_BASE = "https://bsky.social/xrpc"

PHILIPPINES_COUNTRY_CODE = {"PH"}


def get_chaturbate_rooms() -> list[dict]:
    """Fetch online rooms from the Chaturbate API."""
    try:
        resp = requests.get(CHATURBATE_API_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except Exception as exc:
        log.error("Failed to fetch Chaturbate rooms: %s", exc)
        return []


def filter_rooms(rooms: list[dict]) -> list[dict]:
    """Keep only female performers from the Philippines."""
    filtered = []
    for room in rooms:
        gender = (room.get("gender") or "").lower()
        country = (room.get("country") or "").upper().strip()

        if gender != "f":
            continue
        if country not in PHILIPPINES_COUNTRY_CODE:
            continue
        filtered.append(room)
    return filtered


def bsky_login(handle: str, app_password: str) -> dict:
    """Create a Bluesky session and return the session dict."""
    url = f"{BLUESKY_API_BASE}/com.atproto.server.createSession"
    resp = requests.post(
        url,
        json={"identifier": handle, "password": app_password},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def upload_image(session: dict, image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Upload an image blob to Bluesky and return the blob reference."""
    url = f"{BLUESKY_API_BASE}/com.atproto.repo.uploadBlob"
    resp = requests.post(
        url,
        data=image_bytes,
        headers={
            "Authorization": f"Bearer {session['accessJwt']}",
            "Content-Type": mime_type,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["blob"]


def fetch_image(url: str) -> tuple[bytes, str]:
    """Download an image from a URL; return (bytes, mime_type)."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    return resp.content, content_type


def build_post(room: dict) -> tuple[str, list[dict]]:
    """
    Build the post text and facets (rich-text annotations) for a room.
    Returns (text, facets).
    """
    username = room.get("username", "unknown")
    room_url = (
        room.get("chat_room_url_revshare")
        or f"https://chaturbate.com/in/?tour=LQps&campaign=HcOhv&track=default&room={username}"
    )

    subject = room.get("room_subject", "")
    raw_tags = room.get("tags", []) or []

    base_hashtags = ["#nsfw", "#nsfwsky", "#pinay", "#filipina"]
    room_hashtags = [f"#{tag.strip().lstrip('#')}" for tag in raw_tags if tag.strip()]

    all_hashtags = base_hashtags + room_hashtags

    watch_label = "Watch Now"
    watch_url = room_url

    subject_line = subject.strip() if subject.strip() else username
    if len(subject_line) > 180:
        subject_line = subject_line[:177] + "..."

    hashtags_line = " ".join(all_hashtags)
    if len(hashtags_line) > 200:
        hashtags_line = " ".join(all_hashtags[:10])

    text = f"{subject_line}\n\n{hashtags_line}\n\n{watch_label}"

    facets = []
    byte_text = text.encode("utf-8")

    pos = 0
    for tag in all_hashtags:
        idx = text.find(tag, pos)
        if idx == -1:
            continue
        start_byte = len(text[:idx].encode("utf-8"))
        end_byte = start_byte + len(tag.encode("utf-8"))
        facets.append(
            {
                "index": {"byteStart": start_byte, "byteEnd": end_byte},
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#tag",
                        "tag": tag.lstrip("#"),
                    }
                ],
            }
        )
        pos = idx + len(tag)

    watch_idx = text.rfind(watch_label)
    if watch_idx != -1:
        start_byte = len(text[:watch_idx].encode("utf-8"))
        end_byte = start_byte + len(watch_label.encode("utf-8"))
        facets.append(
            {
                "index": {"byteStart": start_byte, "byteEnd": end_byte},
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": watch_url,
                    }
                ],
            }
        )

    if len(byte_text) > 300:
        log.warning("Post text may exceed 300 graphemes limit, truncating.")

    return text, facets


def post_room(session: dict, room: dict) -> bool:
    """Create a Bluesky post for the given room. Returns True on success."""
    username = room.get("username", "unknown")
    log.info("Posting room: %s", username)

    text, facets = build_post(room)

    embed = None
    image_url = room.get("image_url") or room.get("thumb") or room.get("preview")
    if image_url:
        try:
            img_bytes, mime_type = fetch_image(image_url)
            blob = upload_image(session, img_bytes, mime_type)
            embed = {
                "$type": "app.bsky.embed.images",
                "images": [
                    {
                        "image": blob,
                        "alt": f"Preview of {username}'s live room",
                    }
                ],
            }
        except Exception as exc:
            log.warning("Could not upload image for %s: %s", username, exc)

    record: dict = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "facets": facets,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "langs": ["tl", "en"],
    }
    if embed:
        record["embed"] = embed

    url = f"{BLUESKY_API_BASE}/com.atproto.repo.createRecord"
    try:
        resp = requests.post(
            url,
            json={
                "repo": session["did"],
                "collection": "app.bsky.feed.post",
                "record": record,
            },
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
            timeout=30,
        )
        resp.raise_for_status()
        log.info("Posted successfully: %s -> %s", username, resp.json().get("uri"))
        return True
    except Exception as exc:
        log.error("Failed to post room %s: %s", username, exc)
        return False


def run_once():
    """Fetch rooms, pick one at random, and post it to Bluesky."""
    handle = os.environ.get("BLUESKY_HANDLE", "").strip()
    app_password = os.environ.get("BLUESKY_APP_PASSWORD", "").strip()

    if not handle or not app_password:
        log.error(
            "Missing BLUESKY_HANDLE or BLUESKY_APP_PASSWORD environment variables."
        )
        sys.exit(1)

    log.info("Fetching Chaturbate rooms...")
    all_rooms = get_chaturbate_rooms()
    log.info("Total rooms fetched: %d", len(all_rooms))

    rooms = filter_rooms(all_rooms)
    log.info("Filipina/Pinay female rooms: %d", len(rooms))

    if not rooms:
        log.warning("No matching rooms found. Skipping post.")
        return

    room = random.choice(rooms)
    log.info("Selected room: %s (%s)", room.get("username"), room.get("location"))

    try:
        session = bsky_login(handle, app_password)
    except Exception as exc:
        log.error("Bluesky login failed: %s", exc)
        sys.exit(1)

    post_room(session, room)


def main():
    """
    Run in a loop: wait a random number of seconds within 0–3600 before the
    first post, then post every hour at a random offset within the hour.
    """
    first_delay = random.randint(0, 3600)
    log.info(
        "Waiting %d seconds (%.1f minutes) before first post...",
        first_delay,
        first_delay / 60,
    )
    time.sleep(first_delay)

    while True:
        try:
            run_once()
        except Exception as exc:
            log.error("Unexpected error during run: %s", exc)

        next_delay = 3600 + random.randint(-300, 300)
        log.info(
            "Next post in %d seconds (%.1f minutes)...",
            next_delay,
            next_delay / 60,
        )
        time.sleep(next_delay)


if __name__ == "__main__":
    if "--once" in sys.argv or os.environ.get("ONE_SHOT", "").lower() in ("1", "true", "yes"):
        run_once()
    else:
        main()
