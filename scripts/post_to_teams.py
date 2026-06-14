#!/usr/bin/env python3
"""Post MCP / Claude Code related GitHub trending ranking to Teams."""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib import error, request

OUTPUT_DIR = Path("output")
EXPLANATION_DIR = OUTPUT_DIR / "claude_explanations"
CSV_PATTERN = "mcp_repositories_*.csv"

DEFAULT_TEAMS_TITLE = "Claude Code向けMCP・関連ツール急上昇ランキング Top10"
DEFAULT_QIITA_TRENDING_URL = "https://qiita.com/4q_sano/items/cc27d3564a657046242a"
DEFAULT_GITHUB_REPOSITORY_URL = "https://github.com/TakanobuSano/mcp-github-ranking"


@dataclass
class RepositorySnapshot:
    rank: int
    full_name: str
    url: str
    description: str
    stars: int
    forks: int
    topics: str


@dataclass
class TrendingRepository:
    current: RepositorySnapshot
    previous: RepositorySnapshot
    star_delta: int
    fork_delta: int
    rank_delta: int | None


def get_env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        print(f"[warn] {name} must be integer. fallback to {default}.", file=sys.stderr)
        return default


def parse_int(value: str | None, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def extract_date_from_path(path: Path) -> str:
    match = re.fullmatch(r"mcp_repositories_(\d{4}-\d{2}-\d{2})\.csv", path.name)
    return match.group(1) if match else ""


def find_latest_csv_path() -> Path:
    dated_files = [p for p in OUTPUT_DIR.glob(CSV_PATTERN) if extract_date_from_path(p)]
    if not dated_files:
        raise FileNotFoundError(f"{OUTPUT_DIR}/{CSV_PATTERN} was not found.")
    return sorted(dated_files, key=lambda p: extract_date_from_path(p))[-1]


def get_baseline_csv_path(latest_date_text: str, period_days: int) -> tuple[str, Path]:
    latest_date = datetime.strptime(latest_date_text, "%Y-%m-%d").date()
    baseline_date_text = (latest_date - timedelta(days=period_days)).strftime("%Y-%m-%d")
    return baseline_date_text, OUTPUT_DIR / f"mcp_repositories_{baseline_date_text}.csv"


def read_snapshots(csv_path: Path) -> dict[str, RepositorySnapshot]:
    snapshots: dict[str, RepositorySnapshot] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            full_name = (row.get("full_name") or "").strip()
            if not full_name:
                continue
            snapshots[full_name] = RepositorySnapshot(
                rank=parse_int(row.get("rank")),
                full_name=full_name,
                url=(row.get("url") or "").strip(),
                description=(row.get("description") or "").strip(),
                stars=parse_int(row.get("stars")),
                forks=parse_int(row.get("forks")),
                topics=(row.get("topics") or "").strip(),
            )
    return snapshots


def calculate_trending(
    current_snapshots: dict[str, RepositorySnapshot],
    baseline_snapshots: dict[str, RepositorySnapshot],
) -> list[TrendingRepository]:
    items: list[TrendingRepository] = []
    for full_name, current in current_snapshots.items():
        previous = baseline_snapshots.get(full_name)
        if previous is None:
            continue
        star_delta = current.stars - previous.stars
        fork_delta = current.forks - previous.forks
        rank_delta = previous.rank - current.rank if current.rank > 0 and previous.rank > 0 else None
        if star_delta <= 0:
            continue
        items.append(TrendingRepository(current, previous, star_delta, fork_delta, rank_delta))
    return sorted(items, key=lambda item: (item.star_delta, item.fork_delta, item.current.stars), reverse=True)


def safe_repo_file_stem(full_name: str) -> str:
    return full_name.replace("/", "__").replace(":", "_")


def read_explanation(full_name: str) -> str:
    path = EXPLANATION_DIR / f"{safe_repo_file_stem(full_name)}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def truncate_text(text: str, max_length: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 1].rstrip() + "…"


def format_delta(value: int) -> str:
    return f"+{value:,}" if value > 0 else f"{value:,}"


def format_rank_delta(value: int | None) -> str:
    if value is None:
        return "順位差なし"
    if value > 0:
        return f"{value}位上昇"
    if value < 0:
        return f"{abs(value)}位下降"
    return "順位差なし"


def format_topics(topics: str, max_count: int = 5) -> str:
    values = [topic.strip() for topic in topics.split(",") if topic.strip()]
    return " ".join([f"`{topic}`" for topic in values[:max_count]]) if values else ""


def build_item_text(item: TrendingRepository, rank: int, period_days: int) -> str:
    current = item.current
    explanation = read_explanation(current.full_name)
    description = truncate_text(current.description, 120)
    topics = format_topics(current.topics)
    lines = [f"**{rank}位** **[{current.full_name}]({current.url})**"]
    if description:
        lines.append(description)
    if explanation:
        lines.append(f"解説: {truncate_text(explanation, 170)}")
    lines.append(
        f"⭐ {current.stars:,} Stars（{period_days}日間 {format_delta(item.star_delta)}） / "
        f"🍴 {current.forks:,} Forks（{period_days}日間 {format_delta(item.fork_delta)}）"
    )
    lines.append(f"順位: {current.rank}位（{format_rank_delta(item.rank_delta)}）")
    if topics:
        lines.append(topics)
    return "\n".join(lines)


def build_adaptive_card(body: list[dict[str, object]], qiita_url: str, github_url: str) -> dict:
    actions = []
    if qiita_url:
        actions.append({"type": "Action.OpenUrl", "title": "Qiita記事で全文を見る", "url": qiita_url})
    if github_url:
        actions.append({"type": "Action.OpenUrl", "title": "GitHubリポジトリを開く", "url": github_url})
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": body,
                    "actions": actions,
                },
            }
        ],
    }


def build_no_baseline_payload(
    latest_date_text: str,
    baseline_date_text: str,
    baseline_path: Path,
    period_days: int,
) -> dict:
    title = os.getenv("TEAMS_TITLE", DEFAULT_TEAMS_TITLE)
    qiita_url = os.getenv("QIITA_TRENDING_URL", DEFAULT_QIITA_TRENDING_URL)
    github_url = os.getenv("GITHUB_REPOSITORY_URL", DEFAULT_GITHUB_REPOSITORY_URL)
    body = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Default", "wrap": True},
        {
            "type": "TextBlock",
            "text": f"{period_days}日前のCSVがまだ存在しないため、今回のTeams配信では急上昇ランキングを作成できませんでした。",
            "wrap": True,
            "spacing": "Medium",
        },
        {
            "type": "TextBlock",
            "text": (
                f"最新データ: {latest_date_text} UTC\n"
                f"比較元データ: {baseline_date_text} UTC\n"
                f"見つからなかったファイル: `{baseline_path}`"
            ),
            "wrap": True,
            "spacing": "Small",
            "isSubtle": True,
        },
    ]
    return build_adaptive_card(body, qiita_url, github_url)


def build_trending_payload(
    trending: list[TrendingRepository],
    latest_date_text: str,
    baseline_date_text: str,
    period_days: int,
    display_results: int,
) -> dict:
    title = os.getenv("TEAMS_TITLE", DEFAULT_TEAMS_TITLE)
    qiita_url = os.getenv("QIITA_TRENDING_URL", DEFAULT_QIITA_TRENDING_URL)
    github_url = os.getenv("GITHUB_REPOSITORY_URL", DEFAULT_GITHUB_REPOSITORY_URL)
    display_items = trending[:display_results]
    body: list[dict[str, object]] = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Default", "wrap": True},
        {
            "type": "TextBlock",
            "text": f"比較期間: {baseline_date_text} UTC → {latest_date_text} UTC\nランキング指標: {period_days}日間のStars増加数",
            "isSubtle": True,
            "spacing": "Small",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": "Claude Code向けMCP・関連ツール候補のうち、直近で注目度が上がっているGitHubリポジトリをTop10で共有します。",
            "wrap": True,
            "spacing": "Small",
        },
    ]
    if not display_items:
        body.append({"type": "TextBlock", "text": "表示対象がありませんでした。", "wrap": True, "separator": True, "spacing": "Medium"})
    else:
        for rank, item in enumerate(display_items, start=1):
            body.append(
                {
                    "type": "TextBlock",
                    "text": build_item_text(item, rank, period_days),
                    "wrap": True,
                    "separator": True,
                    "spacing": "Medium",
                }
            )
        body.append(
            {
                "type": "TextBlock",
                "text": "11位以降はQiita記事で確認できます。",
                "wrap": True,
                "separator": True,
                "spacing": "Medium",
                "isSubtle": True,
            }
        )
    return build_adaptive_card(body, qiita_url, github_url)


def build_payload() -> dict:
    period_days = get_env_int("TRENDING_PERIOD_DAYS", 7)
    display_results = get_env_int("TEAMS_DISPLAY_RESULTS", 10)
    latest_csv_path = find_latest_csv_path()
    latest_date_text = extract_date_from_path(latest_csv_path)
    baseline_date_text, baseline_csv_path = get_baseline_csv_path(latest_date_text, period_days)
    if not baseline_csv_path.exists():
        return build_no_baseline_payload(latest_date_text, baseline_date_text, baseline_csv_path, period_days)
    current_snapshots = read_snapshots(latest_csv_path)
    baseline_snapshots = read_snapshots(baseline_csv_path)
    trending = calculate_trending(current_snapshots, baseline_snapshots)
    print(f"[info] latest_csv: {latest_csv_path}", file=sys.stderr)
    print(f"[info] baseline_csv: {baseline_csv_path}", file=sys.stderr)
    print(f"[info] trending_count: {len(trending)}", file=sys.stderr)
    print(f"[info] teams_display_results: {display_results}", file=sys.stderr)
    return build_trending_payload(trending, latest_date_text, baseline_date_text, period_days, display_results)


def post_to_teams(webhook_url: str, payload_obj: dict) -> None:
    payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        webhook_url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "mcp-github-ranking-teams-notifier/1.0"},
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            status_code = resp.getcode()
            response_body = resp.read().decode("utf-8", errors="replace")
            if status_code < 200 or status_code >= 300:
                raise RuntimeError(f"Teams webhook failed: HTTP {status_code}: {response_body}")
    except error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Teams webhook failed: HTTP {e.code}: {err_body}") from e
    except error.URLError as e:
        raise RuntimeError(f"Teams webhook request failed: {e}") from e


def main() -> int:
    webhook_url = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("[error] TEAMS_WEBHOOK_URL is required.", file=sys.stderr)
        return 1
    try:
        payload = build_payload()
        print("[info] posting MCP trending ranking card to Teams...", file=sys.stderr)
        post_to_teams(webhook_url, payload)
        print("[done] posted Teams card successfully.")
        return 0
    except Exception as exc:
        print(f"[error] failed to post to Teams: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
