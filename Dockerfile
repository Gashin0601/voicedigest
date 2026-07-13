# Momo — Slack accessibility agent (Socket Mode worker; no inbound port).
FROM python:3.11-slim

WORKDIR /app/src

# deps (only slack_bolt; mcp_server is stdlib-only)
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app code (bot.py spawns mcp_server.py as a child; uses the container python
# since there is no .venv here — bot.py falls back to sys.executable)
COPY src/ /app/src/

# long-running worker; connects out to Slack via Socket Mode
CMD ["python", "bot.py"]
