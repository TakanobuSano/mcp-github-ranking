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
    '"mcp" "claude code" in:name,description,readme',
    '"modelcontextprotocol" in:name,description,readme stars:>10',
]

MCP_KEYWORDS = [
    "mcp",
    "model context protocol",
    "modelcontextprotocol",
]

EXCLUDE_KEYWORDS = [
    "minecraft",  # MCPという略語のノイズ対策
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


def request_with_retry(
    session: requests.Session,
    params: dict[str, Any],
    headers: dict[str, str],
    max_retries: int = 3,
) -> dict[str, Any]:
    for attempt in range(1, max_retries + 1):
        response = session.get(
            GITHUB_SEARCH_API_URL,
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


def is_candidate_repository(repo: Repository) -> bool:
    if repo.archived:
        return False

    text = " ".join(
        [
            repo.full_name,
            repo.description,
            repo.language,
            " ".join(repo.topics),
        ]
    ).lower()

    if any(excluded in text for excluded in EXCLUDE_KEYWORDS):
        return False

    return any(keyword in text for keyword in MCP_KEYWORDS)


def search_repositories() -> list[Repository]:
    queries = get_search_queries()
    max_pages = get_env_int("MAX_PAGES_PER_QUERY", 2)
    per_page = min(get_env_int("PER_PAGE", 100), 100)
    max_results = get_env_int("MAX_RESULTS", 30)

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

                data = request_with_retry(session, params, headers)
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


def date_only(value: str) -> str:
    if not value:
        return ""

    return value.split("T")[0]


def build_topics(topics: list[str]) -> str:
    if not topics:
        return "`topicなし`"

    return " ".join([f"`{md_escape(topic)}`" for topic in topics[:8]])


def build_markdown(repositories: list[Repository], now: datetime) -> str:
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S JST")

    lines = [
        f"最終更新: **{generated_at}**",
        "",
        "GitHub Search APIでMCP関連リポジトリを検索し、Claude Code周辺で活用候補になりそうなリポジトリをランキング形式で表示しています。",
        "",
        "> 注意: この一覧はClaude Codeでの動作を保証するものではありません。  ",
        "> GitHub上のリポジトリ名・説明文・Topicsなどをもとに、MCP関連ツール候補を探すための入口として利用してください。",
        "",
        "# 注目MCPリポジトリランキング",
        "",
    ]

    for index, repo in enumerate(repositories, start=1):
        description = md_escape(repo.description) or "説明なし"
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
                    f"◇ **{repo.stargazers_count:,} Stars**"
                    f"　♡ **{repo.forks_count:,} Forks**"
                    f"　/　**{repo.open_issues_count:,} Issues**"
                    f"　/　{language}"
                    f"　/　最終更新: {updated_at}"
                ),
                "",
                topics,
                "",
                "---",
                "",
            ]
        )

    lines.extend(
        [
            "# 最近更新されたMCP関連リポジトリ",
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
        description = md_escape(repo.description) or "説明なし"
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
                    f"◇ **{repo.stargazers_count:,} Stars**"
                    f"　♡ **{repo.forks_count:,} Forks**"
                    f"　/　{language}"
                    f"　/　最終更新: {updated_at}"
                ),
                "",
                topics,
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
            "```text",
            *get_search_queries(),
            "```",
            "",
        ]
    )

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
    return f"""# Claude Code向けMCPツール候補ランキング

GitHub Search APIを使って、Claude Code周辺で活用候補になりそうなMCP関連リポジトリを定期収集するリポジトリです。

> 注意: この一覧は「Claude Codeでの動作」を保証するものではありません。  
> GitHub上のリポジトリ名・説明文・Topicsなどに含まれる情報をもとに、MCP関連ツール候補を探すための入口として利用します。

{START_MARKER}
まだランキングは生成されていません。
{END_MARKER}

## 仕組み

```text
GitHub Search API
  ↓
MCP / Claude Code / Model Context Protocol 関連リポジトリを検索
  ↓
スター数・更新日・Fork数・説明文を取得
  ↓
Markdown / CSV を生成
  ↓
GitHub Actionsで毎日自動実行
  ↓
READMEを自動更新
