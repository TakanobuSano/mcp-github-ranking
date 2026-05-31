import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
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
    '"modelcontextprotocol" in:name,description,readme stars:>10',
    '"claude code" in:name,description,readme stars:>10',
    '"claude" "plugin" in:name,description,readme stars:>10',
    '"claude" "memory" in:name,description,readme stars:>10',
    '"claude" "agent" in:name,description,readme stars:>10',
    '"claude" "tool" in:name,description,readme stars:>10',
]

EXCLUDE_KEYWORDS = [
    "minecraft",
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


def truncate_text(value: str, max_chars: int = 120) -> str:
    text = md_escape(value)

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


def build_markdown(repositories: list[Repository], now: datetime) -> str:
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S JST")

    lines = [
        f"最終更新: **{generated_at}**",
        "",
        "MCP関連リポジトリに加え、Claude Code周辺で活用候補になりそうな関連ツールをGitHub Search APIで毎日自動収集してランキング化しています。",
        "",
        "> 注意: この一覧はClaude Codeでの動作を保証するものではありません。  ",
        "> MCP関連ツールまたはClaude Code関連ツール候補を探すための入口として利用してください。",
        "",
        "# 注目MCP・関連ツール候補ランキング",
        "",
    ]

    for index, repo in enumerate(repositories, start=1):
        description = truncate_text(repo.description, 120) or "説明なし"
        language = md_escape(repo.language) or "不明"
        updated_at = date_only(repo.updated_at)
        topics = build_topics(repo.topics)

        lines.extend(
            [
                f"## {index}位 [{md_escape(repo.full_name)}]({repo.html_url})",
                "",
                description,
                "",
                (
                    f"⭐ **{repo.stargazers_count:,} Stars**"
                    f"　🍴 **{repo.forks_count:,} Forks**"
                    f"　/　🟢 **{repo.open_issues_count:,} Open Issues**"
                    f"　/　{language}"
                ),
                "",
                f"Topics: {topics}",
                "",
                "---",
                "",
            ]
        )

    lines.extend(
        [
            "# 最近更新されたMCP・関連ツール候補",
            "",
            "スター数ランキングとは別に、最近更新されたリポジトリを表示します。古いスター数だけではなく、現在もメンテナンスされていそうな候補を探すための一覧です。",
            "",
        ]
    )

    recently_updated = sorted(
        repositories,
        key=lambda repo: repo.updated_at,
        reverse=True,
    )[:30]

    for index, repo in enumerate(recently_updated, start=1):
        description = truncate_text(repo.description, 120) or "説明なし"
        language = md_escape(repo.language) or "不明"
        updated_at = date_only(repo.updated_at)
        topics = build_topics(repo.topics)

        lines.extend(
            [
                f"## 更新順 {index}位 [{md_escape(repo.full_name)}]({repo.html_url})",
                "",
                description,
                "",
                (
                    f"⭐ **{repo.stargazers_count:,} Stars**"
                    f"　🍴 **{repo.forks_count:,} Forks**"
                    f"　/　{language}"
                    f"　/　最終更新: {updated_at}"
                ),
                "",
                f"Topics: {topics}",
                "",
                "---",
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


def write_csv(repositories: list[Repository], path: Path) -> None:
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
                "forks",
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
            writer.writerow(
                [
                    index,
                    repo.full_name,
                    repo.html_url,
                    repo.description,
                    repo.stargazers_count,
                    repo.forks_count,
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
        "# Claude Code向けMCP・関連ツール候補ランキング【毎日自動更新】",
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
        "3. スター数・更新日・Fork数・説明文を取得",
        "4. Markdown / CSV を生成",
        "5. GitHub Actionsで毎日自動実行",
        "6. READMEを自動更新",
        "",
        "## 生成ファイル",
        "",
        "- output/mcp_repositories_latest.md",
        "- output/mcp_repositories_latest.csv",
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
    now = datetime.now(JST)
    date_text = now.strftime("%Y-%m-%d")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    repositories = search_repositories()

    if not repositories:
        print("[ERROR] No repositories found.")
        return 1

    markdown = build_markdown(repositories, now)

    latest_md_path = OUTPUT_DIR / "mcp_repositories_latest.md"
    latest_csv_path = OUTPUT_DIR / "mcp_repositories_latest.csv"
    dated_md_path = OUTPUT_DIR / f"mcp_repositories_{date_text}.md"
    dated_csv_path = OUTPUT_DIR / f"mcp_repositories_{date_text}.csv"

    latest_md_path.write_text(markdown, encoding="utf-8")
    dated_md_path.write_text(markdown, encoding="utf-8")

    write_csv(repositories, latest_csv_path)
    write_csv(repositories, dated_csv_path)

    update_readme(markdown)

    retention_days = get_env_int("OUTPUT_RETENTION_DAYS", 5)
    cleanup_old_outputs(retention_days)

    print(f"[INFO] Updated README and output files. repositories={len(repositories)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
