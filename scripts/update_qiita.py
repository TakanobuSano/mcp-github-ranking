import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from zoneinfo import ZoneInfo


QIITA_API_BASE_URL = "https://qiita.com/api/v2"
JST = ZoneInfo("Asia/Tokyo")

REPORT_PATH = Path("output/mcp_repositories_latest.md")

DEFAULT_TITLE = "Claude Code向けMCPツール候補ランキング【毎日自動更新】"

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


def build_qiita_body(report_markdown: str) -> str:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

    lines = [
        "# 概要",
        "",
        "Claude Code周辺で使えそうなMCP関連リポジトリを、GitHub Search APIで定期収集して一覧化しています。",
        "",
        "MCP関連ツールは増えるスピードが速く、手動で探し続けるのが大変です。",
        "そこで、GitHub Actionsで毎日自動実行し、スター数・Fork数・Open Issues・最終更新日をもとにランキングを更新する仕組みにしました。",
        "",
        "> 注意: この一覧はClaude Codeでの動作を保証するものではありません。  ",
        "> GitHub上のリポジトリ名・説明文・Topicsなどをもとに、MCP関連ツール候補を探すための入口として利用します。",
        "",
        f"最終更新: **{now}**",
        "",
        "---",
        "",
        report_markdown,
        "",
        "---",
        "",
        "# このランキングで確認できること",
        "",
        "- MCP関連リポジトリのスター数ランキング",
        "- 最近更新されたMCP関連リポジトリ",
        "- Fork数、Open Issues、使用言語、Topics",
        "- GitHub Search APIで使用している検索条件",
        "",
        "# 仕組み",
        "",
        "```text",
        "GitHub Search API",
        "  ↓",
        "MCP / Claude Code / Model Context Protocol 関連リポジトリを検索",
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
        "この仕組みは、GitHub Search APIの検索結果をもとにした自動集計です。",
        "そのため、以下のようなリポジトリが混ざる可能性があります。",
        "",
        "- Claude Desktop向けのMCPサーバー",
        "- Cursorなど他エディタ向けのMCPツール",
        "- MCP関連のサンプルコード",
        "- MCPに関するドキュメント用リポジトリ",
        "- READMEにMCPと書かれているだけのリポジトリ",
        "",
        "そのため、このランキングは「導入推奨リスト」ではなく、あくまで「探索リスト」として使う想定です。",
        "",
        "# 補足",
        "",
        "GitHubのスター数は人気度の参考になりますが、実務で使う場合は以下も確認することをおすすめします。",
        "",
        "- READMEの内容",
        "- 最終更新日",
        "- Issues / Pull Requests の状況",
        "- ライセンス",
        "- Claude Codeで利用する場合の設定方法",
        "",
    ]

    return "\n".join(lines)


def update_qiita_item() -> None:
    token = require_env("QIITA_TOKEN")
    item_id = require_env("QIITA_ITEM_ID")

    title = os.getenv("QIITA_TITLE", DEFAULT_TITLE)

    # 最初は true 推奨です。
    # 公開する場合は GitHub Actions 側で QIITA_PRIVATE: "false" にしてください。
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
    if url:
        print(f"[INFO] URL: {url}")


def main() -> int:
    update_qiita_item()
    return 0


if __name__ == "__main__":
    sys.exit(main())
