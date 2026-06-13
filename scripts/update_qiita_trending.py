import csv
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests
from zoneinfo import ZoneInfo


QIITA_API_BASE_URL = "https://qiita.com/api/v2"
JST = ZoneInfo("Asia/Tokyo")

OUTPUT_DIR = Path("output")
GITHUB_REPOSITORY_URL = "https://github.com/TakanobuSano/mcp-github-ranking"

DEFAULT_TITLE = "Claude Code向けMCP・関連ツール急上昇ランキング【7日間Stars増加数で毎日自動更新】"

DEFAULT_TAGS = [
    {"name": "ClaudeCode", "versions": []},
    {"name": "MCP", "versions": []},
    {"name": "GitHubActions", "versions": []},
    {"name": "Python", "versions": []},
    {"name": "GitHub", "versions": []},
]


@dataclass
class RepositorySnapshot:
    rank: int
    full_name: str
    url: str
    description: str
    stars: int
    forks: int
    open_issues: int
    language: str
    topics: str
    created_at: str
    updated_at: str
    pushed_at: str


@dataclass
class TrendingRepository:
    current: RepositorySnapshot
    previous: RepositorySnapshot
    star_delta_7d: int
    fork_delta_7d: int
    rank_delta_7d: int | None


def require_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(f"{name} is required.")

    return value


def get_env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.lower() in {"1", "true", "yes", "y", "on"}


def get_env_int(name: str, default: int) -> int:
    value = os.getenv(name)

    if not value:
        return default

    try:
        return int(value)
    except ValueError:
        print(f"[WARN] {name} must be integer. fallback to {default}.")
        return default


def parse_int(value: str | None, default: int = 0) -> int:
    if value is None or value == "":
        return default

    try:
        return int(value)
    except ValueError:
        return default


def extract_date_from_csv_path(path: Path) -> str:
    match = re.fullmatch(r"mcp_repositories_(\d{4}-\d{2}-\d{2})\.csv", path.name)

    if not match:
        return ""

    return match.group(1)


def find_latest_csv_path() -> Path:
    candidates: list[tuple[str, Path]] = []

    for path in OUTPUT_DIR.glob("mcp_repositories_20??-??-??.csv"):
        date_text = extract_date_from_csv_path(path)

        if not date_text:
            continue

        candidates.append((date_text, path))

    if not candidates:
        raise FileNotFoundError(
            "No dated CSV file found. Expected output/mcp_repositories_YYYY-MM-DD.csv."
        )

    candidates.sort(key=lambda item: item[0], reverse=True)
    latest_date, latest_path = candidates[0]

    print(f"[INFO] Latest dated CSV: {latest_path} ({latest_date})")
    return latest_path


def to_date(date_text: str) -> datetime:
    return datetime.strptime(date_text, "%Y-%m-%d")


def get_baseline_csv_path(latest_date_text: str, days: int) -> tuple[str, Path]:
    latest_date = to_date(latest_date_text)
    baseline_date = latest_date - timedelta(days=days)
    baseline_date_text = baseline_date.strftime("%Y-%m-%d")
    baseline_path = OUTPUT_DIR / f"mcp_repositories_{baseline_date_text}.csv"

    return baseline_date_text, baseline_path


def read_snapshots(path: Path) -> dict[str, RepositorySnapshot]:
    snapshots: dict[str, RepositorySnapshot] = {}

    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)

        for row in reader:
            full_name = row.get("full_name", "").strip()

            if not full_name:
                continue

            snapshots[full_name] = RepositorySnapshot(
                rank=parse_int(row.get("rank")),
                full_name=full_name,
                url=row.get("url", "").strip(),
                description=row.get("description", "").strip(),
                stars=parse_int(row.get("stars")),
                forks=parse_int(row.get("forks")),
                open_issues=parse_int(row.get("open_issues")),
                language=row.get("language", "").strip(),
                topics=row.get("topics", "").strip(),
                created_at=row.get("created_at", "").strip(),
                updated_at=row.get("updated_at", "").strip(),
                pushed_at=row.get("pushed_at", "").strip(),
            )

    return snapshots


def calculate_trending(
    current_snapshots: dict[str, RepositorySnapshot],
    baseline_snapshots: dict[str, RepositorySnapshot],
) -> list[TrendingRepository]:
    trending: list[TrendingRepository] = []

    for full_name, current in current_snapshots.items():
        previous = baseline_snapshots.get(full_name)

        if previous is None:
            continue

        star_delta_7d = current.stars - previous.stars
        fork_delta_7d = current.forks - previous.forks

        if star_delta_7d <= 0:
            continue

        rank_delta_7d: int | None = None

        if current.rank > 0 and previous.rank > 0:
            rank_delta_7d = previous.rank - current.rank

        trending.append(
            TrendingRepository(
                current=current,
                previous=previous,
                star_delta_7d=star_delta_7d,
                fork_delta_7d=fork_delta_7d,
                rank_delta_7d=rank_delta_7d,
            )
        )

    trending.sort(
        key=lambda item: (
            item.star_delta_7d,
            item.fork_delta_7d,
            item.current.stars,
        ),
        reverse=True,
    )

    return trending


def md_escape(value: str) -> str:
    return (
        value.replace("\n", " ")
        .replace("\r", " ")
        .replace("|", "\\|")
        .strip()
    )


def remove_urls(value: str) -> str:
    return re.sub(r"https?://\S+", "", value).strip()


def contains_fullwidth_text(value: str) -> bool:
    return any(
        unicodedata.east_asian_width(char) in {"F", "W"}
        for char in value
    )


def truncate_description(value: str) -> str:
    text = md_escape(remove_urls(value))

    if not text:
        return "説明なし"

    max_chars = 160 if contains_fullwidth_text(text) else 360

    if len(text) <= max_chars:
        return text

    return text[:max_chars].rstrip() + "..."


def format_delta(value: int) -> str:
    if value > 0:
        return f"+{value:,}"

    if value == 0:
        return "±0"

    return f"{value:,}"


def format_rank_delta(value: int | None) -> str:
    if value is None:
        return "順位変動なし"

    if value > 0:
        return f"{value}位上昇"

    if value == 0:
        return "順位変動なし"

    return f"{abs(value)}位低下"


def date_only(value: str) -> str:
    if not value:
        return ""

    return value.split("T")[0]


def format_topics(value: str) -> str:
    topics = [topic.strip() for topic in value.split(",") if topic.strip()]

    if not topics:
        return "`topicなし`"

    return " / ".join([f"`{md_escape(topic)}`" for topic in topics[:8]])


def build_no_baseline_body(
    latest_date_text: str,
    baseline_date_text: str,
    baseline_path: Path,
    period_days: int,
) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    lines = [
        ":::note info",
        f"最終更新: **{now}**",
        f"最新データ: **{latest_date_text} UTC**",
        f"比較予定期間: **{period_days}日間**",
        ":::",
        "",
        "# Claude Code向けMCP・関連ツール急上昇ランキング",
        "",
        f"{period_days}日前のCSVがまだ存在しないため、急上昇ランキングは次回以降の更新で表示されます。",
        "",
        "比較に必要なファイルは以下です。",
        "",
        f"- 最新CSV: `output/mcp_repositories_{latest_date_text}.csv`",
        f"- 比較元CSV: `output/mcp_repositories_{baseline_date_text}.csv`",
        "",
        f"現在、比較元CSVが見つかりませんでした: `{baseline_path}`",
        "",
        "# 仕組み",
        "",
        "この急上昇ランキングは、最新CSVと7日前CSVを比較して、Stars増加数が大きい順に並べる記事です。",
        "",
        GITHUB_REPOSITORY_URL,
        "",
    ]

    return "\n".join(lines)


def build_trending_body(
    trending: list[TrendingRepository],
    latest_date_text: str,
    baseline_date_text: str,
    period_days: int,
    display_results: int,
    current_count: int,
    baseline_count: int,
) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    display_items = trending[:display_results]

    lines = [
        ":::note info",
        f"最終更新: **{now}**",
        f"比較期間: **{baseline_date_text} UTC → {latest_date_text} UTC**",
        f"ランキング指標: **{period_days}日間のStars増加数**",
        "",
        "MCP関連リポジトリに加え、Claude Code周辺で活用候補になりそうな関連ツールのうち、直近7日間でStarsが増えたものをランキング化しています。",
        ":::",
        "",
        "# Claude Code向けMCP・関連ツール急上昇ランキング",
        "",
        f"比較対象CSVの件数は、最新日が **{current_count}件**、比較元が **{baseline_count}件** です。",
        f"このうち、{period_days}日間でStarsが増加したリポジトリを上位 **{len(display_items)}件** 表示しています。",
        "",
    ]

    if not display_items:
        lines.extend(
            [
                "表示対象がありませんでした。",
                "",
                "7日間でStarsが増加したリポジトリがない、または最新CSVと7日前CSVに共通して存在するリポジトリが少ない可能性があります。",
                "",
            ]
        )
    else:
        for index, item in enumerate(display_items, start=1):
            current = item.current
            description = truncate_description(current.description)
            language = md_escape(current.language) or "不明"
            pushed_at = date_only(current.pushed_at or current.updated_at)
            topics = format_topics(current.topics)

            lines.extend(
                [
                    f"## {index}位 [{md_escape(current.full_name)}]({current.url})",
                    "",
                    description,
                    "",
                    f"⭐ **{current.stars:,} Stars**（{period_days}日間 {format_delta(item.star_delta_7d)}）",
                    f"🍴 **{current.forks:,} Forks**（{period_days}日間 {format_delta(item.fork_delta_7d)}）",
                    f"順位: {current.rank}位（{format_rank_delta(item.rank_delta_7d)}）",
                    f"言語: {language}",
                    f"最終プッシュ: {pushed_at}",
                    "",
                    f"Topics: {topics}",
                    "",
                ]
            )

    lines.extend(
        [
            "# 集計方法",
            "",
            f"- 最新CSV: `output/mcp_repositories_{latest_date_text}.csv`",
            f"- 比較元CSV: `output/mcp_repositories_{baseline_date_text}.csv`",
            f"- 比較期間: {period_days}日間",
            "- 並び順: Stars増加数、Forks増加数、現在のStars数の順",
            "- 除外条件: 7日間のStars増加数が0以下のリポジトリ",
            "",
            "# 仕組み",
            "",
            "このランキングの生成コードとGitHub Actionsの設定は、以下のリポジトリで管理しています。",
            "",
            GITHUB_REPOSITORY_URL,
            "",
            "# 注意点",
            "",
            "このランキングは、GitHub Search APIの検索結果をもとにした自動集計です。",
            "急上昇していることは品質や導入推奨を意味しません。",
            "",
            "実際に導入する場合は、README、最終プッシュ日、Issues、Pull Requests、ライセンス、Claude Codeでの利用方法を確認してください。",
            "",
        ]
    )

    return "\n".join(lines)


def update_qiita_item() -> None:
    token = require_env("QIITA_TOKEN")
    item_id = require_env("QIITA_TRENDING_ITEM_ID")

    title = os.getenv("QIITA_TRENDING_TITLE", DEFAULT_TITLE)
    private = get_env_bool("QIITA_TRENDING_PRIVATE", True)
    period_days = get_env_int("TRENDING_PERIOD_DAYS", 7)
    display_results = get_env_int("TRENDING_DISPLAY_RESULTS", 30)

    latest_csv_path = find_latest_csv_path()
    latest_date_text = extract_date_from_csv_path(latest_csv_path)
    baseline_date_text, baseline_csv_path = get_baseline_csv_path(latest_date_text, period_days)

    if not baseline_csv_path.exists():
        body = build_no_baseline_body(
            latest_date_text=latest_date_text,
            baseline_date_text=baseline_date_text,
            baseline_path=baseline_csv_path,
            period_days=period_days,
        )
    else:
        current_snapshots = read_snapshots(latest_csv_path)
        baseline_snapshots = read_snapshots(baseline_csv_path)
        trending = calculate_trending(current_snapshots, baseline_snapshots)

        body = build_trending_body(
            trending=trending,
            latest_date_text=latest_date_text,
            baseline_date_text=baseline_date_text,
            period_days=period_days,
            display_results=display_results,
            current_count=len(current_snapshots),
            baseline_count=len(baseline_snapshots),
        )

    payload = {
        "title": title,
        "body": body,
        "tags": DEFAULT_TAGS,
        "private": private,
        "coediting": False,
        "slide": False,
    }

    response = requests.patch(
        f"{QIITA_API_BASE_URL}/items/{item_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )

    if not response.ok:
        print("[ERROR] Qiita API request failed.")
        print(f"status: {response.status_code}")
        print(response.text)
        response.raise_for_status()

    data = response.json()
    url = data.get("url", "")

    print("[INFO] Qiita trending item updated.")
    print(f"[INFO] private: {private}")
    print(f"[INFO] latest_csv_path: {latest_csv_path}")
    print(f"[INFO] baseline_csv_path: {baseline_csv_path}")

    if url:
        print(f"[INFO] URL: {url}")


def main() -> int:
    update_qiita_item()
    return 0


if __name__ == "__main__":
    sys.exit(main())
