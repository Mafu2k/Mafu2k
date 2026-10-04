#!/usr/bin/env python3
"""Generate self-hosted SVG cards for the GitHub profile README.

Public instances of github-readme-stats and streak-stats are regularly
rate-limited or taken down, which leaves broken images on the profile.
This script builds equivalent cards straight from the GitHub GraphQL API;
.github/workflows/profile-stats.yml runs it daily and commits the result
to assets/. Only the standard library is used, so nothing has to be installed.

Usage:
    GITHUB_TOKEN=... python3 scripts/generate_stats.py --user Mafu2k --out assets
    python3 scripts/generate_stats.py --fixture data.json --out preview  # offline
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

API_URL = "https://api.github.com/graphql"

PROFILE_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    login
    createdAt
    pullRequests { totalCount }
    issues { totalCount }
    repositoriesContributedTo(
      contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, PULL_REQUEST_REVIEW]
    ) { totalCount }
    repositories(
      first: 100, after: $cursor, ownerAffiliations: OWNER,
      isFork: false, privacy: PUBLIC
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        stargazerCount
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

CALENDAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"

# Colours follow GitHub's own dark and light palettes, so the cards blend in
# with whichever theme the visitor uses (see the <picture> tags in Readme.md).
THEMES = {
    "dark": {
        "bg": "#0d1117",
        "border": "#30363d",
        "title": "#58a6ff",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "accent": "#ffa657",
        "track": "#21262d",
    },
    "light": {
        "bg": "#ffffff",
        "border": "#d0d7de",
        "title": "#0969da",
        "text": "#1f2328",
        "muted": "#59636e",
        "accent": "#bc4c00",
        "track": "#eaeef2",
    },
}

CARD_WIDTH = 495
CARD_HEIGHT = 195
MAX_LANGUAGES = 8


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def graphql(query: str, variables: dict, token: str) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-stats-generator",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as err:
            if err.code < 500 or attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(2**attempt)
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload["data"]


def fetch_data(login: str, token: str, now: datetime) -> dict:
    """Collect everything the cards need into a plain, JSON-serialisable dict."""
    repos, cursor = [], None
    while True:
        user = graphql(PROFILE_QUERY, {"login": login, "cursor": cursor}, token)["user"]
        if user is None:
            raise RuntimeError(f"GitHub user {login!r} not found")
        page = user["repositories"]
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]

    sizes: dict[str, int] = {}
    colors: dict[str, str | None] = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            sizes[name] = sizes.get(name, 0) + edge["size"]
            colors[name] = edge["node"]["color"]

    # contributionsCollection accepts at most one year per query.
    created = datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))
    days: dict[str, int] = {}
    commits = 0
    for year in range(created.year, now.year + 1):
        start = max(created, datetime(year, 1, 1, tzinfo=timezone.utc))
        end = min(now, datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc))
        variables = {
            "login": login,
            "from": start.isoformat().replace("+00:00", "Z"),
            "to": end.isoformat().replace("+00:00", "Z"),
        }
        collection = graphql(CALENDAR_QUERY, variables, token)["user"]["contributionsCollection"]
        commits += collection["totalCommitContributions"]
        for week in collection["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                # Weeks are padded to full Sun-Sat rows; ignore days outside
                # the queried range so they cannot overwrite real counts.
                if start.date().isoformat() <= day["date"] <= end.date().isoformat():
                    days[day["date"]] = day["contributionCount"]

    return {
        "login": user["login"],
        "stars": sum(repo["stargazerCount"] for repo in repos),
        "commits": commits,
        "pull_requests": user["pullRequests"]["totalCount"],
        "issues": user["issues"]["totalCount"],
        "repos": page["totalCount"],
        "contributed_to": user["repositoriesContributedTo"]["totalCount"],
        "languages": [
            {"name": name, "color": colors[name], "size": size}
            for name, size in sorted(sizes.items(), key=lambda item: (-item[1], item[0]))
        ],
        "days": dict(sorted(days.items())),
    }


# --------------------------------------------------------------------------
# Calculations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Streak:
    length: int = 0
    start: date | None = None
    end: date | None = None


def compute_streaks(days: dict[str, int], today: date) -> tuple[Streak, Streak]:
    """Return (current, longest) streaks of consecutive days with contributions.

    A day without contributions so far today does not break the current
    streak yet - the day is not over.
    """
    run = longest = Streak()
    for day, count in sorted(days.items()):
        current_day = date.fromisoformat(day)
        if current_day > today or count <= 0:
            continue
        if run.end == current_day - timedelta(days=1):
            run = Streak(run.length + 1, run.start, current_day)
        else:
            run = Streak(1, current_day, current_day)
        if run.length > longest.length:
            longest = run
    current = run if run.end in (today, today - timedelta(days=1)) else Streak()
    return current, longest


def top_languages(languages: list[dict], limit: int = MAX_LANGUAGES) -> list[dict]:
    """Share of each language in percent; the tail is folded into "Other"."""
    total = sum(lang["size"] for lang in languages)
    if not total:
        return []
    shown = languages if len(languages) <= limit else languages[: limit - 1]
    result = [
        {"name": lang["name"], "color": lang["color"], "percent": 100 * lang["size"] / total}
        for lang in shown
    ]
    rest = sum(lang["size"] for lang in languages[len(shown):])
    if rest:
        result.append({"name": "Other", "color": None, "percent": 100 * rest / total})
    return result


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def format_number(value: int) -> str:
    return f"{value:,}"


def format_date(value: date, today: date) -> str:
    label = f"{value:%b} {value.day}"
    return label if value.year == today.year else f"{label}, {value.year}"


def format_range(streak: Streak, today: date) -> str:
    if not streak.length:
        return "No active streak"
    if streak.start == streak.end:
        return format_date(streak.start, today)
    return f"{format_date(streak.start, today)} – {format_date(streak.end, today)}"


def card(theme: dict, label: str, body: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{CARD_WIDTH}" height="{CARD_HEIGHT}" viewBox="0 0 {CARD_WIDTH} {CARD_HEIGHT}" role="img" aria-labelledby="title">
  <title id="title">{escape(label)}</title>
  <style>
    text {{ font-family: {FONT}; fill: {theme['text']}; }}
    .title {{ font-size: 18px; font-weight: 600; fill: {theme['title']}; }}
    .value {{ font-size: 26px; font-weight: 600; }}
    .label {{ font-size: 13px; fill: {theme['muted']}; }}
    .accent {{ fill: {theme['accent']}; }}
    .lang {{ font-size: 13px; }}
  </style>
  <rect x="0.5" y="0.5" width="{CARD_WIDTH - 1}" height="{CARD_HEIGHT - 1}" rx="6" fill="{theme['bg']}" stroke="{theme['border']}"/>
{body}
</svg>
"""


def render_stats(data: dict, theme: dict) -> str:
    tiles = [
        ("Total stars", data["stars"]),
        ("Commits (all time)", data["commits"]),
        ("Pull requests", data["pull_requests"]),
        ("Public repos", data["repos"]),
        ("Issues", data["issues"]),
        ("Contributed to", data["contributed_to"]),
    ]
    column_width = CARD_WIDTH / 3
    parts = [f'  <text x="25" y="38" class="title">{escape(data["login"])}\'s GitHub Stats</text>']
    for index, (label, value) in enumerate(tiles):
        x = column_width * (index % 3) + column_width / 2
        y = 92 if index < 3 else 152
        parts.append(f'  <text x="{x:.1f}" y="{y}" text-anchor="middle" class="value">{format_number(value)}</text>')
        parts.append(f'  <text x="{x:.1f}" y="{y + 20}" text-anchor="middle" class="label">{escape(label)}</text>')
    return card(theme, f"{data['login']}'s GitHub stats", "\n".join(parts))


def render_streak(data: dict, theme: dict, today: date) -> str:
    current, longest = compute_streaks(data["days"], today)
    total = sum(data["days"].values())
    first_day = min(data["days"], default=today.isoformat())
    since = f"{format_date(date.fromisoformat(first_day), today)} – Present"

    left, middle, right = CARD_WIDTH / 6, CARD_WIDTH / 2, CARD_WIDTH * 5 / 6
    parts = [
        f'  <line x1="{CARD_WIDTH / 3:.1f}" y1="30" x2="{CARD_WIDTH / 3:.1f}" y2="165" stroke="{theme["border"]}"/>',
        f'  <line x1="{CARD_WIDTH * 2 / 3:.1f}" y1="30" x2="{CARD_WIDTH * 2 / 3:.1f}" y2="165" stroke="{theme["border"]}"/>',
        # Total contributions
        f'  <text x="{left:.1f}" y="88" text-anchor="middle" class="value">{format_number(total)}</text>',
        f'  <text x="{left:.1f}" y="120" text-anchor="middle" class="lang">Total Contributions</text>',
        f'  <text x="{left:.1f}" y="145" text-anchor="middle" class="label">{escape(since)}</text>',
        # Current streak, drawn inside a ring like streak-stats does
        f'  <circle cx="{middle:.1f}" cy="72" r="40" fill="none" stroke="{theme["accent"]}" stroke-width="5"/>',
        f'  <text x="{middle:.1f}" y="82" text-anchor="middle" class="value">{format_number(current.length)}</text>',
        f'  <text x="{middle:.1f}" y="140" text-anchor="middle" class="lang accent" font-weight="600">Current Streak</text>',
        f'  <text x="{middle:.1f}" y="165" text-anchor="middle" class="label">{escape(format_range(current, today))}</text>',
        # Longest streak
        f'  <text x="{right:.1f}" y="88" text-anchor="middle" class="value">{format_number(longest.length)}</text>',
        f'  <text x="{right:.1f}" y="120" text-anchor="middle" class="lang">Longest Streak</text>',
        f'  <text x="{right:.1f}" y="145" text-anchor="middle" class="label">{escape(format_range(longest, today))}</text>',
    ]
    label = f"{data['login']}'s contribution streak: current {current.length} days, longest {longest.length} days"
    return card(theme, label, "\n".join(parts))


def render_languages(data: dict, theme: dict) -> str:
    languages = top_languages(data["languages"])
    parts = ['  <text x="25" y="38" class="title">Most Used Languages</text>']
    if not languages:
        parts.append('  <text x="25" y="100" class="label">No public code yet</text>')
        return card(theme, "Most used languages", "\n".join(parts))

    bar_x, bar_y, bar_width, bar_height = 25, 58, CARD_WIDTH - 50, 8
    parts.append(
        f'  <clipPath id="bar"><rect x="{bar_x}" y="{bar_y}" width="{bar_width}" height="{bar_height}" rx="4"/></clipPath>'
    )
    parts.append('  <g clip-path="url(#bar)">')
    parts.append(f'    <rect x="{bar_x}" y="{bar_y}" width="{bar_width}" height="{bar_height}" fill="{theme["track"]}"/>')
    offset = bar_x
    for lang in languages:
        width = bar_width * lang["percent"] / 100
        color = lang["color"] or theme["muted"]
        parts.append(f'    <rect x="{offset:.2f}" y="{bar_y}" width="{width:.2f}" height="{bar_height}" fill="{color}"/>')
        offset += width
    parts.append("  </g>")

    for index, lang in enumerate(languages):
        x = 25 if index % 2 == 0 else CARD_WIDTH / 2 + 10
        y = 98 + 25 * (index // 2)
        color = lang["color"] or theme["muted"]
        parts.append(f'  <circle cx="{x + 5:.1f}" cy="{y - 4.5}" r="5" fill="{color}"/>')
        parts.append(
            f'  <text x="{x + 18:.1f}" y="{y}" class="lang">{escape(lang["name"])} '
            f'<tspan fill="{theme["muted"]}">{lang["percent"]:.1f}%</tspan></text>'
        )
    summary = ", ".join(f"{lang['name']} {lang['percent']:.1f}%" for lang in languages)
    return card(theme, f"Most used languages: {summary}", "\n".join(parts))


def write_cards(data: dict, out_dir: Path, today: date) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for theme_name, theme in THEMES.items():
        cards = {
            "stats": render_stats(data, theme),
            "streak": render_streak(data, theme, today),
            "top-langs": render_languages(data, theme),
        }
        for name, svg in cards.items():
            path = out_dir / f"{name}-{theme_name}.svg"
            path.write_text(svg, encoding="utf-8")
            written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", default=os.environ.get("GITHUB_REPOSITORY_OWNER"), help="GitHub login")
    parser.add_argument("--out", type=Path, default=Path("assets"), help="output directory")
    parser.add_argument("--fixture", type=Path, help="render from saved JSON instead of calling the API")
    parser.add_argument("--dump", type=Path, help="also save the fetched data as JSON (for --fixture)")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    if args.fixture:
        data = json.loads(args.fixture.read_text(encoding="utf-8"))
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not token or not args.user:
            parser.error("GITHUB_TOKEN env variable and --user are required (or use --fixture)")
        data = fetch_data(args.user, token, now)
        if args.dump:
            args.dump.write_text(json.dumps(data, indent=2), encoding="utf-8")

    for path in write_cards(data, args.out, now.date()):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
