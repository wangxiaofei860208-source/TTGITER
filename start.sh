#!/bin/bash
# PyClaudeCode 完整启动脚本（本地服务 + Cloudflare Tunnel）
cd "$(dirname "$0")"

if [ -f .env ]; then set -a; source .env; set +a; fi
if [ -z "$DEEPSEEK_API_KEY" ]; then echo "❌ 请设置 DEEPSEEK_API_KEY"; exit 1; fi

export PORT="${PORT:-5001}"
export WORKSPACE="${WORKSPACE:-$HOME/workspace}"
mkdir -p "$WORKSPACE"

# Kill existing processes
lsof -ti:$PORT 2>/dev/null | xargs kill 2>/dev/null
pkill -f "cloudflared.*py-claude" 2>/dev/null
sleep 1

# Start Flask app in background
python3 app.py &
FLASK_PID=$!
echo "⚡ Flask started (PID: $FLASK_PID, Port: $PORT)"
sleep 2

# Start Cloudflare Tunnel
if [ -x "$HOME/bin/cloudflared" ]; then
    echo "🌐 Starting Cloudflare Tunnel..."
    $HOME/bin/cloudflared tunnel --protocol http2 --url http://localhost:$PORT 2>&1 | grep -m1 "trycloudflare.com" | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com'
    wait $FLASK_PID
else
    echo "⚠️ cloudflared not found, local only"
    echo "📍 http://localhost:$PORT"
    wait $FLASK_PID
fi
