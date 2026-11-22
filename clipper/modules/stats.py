"""Real performance of posted clips: views / likes / comments from YouTube and Facebook.

YouTube: ``videos.list(part=statistics,status)`` (1 quota unit per 50 videos).
Facebook: the Reel's ``views`` field via the Page token.
Also flags things that silently kill reach: a non-public or region-blocked YouTube video,
or Reels that logged-out viewers can't see while the Meta app is in development mode.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from clipper.config import Settings


def collect(s: Settings) -> list[dict[str, Any]]:
    """One row per posted job: platform, id, title, posted time, views, likes, comments, note."""
    from googleapiclient.discovery import build

    from clipper.modules.publisher import facebook_token_store, youtube_credentials

    with sqlite3.connect(s.state_dir / "publisher.db") as db:
        jobs = db.execute("SELECT j.platform, j.remote_id, j.posted_at, j.metadata FROM jobs j "
                          "WHERE j.status='posted' AND j.remote_id IS NOT NULL ORDER BY j.posted_at").fetchall()
    import json

    rows: list[dict[str, Any]] = []
    yt_ids = [r[1] for r in jobs if r[0].startswith("youtube")]
    yt: dict[str, dict[str, Any]] = {}
    if yt_ids:
        client = build("youtube", "v3", credentials=youtube_credentials(s), cache_discovery=False)
        for i in range(0, len(yt_ids), 50):
            res = client.videos().list(part="statistics,status,contentDetails", id=",".join(yt_ids[i:i + 50])).execute()
            yt |= {v["id"]: v for v in res.get("items", [])}
    fb_token = None
    fb_ids = [r[1] for r in jobs if r[0].startswith("facebook")]
    if fb_ids:
        try:
            fb_token = facebook_token_store(s).load()["page_access_token"]
        except Exception:  # noqa: BLE001 - shown as a note
            fb_token = None
    base = f"https://graph.facebook.com/{s.facebook_graph_version}"
    with httpx.Client(timeout=30) as http:
        for platform, rid, posted, meta in jobs:
            title = (json.loads(meta).get("title") if meta else "") or ""
            row: dict[str, Any] = {"platform": platform.split(":")[0], "id": rid, "posted_at": posted,
                                   "title": title, "views": None, "likes": None, "comments": None, "note": ""}
            if platform.startswith("youtube"):
                v = yt.get(rid)
                if v is None:
                    row["note"] = "deleted or removed"
                else:
                    st, stat = v["status"], v.get("statistics", {})
                    row |= {"views": int(stat.get("viewCount", 0)), "likes": int(stat.get("likeCount", 0)),
                            "comments": int(stat.get("commentCount", 0))}
                    notes = []
                    if st.get("privacyStatus") != "public":
                        notes.append(st.get("privacyStatus", "?"))
                    if blocked := (v["contentDetails"].get("regionRestriction") or {}).get("blocked"):
                        notes.append("blocked in " + ",".join(blocked) + " (likely a copyright claim)")
                    row["note"] = "; ".join(notes)
            elif fb_token:
                x = http.get(f"{base}/{rid}", params={"fields": "views,post_views", "access_token": fb_token}).json()
                if "error" in x:
                    row["note"] = "not readable (deleted?)"
                else:
                    row["views"] = x.get("views")
            rows.append(row)
    return rows


def print_report(s: Settings) -> int:
    import time

    rows = collect(s)
    if not rows:
        print("Nothing posted yet.")
        return 0
    print(f"{'platform':9} {'posted':16} {'views':>6} {'likes':>5} {'cmts':>4}  title / note")
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["posted_at"] or 0))
        v = "-" if r["views"] is None else r["views"]
        lk = "-" if r["likes"] is None else r["likes"]
        cm = "-" if r["comments"] is None else r["comments"]
        note = f"  [{r['note']}]" if r["note"] else ""
        print(f"{r['platform']:9} {when:16} {v:>6} {lk:>5} {cm:>4}  {r['title'][:60]}{note}")
    for p in ("youtube", "facebook"):
        vs = [r["views"] for r in rows if r["platform"] == p and r["views"] is not None]
        if vs:
            print(f"\n{p}: {len(vs)} posts, {sum(vs)} views total, median {sorted(vs)[len(vs) // 2]}")
    return 0
