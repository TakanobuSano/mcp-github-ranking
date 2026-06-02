import csv
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


GITHUB_SEARCH_API_URL = "https://api.github.com/search/repositories"

README_PATH = Path("README.md")
OUTPUT_DIR = Path("output")

START_MARKER = "<!-- MCP_REPOS_START -->"
END_MARKER = "<!-- MCP_REPOS_END -->"

JST = ZoneInfo("Asia/Tokyo")

DEFAULT_SEARCH_QUERIES = [
    '"model context protocol" in:name,description,readme stars:>10',
    '"mcp server" in:name,description,readme stars:>10',
    '"mcp" "claude" in:name,description,readme stars:>10',
    '"claude" "mcp" in:name,description,readme stars:>10',
    '"modelcontextprotocol" in:name,description,readme stars:>10',
    '"claude code" in:name,description,readme stars:>10',
    '"claude" "plugin" in:name,description,readme stars:>10',
    '"claude" "memory" in:name,description,readme stars:>10',
]

EXCLUDE_KEYWORDS = [
    "minecraft",
    "awesome",
    "roadmap",
    "interview",
    "coding-interview",
    "leetcode",
    "awesome-python",
    "awesome-go",
    "awesome-mac",
    "awesome-llm-apps",
    "funnlp",
]


@dataclass
class Repository:
    full_name: str
    html_url: str
    description: str
    stargazers_count: int
    forks_count: int
    open_issues_count: int
    language: str
    topics: list[str]
    created_at: str
    updated_at: str
    pushed_at: str
    archived: bool


@dataclass
class MetricDelta:
    star_delta: int | None
    fork_delta: int | None


def get_env_int(name: str, default: int) -> int:
    value = os.getenv(name)

    if not value:
        return default

    try:
        return int(value)
    except ValueError:
        print(f"[WARN] {name} must be integer. fallback to {default}.")
        return default


def get_search_queries() -> list[str]:
    raw = os.getenv("SEARCH_QUERIES", "").strip()

    if not raw:
        return DEFAULT_SEARCH_QUERIES

    return [line.strip() for line in raw.splitlines() if line.strip()]


def build_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mcp-github-ranking",
    }

    token = os.getenv("GITHUB_TOKEN")

    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        print("[WARN] GITHUB_TOKEN is not set. Unauthenticated rate limit will be used.")

    return headers


def request_json_with_retry(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    max_retries: int = 3,
) -> dict[str, Any]:
    for attempt in range(1, max_retries + 1):
        response = session.get(
            url,
            params=params,
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


def to_repository(item: dict[str, Any]) -> Repository:
    return Repository(
        full_name=item.get("full_name", ""),
        html_url=item.get("html_url", ""),
        description=item.get("description") or "",
        stargazers_count=item.get("stargazers_count") or 0,
        forks_count=item.get("forks_count") or 0,
        open_issues_count=item.get("open_issues_count") or 0,
        language=item.get("language") or "",
        topics=item.get("topics") or [],
        created_at=item.get("created_at") or "",
        updated_at=item.get("updated_at") or "",
        pushed_at=item.get("pushed_at") or "",
        archived=item.get("archived") or False,
    )


def repository_text(repo: Repository) -> str:
    return " ".join(
        [
            repo.full_name,
            repo.description,
            repo.language,
            " ".join(repo.topics),
        ]
    ).lower()


def is_candidate_repository(repo: Repository) -> bool:
    if repo.archived:
        return False

    text = repository_text(repo)

    if any(excluded in text for excluded in EXCLUDE_KEYWORDS):
        return False

    # GitHub Search API の in:name,description,readme にヒットした結果を信頼する。
    # READMEにだけClaude Code / MCP関連情報があるリポジトリも取りこぼさないため、
    # ここでは明確なノイズ除外だけを行う。
    return True


def search_repositories() -> list[Repository]:
    queries = get_search_queries()
    max_pages = get_env_int("MAX_PAGES_PER_QUERY", 2)
    per_page = min(get_env_int("PER_PAGE", 100), 100)
    max_results = get_env_int("MAX_RESULTS", 100)

    headers = build_headers()
    repositories: dict[str, Repository] = {}

    with requests.Session() as session:
        for query in queries:
            print(f"[INFO] Search query: {query}")

            for page in range(1, max_pages + 1):
                params = {
                    "q": query,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": per_page,
                    "page": page,
                }

                data = request_json_with_retry(
                    session=session,
                    url=GITHUB_SEARCH_API_URL,
                    params=params,
                    headers=headers,
                )

                items = data.get("items", [])

                print(f"[INFO] page={page}, items={len(items)}")

                if not items:
                    break

                for item in items:
                    repo = to_repository(item)

                    if not is_candidate_repository(repo):
                        continue

                    repositories[repo.full_name] = repo

                time.sleep(1)

    sorted_repositories = sorted(
        repositories.values(),
        key=lambda repo: repo.stargazers_count,
        reverse=True,
    )

    return sorted_repositories[:max_results]


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
        return ""

    max_chars = 160 if contains_fullwidth_text(text) else 360

    if len(text) <= max_chars:
        return text

    return text[:max_chars].rstrip() + "..."


def date_only(value: str) -> str:
    if not value:
        return ""

    return value.split("T")[0]


def build_topics(topics: list[str]) -> str:
    if not topics:
        return "`topicなし`"

    return " / ".join([f"`{md_escape(topic)}`" for topic in topics[:8]])


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def load_previous_metrics(path: Path) -> dict[str, dict[str, int]]:
    if not path.exists():
        print(f"[INFO] Previous day CSV does not exist: {path}")
        return {}

    metrics: dict[str, dict[str, int]] = {}

    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)

        for row in reader:
            full_name = row.get("full_name", "")
            stars = parse_int(row.get("stars"))
            forks = parse_int(row.get("forks"))

            if not full_name or stars is None or forks is None:
                continue

            metrics[full_name] = {
                "stars": stars,
                "forks": forks,
            }

    print(f"[INFO] Loaded previous day metrics. repositories={len(metrics)}")
    return metrics


def calculate_metric_deltas(
    repositories: list[Repository],
    previous_metrics: dict[str, dict[str, int]],
) -> dict[str, MetricDelta]:
    deltas: dict[str, MetricDelta] = {}

    for repo in repositories:
        previous = previous_metrics.get(repo.full_name)

        if previous is None:
            deltas[repo.full_name] = MetricDelta(
                star_delta=None,
                fork_delta=None,
            )
            continue

        deltas[repo.full_name] = MetricDelta(
            star_delta=repo.stargazers_count - previous["stars"],
            fork_delta=repo.forks_count - previous["forks"],
        )

    return deltas


def format_delta(delta: int | None) -> str:
    if delta is None:
        return "（前日なし）"

    if delta == 0:
        return "（±0）"

    if delta > 0:
        return f"（+{delta:,}）"

    return f"（{delta:,}）"


def build_metric_line(
    repo: Repository,
    metric_delta: MetricDelta,
    language: str,
    include_open_issues: bool,
    pushed_at: str | None = None,
) -> str:
    line = (
        f"⭐ **{repo.stargazers_count:,} Stars**{format_delta(metric_delta.star_delta)}"
        f"　🍴 **{repo.forks_count:,} Forks**{format_delta(metric_delta.fork_delta)}"
    )

    if include_open_issues:
        line += f"　/　🟢 **{repo.open_issues_count:,} Open Issues**"

    line += f"　/　{language}"

    if pushed_at:
        line += f"　/　最終プッシュ: {pushed_at}"

    return line


def build_markdown(
    repositories: list[Repository],
    now_jst: datetime,
    metric_deltas: dict[str, MetricDelta],
    current_date_text: str,
    previous_date_text: str,
) -> str:
    generated_at = now_jst.strftime("%Y-%m-%d %H:%M:%S JST")

    lines = [
        f"最終更新: **{generated_at}**",
        "",
        "MCP関連リポジトリに加え、Claude Code周辺で活用候補になりそうな関連ツールをGitHub Search APIで毎日自動収集してランキング化しています。",
        "",
        f"Stars / Forks の差分は、UTC基準の前日データ（{previous_date_text}）との差分です。",
        "",
        "> 注意: この一覧はClaude Codeでの動作を保証するものではありません。  ",
        "> MCP関連ツールまたはClaude Code関連ツール候補を探すための入口として利用してください。",
        "",
        "# 注目MCP・関連ツール候補ランキング",
        "",
    ]

    for index, repo in enumerate(repositories, start=1):
        description = truncate_description(repo.description) or "説明なし"
        language = md_escape(repo.language) or "不明"
        topics = build_topics(repo.topics)
        metric_delta = metric_deltas.get(repo.full_name, MetricDelta(None, None))

        lines.extend(
            [
                f"## {index}位 [{md_escape(repo.full_name)}]({repo.html_url})",
                "",
                description,
                "",
                build_metric_line(
                    repo=repo,
                    metric_delta=metric_delta,
                    language=language,
                    include_open_issues=True,
                ),
                "",
                f"Topics: {topics}",
                "",
            ]
        )

    lines.extend(
        [
            "# 最近プッシュされたMCP・関連ツール候補",
            "",
            "スター数ランキングとは別に、最近コードがプッシュされたリポジトリを表示します。古いスター数だけではなく、現在も開発が動いていそうな候補を探すための一覧です。",
            "",
        ]
    )

    recently_updated = sorted(
        repositories,
        key=lambda repo: repo.pushed_at or repo.updated_at,
        reverse=True,
    )[:30]

    for index, repo in enumerate(recently_updated, start=1):
        description = truncate_description(repo.description) or "説明なし"
        language = md_escape(repo.language) or "不明"
        pushed_at = date_only(repo.pushed_at or repo.updated_at)
        topics = build_topics(repo.topics)
        metric_delta = metric_deltas.get(repo.full_name, MetricDelta(None, None))

        lines.extend(
            [
                f"## プッシュ順 {index}位 [{md_escape(repo.full_name)}]({repo.html_url})",
                "",
                description,
                "",
                build_metric_line(
                    repo=repo,
                    metric_delta=metric_delta,
                    language=language,
                    include_open_issues=False,
                    pushed_at=pushed_at,
                ),
                "",
                f"Topics: {topics}",
                "",
            ]
        )

    lines.extend(
        [
            "# 検索条件",
            "",
            "以下の検索条件でGitHubリポジトリを収集しています。",
            "",
        ]
    )

    for query in get_search_queries():
        lines.append(f"- {query}")

    lines.append("")

    return "\n".join(lines)


def write_csv(
    repositories: list[Repository],
    metric_deltas: dict[str, MetricDelta],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)

        writer.writerow(
            [
                "rank",
                "full_name",
                "url",
                "description",
                "stars",
                "star_delta_vs_previous_day",
                "forks",
                "fork_delta_vs_previous_day",
                "open_issues",
                "language",
                "topics",
                "created_at",
                "updated_at",
                "pushed_at",
                "archived",
            ]
        )

        for index, repo in enumerate(repositories, start=1):
            metric_delta = metric_deltas.get(repo.full_name, MetricDelta(None, None))

            writer.writerow(
                [
                    index,
                    repo.full_name,
                    repo.html_url,
                    repo.description,
                    repo.stargazers_count,
                    "" if metric_delta.star_delta is None else metric_delta.star_delta,
                    repo.forks_count,
                    "" if metric_delta.fork_delta is None else metric_delta.fork_delta,
                    repo.open_issues_count,
                    repo.language,
                    ",".join(repo.topics),
                    repo.created_at,
                    repo.updated_at,
                    repo.pushed_at,
                    repo.archived,
                ]
            )


def build_default_readme() -> str:
    lines = [
        "# Claude Code向けMCP・関連ツール候補ランキング【GitHub Search APIで毎日自動更新】",
        "",
        "GitHub Search APIを使って、MCP関連リポジトリとClaude Code周辺で活用候補になりそうな関連ツールを定期収集するリポジトリです。",
        "",
        "> 注意: この一覧は「Claude Codeでの動作」を保証するものではありません。  ",
        "> MCP関連ツールまたはClaude Code関連ツール候補を探すための入口として利用します。",
        "",
        START_MARKER,
        "まだランキングは生成されていません。",
        END_MARKER,
        "",
        "## 仕組み",
        "",
        "1. GitHub Search APIでMCP関連リポジトリを検索",
        "2. Claude Code関連ツール候補を検索",
        "3. スター数・Fork数・Open Issues・説明文・Topicsを取得",
        "4. UTC基準の前日CSVと比較してStars/Forksの前日比を計算",
        "5. 日付付きMarkdown / CSV を生成",
        "6. GitHub Actionsで毎日自動実行",
        "7. READMEを自動更新",
        "",
        "## 生成ファイル",
        "",
        "- output/mcp_repositories_YYYY-MM-DD.md",
        "- output/mcp_repositories_YYYY-MM-DD.csv",
        "",
    ]

    return "\n".join(lines)


def ensure_readme_exists() -> None:
    if README_PATH.exists():
        return

    print("[WARN] README.md does not exist. Create default README.md.")
    README_PATH.write_text(build_default_readme(), encoding="utf-8")


def update_readme(markdown: str) -> None:
    ensure_readme_exists()

    readme = README_PATH.read_text(encoding="utf-8")

    pattern = re.compile(
        f"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}",
        re.DOTALL,
    )

    replacement = f"{START_MARKER}\n{markdown}\n{END_MARKER}"

    if pattern.search(readme):
        updated = pattern.sub(replacement, readme)
    else:
        updated = readme.rstrip() + "\n\n" + replacement + "\n"

    README_PATH.write_text(updated, encoding="utf-8")


def cleanup_old_outputs(retention_days: int) -> None:
    if retention_days <= 0:
        return

    if not OUTPUT_DIR.exists():
        return

    dated_files = sorted(OUTPUT_DIR.glob("mcp_repositories_20??-??-??.*"))

    date_to_files: dict[str, list[Path]] = {}

    for file in dated_files:
        match = re.search(r"mcp_repositories_(\d{4}-\d{2}-\d{2})\.", file.name)

        if not match:
            continue

        date_to_files.setdefault(match.group(1), []).append(file)

    keep_dates = set(sorted(date_to_files.keys(), reverse=True)[:retention_days])

    for date, files in date_to_files.items():
        if date in keep_dates:
            continue

        for file in files:
            print(f"[INFO] Remove old output: {file}")
            file.unlink(missing_ok=True)


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)

    current_date_text = now_utc.strftime("%Y-%m-%d")
    previous_date_text = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    previous_csv_path = OUTPUT_DIR / f"mcp_repositories_{previous_date_text}.csv"
    previous_metrics = load_previous_metrics(previous_csv_path)

    repositories = search_repositories()

    if not repositories:
        print("[ERROR] No repositories found.")
        return 1

    metric_deltas = calculate_metric_deltas(repositories, previous_metrics)
    markdown = build_markdown(
        repositories=repositories,
        now_jst=now_jst,
        metric_deltas=metric_deltas,
        current_date_text=current_date_text,
        previous_date_text=previous_date_text,
    )

    dated_md_path = OUTPUT_DIR / f"mcp_repositories_{current_date_text}.md"
    dated_csv_path = OUTPUT_DIR / f"mcp_repositories_{current_date_text}.csv"

    dated_md_path.write_text(markdown, encoding="utf-8")
    write_csv(repositories, metric_deltas, dated_csv_path)

    update_readme(markdown)

    retention_days = get_env_int("OUTPUT_RETENTION_DAYS", 30)
    cleanup_old_outputs(retention_days)

    print(
        "[INFO] Updated README and dated output files. "
        f"repositories={len(repositories)}, "
        f"current_date={current_date_text}, "
        f"previous_date={previous_date_text}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
