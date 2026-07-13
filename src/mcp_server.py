"""
vd-accessibility MCP server — 全盲ユーザーが Slack で一番困る「画像」と「リンク」を
言葉にするためのツールを公開する MCP サーバー。

トランスポート = stdio（改行区切り JSON-RPC 2.0）。標準ライブラリのみ。
公開ツール:
  - describe_image(image_url, context?)  画像を OpenAI vision(gpt-4o)で全盲向けに説明
  - read_link(url)                       リンク先を取得して音声向けに要約

環境変数:
  OPENAI_API_KEY   必須（無ければ sk- で始まる GEMINI_API_KEY を流用）
  OPENAI_MODEL     省略時 gpt-4o（ビジョン対応）
  SLACK_BOT_TOKEN  任意（Slack の url_private 画像を取りに行くとき Authorization に使う）

単体テスト:
  set -a; source ../.env; set +a
  echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_link","arguments":{"url":"https://example.com"}}}' \
    | ./.venv/bin/python mcp_server.py
"""
import os
import re
import sys
import json
import time
import base64
import html
import urllib.error
import urllib.request

# LLM = OpenAI（gpt-4o はビジョン＋テキスト対応）。キーは OPENAI_API_KEY、
# 無ければ sk- で始まる GEMINI_API_KEY を流用。
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
ENDPOINT = "https://api.openai.com/v1/chat/completions"
PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "describe_image",
        "description": (
            "Slack に貼られた画像を、画面を見られない全盲・弱視のユーザー向けに"
            "日本語で説明する。図・写真・スクショ・グラフに対応。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": "画像の URL。Slack の url_private でも公開 URL でも可。",
                },
                "context": {
                    "type": "string",
                    "description": "その画像が投稿された文脈（任意）。説明を的確にする。",
                },
            },
            "required": ["image_url"],
        },
    },
    {
        "name": "read_link",
        "description": (
            "リンク先の Web ページを取得し、画面を見られないユーザーが耳で"
            "把握できるように日本語で短く要約する。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "読み上げたいページの URL。"},
            },
            "required": ["url"],
        },
    },
]


# ---- OpenAI 呼び出し（テキスト / 画像）----
def _openai(user_content, system, max_tokens=700):
    """user_content は str（テキスト）か、OpenAI の content パート配列（画像込み）。"""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user_content}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {OPENAI_KEY}"}
    for attempt in range(4):
        try:
            req = urllib.request.Request(ENDPOINT, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            return (data["choices"][0]["message"].get("content") or "").strip()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise


_MAX_FETCH_BYTES = 800_000  # 巨大ページ（例: W3C の一枚もの）で固まらないよう上限を設ける


def _fetch_bytes(url):
    headers = {"User-Agent": "vd-mcp/1.0"}
    # Slack の非公開ファイルは Bearer 認証が要る
    if SLACK_TOKEN and ("slack.com" in url or "slack-files.com" in url):
        headers["Authorization"] = f"Bearer {SLACK_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip()
        return r.read(_MAX_FETCH_BYTES), ctype   # 上限まで読む（部分でも要約は成立）


_TAG = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_ANY_TAG = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n\s*")


def _strip_html(raw):
    txt = _TAG.sub(" ", raw)
    txt = _ANY_TAG.sub(" ", txt)
    txt = html.unescape(txt)
    txt = _WS.sub(" ", txt)
    txt = _NL.sub("\n", txt)
    return txt.strip()


# ---- ツール本体 ----
def tool_describe_image(args):
    url = args.get("image_url", "").strip()
    if not url:
        return "画像の URL が指定されていません。"
    raw, ctype = _fetch_bytes(url)
    if not ctype.startswith("image/"):
        ctype = "image/jpeg"  # Slack が octet-stream を返すことがある
    b64 = base64.b64encode(raw).decode()
    ctx = args.get("context", "")
    prompt = "この画像を全盲の人向けに説明してください。"
    if ctx:
        prompt += f"\n文脈: {ctx}"
    system = (
        "You describe images for blind / low-vision users so they can understand by ear. "
        "Say what the image is in one line, then the important elements concretely. "
        "Read out any text shown. Avoid decorative phrasing; be concise. Mark guesses as guesses. "
        "Reply in the same language as the provided context (Japanese or English)."
    )
    user_content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{ctype};base64,{b64}"}},
    ]
    return _openai(user_content, system, max_tokens=600)


def tool_read_link(args):
    url = args.get("url", "").strip()
    if not url:
        return "URL が指定されていません。"
    raw, ctype = _fetch_bytes(url)
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = raw
    body = _strip_html(text)[:6000]
    system = (
        "You summarize a web page for blind / low-vision users to grasp by ear. "
        "Say what the page is in one line, then the key points in a listenable length (3-5 lines). "
        "Ignore navigation/ads/non-body content. Avoid symbol soup. "
        "Reply in the same language as the page/user."
    )
    prompt = f"Summarize the body of this page. URL: {url}\n----\n{body}\n----"
    return _openai(prompt, system, max_tokens=500)


DISPATCH = {"describe_image": tool_describe_image, "read_link": tool_read_link}


# ---- JSON-RPC over stdio ----
def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message):
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def handle(msg):
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        _result(req_id, {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "vd-accessibility", "version": "1.0.0"},
        })
    elif method == "notifications/initialized":
        pass  # 通知（応答不要）
    elif method == "tools/list":
        _result(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = DISPATCH.get(name)
        if not fn:
            _error(req_id, -32602, f"unknown tool: {name}")
            return
        try:
            text = fn(args)
            _result(req_id, {"content": [{"type": "text", "text": text}]})
        except Exception as e:
            # ツール内エラーは isError で返す（プロトコルエラーにしない）
            _result(req_id, {
                "content": [{"type": "text", "text": f"ツール実行に失敗: {type(e).__name__}: {e}"}],
                "isError": True,
            })
    elif method == "ping":
        _result(req_id, {})
    elif req_id is not None:
        _error(req_id, -32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle(msg)


if __name__ == "__main__":
    main()
