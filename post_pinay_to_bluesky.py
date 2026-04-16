#!/usr/bin/env python3
"""
Bluesky Bot - Posts Filipina/Pinay female Chaturbate rooms to Bluesky
Fetches rooms by tag (pinay, filipina) and country (PH).
Runs hourly at a random time within the hour.
Requires environment variables:
  BLUESKY_HANDLE - your Bluesky handle (e.g. yourname.bsky.social)
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

CHATURBATE_API_BASE = (
    "https://chaturbate.com/api/public/affiliates/onlinerooms/"
    "?wm=HcOhv&client_ip=request_ip&limit=500&gender=f"
)

PINAY_TAGS = ["pinay", "filipina"]

BLUESKY_API_BASE = "https://bsky.social/xrpc"

def get_chaturbate_rooms() -> list:
    """Fetch Filipina/Pinay rooms by tag and by country, deduplicated."""
    seen = set()
    combined = []

    for tag in PINAY_TAGS:
        url = f"{CHATURBATE_API_BASE}&tag={tag}"
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            log.info("Tag '%s': %d rooms found", tag, len(results))
            for room in results:
                username = room.get("username")
                if username and username not in seen:
                    seen.add(username)
                    combined.append(room)
        except Exception as exc:
            log.error("Failed to fetch rooms for tag=%s: %s", tag, exc)

    url = f"{CHATURBATE_API_BASE}&country=PH"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        added = 0
        for room in results:
            username = room.get("username")
            if username and username not in seen:
                seen.add(username)
                combined.append(room)
                added += 1
        log.info("Country PH: %d rooms found (%d new)", len(results), added)
    except Exception as exc:
        log.error("Failed to fetch rooms for country=PH: %s", exc)

    return combined

def filter_rooms(rooms: list) -> list:
    """Keep only female performers."""
    return [r for r in rooms if (r.get("gender") or "").lower() == "f"]

def bsky_login(handle: str, app_password: str) -> dict:
    """Create a Bluesky session and return the session dict."""
    url = f"{BLUESKY_API_BASE}/com.atproto.server.createSession"
    payload = {"identifier": handle, "password": app_password}
    resp = requests.post(
        url,
        json=payload,
        timeout=30,
    )
    # Add more detail on 401 errors
    if resp.status_code == 401:
        log.error("401 from createSession. Check BLUESKY_HANDLE and BLUESKY_APP_PASSWORD.")
        log.error("Response: %s", resp.text)
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

def fetch_image(url: str) -> tuple:
    """Download an image from a URL; return (bytes, mime_type)."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    return resp.content, content_type

def build_post(room: dict) -> tuple:
    """Build the post text and facets (rich-text annotations) for a room."""
    username = room.get("username", "unknown")
    room_url = (
        room.get("chat_room_url_revshare")
        or f"https://chaturbate.com/in/?tour=LQps&campaign=HcOhv&track=default&room={username}"
    )

    subject = room.get("room_subject", "") or ""
    raw_tags = room.get("tags", []) or []

    base_hashtags = ["#nsfw", "#nsfwsky", "#pinay", "#filipina"]
    room_hashtags = [
        f"#{tag.strip().lstrip('#')}"
        for tag in raw_tags
        if tag.strip() and tag.strip().lower() not in ("pinay", "filipina")
    ]
    all_hashtags = base_hashtags + room_hashtags

    watch_label = "Watch Now"

    subject_line = subject.strip() if subject.strip() else username
    if len(subject_line) > 180:
        subject_line = subject_line[:177] + "..."

    hashtags_line = " ".join(all_hashtags)
    if len(hashtags_line) > 200:
        hashtags_line = " ".join(all_hashtags[:10])

    text = f"{subject_line}\n\n{hashtags_line}\n\n{watch_label}"

    facets = []

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
    if watch_idx!= -1:
        start_byte = len(text[:watch_idx].encode("utf-8"))
        end_byte = start_byte + len(watch_label.encode("utf-8"))
        facets.append(
            {
                "index": {"byteStart": start_byte, "byteEnd": end_byte},
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": room_url,
                    }
                ],
            }
        )

    return text, facets

def post_room(session: dict, room: dict) -> bool:
    """Create a Bluesky post for the given room. Returns True on success."""
    username = room.get("username", "unknown")
    log.info("Posting room: %s", username)

    text, facets = build_post(room)

    embed = None
    image_url = room.get("image_url") or room.get("image_url_360x270")
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

    record = {
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
        if hasattr(exc, 'response') and exc.response is not None:
            log.error("Response: %s", exc.response.text)
        return False

def run_once():
    """Fetch rooms, pick one at random, and post it to Bluesky."""
    handle = os.environ.get("BLUESKY_HANDLE", "").strip()
    app_password = os.environ.get("BLUESKY_APP_PASSWORD", "").strip()

    # DEBUG: Shows what the script actually received from GitHub Actions
    log.info("Handle loaded: '%s'", handle)
    log.info("Password present: %s", 'yes' if app_password else 'no')
    log.info("Password length: %d", len(app_password))

    if not handle or not app_password:
        log.error("Missing BLUESKY_HANDLE or BLUESKY_APP_PASSWORD environment variables.")
        sys.exit(1)

    if len(app_password)!= 19:
        log.warning("App password length is %d, expected 19. Make sure you're using an app password, not your main password.", len(app_password))

    if '@' in handle:
        log.warning("Handle contains '@'. Should be 'name.bsky.social' not '@name.bsky.social'")
    if not ('.' in handle):
        log.warning("Handle '%s' has no domain. Should be 'name.bsky.social'", handle)

    log.info("Fetching Chaturbate rooms...")
    all_rooms = get_chaturbate_rooms()

    rooms = filter_rooms(all_rooms)
    log.info("Total Filipina/Pinay rooms: %d", len(rooms))

    if not rooms:
        log.warning("No matching rooms found. Skipping post.")
        return

    room = random.choice(rooms)
    log.info("Selected room: %s", room.get("username"))

    try:
        session = bsky_login(handle, app_password)
        log.info("Bluesky login success for DID: %s", session.get("did"))
    except Exception as exc:
        log.error("Bluesky login failed: %s", exc)
        sys.exit(1)

    post_room(session, room)

def main():
    """Run in a loop, posting every hour at a random offset."""
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
