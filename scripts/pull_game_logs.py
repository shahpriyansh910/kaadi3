"""
Pulls the live server's accumulated game log (via the token-protected
/admin/export-games endpoint) and merges it into the repo's durable
archive at data/games_archive.jsonl, deduped by round_id.

Meant to be run BEFORE every deploy, since Render's free-tier disk is
wiped on redeploy -- the live server's own copy is ephemeral, this
archive is the durable one (and the only one meant to be committed).

Usage:
    python scripts/pull_game_logs.py --url https://kaadi3.onrender.com --token YOUR_ADMIN_TOKEN
    python scripts/pull_game_logs.py --url http://localhost:8000 --token dev-token   # local testing
"""
import argparse
import json
import os
import urllib.request

ARCHIVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "games_archive.jsonl")


def fetch(url, token):
    req = urllib.request.Request(url.rstrip("/") + "/admin/export-games", headers={"X-Admin-Token": token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"export endpoint returned {resp.status}")
        return resp.read().decode("utf-8")


def merge(new_content):
    existing_ids = set()
    existing_lines = []
    if os.path.exists(ARCHIVE_PATH):
        with open(ARCHIVE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                existing_lines.append(line)
                existing_ids.add(json.loads(line)["round_id"])

    added = 0
    for line in new_content.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        if entry["round_id"] not in existing_ids:
            existing_lines.append(line)
            existing_ids.add(entry["round_id"])
            added += 1

    os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
    with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
        for line in existing_lines:
            f.write(line + "\n")
    return added, len(existing_lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="server base URL, e.g. https://kaadi3.onrender.com")
    ap.add_argument("--token", required=True, help="ADMIN_TOKEN configured on that server")
    args = ap.parse_args()

    content = fetch(args.url, args.token)
    added, total = merge(content)
    print(f"pulled from {args.url}: {added} new rounds merged, {total} total in {ARCHIVE_PATH}")
    print("remember to `git add data/games_archive.jsonl` and commit before/with your next deploy.")


if __name__ == "__main__":
    main()
