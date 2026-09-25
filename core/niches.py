"""ClipForge2 niche catalogue — 40 locked niches + Custom.

Locked by Ashu (2026-09-25). The wizard offers multi-select; "custom" lets
the user type any niche of their own.
"""

from __future__ import annotations

# (id, display name, category)
_NICHES: tuple[tuple[str, str, str], ...] = (
    # Gaming
    ("gta6-breakdowns", "GTA 6 breakdowns", "Gaming"),
    ("gameplay-highlights", "Gameplay highlights", "Gaming"),
    ("gaming-news", "Gaming news", "Gaming"),
    ("speedruns", "Speedruns", "Gaming"),
    # Podcast / Talks
    ("business-podcasts", "Business podcasts", "Podcast/Talks"),
    ("comedy-podcasts", "Comedy podcasts", "Podcast/Talks"),
    ("motivational-speeches", "Motivational speeches", "Podcast/Talks"),
    ("celebrity-interviews", "Celebrity interviews", "Podcast/Talks"),
    # Finance
    ("stock-market", "Stock market", "Finance"),
    ("crypto", "Crypto", "Finance"),
    ("personal-finance", "Personal finance", "Finance"),
    ("business-news", "Business news", "Finance"),
    # Motivation
    ("gym-fitness", "Gym / fitness", "Motivation"),
    ("success-stories", "Success stories", "Motivation"),
    ("productivity", "Productivity", "Motivation"),
    ("discipline-mindset", "Discipline / mindset", "Motivation"),
    # Tech
    ("ai-news", "AI news", "Tech"),
    ("gadgets-reviews", "Gadgets / reviews", "Tech"),
    ("coding-programming", "Coding / programming", "Tech"),
    ("startups", "Startups", "Tech"),
    # Sports
    ("cricket", "Cricket", "Sports"),
    ("football", "Football", "Sports"),
    ("fighting-wwe", "Fighting / WWE", "Sports"),
    # Comedy
    ("stand-up", "Stand-up", "Comedy"),
    ("pranks", "Pranks", "Comedy"),
    ("funny-fails", "Funny fails", "Comedy"),
    ("memes", "Memes", "Comedy"),
    # Education
    ("history", "History", "Education"),
    ("science", "Science", "Education"),
    ("study-exam-tips", "Study / exam tips", "Education"),
    ("space-universe", "Space / universe", "Education"),
    # Lifestyle
    ("travel", "Travel", "Lifestyle"),
    ("food-cooking", "Food / cooking", "Lifestyle"),
    ("fashion", "Fashion", "Lifestyle"),
    ("pets-animals", "Pets / animals", "Lifestyle"),
    # Entertainment
    ("movie-explainers", "Movie explainers", "Entertainment"),
    ("celebrity-news", "Celebrity news", "Entertainment"),
    ("music", "Music", "Entertainment"),
    # Other
    ("horror-stories", "Horror stories", "Other"),
    ("true-crime", "True crime", "Other"),
)

CUSTOM_NICHE = {"id": "custom", "name": "Custom (your own niche)", "category": "Custom"}


def list_niches() -> list[dict]:
    """All niches as [{id, name, category}], Custom last. 40 + Custom = 41."""
    out = [{"id": i, "name": n, "category": c} for i, n, c in _NICHES]
    out.append(dict(CUSTOM_NICHE))
    return out


def niche_ids() -> set[str]:
    return {n["id"] for n in list_niches()}


def niche_name(niche_id: str) -> str:
    for n in list_niches():
        if n["id"] == niche_id:
            return n["name"]
    raise ValueError(f"unknown niche id: {niche_id!r}")
