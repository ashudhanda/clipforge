"""No-API-quota fallback: YouTube Studio manual upload helper.

When the Data API quota is used up (or the user never connects OAuth),
ClipForge2 prepares everything so the manual YouTube Studio upload takes
under a minute: the video file plus title/description/hashtags ready to
paste, and exact numbered steps. Full browser automation is deliberately
out of scope — Studio's upload flow changes often and breaks bots.
"""

from __future__ import annotations

from pathlib import Path

STUDIO_STEPS = [
    "Open YouTube Studio in your browser: https://studio.youtube.com",
    "Click CREATE (top-right) → Upload videos.",
    "Choose the clip file shown below (or drag it in).",
    "Paste the TITLE below into the title box.",
    "Paste the DESCRIPTION below (hashtags are already at the end).",
    "On the visibility page choose Public, then Publish.",
    "Done — Shorts appear on your channel within minutes. "
    "Come back here and log the views under 📊 Analytics.",
]


def prepare_manual_upload(clip: dict) -> dict:
    """Bundle everything a manual Studio upload needs.

    ``clip`` needs: video file path under "file" (or the dashboard's
    "video" URL path, resolved against the clips dir), plus "title",
    "description", "hashtags". Never invents missing fields — gaps are
    reported honestly in "warnings".
    """
    from core.config import config_dir

    warnings: list[str] = []
    file_path = str(clip.get("file") or "").strip()
    if not file_path and clip.get("video"):
        # Dashboard clip entries carry "/clips/<job>_<i>.mp4".
        v = str(clip["video"]).strip()
        if v.startswith("/clips/"):
            stem = v[len("/clips/"):].replace("/", "_")
            if stem.endswith(".mp4"):
                file_path = str(config_dir() / "clips" / stem)
    if file_path and not Path(file_path).is_file():
        warnings.append(f"Video file not found on disk: {file_path}")
    title = str(clip.get("title") or "Untitled clip").strip()
    description = str(clip.get("description") or "").strip()
    hashtags = [str(t).strip("# ").lower()
                for t in (clip.get("hashtags") or []) if t]
    if not description:
        warnings.append("This clip has no description text — "
                        "the title alone will be used.")
    desc_with_tags = description
    if hashtags:
        tag_line = " ".join("#" + t for t in hashtags)
        if tag_line not in desc_with_tags:
            desc_with_tags = (desc_with_tags + "\n\n" + tag_line).strip()
    return {
        "file": file_path,
        "title": title[:100],
        "description": desc_with_tags[:5000],
        "hashtags": hashtags,
        "steps": list(STUDIO_STEPS),
        "warnings": warnings,
    }


def steps_text(manual: dict) -> str:
    """Render the manual bundle as plain text (copy-paste friendly)."""
    lines = ["MANUAL UPLOAD — YouTube Studio", "",
             f"File: {manual['file']}", "",
             f"Title:\n{manual['title']}", "",
             f"Description:\n{manual['description']}", "",
             "Steps:"]
    lines += [f"{i + 1}. {s}" for i, s in enumerate(manual["steps"])]
    if manual.get("warnings"):
        lines += ["", "Warnings:"] + [f"- {w}"
                                      for w in manual["warnings"]]
    return "\n".join(lines)
