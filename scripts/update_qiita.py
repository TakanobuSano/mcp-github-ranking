import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from zoneinfo import ZoneInfo


QIITA_API_BASE_URL = "https://qiita.com/api/v2"
JST = ZoneInfo("Asia/Tokyo")

OUTPUT_DIR = Path("output")
CLAUDE_EXPLANATION_DIR = OUTPUT_DIR / "claude_explanations"

DEFAULT_TITLE = "【毎日更新】Claude Code向けスキル・MCP・関連ツール人気ランキングTOP100をGitHubスターで自動集計"
GITHUB_REPOSITORY_URL = "https://github.com/TakanobuSano/mcp-github-ranking"

DEFAULT_TAGS = [
    {"name": "ClaudeCode", "versions": []},
    {"name": "MCP", "versions": []},
    {"name": "GitHubActions", "versions": []},
    {"name": "Python", "versions": []},
    {"name": "GitHub", "versions": []},
]


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


def extract_date_from_report_path(path: Path) -> str:
    match = re.fullmatch(r"mcp_repositories_(\d{4}-\d{2}-\d{2})\.md", path.name)

    if not match:
        return ""

    return match.group(1)


def find_latest_report_path() -> Path:
    candidates: list[tuple[str, Path]] = []

    for path in OUTPUT_DIR.glob("mcp_repositories_20??-??-??.md"):
        date_text = extract_date_from_report_path(path)

        if not date_text:
            continue

        candidates.append((date_text, path))

    if not candidates:
        raise FileNotFoundError(
            "No dated report file found. Expected output/mcp_repositories_YYYY-MM-DD.md."
        )

    candidates.sort(key=lambda item: item[0], reverse=True)
    latest_date, latest_path = candidates[0]

    print(f"[INFO] Latest dated report: {latest_path} ({latest_date})")
    return latest_path


def trim_report_intro(report_markdown: str) -> str:
    heading_candidates = [
        "# 注目MCP・関連ツール候補ランキング",
        "# 注目MCPリポジトリランキング",
        "# 注目MCPリポジトリ一覧",
    ]

    for heading in heading_candidates:
        index = report_markdown.find(heading)

        if index != -1:
            return report_markdown[index:].strip()

    return report_markdown.strip()


def normalize_ranking_heading(report_markdown: str) -> str:
    # Qiitaの記事タイトルと重複するため、ランキング本文内のH1見出しは表示しません。
    return report_markdown


def remove_leading_ranking_h1(report_markdown: str) -> str:
    lines = report_markdown.splitlines()

    while lines and not lines[0].strip():
        lines.pop(0)

    if lines and lines[0].startswith("# "):
        lines.pop(0)

        while lines and not lines[0].strip():
            lines.pop(0)

    return "\n".join(lines).strip()


def md_escape(value: str) -> str:
    """Escape square brackets used by Markdown link syntax."""
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def extract_heading_summary(explanation: str, max_length: int = 100) -> str:
    """Return the first Japanese sentence for use in ranking headings."""
    text = re.sub(r"\s+", " ", explanation).strip()

    if not text:
        return ""

    end_index = text.find("。")
    if end_index != -1:
        text = text[:end_index]

    text = text.strip(" 。")

    if len(text) <= max_length:
        return text

    return text[: max_length - 1].rstrip() + "…"


def safe_repo_file_stem(full_name: str) -> str:
    return full_name.replace("/", "__").replace(":", "_")


def normalize_explanation_text(text: str, max_length: int = 260) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()

    if len(normalized) <= max_length:
        return normalized

    return normalized[: max_length - 1].rstrip() + "…"


def read_cached_explanation(full_name: str) -> str:
    path = CLAUDE_EXPLANATION_DIR / f"{safe_repo_file_stem(full_name)}.md"

    if not path.exists():
        return ""

    return normalize_explanation_text(
        path.read_text(encoding="utf-8", errors="replace")
    )


def inject_cached_explanations(report_markdown: str) -> str:
    """Insert cached Japanese summaries into ranking sections when available.

    The weekly trend article creates:
      output/claude_explanations/owner__repo.md

    This popular ranking article reuses those files only when they already exist.
    It does not call the Claude API.

    If an explanation exists, the H2 heading is also rewritten to:
      ## N位 First Japanese sentence - [owner/repo](url)
    """
    heading_pattern = re.compile(
        r"##\s+(\d+位)\s+\[([^\]]+)\]\(([^)]+)\)"
    )
    matches = list(heading_pattern.finditer(report_markdown))

    if not matches:
        return report_markdown

    result_parts: list[str] = []
    cursor = 0
    inserted_count = 0
    heading_rewrite_count = 0

    for index, match in enumerate(matches):
        section_start = match.start()
        section_end = matches[index + 1].start() if index + 1 < len(matches) else len(report_markdown)

        result_parts.append(report_markdown[cursor:section_start])

        section = report_markdown[section_start:section_end]
        rank_text = match.group(1).strip()
        full_name = match.group(2).strip()
        url = match.group(3).strip()
        explanation = read_cached_explanation(full_name)

        if explanation:
            heading_summary = extract_heading_summary(explanation)
            if heading_summary:
                line_end = section.find("\n")
                if line_end == -1:
                    line_end = len(section)

                rewritten_heading = (
                    f"## {rank_text} [{md_escape(full_name)}]({url})："
                    f"{md_escape(heading_summary)}"
                )
                section = rewritten_heading + section[line_end:]
                heading_rewrite_count += 1

        if explanation and explanation not in section:
            summary_block = f"\n\n> {md_escape(explanation)}\n\n"
            star_index = section.find("⭐")

            if star_index != -1:
                section = section[:star_index].rstrip() + summary_block + section[star_index:].lstrip()
            else:
                section = section.rstrip() + summary_block

            inserted_count += 1

        result_parts.append(section)
        cursor = section_end

    result_parts.append(report_markdown[cursor:])

    print(f"[INFO] Cached explanations inserted into popular ranking: {inserted_count}")
    print(f"[INFO] Popular ranking headings rewritten from cached explanations: {heading_rewrite_count}")

    return "".join(result_parts)

def build_search_policy_explanation() -> str:
    lines = [
        "# 検索条件の考え方",
        "",
        "このランキングでは、GitHub Search APIを使って、リポジトリ名・説明文・READMEに含まれるキーワードをもとに候補を収集しています。",
        "",
        "主に以下の2系統を対象にしています。",
        "",
        "- MCP関連リポジトリ",
        "- Claude Code周辺で使われる可能性がある関連ツール",
        "",
        "具体的には、`model context protocol`、`mcp server`、`claude code`、`claude plugin`、`claude memory` などのキーワードを使って検索しています。",
        "",
        "一方で、`awesome`、`roadmap`、`interview`、`leetcode` など、ランキングの趣旨から外れやすいリポジトリは除外しています。",
        "",
        "なお、GitHub Search APIではREADME本文も検索対象にしているため、リポジトリの説明文やTopicsに `Claude Code` や `MCP` が含まれていなくても、README内に関連情報がある場合は候補に入ることがあります。",
    ]

    return "\n".join(lines)


def insert_search_policy_explanation(report_markdown: str) -> str:
    search_heading = "# 検索条件"

    if search_heading not in report_markdown:
        return report_markdown

    explanation = build_search_policy_explanation()

    return report_markdown.replace(
        search_heading,
        f"{explanation}\n\n{search_heading}",
        1,
    )


def build_qiita_body(report_markdown: str, report_date: str) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    ranking_markdown = trim_report_intro(report_markdown)
    ranking_markdown = normalize_ranking_heading(ranking_markdown)
    ranking_markdown = remove_leading_ranking_h1(ranking_markdown)
    ranking_markdown = inject_cached_explanations(ranking_markdown)
    ranking_markdown = insert_search_policy_explanation(ranking_markdown)

    lines = [
        ":::note info",
        f"最終更新: **{now}**",
        f"使用データ: **{report_date} UTC**",
        "",
        "Claude Code向けスキル・MCP・関連ツール候補を、GitHubスター数をもとに毎日自動収集して人気ランキング化しています。",
        "[週間トレンド記事](https://qiita.com/4q_sano/items/cc27d3564a657046242a)で生成済みの日本語要約がある場合は、この人気ランキング記事にも再利用して表示します。",
        ":::",
        "",
        ranking_markdown,
        "",
        "# このランキングで確認できること",
        "",
        "- GitHubスター数で見たClaude Code向けMCP・関連ツールの定番候補",
        "- MCP関連リポジトリの人気ランキング",
        "- Claude Code周辺で活用候補になりそうな関連ツール",
        "- 週間トレンド記事で生成済みの日本語要約",
        "- Stars / Forks のUTC基準の前日比",
        "- Fork数、Open Issues、使用言語、Topics",
        "- GitHub Search APIで使用している検索条件",
        "",
        "# 仕組み",
        "",
        "このランキングの生成コードとGitHub Actionsの設定は、以下のリポジトリで管理しています。",
        "",
        GITHUB_REPOSITORY_URL,
        "",
        "1. GitHub Search APIでMCP関連リポジトリを検索",
        "2. Claude Code関連ツール候補を検索",
        "3. スター数・Fork数・Open Issues・説明文・Topicsを取得",
        "4. UTC基準の前日CSVと比較してStars / Forksの前日比を計算",
        "5. 日付付きMarkdown / CSV を生成",
        "6. 週間トレンド記事で生成済みの日本語要約キャッシュがあれば再利用",
        "7. GitHub Actionsで毎日自動実行",
        "8. 最新の日付付きMarkdownをもとにQiita記事を自動更新",
        "",
        "# 注意点",
        "",
        "このランキングは、GitHub Search APIの検索結果をもとにした自動集計です。",
        "そのため、以下のようなリポジトリが混ざる可能性があります。",
        "",
        "- Claude Desktop向けのMCPサーバー",
        "- Cursorなど他エディタ向けのMCPツール",
        "- MCP関連のサンプルコード",
        "- MCPに関するドキュメント用リポジトリ",
        "- Claude APIやClaude Desktop向けのサンプル",
        "- READMEにMCPやClaudeと書かれているだけのリポジトリ",
        "",
        ":::note warn",
        "このランキングは「導入推奨リスト」ではなく、あくまで「探索リスト」として利用する想定です。",
        "実際に導入する場合は、README、最終プッシュ日、Issues、Pull Requests、ライセンス、Claude Codeでの利用方法を確認してください。",
        ":::",
        "",
        "# 補足",
        "",
        "GitHubスター数は人気度の参考になりますが、実務で使う場合はスター数だけで判断しないほうが安全です。",
        "",
        "特にClaude Codeで利用する場合は、以下を確認することをおすすめします。",
        "",
        "- Claude Codeで利用できるMCPサーバーまたは関連ツールか",
        "- Claude Desktop向け設定だけでなく、Claude Code向けの設定例があるか",
        "- 最終プッシュ日が古すぎないか",
        "- IssuesやPull Requestsが放置されていないか",
        "- 商用利用や社内利用に問題ないライセンスか",
        "- セキュリティ上、社内コードや機密情報を扱って問題ない設計か",
        "",
    ]

    return "\n".join(lines)


def update_qiita_item() -> None:
    token = require_env("QIITA_TOKEN")
    item_id = require_env("QIITA_ITEM_ID")

    title = os.getenv("QIITA_TITLE", DEFAULT_TITLE)
    private = get_env_bool("QIITA_PRIVATE", True)
    organization_url_name = os.getenv("QIITA_ORGANIZATION_URL_NAME", "").strip()

    report_path = find_latest_report_path()
    report_date = extract_date_from_report_path(report_path)
    report_markdown = report_path.read_text(encoding="utf-8")
    body = build_qiita_body(report_markdown, report_date)

    payload = {
        "title": title,
        "body": body,
        "tags": DEFAULT_TAGS,
        "private": private,
        "coediting": False,
        "slide": False,
    }

    if organization_url_name:
        payload["organization_url_name"] = organization_url_name

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

    print("[INFO] Qiita item updated.")
    print(f"[INFO] private: {private}")
    print(f"[INFO] organization_url_name: {organization_url_name or '(not set)'}")
    print(f"[INFO] report_path: {report_path}")

    if url:
        print(f"[INFO] URL: {url}")


def main() -> int:
    update_qiita_item()
    return 0


if __name__ == "__main__":
    sys.exit(main())
