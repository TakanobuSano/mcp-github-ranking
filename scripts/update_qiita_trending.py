import base64
import csv
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from zoneinfo import ZoneInfo


QIITA_API_BASE_URL = "https://qiita.com/api/v2"
GITHUB_API_BASE_URL = "https://api.github.com"
JST = ZoneInfo("Asia/Tokyo")

OUTPUT_DIR = Path("output")
README_CACHE_DIR = OUTPUT_DIR / "readmes"
CLAUDE_INPUT_DIR = OUTPUT_DIR / "claude_inputs"
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


@dataclass
class ReadmeCacheResult:
    full_name: str
    readme_cache_path: str
    readme_meta_path: str
    readme_length: int
    readme_truncated_for_claude_input: bool
    readme_text_for_claude_input: str
    cache_status: str


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


def build_github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mcp-github-ranking",
    }

    token = os.getenv("GITHUB_TOKEN")

    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def safe_repo_file_stem(full_name: str) -> str:
    return full_name.replace("/", "__").replace(" ", "_")


def readme_cache_paths(full_name: str) -> tuple[Path, Path]:
    stem = safe_repo_file_stem(full_name)
    return README_CACHE_DIR / f"{stem}.md", README_CACHE_DIR / f"{stem}.json"


def request_json_with_retry(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    max_retries: int = 3,
) -> dict[str, Any]:
    for attempt in range(1, max_retries + 1):
        response = session.get(
            url,
            headers=headers,
            timeout=30,
        )

        if response.status_code == 403:
            reset_timestamp = response.headers.get("X-RateLimit-Reset")
            remaining = response.headers.get("X-RateLimit-Remaining")

            if remaining == "0" and reset_timestamp:
                wait_seconds = max(int(reset_timestamp) - int(time.time()) + 5, 5)
                print(f"[WARN] GitHub API rate limit reached. wait {wait_seconds} seconds.")
                time.sleep(wait_seconds)
                continue

        if response.status_code == 404:
            print(f"[WARN] README not found: {url}")
            return {}

        if response.status_code in {500, 502, 503, 504}:
            wait_seconds = attempt * 5
            print(
                f"[WARN] GitHub API temporary error: {response.status_code}. "
                f"retry in {wait_seconds}s."
            )
            time.sleep(wait_seconds)
            continue

        if not response.ok:
            print("[ERROR] GitHub API request failed.")
            print(f"url: {url}")
            print(f"status: {response.status_code}")
            print(response.text)
            response.raise_for_status()

        return response.json()

    raise RuntimeError("GitHub API request failed after retries.")


def decode_readme_content(data: dict[str, Any]) -> str:
    encoded_content = data.get("content")

    if not encoded_content:
        return ""

    encoding = data.get("encoding", "")

    if encoding != "base64":
        return ""

    normalized = encoded_content.replace("\n", "")

    try:
        return base64.b64decode(normalized).decode("utf-8", errors="replace")
    except Exception as error:
        print(f"[WARN] Failed to decode README content: {error}")
        return ""


def fetch_readme_from_github(
    session: requests.Session,
    full_name: str,
    headers: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    url = f"{GITHUB_API_BASE_URL}/repos/{full_name}/readme"
    data = request_json_with_retry(session=session, url=url, headers=headers)

    if not data:
        return "", {}

    readme_text = decode_readme_content(data)

    metadata = {
        "github_api_url": url,
        "readme_name": data.get("name", ""),
        "readme_path": data.get("path", ""),
        "readme_html_url": data.get("html_url", ""),
        "readme_download_url": data.get("download_url", ""),
        "readme_sha": data.get("sha", ""),
        "readme_size": data.get("size", 0),
    }

    return readme_text, metadata


def get_or_fetch_readme(
    item: TrendingRepository,
    session: requests.Session,
    headers: dict[str, str],
    max_chars_for_claude_input: int,
) -> ReadmeCacheResult:
    current = item.current
    readme_path, meta_path = readme_cache_paths(current.full_name)

    README_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if readme_path.exists() and meta_path.exists():
        readme_text = readme_path.read_text(encoding="utf-8", errors="replace")
        cache_status = "cache_hit"

        try:
            existing_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing_meta = {}

        readme_meta = {
            **existing_meta,
            "last_used_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        readme_text, github_meta = fetch_readme_from_github(
            session=session,
            full_name=current.full_name,
            headers=headers,
        )
        cache_status = "fetched"

        readme_path.write_text(readme_text, encoding="utf-8")

        readme_meta = {
            "full_name": current.full_name,
            "url": current.url,
            "readme_cache_path": str(readme_path),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "repo_pushed_at_when_fetched": current.pushed_at,
            "readme_length": len(readme_text),
            "readme_available": bool(readme_text),
            **github_meta,
        }

    readme_text_for_claude_input = readme_text[:max_chars_for_claude_input]
    readme_truncated = len(readme_text) > max_chars_for_claude_input

    readme_meta.update(
        {
            "last_used_at": datetime.now(timezone.utc).isoformat(),
            "readme_length": len(readme_text),
            "readme_truncated_for_claude_input": readme_truncated,
            "claude_input_max_chars": max_chars_for_claude_input,
            "cache_status": cache_status,
        }
    )

    meta_path.write_text(
        json.dumps(readme_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return ReadmeCacheResult(
        full_name=current.full_name,
        readme_cache_path=str(readme_path),
        readme_meta_path=str(meta_path),
        readme_length=len(readme_text),
        readme_truncated_for_claude_input=readme_truncated,
        readme_text_for_claude_input=readme_text_for_claude_input,
        cache_status=cache_status,
    )


def build_claude_input_json(
    trending: list[TrendingRepository],
    latest_date_text: str,
    baseline_date_text: str,
    period_days: int,
    readme_target_count: int,
    readme_max_chars_for_claude_input: int,
) -> tuple[Path, dict[str, Any]]:
    CLAUDE_INPUT_DIR.mkdir(parents=True, exist_ok=True)

    selected_items = trending[:readme_target_count]
    headers = build_github_headers()
    repositories: list[dict[str, Any]] = []

    with requests.Session() as session:
        for index, item in enumerate(selected_items, start=1):
            readme_result = get_or_fetch_readme(
                item=item,
                session=session,
                headers=headers,
                max_chars_for_claude_input=readme_max_chars_for_claude_input,
            )

            current = item.current

            repositories.append(
                {
                    "trending_rank": index,
                    "full_name": current.full_name,
                    "url": current.url,
                    "description": current.description,
                    "language": current.language,
                    "topics": [topic.strip() for topic in current.topics.split(",") if topic.strip()],
                    "stars": current.stars,
                    "star_delta_7d": item.star_delta_7d,
                    "forks": current.forks,
                    "fork_delta_7d": item.fork_delta_7d,
                    "open_issues": current.open_issues,
                    "rank": current.rank,
                    "rank_delta_7d": item.rank_delta_7d,
                    "pushed_at": current.pushed_at,
                    "readme_cache_path": readme_result.readme_cache_path,
                    "readme_meta_path": readme_result.readme_meta_path,
                    "readme_length": readme_result.readme_length,
                    "readme_truncated_for_claude_input": readme_result.readme_truncated_for_claude_input,
                    "readme_cache_status": readme_result.cache_status,
                    "readme_text": readme_result.readme_text_for_claude_input,
                }
            )

            time.sleep(0.5)

    payload = {
        "purpose": "Claude API input for generating Japanese explanations for Qiita trending ranking article.",
        "ranking_date": latest_date_text,
        "baseline_date": baseline_date_text,
        "period_days": period_days,
        "readme_target_count": readme_target_count,
        "readme_max_chars_for_claude_input": readme_max_chars_for_claude_input,
        "prompt_policy": {
            "language": "Japanese",
            "style": "Qiita向けの簡潔で実務寄りの解説",
            "avoid_overclaiming": True,
            "notes": [
                "CSVとREADMEに書かれていない事実は断定しない。",
                "Claude Code対応やMCP対応はREADMEで確認できる場合のみ断定する。",
                "不明な場合は、対応している可能性、確認が必要、という表現にする。",
            ],
        },
        "repositories": repositories,
    }

    output_path = CLAUDE_INPUT_DIR / f"trending_readme_context_{latest_date_text}.json"
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[INFO] Claude input JSON written: {output_path}")

    return output_path, payload


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
    claude_input_path: Path | None,
    claude_input_repository_count: int,
) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    display_items = trending[:display_results]

    # READMEキャッシュとClaude入力JSON、CSV件数は内部処理として利用する。
    # Qiita記事本文には、運用者向けの内部情報を表示しない。
    _ = current_count
    _ = baseline_count
    _ = claude_input_path
    _ = claude_input_repository_count

    lines = [
        ":::note info",
        f"最終更新: **{now}**",
        f"比較期間: **{baseline_date_text} UTC → {latest_date_text} UTC**",
        f"ランキング指標: **{period_days}日間のStars増加数**",
        ":::",
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
            topics = format_topics(current.topics)

            lines.extend(
                [
                    f"## {index}位 [{md_escape(current.full_name)}]({current.url})",
                    "",
                    description,
                    "",
                    f"⭐ **{current.stars:,} Stars**（{period_days}日間 {format_delta(item.star_delta_7d)}）　🍴 **{current.forks:,} Forks**（{period_days}日間 {format_delta(item.fork_delta_7d)}）",
                    f"順位: {current.rank}位（{format_rank_delta(item.rank_delta_7d)}）",
                    "",
                    f"Topics: {topics}",
                    "",
                ]
            )

    lines.extend(
        [
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
    readme_target_count = get_env_int("README_TARGET_COUNT", 10)
    readme_max_chars_for_claude_input = get_env_int("README_MAX_CHARS_FOR_CLAUDE_INPUT", 30000)
    prepare_claude_input = get_env_bool("PREPARE_CLAUDE_INPUT", True)

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

        claude_input_path: Path | None = None
        claude_input_repository_count = 0

        if prepare_claude_input and trending:
            claude_input_path, claude_payload = build_claude_input_json(
                trending=trending,
                latest_date_text=latest_date_text,
                baseline_date_text=baseline_date_text,
                period_days=period_days,
                readme_target_count=readme_target_count,
                readme_max_chars_for_claude_input=readme_max_chars_for_claude_input,
            )
            claude_input_repository_count = len(claude_payload.get("repositories", []))

        body = build_trending_body(
            trending=trending,
            latest_date_text=latest_date_text,
            baseline_date_text=baseline_date_text,
            period_days=period_days,
            display_results=display_results,
            current_count=len(current_snapshots),
            baseline_count=len(baseline_snapshots),
            claude_input_path=claude_input_path,
            claude_input_repository_count=claude_input_repository_count,
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
