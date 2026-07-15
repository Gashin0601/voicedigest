"""
0710GashinBot / VoiceDigest — 全盲でも使える Slack エージェント（コア）
Bolt (Socket Mode) + OpenAI (gpt-4o · tts-1) + 自作 MCP サーバー(vd-accessibility)。
（注: 関数名の gemini_* は歴史的な名残で、実体は OpenAI を呼ぶ。）

思想: 画面を読み上げる代わりに、チャンネル/スレッドの流れを取得して
「誰が・何を・何に反応が要るか」を音声で聞きやすくまとめて返す。
画面を見られない人が一番困る「画像」と「リンク」は MCP ツールで言葉にする。

構成:
  - Slack Web API 読取（conversations_history / _replies / users_info）で文脈取得
  - MCP サーバー(mcp_server.py)を子プロセスで起動し、describe_image / read_link を利用
  - 返信は Block Kit（見出し＋要約＋出典＋操作ボタン）。スクリーンリーダー配慮

使い方:
  チャンネルで  @0710GashinBot まとめて / 私に何か頼んでる？ / 今北産業
  スレッド内で  @0710GashinBot このスレ何の話？
  DM で         最近どう？ / 何ができる？
  ボタン        「やることリスト」＝あなたの ToDo を抽出 / 👍👎 ＝フィードバック

実行（ネットワークサンドボックス無効で起動すること。DNS 制限下だと OpenAI に届かない）:
  cd src && set -a; source ../.env; set +a; ./.venv/bin/python bot.py
"""
import os
import re
import io
import sys
import json
import wave
import time
import base64
import threading
import urllib.error
import urllib.request

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from mcp_client import MCPClient

# ---- LLM = OpenAI（有料キー・スロットリング無し）----
# OPENAI_API_KEY を使う。無ければ（キーを GEMINI_API_KEY 行に貼った場合の保険で）
# sk- で始まる GEMINI_API_KEY を流用する。
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")   # 推論＋function calling
CHAT_ENDPOINT = "https://api.openai.com/v1/chat/completions"

# ---- 機能フラグ ----
# Data Table ブロックは新しめ。既定は OFF（GA ブロックで確実に描画）。
# ワークスペースが対応していれば VD_ENABLE_TABLE=1 で有効化、失敗時は自動フォールバック。
ENABLE_TABLE = os.environ.get("VD_ENABLE_TABLE", "") not in ("", "0", "false")
# 音声（OpenAI TTS→Slack に再生可能な音声を投稿）。既定 ON。
ENABLE_VOICE = os.environ.get("VD_ENABLE_VOICE", "1") not in ("0", "false", "")
OPENAI_TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "tts-1")
OPENAI_TTS_VOICE = os.environ.get("OPENAI_TTS_VOICE", "alloy")
TTS_ENDPOINT = "https://api.openai.com/v1/audio/speech"

SYSTEM = (
    "You are vd (VoiceDigest), an assistant that helps blind and low-vision users use Slack by ear. "
    "You turn a channel or thread into a short, clear, spoken-style summary.\n"
    "Rules:\n"
    "- Lead with the conclusion / what matters most. No preamble or meta commentary.\n"
    "- Never read out symbols, raw URLs, or user IDs. Refer to a link as 'a link about X'.\n"
    "- Name who said what.\n"
    "- If anything needs the user (a request, question, or deadline addressed to them), say it FIRST.\n"
    "- If image content is given as [image: ...], include it in the summary.\n"
    "- Keep bullets short enough to listen to; at most 6.\n"
    "- Do NOT use Markdown (**bold**, # headings, * bullet symbols). Write plain text meant to be read aloud.\n"
    "- If the log is empty or only small talk, say so honestly. Never make things up.\n"
    "- IMPORTANT: reply in the SAME language the user wrote their request in (English or Japanese)."
)


# ---- 引用ルール（過去ログに出典番号が付いているときだけ system に追加）----
CITE_RULE = (
    "CITATIONS ARE REQUIRED IN THIS ANSWER.\n"
    "The conversation log is numbered: lines look like '[3] Alice: ...'. Every sentence in your "
    "answer that restates or summarizes information from a numbered line MUST end with that line's "
    "number in square brackets.\n"
    "Format:\n"
    "- Put the [n] at the very end of the sentence (right after the period is fine): "
    "'出荷は金曜に確定しました。[3]'\n"
    "- If one sentence draws on two lines, cite both: '会議は水曜10時です。[1][2]'\n"
    "- Use ONLY numbers that actually appear in the log. NEVER invent a number.\n"
    "- A factual sentence with no citation is an ERROR — do not skip it. Cite even after using tools.\n"
    "- When you describe an IMAGE or summarize a LINKED PAGE, cite the numbered line whose message "
    "contained that image or link. The description still belongs to that message, so it needs its "
    "[n] (e.g. if line [1] shared the photo, every sentence describing the photo ends with [1]).\n"
    "- The ONLY sentences that get NO citation are ones speaking directly to the user about their "
    "OWN task, question, or deadline."
)


# ---- 言語判定 + UI 文言（英日対応）----
def detect_lang(t):
    """ユーザーの文にかな/カナ/漢字が含まれれば日本語、なければ英語。"""
    return "ja" if re.search(r"[぀-ヿ一-鿿]", t or "") else "en"


STR = {
    "en": {
        "placeholder": "🎧 Working on it… (reading any images and links too)",
        "digest_header": "🎧 vd summary",
        "todo_btn": "To-do list", "up_btn": "👍 Helpful", "down_btn": "👎 Not great",
        "todo_header": "✅ Your to-dos",
        "todo_empty": "Nothing seems to need action from you right now.",
        "todo_hint": "Check them off when done.",
        "table_cols": ["To-do", "Status"], "status_pending": "Open",
        "src_rts": "🔎 From fresh workspace-wide search",
        "src_thread": "From this thread’s recent messages",
        "src_channel": "From this channel’s recent messages",
        "worker_err": "Sorry, I couldn’t summarize that. Please try again.",
        "answer_fail": "I couldn’t put an answer together. Could you say that again?",
        "fb_up": "Thanks — glad it helped.",
        "fb_down": "Thanks for the feedback — I’ll make it clearer.",
        "audio_title": "🎧 Spoken summary",
        "cite_hint": "🔢 Tap a number to get the link to that message.",
        "cite_link": "🔗 Source [{n}]:\n{url}",
        "cite_gone": "Sorry, I couldn’t find the link for that one.",
        "voice_cite_word": ", citation {n}",
        "voice_cite_tail": " Finally, press a numbered button below to open the original message for each citation.",
    },
    "ja": {
        "placeholder": "🎧 いま、まとめています…（画像やリンクも読みます）",
        "digest_header": "🎧 vd の要約",
        "todo_btn": "やることリスト", "up_btn": "👍 役に立った", "down_btn": "👎 いまいち",
        "todo_header": "✅ あなたのやること",
        "todo_empty": "いまのところ、あなたがやるべきことは見当たりませんでした。",
        "todo_hint": "終わったらチェックを入れてください。",
        "table_cols": ["やること", "状態"], "status_pending": "未対応",
        "src_rts": "🔎 全社検索の最新コンテキストから",
        "src_thread": "このスレッドの直近ログから",
        "src_channel": "このチャンネルの直近ログから",
        "worker_err": "すみません、うまくまとめられませんでした。もう一度試してください。",
        "answer_fail": "うまく答えを作れませんでした。もう一度言ってもらえますか。",
        "fb_up": "ありがとうございます。参考になったようで良かったです。",
        "fb_down": "教えてくれてありがとうございます。もっと分かりやすく直します。",
        "audio_title": "🎧 VoiceDigestの音声",
        "cite_hint": "🔢 番号を押すと、その発言の元メッセージのリンクが出ます。",
        "cite_link": "🔗 出典[{n}] の元メッセージ:\n{url}",
        "cite_gone": "すみません、その出典のリンクが見つかりませんでした。",
        "voice_cite_word": "、引用{n}",
        "voice_cite_tail": " 最後に、下の番号のボタンを押すと、その発言の元メッセージのリンクが表示されます。",
    },
}

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# ---- 自分(bot)の ID を把握 ----
# Slack の @mention は解決先が2通りある:
#   <@U…>  = bot ユーザー → app_mention イベントが発火する
#   <@B…>  = アプリ実体   → app_mention は発火しない（message イベントで拾うしかない）
# オートコンプリートがどちらを挿すかは環境依存なので、両方に反応できるよう ID を控える。
try:
    _auth = app.client.auth_test()
    BOT_USER_ID = _auth.get("user_id")   # <@U…>
    BOT_ID = _auth.get("bot_id")         # <@B…>
    print(f"bot ids: user={BOT_USER_ID} bot={BOT_ID}", flush=True)
except Exception as _e:
    BOT_USER_ID = BOT_ID = None
    print("auth_test 失敗（bot id 取得できず）:", _e, flush=True)

# ---- MCP サーバー(vd-accessibility)を子プロセスで起動 ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_PY = os.path.join(_HERE, ".venv", "bin", "python")
if not os.path.exists(_PY):
    _PY = sys.executable
mcp = MCPClient([_PY, os.path.join(_HERE, "mcp_server.py")],
                env=os.environ.copy(), cwd=_HERE)


def _mcp_start():
    try:
        tools = mcp.start()
        print("MCP tools:", [t["name"] for t in tools], flush=True)
    except Exception as e:
        print("MCP 起動失敗（画像・リンク説明は無効で継続）:", e, flush=True)


# ---- ユーザー名の解決（ID→表示名、キャッシュ）----
_user_cache = {}


def user_name(uid):
    if not uid:
        return "誰か"
    if uid in _user_cache:
        return _user_cache[uid]
    name = uid
    try:
        p = app.client.users_info(user=uid)["user"]
        prof = p.get("profile", {})
        name = prof.get("display_name") or prof.get("real_name") or p.get("name") or uid
    except Exception:
        pass
    _user_cache[uid] = name
    return name


# ---- Slack 記法を音声向けに整形 ----
_MENTION = re.compile(r"<@([A-Z0-9]+)(\|[^>]+)?>")
_LINK = re.compile(r"<(https?://[^|>]+)(\|([^>]+))?>")
_IMG_URL = re.compile(r"https?://[^\s<>|]+?\.(?:png|jpe?g|gif|webp)(?:\?[^\s<>|]*)?", re.I)


def clean_text(t):
    t = t or ""
    t = _MENTION.sub(lambda m: "@" + user_name(m.group(1)), t)
    t = _LINK.sub(lambda m: (m.group(3) or "リンク"), t)
    return t.strip()


def clean_text_keep_urls(t):
    """clean_text と同じだが、生 URL を残す（エージェントが read_link を選べるように）。
    ユーザー ID→名前の変換だけ行い、<url|label> は「label（url）」の形に展開する。"""
    t = t or ""
    t = _MENTION.sub(lambda m: "@" + user_name(m.group(1)), t)
    t = _LINK.sub(lambda m: (f"{m.group(3)}（{m.group(1)}）" if m.group(3) else m.group(1)), t)
    return t.strip()


def strip_bot_mention(text):
    """本文先頭の @bot メンションだけ落とす。"""
    return _MENTION.sub("", text or "").strip()


# ---- 画像を MCP describe_image で言葉にする（キャッシュ）----
_img_cache = {}


def describe_image(url, context=""):
    if not mcp.has_tool("describe_image"):
        return ""
    if url in _img_cache:
        return _img_cache[url]
    desc = mcp.call_tool("describe_image", {"image_url": url, "context": context})
    _img_cache[url] = desc
    return desc


def collect_image_urls(msg):
    """メッセージ本文中の画像 URL＋添付ファイルの画像を集める。"""
    urls = []
    for m in _IMG_URL.finditer(msg.get("text", "") or ""):
        urls.append(m.group(0))
    for f in msg.get("files", []) or []:
        if str(f.get("mimetype", "")).startswith("image/"):
            u = f.get("url_private_download") or f.get("url_private")
            if u:
                urls.append(u)
    # 順序維持で重複除去
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ---- 会話ログの取得（チャンネル履歴 or スレッド返信）----
def fetch_log(channel, thread_ts=None, limit=30, describe_images=False,
              max_images=4, keep_urls=True):
    """会話ログを 1 本のテキストに。
    keep_urls=True（既定/エージェント経路）: 生 URL・画像 URL を残し、モデルに
      describe_image / read_link を呼ぶかどうかを委ねる。
    describe_images=True（旧・決定論経路）: ここで画像を先に説明して埋め込む。"""
    if thread_ts:
        res = app.client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
        msgs = res.get("messages", [])          # 古い順で返る
    else:
        res = app.client.conversations_history(channel=channel, limit=limit)
        msgs = list(reversed(res.get("messages", [])))  # 新しい順→古い順に直す

    clean = clean_text_keep_urls if keep_urls else clean_text
    lines = []
    img_budget = max_images
    for m in msgs:
        txt = clean(m.get("text", ""))
        who = user_name(m.get("user")) if m.get("user") else (m.get("username") or "アプリ")
        if txt:
            lines.append(f"{who}: {txt}")
        imgs = collect_image_urls(m)
        if describe_images and img_budget > 0:
            for url in imgs:
                if img_budget <= 0:
                    break
                desc = describe_image(url, context=f"{who} が投稿した画像")
                if desc:
                    lines.append(f"[画像の内容: {desc}]")
                    img_budget -= 1
        elif keep_urls:
            # 添付画像は本文に URL が出ないので、明示的に行として残す
            for url in imgs:
                lines.append(f"[{who} が投稿した画像 URL: {url}]")
    return "\n".join(lines) or "(メッセージがありません)"


# ---- OpenAI 呼び出し（chat completions + function calling）----
def _openai_chat(messages, tools=None, max_tokens=1200):
    """assistant メッセージ（content / tool_calls を含む dict）を返す。
    有料キーなので基本速いが、念のため 429/5xx はバックオフでリトライ。"""
    body = {"model": OPENAI_MODEL, "messages": messages,
            "max_tokens": max_tokens, "temperature": 0.4}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {OPENAI_KEY}"}
    for attempt in range(4):
        try:
            req = urllib.request.Request(CHAT_ENDPOINT, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["choices"][0]["message"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                wait = 2 * (attempt + 1)
                print(f"[vd] openai {e.code}, retry in {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise


def _gemini(prompt, max_tokens=1200):
    """テキスト 1 発叩き（gemini_tasks など従来用途。名前は据え置き）。"""
    msg = _openai_chat([{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": prompt}], max_tokens=max_tokens)
    return (msg.get("content") or "").strip()


# ---- MCP ツール → OpenAI tools 定義 ----
_ALLOWED_SCHEMA_KEYS = {"type", "properties", "items", "required", "description", "enum"}


def _sanitize_schema(schema):
    """MCP の inputSchema を OpenAI parameters が受ける JSON Schema サブセットに絞る。"""
    if not isinstance(schema, dict):
        return {"type": "object"}
    out = {}
    for k, v in schema.items():
        if k not in _ALLOWED_SCHEMA_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _sanitize_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _sanitize_schema(v)
        else:
            out[k] = v
    return out


_tool_decls_cache = None


def _mcp_tool_decls():
    """mcp.list_tools()（＝MCP サーバーが唯一の真実）から OpenAI ツール定義を作る。"""
    global _tool_decls_cache
    if _tool_decls_cache is None:
        _tool_decls_cache = [
            {"type": "function", "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": _sanitize_schema(t.get("inputSchema") or {"type": "object"}),
            }}
            for t in mcp.list_tools()
        ]
    return _tool_decls_cache


def _run_tool(name, args):
    """モデルが選んだツールを実行。describe_image はキャッシュ付き wrapper 経由。"""
    t0 = time.time()
    if name == "describe_image":
        out = describe_image(args.get("image_url", ""), args.get("context", ""))
    else:
        out = mcp.call_tool(name, args or {})
    print(f"[vd] tool {name} took {time.time()-t0:.1f}s", flush=True)
    return out


def gemini_answer_agentic(user_text, log, lang="ja", max_steps=2, has_sources=False):
    """function calling ループ。画像 URL / リンクが答えに要るとき、モデル自身が
    describe_image / read_link を呼んでから本文を作る。lang でユーザーの言語に合わせて返答。"""
    tools = _mcp_tool_decls()
    lang_name = "English" if lang == "en" else "Japanese"
    fail = STR[lang]["answer_fail"]
    prompt = (
        "Here is a Slack conversation log (oldest first).\n"
        f"----\n{log}\n----\n\n"
        f'A blind user asks: "{user_text}"\n'
        f"Answer in {lang_name}, in a clear spoken style, leading with anything that needs them. "
        "If an image URL or a link in the log is needed to answer, call the "
        "describe_image / read_link tools first to turn it into words, then answer."
    )
    messages = [{"role": "system", "content": SYSTEM}]
    if has_sources:
        # 引用は必須ルールとして system に格上げ（プロンプト末尾だと gpt-4o が
        # 無視しがち＋ツール呼び出しループで後方に流れて効かなくなるため）。
        messages.append({"role": "system", "content": CITE_RULE})
        prompt += ("\n\n(Remember: end every sentence that restates a numbered log line "
                   "with that line's [n], e.g. '出荷は金曜に確定しました。[2]'.)")
    messages.append({"role": "user", "content": prompt})
    try:
        for step in range(max_steps):
            t0 = time.time()
            msg = _openai_chat(messages, tools=tools)
            calls = msg.get("tool_calls") or []
            print(f"[vd] step{step} openai {time.time()-t0:.1f}s, "
                  f"tool_calls={[c['function']['name'] for c in calls]}", flush=True)
            if not calls:
                return (msg.get("content") or "").strip() or fail
            messages.append(msg)                       # assistant の tool_calls をそのまま返す
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except Exception:
                    args = {}
                out = _run_tool(name, args)
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": str(out)})
        # ツール予算切れ → 最後はツール無しで本文を出させる
        msg = _openai_chat(messages, tools=None)
        return (msg.get("content") or "").strip() or fail
    except (KeyError, IndexError):
        return fail
    except Exception as e:
        print("answer error:", type(e).__name__, e, flush=True)
        return fail


def gemini_answer(user_text, log):
    prompt = (
        "次は Slack の会話ログです（古い順）。\n"
        f"----\n{log}\n----\n\n"
        f"画面を見られないユーザーからの依頼:「{user_text}」\n"
        "この依頼に、音声で聞いて分かりやすいように日本語で答えてください。"
    )
    try:
        text = _gemini(prompt, max_tokens=1200)
        return text or "うまく答えを作れませんでした。もう一度言ってもらえますか。"
    except (KeyError, IndexError):
        return "うまく答えを作れませんでした。もう一度言ってもらえますか。"
    except Exception as e:
        return f"すみません、いま応答を作れませんでした（{type(e).__name__}）。少し置いて試してください。"


def gemini_tasks(log):
    """会話ログから“あなた(ユーザー)がやるべきこと”だけを短い配列で抽出。"""
    prompt = (
        "次は Slack の会話ログです（古い順）。\n"
        f"----\n{log}\n----\n\n"
        "この中から、画面を見られないユーザー本人がこれからやるべき ToDo だけを抜き出し、"
        "JSON 配列で返してください。各要素は動詞で終わる短い一文（20字程度）。"
        "やることが無ければ空配列 []。JSON 以外は出力しないでください。"
    )
    try:
        raw = _gemini(prompt, max_tokens=500)
    except Exception:
        return []
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except Exception:
        return []
    return [str(x).strip() for x in items if str(x).strip()][:8]


# ---- Real-Time Search（assistant.search.context）で最新の全社文脈を引く ----
def search_context(query, action_token=None, limit=20):
    """Slack RTS でワークスペース横断の新鮮な文脈を取得。
    使えない/未認可/AI 非対応ワークスペースなら None を返し、呼び出し側は
    conversations.history にフォールバックする（デモは絶対に止めない）。"""
    if not query:
        return None
    params = {"query": query, "limit": limit}
    if action_token:                       # bot トークン＋イベント由来の action_token 経路
        params["action_token"] = action_token
    try:
        res = app.client.api_call("assistant.search.context", params=params)
    except Exception:
        return None
    if not res or not res.get("ok"):        # not_authed / missing_scope / 非 AI ワークスペース
        return None
    results = res.get("results") or {}
    msgs = results.get("messages") or res.get("messages") or []
    lines = []
    for r in msgs:
        who = user_name(r.get("user")) if r.get("user") else (r.get("username") or "誰か")
        txt = clean_text_keep_urls(r.get("text", ""))
        if txt:
            lines.append(f"{who}: {txt}")
    return "\n".join(lines) or None


def build_log(event, user_text, lang="ja"):
    """まず RTS で全社検索、ダメなら直近ログ。(ログ本文, 出典ラベル) を返す。"""
    ctx = search_context(user_text, action_token=event.get("action_token"))
    if ctx:
        return ctx, STR[lang]["src_rts"]
    thread_ts = event.get("thread_ts")
    log = fetch_log(event["channel"], thread_ts=thread_ts, keep_urls=True)
    return log, STR[lang]["src_thread" if thread_ts else "src_channel"]


# ---- 出典（引用）: メッセージ→permalink を解決し、行に [n] を採番する ----
MAX_SOURCES = 8            # ボタン数・getPermalink 回数の上限
_perm_cache = {}


def get_permalink(channel, ts):
    """(channel, ts) の permalink を返す。取れなければ None。結果はキャッシュ。"""
    if not channel or not ts:
        return None
    key = (channel, ts)
    if key in _perm_cache:
        return _perm_cache[key]
    url = None
    try:
        r = app.client.chat_getPermalink(channel=channel, message_ts=ts)
        if r.get("ok"):
            url = r.get("permalink")
    except Exception:
        url = None
    _perm_cache[key] = url
    return url


def _rts_locate(r):
    """RTS 結果メッセージから (channel_id, ts, permalink) を防御的に取り出す。"""
    perm = r.get("permalink")
    ts = r.get("ts") or r.get("message_ts")
    ch = r.get("channel")
    if isinstance(ch, dict):
        ch = ch.get("id")
    ch = ch or r.get("channel_id")
    return ch, ts, perm


def _messages_to_log_and_sources(msgs, lang, from_rts, channel=None):
    """メッセージ列を (ログ本文, sources) に。permalink が取れた行だけ [n] を採番し、
    sources に {n, who, text, url} を積む。位置が取れない行は番号なしで文脈にだけ残す。"""
    lines, sources, n = [], [], 0
    for m in msgs:
        txt = clean_text_keep_urls(m.get("text", ""))
        imgs = [] if from_rts else collect_image_urls(m)   # 添付画像も文脈に載せる
        if imgs:
            marker = " ".join(f"[image: {u}]" for u in imgs)
            txt = (txt + " " + marker).strip() if txt else marker
        if not txt:
            continue
        who = (user_name(m.get("user")) if m.get("user")
               else (m.get("username") or ("someone" if lang == "en" else "誰か")))
        url = None
        if n < MAX_SOURCES:
            if from_rts:
                ch, ts, perm = _rts_locate(m)
            else:
                ch, ts, perm = channel, m.get("ts"), None
            url = perm or get_permalink(ch, ts)
        if url:
            n += 1
            sources.append({"n": n, "who": who, "text": txt[:140], "url": url})
            lines.append(f"[{n}] {who}: {txt}")
        else:
            lines.append(f"{who}: {txt}")
    empty = "(no messages)" if lang == "en" else "(メッセージがありません)"
    return ("\n".join(lines) or empty), sources


def _rts_context_and_sources(query, action_token, lang):
    if not query:
        return None
    params = {"query": query, "limit": 20}
    if action_token:
        params["action_token"] = action_token
    try:
        res = app.client.api_call("assistant.search.context", params=params)
    except Exception:
        return None
    if not res or not res.get("ok"):
        return None
    results = res.get("results") or {}
    msgs = results.get("messages") or res.get("messages") or []
    if not msgs:
        return None
    return _messages_to_log_and_sources(msgs, lang, from_rts=True)


def build_log_with_sources(event, user_text, lang="ja"):
    """RTS→ダメなら直近ログ。(ログ本文, 出典ラベル, sources) を返す。
    sources[i] = {n, who, text, url(permalink)}。ログ本文の該当行には [n] が前置される。"""
    rts = _rts_context_and_sources(user_text, event.get("action_token"), lang)
    if rts:
        return rts[0], STR[lang]["src_rts"], rts[1]
    thread_ts = event.get("thread_ts")
    channel = event["channel"]
    if thread_ts:
        res = app.client.conversations_replies(channel=channel, ts=thread_ts, limit=30)
        msgs = res.get("messages", [])                      # 古い順
    else:
        res = app.client.conversations_history(channel=channel, limit=30)
        msgs = list(reversed(res.get("messages", [])))      # 新しい順→古い順
    log, sources = _messages_to_log_and_sources(msgs, lang, from_rts=False, channel=channel)
    return log, STR[lang]["src_thread" if thread_ts else "src_channel"], sources


# ---- 音声出力（OpenAI TTS → Slack に再生可能な音声を投稿）----
def gemini_tts(text):
    """要約テキストを音声(mp3 bytes)にする。失敗時は None（テキストのみで継続）。名前は据え置き。"""
    if not ENABLE_VOICE or not text:
        return None
    body = json.dumps({
        "model": OPENAI_TTS_MODEL,
        "voice": OPENAI_TTS_VOICE,
        "input": text[:4000],          # OpenAI TTS の入力上限に合わせて切る
        "response_format": "mp3",
    }).encode()
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {OPENAI_KEY}"}
    for attempt in range(4):
        try:
            req = urllib.request.Request(TTS_ENDPOINT, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()          # 生の mp3 バイト列
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                print(f"[vd] TTS {e.code}, retry in {2*(attempt+1)}s", flush=True)
                time.sleep(2 * (attempt + 1))
                continue
            print("TTS 失敗（テキストのみで継続）:", e.code, flush=True)
            return None
        except Exception as e:
            print("TTS 失敗（テキストのみで継続）:", type(e).__name__, e, flush=True)
            return None


def post_voice(client, channel, thread_ts, text, lang="ja"):
    """デジタル要約を音声化して同スレッドに投稿。失敗は握りつぶす（テキストが正）。"""
    audio = gemini_tts(text)
    if not audio:
        return
    try:
        client.files_upload_v2(channel=channel, thread_ts=thread_ts,
                               content=audio, filename="voicedigest.mp3",
                               title=STR[lang]["audio_title"])
    except Exception as e:
        print("音声アップロード失敗:", type(e).__name__, e, flush=True)


def spoken_text(digest, lang, sources):
    """音声用テキスト。本文中の [n] を「引用n」と読み上げる形にし、出典があれば
    最後に「番号ボタンでリンクが出る」案内を添える。"""
    word = STR[lang]["voice_cite_word"]
    has_cite = bool(re.search(r"\[\d+\]", digest))
    spoken = re.sub(r"\s*\[(\d+)\]",
                    lambda m: word.format(n=m.group(1)), digest)
    if sources and has_cite:
        spoken = spoken.rstrip() + STR[lang]["voice_cite_tail"]
    return spoken


# ---- Block Kit 組み立て ----
_SENT_RE = re.compile(r".*?[。．.!?！？]+(?:\s*\[\d+\])*", re.S)


def _cite_units(digest):
    """要約を文単位に分け、各文の (表示テキスト[n]除去済, [引用番号...]) を返す。
    行(改行)＋文末(。．.!?！？)で分割。**文末の直後に続く [n] はその文に含める**
    (モデルは句点の後ろに [2][3] を置きがちで、素朴に「。」で切ると引用が次の文へ
    ズレて末尾の [n] が孤立・欠落するため)。"""
    units = []
    for line in (digest or "").split("\n"):
        if not line.strip():
            continue
        parts, pos = [], 0
        for m in _SENT_RE.finditer(line):
            if m.group(0).strip():
                parts.append(m.group(0))
            pos = m.end()
        tail = line[pos:]
        if tail.strip():
            parts.append(tail)          # 句点で終わらない最後の断片
        for part in parts:
            nums = [int(x) for x in re.findall(r"\[(\d+)\]", part)]
            text = re.sub(r"\s*\[\d+\]", "", part).strip()
            if text:
                units.append((text, nums))
            elif nums and units:        # 保険: [n] だけの孤児は直前の文に合流
                pt, pn = units[-1]
                units[-1] = (pt, pn + [n for n in nums if n not in pn])
    return units


def renumber_citations(digest, sources):
    """要約中の [n]（元ログの行番号）を、本文の出現順に 1,2,3… へ振り直す。
    同じ元番号は同じ表示番号に統一。出典テーブルに無い [n] は削除する。
    ボタン・音声・出典メッセージが全て同じ連番になるよう digest と sources を揃えて返す
    （モデルは行番号順でなく話題順に引用するので、素のままだと 8→2→3 と散らばるため）。"""
    if not digest or not sources:
        return digest, sources
    by_n = {item["n"]: item for item in sources}
    order = []                                   # 出典を持つ [n] を出現順(ユニーク)に
    for m in re.finditer(r"\[(\d+)\]", digest):
        n = int(m.group(1))
        if n in by_n and n not in order:
            order.append(n)
    remap = {old: i + 1 for i, old in enumerate(order)}   # 元番号 → 表示番号(1始まり)

    def repl(m):
        n = int(m.group(1))
        return f"[{remap[n]}]" if n in remap else ""       # 出典無しの[n]は消す
    new_digest = re.sub(r"\s*\[(\d+)\]", repl, digest)
    new_sources = []
    for old, disp in remap.items():
        item = dict(by_n[old])
        item["n"] = disp
        new_sources.append(item)
    new_sources.sort(key=lambda x: x["n"])
    return new_digest, new_sources


def digest_blocks(digest, channel, source_thread_ts, source_label, lang="ja", sources=None):
    """要約を Block Kit で。各文をセクションにし、その文の出典番号ボタンを行末(横並び)に付ける。
    単一引用はセクション右のアクセサリ＝同じ行の末尾。複数引用は文の直下に横並びボタン。"""
    s = STR[lang]
    src = json.dumps({"c": channel, "t": source_thread_ts or "", "l": lang}, ensure_ascii=False)
    by_n = {item["n"]: item for item in (sources or [])}
    counter = [0]

    def cite_button(item):
        counter[0] += 1                       # action_id は毎回ユニーク(同じ番号を複数行で使っても衝突しない)
        return {"type": "button",
                "text": {"type": "plain_text", "text": str(item["n"]), "emoji": False},
                "action_id": f"cite_{counter[0]}",
                "value": json.dumps({"n": item["n"], "u": item["url"], "l": lang},
                                    ensure_ascii=False)}

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": s["digest_header"], "emoji": True}},
    ]
    for text, nums in _cite_units(digest):
        seen, items = set(), []
        for n in nums:                        # by_n に無い番号は捨て、重複は 1 回に
            if n in by_n and n not in seen:
                seen.add(n)
                items.append(by_n[n])
        if len(items) == 1:                   # 単一引用: 同じ行の右端にボタン
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": text},
                           "accessory": cite_button(items[0])})
        elif len(items) >= 2:                 # 複数引用: 文の直下に横並び(5個で折返し)
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": text}})
            row = [cite_button(i) for i in items]
            for k in range(0, len(row), 5):
                blocks.append({"type": "actions", "elements": row[k:k + 5]})
        else:                                 # 引用なし
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": text}})
    if len(blocks) == 1:                      # 本文が空だった時の保険
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": digest or "…"}})
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn", "text": source_label}]})
    if sources:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": s["cite_hint"]}]})
    blocks.append({"type": "divider"})
    blocks.append({"type": "actions",
                   "elements": [
                       {"type": "button",
                        "text": {"type": "plain_text", "text": s["todo_btn"], "emoji": True},
                        "action_id": "make_task", "value": src},
                       {"type": "button",
                        "text": {"type": "plain_text", "text": s["up_btn"], "emoji": True},
                        "action_id": "fb_up", "value": lang},
                       {"type": "button",
                        "text": {"type": "plain_text", "text": s["down_btn"], "emoji": True},
                        "action_id": "fb_down", "value": lang},
                   ]})
    return blocks


def task_blocks(items, lang="ja"):
    s = STR[lang]
    if not items:
        return [
            {"type": "header",
             "text": {"type": "plain_text", "text": s["todo_header"], "emoji": True}},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": s["todo_empty"]}},
        ], s["todo_empty"]
    # 実際の Block Kit checkboxes コンポーネント（トグル可能・読み上げ対応）
    options = [
        {"text": {"type": "mrkdwn", "text": t},
         "value": f"todo_{i}"}
        for i, t in enumerate(items)
    ]
    fallback = "・".join(items)
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": s["todo_header"], "emoji": True}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": s["todo_hint"]},
         "accessory": {"type": "checkboxes", "action_id": "todo_toggle",
                       "options": options}},
    ]
    return blocks, fallback


# ---- Data Table ブロック（新 Block Kit。既定 OFF、非対応なら自動フォールバック）----
def _cell(text, bold=False):
    el = {"type": "text", "text": str(text)}
    if bold:
        el["style"] = {"bold": True}
    return {"type": "rich_text",
            "elements": [{"type": "rich_text_section", "elements": [el]}]}


def table_blocks(title, headers, rows):
    """table ブロックを組み立てて (blocks, 読み上げ用フォールバック文字列) を返す。"""
    header_row = [_cell(h, bold=True) for h in headers]
    body = [[_cell(c) for c in row] for row in rows]
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": title, "emoji": True}},
        {"type": "table", "rows": [header_row] + body},
    ]
    flat = "、".join(" / ".join(str(c) for c in row) for row in rows)
    return blocks, (f"{title}：{flat}" if rows else title)


def post_with_fallback(client, channel, thread_ts, text, primary_blocks, fallback_blocks):
    """table ブロックを試し、ワークスペースが未対応（invalid_blocks 等）なら
    GA ブロックで投稿し直す。ENABLE_TABLE が OFF なら最初からフォールバック。"""
    if ENABLE_TABLE:
        try:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=text, blocks=primary_blocks)
            return
        except Exception as e:
            print("table ブロック不可→フォールバック:", type(e).__name__, e, flush=True)
    client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                            text=text, blocks=fallback_blocks)


# ---- 重い処理（RTS→要約→音声）はワーカースレッドで。ハンドラは即 return して
#      Slack に素早く ack を返す（3 秒ルール＝再送・二重投稿を防ぐ）。まず即座に
#      プレースホルダを出し、出来上がったら chat_update で差し替える。----
def _run_digest(client, channel, reply_thread, event, user_text, lang, ph_ts):
    try:
        log, source_label, sources = build_log_with_sources(event, user_text, lang)  # RTS→ダメなら直近ログ
        digest = gemini_answer_agentic(user_text, log, lang, has_sources=bool(sources))
        digest, sources = renumber_citations(digest, sources)  # 引用番号を出現順に1→2→3へ
        blocks = digest_blocks(digest, channel, event.get("thread_ts"), source_label, lang, sources)
        client.chat_update(channel=channel, ts=ph_ts, text=digest, blocks=blocks)
        spoken = spoken_text(digest, lang, sources)  # [n]を「引用n」と読み、末尾に番号ボタン案内
        post_voice(client, channel, reply_thread, spoken, lang)   # 🎧 耳で聞ける音声も投稿
    except Exception as e:
        print("要約ワーカー失敗:", type(e).__name__, e, flush=True)
        try:
            client.chat_update(channel=channel, ts=ph_ts, text=STR[lang]["worker_err"])
        except Exception:
            pass


def _start_digest(client, channel, reply_thread, event, user_text, lang):
    ph = client.chat_postMessage(channel=channel, thread_ts=reply_thread,
                                 text=STR[lang]["placeholder"])
    threading.Thread(target=_run_digest,
                     args=(client, channel, reply_thread, event, user_text, lang, ph["ts"]),
                     daemon=True).start()


# ---- イベント: メンション ----
@app.event("app_mention")
def handle_mention(event, client):
    channel = event["channel"]
    raw = strip_bot_mention(event.get("text", ""))
    lang = detect_lang(raw)
    user_text = raw or ("Summarize what's happening in this channel."
                        if lang == "en" else "このチャンネルの状況を短くまとめて。")
    reply_thread = event.get("thread_ts") or event["ts"]
    _start_digest(client, channel, reply_thread, event, user_text, lang)


# ---- イベント: DM / チャンネルでの <@B…> メンション ----
@app.event("message")
def handle_dm(event, client):
    if event.get("bot_id"):
        return
    text = event.get("text", "") or ""

    # DM は本文全体を要約（従来どおり）
    if event.get("channel_type") == "im":
        channel = event["channel"]
        raw = clean_text(text)
        lang = detect_lang(raw)
        user_text = raw or ("Summarize the recent conversation."
                            if lang == "en" else "最近のやりとりを短くまとめて。")
        _start_digest(client, channel, None, event, user_text, lang)
        return

    # チャンネル/グループで <@B…>（アプリ実体）宛てにメンションされた場合だけ拾う。
    # <@U…>（bot ユーザー）は app_mention が処理するので、二重発火を避けてここでは扱わない。
    if BOT_ID and f"<@{BOT_ID}>" in text and not (BOT_USER_ID and f"<@{BOT_USER_ID}>" in text):
        channel = event["channel"]
        raw = strip_bot_mention(text)
        lang = detect_lang(raw)
        user_text = raw or ("Summarize what's happening in this channel."
                            if lang == "en" else "このチャンネルの状況を短くまとめて。")
        reply_thread = event.get("thread_ts") or event["ts"]
        _start_digest(client, channel, reply_thread, event, user_text, lang)


# ---- ボタン: やることリスト ----
@app.action("make_task")
def on_make_task(ack, body, client):
    ack()
    try:
        src = json.loads(body["actions"][0].get("value") or "{}")
    except Exception:
        src = {}
    channel = src.get("c") or body["channel"]["id"]
    thread_ts = src.get("t") or None
    lang = src.get("l") or "ja"
    s = STR[lang]
    # 押されたメッセージのスレッドに返す
    container = body.get("container", {})
    reply_thread = container.get("thread_ts") or body["message"]["ts"]
    log = fetch_log(channel, thread_ts=thread_ts, describe_images=False, keep_urls=False)
    items = gemini_tasks(log)
    fb_blocks, fallback = task_blocks(items, lang)
    if items:
        rows = [[t, s["status_pending"]] for t in items]
        tbl_blocks, _ = table_blocks(s["todo_header"], s["table_cols"], rows)
        post_with_fallback(client, channel, reply_thread, fallback, tbl_blocks, fb_blocks)
    else:
        client.chat_postMessage(channel=channel, thread_ts=reply_thread,
                                text=fallback, blocks=fb_blocks)


# ---- ボタン: 出典番号（[n]）→ 元メッセージの permalink をエフェメラル表示 ----
@app.action(re.compile(r"^cite_\d+$"))
def on_cite(ack, body, client):
    ack()
    try:
        v = json.loads(body["actions"][0].get("value") or "{}")
    except Exception:
        v = {}
    lang = v.get("l") or "ja"
    s = STR.get(lang, STR["ja"])
    channel = body["channel"]["id"]
    url = v.get("u")
    msg = s["cite_link"].format(n=v.get("n"), url=url) if url else s["cite_gone"]
    # 押されたカードと同じスレッドに、普通の返信として投稿する
    container = body.get("container", {})
    reply_thread = container.get("thread_ts") or body.get("message", {}).get("ts")
    try:
        client.chat_postMessage(channel=channel, thread_ts=reply_thread, text=msg)
    except Exception as e:
        print("出典リンク表示失敗:", type(e).__name__, e, flush=True)


# ---- ボタン: チェックボックスのトグル（no-op で ack）----
@app.action("todo_toggle")
def on_todo_toggle(ack):
    ack()


# ---- ボタン: フィードバック ----
@app.action("fb_up")
def on_fb_up(ack, body, client):
    ack()
    lang = (body["actions"][0].get("value") or "ja")
    _fb_reply(body, client, STR.get(lang, STR["ja"])["fb_up"])


@app.action("fb_down")
def on_fb_down(ack, body, client):
    ack()
    lang = (body["actions"][0].get("value") or "ja")
    _fb_reply(body, client, STR.get(lang, STR["ja"])["fb_down"])


def _fb_reply(body, client, msg):
    channel = body["channel"]["id"]
    user = body["user"]["id"]
    try:
        client.chat_postEphemeral(channel=channel, user=user, text=msg)
    except Exception:
        pass


def _start_health_server():
    """Render の Web Service はポート待受を要求する。VoiceDigest は Socket Mode で
    HTTP ポートを開かないため、ポートスキャンを通すだけの極小ヘルスサーバを立てる。
    PORT はプラットフォームが注入（Render 既定 10000）。ローカル起動時も無害。"""
    import http.server

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"VoiceDigest bot is running (Slack Socket Mode).\n")

        def log_message(self, *args):
            pass  # アクセスログは黙らせる

    port = int(os.environ.get("PORT", "10000"))
    http.server.HTTPServer(("0.0.0.0", port), _H).serve_forever()


if __name__ == "__main__":
    _mcp_start()
    threading.Thread(target=_start_health_server, daemon=True).start()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
