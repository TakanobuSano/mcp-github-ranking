import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from zoneinfo import ZoneInfo


QIITA_API_BASE_URL = "https://qiita.com/api/v2"
JST = ZoneInfo("Asia/Tokyo")

REPORT_PATH = Path("output/mcp_repositories_latest.md")

DEFAULT_TITLE = "Claude Code向けMCP・関連ツール候補ランキング【毎日自動更新】"

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


def trim_report_intro(report_markdown: str) -> str:
    """
    output/mcp_repositories_latest.md の冒頭説明を削り、
    Qiita記事ではランキング本文から表示する。

    update_mcp_repos.py 側の見出し変更にも対応できるよう、
    複数の候補見出しを順番に探す。
    """
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


def build_qiita_body(report_markdown: str) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    ranking_markdown = trim_report_intro(report_markdown)

    lines = [
        "# 概要",
        "",
        "MCP関連リポジトリに加え、Claude Code周辺で活用候補になりそうな関連ツールを、GitHub Search APIで毎日自動収集してランキング化しています。",
        "",
        ":::note info",
        "この記事はGitHub Actionsにより毎日自動更新されます。",
        f"最終更新: **{now}**",
        ":::",
        "",
        ranking_markdown,
        "",
        "---",
        "",
        "# このランキングで確認できること",
        "",
        "- MCP関連リポジトリのスター数ランキング",
        "- Claude Code周辺で活用候補になりそうな関連ツール",
        "- 最近更新されたMCP・関連ツール候補",
        "- Fork数、Open Issues、使用言語、Topics",
        "- GitHub Search APIで使用している検索条件",
        "- allowlistで手動追加した重要候補",
        "",
        "# 仕組み",
        "",
        "```text",
        "GitHub Search API",
        "  ↓",
        "MCP / Claude Code / Model Context Protocol 関連リポジトリを検索",
        "  ↓",
        "Claude Code関連ツール候補を検索",
        "  ↓",
        "allowlistの重要候補を直接取得",
        "  ↓",
        "スター数・更新日・Fork数・説明文を取得",
        "  ↓",
        "Markdown / CSV を生成",
        "  ↓",
        "GitHub Actionsで毎日自動実行",
        "  ↓",
        "Qiita記事を自動更新",
        "```",
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
        "実際に導入する場合は、README、最終更新日、Issues、Pull Requests、ライセンス、Claude Codeでの利用方法を確認してください。",
        ":::",
        "",
        "# 補足",
        "",
        "GitHubのスター数は人気度の参考になりますが、実務で使う場合はスター数だけで判断しないほうが安全です。",
        "",
        "特にClaude Codeで利用する場合は、以下を確認することをおすすめします。",
        "",
        "- Claude Codeで利用できるMCPサーバーまたは関連ツールか",
        "- Claude Desktop向け設定だけでなく、Claude Code向けの設定例があるか",
        "- 最終更新日が古すぎないか",
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

    # 最初は true 推奨。
    # 公開する場合は GitHub Actions 側で QIITA_PRIVATE: "false" にする。
    private = get_env_bool("QIITA_PRIVATE", True)

    if not REPORT_PATH.exists():
        raise FileNotFoundError(
            f"{REPORT_PATH} does not exist. Run scripts/update_mcp_repos.py first."
        )

    report_markdown = REPORT_PATH.read_text(encoding="utf-8")
    body = build_qiita_body(report_markdown)

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

    print("[INFO] Qiita item updated.")
    print(f"[INFO] private: {private}")

    if url:
        print(f"[INFO] URL: {url}")


def main() -> int:
    update_qiita_item()
    return 0


if __name__ == "__main__":
    sys.exit(main())
