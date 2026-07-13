"""
最小の MCP stdio クライアント（標準ライブラリのみ）。

mcp_server.py を子プロセスとして起動し、initialize → tools/list →
tools/call を行う。改行区切り JSON-RPC 2.0。Bolt のハンドラは別スレッドで
走りうるので、1 リクエスト = 1 応答を lock で直列化する。

使い方:
  mcp = MCPClient([python, "mcp_server.py"], env=os.environ)
  mcp.start()
  mcp.list_tools()
  text = mcp.call_tool("describe_image", {"image_url": url})
"""
import os
import json
import threading
import subprocess


class MCPClient:
    def __init__(self, cmd, env=None, cwd=None):
        self.cmd = cmd
        self.env = env or os.environ.copy()
        self.cwd = cwd
        self.proc = None
        self._id = 0
        self._lock = threading.Lock()
        self.tools = []
        self._ready = False

    # ---- 低レベル送受信（lock 前提で呼ぶ）----
    def _write(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _read_result(self, req_id):
        # 目的の id の応答が来るまで読む（通知は id が無いので飛ばす）
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed the connection")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                raise RuntimeError(f"MCP error: {msg['error']}")
            return msg.get("result", {})

    def _request(self, method, params=None):
        with self._lock:
            self._id += 1
            rid = self._id
            self._write({"jsonrpc": "2.0", "id": rid, "method": method,
                         "params": params or {}})
            return self._read_result(rid)

    def _notify(self, method, params=None):
        with self._lock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ---- ライフサイクル ----
    def start(self, timeout=30):
        self.proc = subprocess.Popen(
            self.cmd, cwd=self.cwd, env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "vd-bot", "version": "1.0.0"},
        })
        self._notify("notifications/initialized")
        self.tools = self._request("tools/list").get("tools", [])
        self._ready = True
        return self.tools

    def list_tools(self):
        return self.tools

    def has_tool(self, name):
        return any(t.get("name") == name for t in self.tools)

    def call_tool(self, name, arguments):
        """ツールを呼びテキスト結果を返す。失敗時は例外でなく説明文字列。"""
        try:
            res = self._request("tools/call", {"name": name, "arguments": arguments})
        except Exception as e:
            return f"（{name} を実行できませんでした: {type(e).__name__}）"
        parts = res.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        return text.strip()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
            except Exception:
                pass
