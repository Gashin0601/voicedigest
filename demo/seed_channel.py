#!/usr/bin/env python3
"""デモ用チャンネルに英語の会話履歴を投下する。

- チャンネルを新規作成（既にあれば名前で解決）し、そこへ Kenji / Owais / Hisato / Dai の
  4人が投稿したように見せる（chat:write.customize による username / icon 上書き）。
- 途中でサインアップ画面モック（demo/mockup.png）を添付し、WCAG リンクも共有する。
- 末尾の「あなた(Gashin)が @Momo を呼ぶ」メッセージは録画時に本人が手打ちする想定なので
  ここでは投稿しない。

前提スコープ: chat:write.customize（名前/アイコン上書き）, channels:manage（チャンネル作成）
使い方:
    python3 seed_channel.py                 # momo-demo を作成して投下
    python3 seed_channel.py my-channel-name # 名前を指定
    python3 seed_channel.py C0XXXXXXX       # 既存チャンネルID指定
"""
import os
import sys
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

HERE = os.path.dirname(os.path.abspath(__file__))
client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

# 投稿間隔（秒）。過去時刻の偽装はSlack APIでは不可能なので、会話が自然に流れて
# 見えるよう実際に間隔をあけるだけ。GAP=SEED_GAP で上書き可（例 SEED_GAP=8）。
GAP = float(os.environ.get("SEED_GAP", "1.1"))

# --- 登場人物（表示名・アバター色）---
# 人ごとに色を変えて一目で判別できるように、イニシャル入りの丸アイコンを色違いで振る。
# 弱視配慮で背景は濃色・文字は白抜き（高コントラスト）。
def avatar(name, bg):
    from urllib.parse import quote
    return (f"https://api.dicebear.com/9.x/initials/png?seed={quote(name)}"
            f"&backgroundColor={bg}&textColor=ffffff&fontSize=42&radius=50")

KENJI = ("Kenji Sato", avatar("Kenji Sato", "2563eb"))   # 青
OWAIS = ("Owais Khan", avatar("Owais Khan", "7c3aed"))   # 紫
HISATO = ("Hisato Mori", avatar("Hisato Mori", "15803d"))  # 緑
DAI = ("Dai Ishii", avatar("Dai Ishii", "c2410c"))      # 橙

WCAG = "https://www.w3.org/WAI/WCAG22/quickref/"


def resolve_channel(arg):
    """arg が ID ならそのまま、名前なら作成/検索して channel_id を返す。"""
    if arg and arg.startswith("C") and arg.isalnum():
        return arg
    name = arg or "momo-demo"
    try:
        r = client.conversations_create(name=name, is_private=False)
        print(f"created #{name} -> {r['channel']['id']}")
        return r["channel"]["id"]
    except SlackApiError as e:
        if e.response.get("error") == "name_taken":
            # 既存を探す
            cur = None
            while True:
                res = client.conversations_list(types="public_channel", limit=200, cursor=cur)
                for c in res["channels"]:
                    if c["name"] == name:
                        print(f"reuse #{name} -> {c['id']}")
                        return c["id"]
                cur = res.get("response_metadata", {}).get("next_cursor")
                if not cur:
                    break
        raise


def say(ch, who, text):
    name, icon = who
    r = client.chat_postMessage(channel=ch, username=name, icon_url=icon, text=text)
    if r["message"].get("username") != name:
        print("WARN: username 上書きが未適用 — chat:write.customize が付与されていません")
    time.sleep(GAP)
    return r


def clear_channel(ch):
    """このアプリ/Botが投稿した既存メッセージを全部消してから貼り直す。"""
    deleted = 0
    cur = None
    while True:
        res = client.conversations_history(channel=ch, limit=100, cursor=cur)
        for m in res.get("messages", []):
            ts = m.get("ts")
            if not ts:
                continue
            try:
                client.chat_delete(channel=ch, ts=ts)
                deleted += 1
                time.sleep(0.35)
            except SlackApiError as e:
                print("skip delete:", e.response.get("error"), ts)
        cur = res.get("response_metadata", {}).get("next_cursor")
        if not cur:
            break
    print(f"cleared {deleted} old messages")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    ch = resolve_channel(arg)
    clear_channel(ch)

    say(ch, KENJI, "Morning all ☕ Doing standup in text since Gashin's offline "
                   "— I'll lock decisions at the end.")

    # サインアップ画面モック（添付画像は Bot 名で出るが、要約では説明＆引用できる）
    client.files_upload_v2(channel=ch, file=os.path.join(HERE, "mockup.png"),
                           filename="signup_mockup.png",
                           initial_comment="New sign-up mockup for review \U0001f447 (from Owais)")
    time.sleep(2)

    say(ch, OWAIS, "Main question — is the green primary button obvious enough on white, "
                   "or too subtle?")
    say(ch, HISATO, "Sign-up backend is done. QA wraps Thursday, so we can ship Friday "
                    "if design locks today.")
    say(ch, DAI, "Love it. One ask — make sure it passes accessibility, contrast especially.")
    say(ch, HISATO, f"Good call. WCAG quick reference to check against: {WCAG}")
    say(ch, KENJI, "Decision — we ship Friday. Design locks today, QA Thursday, "
                   "release Friday afternoon.")
    say(ch, KENJI, "@Gashin two things for you: (1) confirm you can join next week's user test, "
                   "(2) we need the consent-form draft by Friday. Can you own that?")
    say(ch, DAI, "Also Gashin — your low-vision take on the button contrast would be gold "
                 "before we lock.")
    say(ch, OWAIS, "No rush, but a \U0001f44d on the mockup direction would help me finalize.")

    print("\nDONE. 録画時は、このチャンネルで自分のアカウントから↓を送ってください:")
    print("  @Momo I just got back and missed the morning. "
          "What are the key points, and is there anything I need to do?")


if __name__ == "__main__":
    main()
