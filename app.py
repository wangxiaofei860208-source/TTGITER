#!/usr/bin/env python3
"""
PyClaudeCode - Python版Claude Code
AI驱动的编程Agent，支持工具调用、文件操作、Shell执行
Web UI + API，可远程访问
"""

import json, os, sys, time, uuid, subprocess, re, threading, traceback
import sqlite3, urllib.request, urllib.parse, signal, ipaddress, socket
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

# === Config ===
API_URL = os.environ.get("API_URL", "https://api.siliconflow.cn/v1/chat/completions")
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL = os.environ.get("MODEL", "deepseek-ai/DeepSeek-V3")
WORKSPACE = os.environ.get("WORKSPACE", os.path.expanduser("~"))
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "pyclaudecode.db")
MAX_HISTORY = 40
MAX_TOOL_ROUNDS = 15

os.makedirs(WORKSPACE, exist_ok=True)

if not API_KEY:
    print("❌ 请设置 DEEPSEEK_API_KEY 环境变量")
    sys.exit(1)

SYSTEM_PROMPT = """你是AI编程助手。用户让你做什么你就做什么，直接行动不要废话。

可用工具：read_file, write_file, edit_file, run_command, list_dir, search, web_fetch

规则：
- 用户让写文件就用write_file，让执行命令就用run_command
- 直接做，不要先解释
- 完成后简要说明

工作目录: {workspace}"""

# === Tools Definition ===
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取文件内容。支持偏移量和行数限制。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径（相对或绝对）"},
            "offset": {"type": "integer", "description": "起始行号（1开始）", "default": 1},
            "limit": {"type": "integer", "description": "最多读取行数", "default": 200}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "创建或覆盖文件。自动创建父目录。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "文件内容"}
        }, "required": ["path", "content"]}
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "精确编辑文件：找到old_text并替换为new_text。必须精确匹配。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_text": {"type": "string", "description": "要替换的原始文本（必须精确匹配）"},
            "new_text": {"type": "string", "description": "替换后的文本"}
        }, "required": ["path", "old_text", "new_text"]}
    }},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "执行Shell命令。超时30秒。只允许安全命令。",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Shell命令"},
            "timeout": {"type": "integer", "description": "超时秒数", "default": 30},
            "workdir": {"type": "string", "description": "工作目录"}
        }, "required": ["command"]}
    }},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "列出目录内容",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "目录路径", "default": "."},
            "recursive": {"type": "boolean", "description": "是否递归", "default": False}
        }}
    }},
    {"type": "function", "function": {
        "name": "search",
        "description": "在文件中搜索文本（类似grep）",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "搜索模式"},
            "path": {"type": "string", "description": "搜索路径", "default": "."}
        }, "required": ["pattern"]}
    }},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "获取URL内容",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "URL地址"}
        }, "required": ["url"]}
    }},
]

# === Security ===
DANGEROUS = ["rm -rf /", "mkfs", "dd if=", "sudo rm", "format ", ":(){", "> /dev/sd",
             "wget", "nc -l", "/dev/tcp", "chmod 777", "chown root", "passwd"]

ALLOWED_COMMANDS = {"ls", "cat", "head", "tail", "wc", "echo", "mkdir", "cp", "mv",
                    "touch", "grep", "find", "which", "python3", "python", "pip", "pip3",
                    "node", "npm", "git", "diff", "sort", "uniq", "tr", "cut",
                    "sed", "awk", "pwd", "whoami", "date", "df", "du", "file",
                    "stat", "tree", "cd", "cargo", "go", "rustc", "gcc", "make",
                    "javac", "java", "ruby", "php", "perl", "bash", "sh", "env",
                    "curl", "tar", "zip", "unzip", "chmod", "chown", "ln", "realpath",
                    "basename", "dirname", "test", "true", "false", "sleep", "seq"}

def resolve_path(p):
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(WORKSPACE, p)
    p = os.path.normpath(p)
    if not p.startswith(os.path.abspath(WORKSPACE)):
        raise ValueError("路径超出工作目录范围")
    return p

# === Tool Execution ===
def run_tool(name, args):
    try:
        if name == "read_file":
            p = resolve_path(args.get("path", ""))
            if not os.path.exists(p):
                return json.dumps({"error": f"文件不存在: {p}"})
            offset = args.get("offset", 1)
            limit = args.get("limit", 200)
            with open(p, 'r', errors='ignore') as f:
                lines = f.readlines()
            total = len(lines)
            selected = lines[offset-1:offset-1+limit]
            return json.dumps({"content": "".join(selected), "lines": total, "showing": f"{offset}-{offset+len(selected)-1}"}, ensure_ascii=False)

        elif name == "write_file":
            p = resolve_path(args.get("path", ""))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w') as f:
                f.write(args.get("content", ""))
            return json.dumps({"ok": True, "path": p})

        elif name == "edit_file":
            p = resolve_path(args.get("path", ""))
            if not os.path.exists(p):
                return json.dumps({"error": f"文件不存在: {p}"})
            old, new = args.get("old_text", ""), args.get("new_text", "")
            with open(p, 'r') as f:
                content = f.read()
            if old not in content:
                return json.dumps({"error": "未找到要替换的文本"})
            if content.count(old) > 1:
                return json.dumps({"error": f"找到{content.count(old)}处匹配，请提供更具体的上下文"})
            with open(p, 'w') as f:
                f.write(content.replace(old, new, 1))
            return json.dumps({"ok": True, "path": p})

        elif name == "run_command":
            cmd = args.get("command", "")
            timeout = min(args.get("timeout", 30), 120)
            workdir = resolve_path(args.get("workdir", "."))
            for d in DANGEROUS:
                if d in cmd:
                    return json.dumps({"error": "危险命令已拦截"})
            base = cmd.strip().split()[0] if cmd.strip() else ""
            if base not in ALLOWED_COMMANDS:
                return json.dumps({"error": f"命令不允许: {base}"})
            if any(c in cmd for c in ['`', '$(', '||', '&&']):
                return json.dumps({"error": "命令包含不安全的shell元字符"})
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=workdir)
            return json.dumps({"exit_code": r.returncode, "stdout": r.stdout[-3000:], "stderr": r.stderr[-1000:]}, ensure_ascii=False)

        elif name == "list_dir":
            p = resolve_path(args.get("path", "."))
            if not os.path.isdir(p):
                return json.dumps({"error": f"不是目录: {p}"})
            entries = []
            for e in sorted(os.listdir(p)):
                if e.startswith('.'):
                    continue
                fp = os.path.join(p, e)
                entries.append({"name": e, "type": "dir" if os.path.isdir(fp) else "file", "size": os.path.getsize(fp) if os.path.isfile(fp) else 0})
            return json.dumps({"entries": entries}, ensure_ascii=False)

        elif name == "search":
            pattern = args.get("pattern", "")
            path = resolve_path(args.get("path", "."))
            r = subprocess.run(['grep', '-rn', pattern, path], capture_output=True, text=True, timeout=15)
            return json.dumps({"results": r.stdout[-3000:] or "无匹配"}, ensure_ascii=False)

        elif name == "web_fetch":
            url = args.get("url", "")
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in ('http', 'https'):
                return json.dumps({"error": "只支持HTTP/HTTPS"})
            hostname = parsed.hostname or ""
            try:
                ip = socket.gethostbyname(hostname)
            except socket.gaierror:
                return json.dumps({"error": "无法解析域名"})
            try:
                if ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback:
                    return json.dumps({"error": "禁止访问内网地址"})
            except ValueError:
                return json.dumps({"error": "无效IP"})
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            content = urllib.request.urlopen(req, timeout=15).read().decode('utf-8', errors='ignore')[:10000]
            return json.dumps({"content": content}, ensure_ascii=False)

        return json.dumps({"error": f"未知工具: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

# === Rate Limiting ===
_rate_lock = threading.Lock()
_rate_counters = {}

def check_rate_limit(ip, limit=30, window=60):
    now = time.time()
    with _rate_lock:
        if ip not in _rate_counters:
            _rate_counters[ip] = []
        _rate_counters[ip] = [t for t in _rate_counters[ip] if now - t < window]
        if len(_rate_counters[ip]) >= limit:
            return False
        _rate_counters[ip].append(now)
        return True

# === API Retry with Backoff ===
def api_request_with_retry(payload, headers, max_retries=3):
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(API_URL, data=json.dumps(payload).encode(), headers=headers)
            resp = urllib.request.urlopen(req, timeout=120)
            return resp
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='ignore')[:500] if hasattr(e, 'read') else ''
            if e.code == 429 and attempt < max_retries - 1:
                wait = (attempt + 1) * 5
                time.sleep(wait)
                continue
            if e.code == 400:
                raise Exception(f"API 400 Bad Request: {body}")
            raise
    return None

# === Database ===
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, name TEXT, workspace TEXT, created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            content TEXT, tool_calls TEXT, tool_result TEXT, timestamp REAL
        );
        CREATE INDEX IF NOT EXISTS idx_msgs_session ON messages(session_id, timestamp);
    """)
    db.commit()
    db.close()

# === Auth ===
@app.before_request
def auth_middleware():
    if request.path == '/api/health':
        return None
    if request.path.startswith('/api/') or request.method == 'POST':
        if AUTH_TOKEN:
            token = request.headers.get('Authorization', '').replace('Bearer ', '')
            if token != AUTH_TOKEN:
                return jsonify({"error": "未授权"}), 401
    return None

@app.before_request
def rate_limit_middleware():
    if request.path.startswith('/api/') and request.method == 'POST':
        ip = request.remote_addr
        if not check_rate_limit(ip):
            return jsonify({"error": "请求太频繁，请稍后再试"}), 429

# === Routes ===
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    db = get_db()
    rows = db.execute("SELECT id, name, workspace, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 50").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/sessions", methods=["POST"])
def create_session():
    data = request.json or {}
    sid = str(uuid.uuid4())[:8]
    name = data.get("name", "新会话")
    db = get_db()
    now = time.time()
    db.execute("INSERT INTO sessions (id,name,workspace,created_at,updated_at) VALUES (?,?,?,?,?)",
               (sid, name, WORKSPACE, now, now))
    db.commit()
    db.close()
    return jsonify({"id": sid, "name": name})

@app.route("/api/sessions/<sid>", methods=["DELETE"])
def delete_session(sid):
    db = get_db()
    db.execute("DELETE FROM messages WHERE session_id=?", (sid,))
    db.execute("DELETE FROM sessions WHERE id=?", (sid,))
    db.commit()
    db.close()
    return jsonify({"ok": True})

@app.route("/api/sessions/<sid>/messages", methods=["GET"])
def get_messages(sid):
    db = get_db()
    rows = db.execute("SELECT id, role, content, tool_calls, tool_result, timestamp FROM messages WHERE session_id=? ORDER BY timestamp", (sid,)).fetchall()
    db.close()
    result = []
    for r in rows:
        d = dict(r)
        if d["tool_calls"]:
            d["tool_calls"] = json.loads(d["tool_calls"])
        result.append(d)
    return jsonify(result)

@app.route("/api/sessions/<sid>/chat", methods=["POST"])
def chat(sid):
    data = request.json
    user_text = data.get("content", "")

    db = get_db()
    now = time.time()
    db.execute("INSERT INTO messages (session_id,role,content,timestamp) VALUES (?,?,?,?)",
               (sid, "user", user_text, now))
    cnt = db.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)).fetchone()[0]
    if cnt <= 1:
        db.execute("UPDATE sessions SET name=?, updated_at=? WHERE id=?", (user_text[:50], now, sid))
    else:
        db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, sid))
    db.commit()

    rows = db.execute("SELECT role, content, tool_calls, tool_result FROM messages WHERE session_id=? ORDER BY timestamp", (sid,)).fetchall()
    db.close()

    # Only send the latest user message + system prompt to avoid SiliconFlow tool message compatibility issues
    last_user_msg = ""
    for r in reversed(rows):
        if r["role"] == "user":
            last_user_msg = r["content"]
            break
    msgs = [{"role": "system", "content": SYSTEM_PROMPT.format(workspace=WORKSPACE)}]
    if last_user_msg:
        msgs.append({"role": "user", "content": last_user_msg})

    def generate():
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}
        full_response = ""
        round_num = 0

        while round_num < MAX_TOOL_ROUNDS:
            round_num += 1
            is_continuation = round_num > 1
            payload = {
                "model": MODEL, "messages": msgs, "max_tokens": 8192,
                "temperature": 0.6, "tools": TOOLS, "tool_choice": "auto",
                "stream": not is_continuation
            }
            if is_continuation:
                # Don't send tools for continuation - just get a summary response
                payload.pop("tools", None)
                payload.pop("tool_choice", None)

            if is_continuation:
                # Non-streaming path for tool continuation rounds (more reliable with SiliconFlow)
                try:
                    resp = api_request_with_retry(payload, headers, max_retries=2)
                    if resp is None:
                        yield f"data: {json.dumps({'type': 'error', 'content': 'API不可用'}, ensure_ascii=False)}\n\n"
                        break
                    r = json.loads(resp.read())
                    msg = r['choices'][0]['message']
                    # Stream the content to frontend
                    if msg.get('content'):
                        yield f"data: {json.dumps({'type': 'content', 'content': msg['content']}, ensure_ascii=False)}\n\n"
                    if msg.get('tool_calls'):
                        tool_calls_buf = {i: tc for i, tc in enumerate(msg['tool_calls'])}
                        content_buf = msg.get('content', '') or ''
                    else:
                        full_response = msg.get('content', '') or ''
                        break
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
                    break
            else:
                # Streaming path for first round
                try:
                    resp = api_request_with_retry(payload, headers)
                    if resp is None:
                        yield f"data: {json.dumps({'type': 'error', 'content': 'API暂时不可用'}, ensure_ascii=False)}\n\n"
                        break
                except Exception as e:
                    err_msg = str(e)
                    if "429" in err_msg:
                        yield f"data: {json.dumps({'type': 'error', 'content': '⚠️ API请求太频繁，请等待30秒后重试'}, ensure_ascii=False)}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'error', 'content': err_msg}, ensure_ascii=False)}\n\n"
                    break

            content_buf = ""
            tool_calls_buf = {}

            if not is_continuation and resp:
                # Parse streaming response (first round only)
                try:
                    for line in resp:
                        line = line.decode("utf-8").strip()
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        chunk = json.loads(line[6:])
                        delta = chunk["choices"][0].get("delta", {})
                        if "content" in delta and delta["content"]:
                            content_buf += delta["content"]
                            yield f"data: {json.dumps({'type': 'content', 'content': delta['content']}, ensure_ascii=False)}\n\n"
                        if "tool_calls" in delta:
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                if idx not in tool_calls_buf:
                                    tool_calls_buf[idx] = {"id": tc.get("id", ""), "type": "function", "function": {"name": "", "arguments": ""}}
                                if tc.get("id"):
                                    tool_calls_buf[idx]["id"] = tc["id"]
                                if "function" in tc:
                                    if tc["function"].get("name"):
                                        tool_calls_buf[idx]["function"]["name"] += tc["function"]["name"]
                                    if tc["function"].get("arguments"):
                                        tool_calls_buf[idx]["function"]["arguments"] += tc["function"]["arguments"]
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
                    break

            if not tool_calls_buf:
                full_response = content_buf
                break

            tc_list = [tool_calls_buf[i] for i in sorted(tool_calls_buf.keys())]
            # Ensure tool call IDs are in standard format for SiliconFlow compatibility
            for idx, tc in enumerate(tc_list):
                if not tc["id"].startswith("call_"):
                    tc["id"] = f"call_{idx}{int(time.time())}"
            assistant_msg = {"role": "assistant", "content": None, "tool_calls": tc_list}
            msgs.append(assistant_msg)

            for tc in tc_list:
                fn = tc["function"]["name"]
                try:
                    fargs = json.loads(tc["function"]["arguments"])
                except:
                    fargs = {}
                yield f"data: {json.dumps({'type': 'tool_start', 'tool': fn, 'args': fargs}, ensure_ascii=False)}\n\n"
                result = run_tool(fn, fargs)
                yield f"data: {json.dumps({'type': 'tool_result', 'tool': fn, 'result': json.loads(result)}, ensure_ascii=False)}\n\n"
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

                db = get_db()
                now = time.time()
                db.execute("INSERT INTO messages (session_id,role,content,tool_calls,timestamp) VALUES (?,?,?,?,?)",
                           (sid, "assistant", content_buf or None, json.dumps(tc_list, ensure_ascii=False), now))
                db.execute("INSERT INTO messages (session_id,role,content,tool_result,timestamp) VALUES (?,?,?,?,?)",
                           (sid, "tool", result, tc["id"], now))
                db.commit()
                db.close()
        else:
            full_response = "⚠️ 工具调用轮次已达上限"

        if full_response:
            db = get_db()
            db.execute("INSERT INTO messages (session_id,role,content,timestamp) VALUES (?,?,?,?)",
                       (sid, "assistant", full_response, time.time()))
            db.commit()
            db.close()

        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

@app.route("/api/files", methods=["GET"])
def list_files():
    path = request.args.get("path", WORKSPACE)
    path = resolve_path(path)
    if not os.path.exists(path):
        return jsonify({"error": "路径不存在"})
    if os.path.isfile(path):
        with open(path, 'r', errors='ignore') as f:
            return jsonify({"type": "file", "content": f.read(50000), "path": path})
    entries = []
    for e in sorted(os.listdir(path)):
        if e.startswith('.'):
            continue
        fp = os.path.join(path, e)
        entries.append({"name": e, "type": "dir" if os.path.isdir(fp) else "file", "size": os.path.getsize(fp) if os.path.isfile(fp) else 0})
    return jsonify({"type": "dir", "entries": entries, "path": path})

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "model": MODEL, "workspace": WORKSPACE})

# === HTML Template ===
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>PyClaudeCode</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;--accent:#58a6ff;--green:#3fb950;--red:#f85149;--orange:#d29922;--purple:#bc8cff}
body{font-family:'SF Mono',Menlo,Consolas,monospace;background:var(--bg);color:var(--text);height:100dvh;display:flex;overflow:hidden}
.sidebar{width:260px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0}
.sidebar-header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
.sidebar-header h2{font-size:14px;color:var(--accent)}
.btn{background:var(--border);color:var(--text);border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-family:inherit}
.btn:hover{background:var(--dim)}
.btn-primary{background:var(--accent);color:#000}
.btn-primary:hover{background:#79c0ff}
.session-list{flex:1;overflow-y:auto;padding:8px}
.session-item{padding:10px 12px;border-radius:6px;cursor:pointer;font-size:13px;margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.session-item:hover{background:var(--border)}
.session-item.active{background:var(--accent);color:#000}
.main{flex:1;display:flex;flex-direction:column;min-width:0}
.toolbar{padding:8px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;font-size:12px;color:var(--dim);background:var(--surface)}
.toolbar .model{color:var(--green);font-weight:bold}
.messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:100%;line-height:1.6;font-size:14px;white-space:pre-wrap;word-break:break-word}
.msg-user{color:var(--accent)}
.msg-assistant{color:var(--text)}
.msg pre{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:12px;overflow-x:auto;margin:8px 0;font-size:13px}
.msg code{background:var(--surface);padding:2px 6px;border-radius:3px;font-size:13px}
.tool-block{background:var(--surface);border:1px solid var(--border);border-radius:8px;margin:8px 0;overflow:hidden;font-size:12px}
.tool-header{padding:6px 12px;background:var(--border);display:flex;align-items:center;gap:8px;font-weight:bold}
.tool-header .icon{font-size:14px}
.tool-header .name{color:var(--orange)}
.tool-body{padding:8px 12px;max-height:300px;overflow-y:auto;white-space:pre-wrap;color:var(--dim)}
.tool-body.error{color:var(--red)}
.input-area{padding:12px 16px;border-top:1px solid var(--border);background:var(--surface);display:flex;gap:8px;align-items:flex-end}
.input-area textarea{flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px 12px;border-radius:8px;font-family:inherit;font-size:14px;resize:none;outline:none;min-height:44px;max-height:200px}
.input-area textarea:focus{border-color:var(--accent)}
.typing{color:var(--dim);font-style:italic;font-size:13px;padding:4px 0}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--dim);gap:16px}
.empty h1{font-size:48px}
.empty p{font-size:14px}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block}
.overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:99}
.overlay.show{display:block}
@media(max-width:768px){
  .sidebar{display:none}
  .sidebar.show{display:flex;position:fixed;z-index:100;width:80%;height:100%}
  .mobile-toggle{display:block!important}
  .msg{font-size:15px}
}
.mobile-toggle{display:none;background:none;border:none;color:var(--text);font-size:20px;cursor:pointer;padding:4px 8px}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style>
</head>
<body>
<div class="overlay" id="overlay" onclick="closeSidebar()"></div>
<div class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <h2>⚡ PyClaudeCode</h2>
    <button class="btn btn-primary" onclick="newSession()">+ 新建</button>
  </div>
  <div class="session-list" id="sessionList"></div>
</div>
<div class="main">
  <div class="toolbar">
    <button class="mobile-toggle" onclick="toggleSidebar()">☰</button>
    <span class="status-dot"></span>
    <span class="model">DeepSeek V3</span>
    <span>|</span>
    <span id="workspace">~/workspace</span>
    <span style="flex:1"></span>
    <span id="sessionName" style="color:var(--accent)"></span>
  </div>
  <div class="messages" id="messages">
    <div class="empty">
      <h1>⚡</h1>
      <p>PyClaudeCode — Python版Claude Code</p>
      <p>新建或选择一个会话开始</p>
    </div>
  </div>
  <div class="input-area">
    <textarea id="input" placeholder="输入指令或代码需求... (Shift+Enter换行)" rows="1"></textarea>
    <button class="btn btn-primary" onclick="send()" id="sendBtn">发送</button>
  </div>
</div>
<script>
let currentSession=null,streaming=false;
const input=document.getElementById('input'),msgBox=document.getElementById('messages');
input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
input.addEventListener('input',()=>{input.style.height='auto';input.style.height=Math.min(input.scrollHeight,200)+'px';});
function toggleSidebar(){document.getElementById('sidebar').classList.toggle('show');document.getElementById('overlay').classList.toggle('show');}
function closeSidebar(){document.getElementById('sidebar').classList.remove('show');document.getElementById('overlay').classList.remove('show');}
async function loadSessions(){const r=await fetch('/api/sessions');const s=await r.json();const l=document.getElementById('sessionList');l.innerHTML='';s.forEach(x=>{const d=document.createElement('div');d.className='session-item'+(x.id===currentSession?' active':'');d.textContent=x.name||x.id;d.onclick=()=>{loadSession(x.id);closeSidebar();};d.oncontextmenu=e=>{e.preventDefault();if(confirm('删除此会话?'))deleteSession(x.id);};l.appendChild(d);});}
async function newSession(){const r=await fetch('/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'新会话'})});const s=await r.json();currentSession=s.id;msgBox.innerHTML='';document.getElementById('sessionName').textContent=s.name;loadSessions();input.focus();}
async function loadSession(id){currentSession=id;const r=await fetch(`/api/sessions/${id}/messages`);const msgs=await r.json();msgBox.innerHTML='';msgs.forEach(m=>{if(m.role==='user')appendMsg('user',m.content);else if(m.role==='assistant'){if(m.tool_calls)m.tool_calls.forEach(tc=>appendTool(tc.function.name,JSON.parse(tc.function.arguments||'{}'),null));if(m.content&&!m.tool_calls)appendMsg('assistant',m.content);} });loadSessions();scrollBottom();}
async function deleteSession(id){await fetch(`/api/sessions/${id}`,{method:'DELETE'});if(currentSession===id){currentSession=null;msgBox.innerHTML='';}loadSessions();}
function appendMsg(role,content){const d=document.createElement('div');d.className='msg msg-'+role;d.innerHTML=formatContent(content);msgBox.appendChild(d);scrollBottom();return d;}
function appendTool(name,args,result){const d=document.createElement('div');d.className='tool-block';let bodyClass=result&&result.error?' error':'';let bodyText=result?(typeof result==='string'?result:JSON.stringify(result,null,2)):'执行中...';if(name==='run_command')bodyText='$ '+(args.command||'')+'\n'+bodyText;div.innerHTML=`<div class="tool-header"><span class="icon">🔧</span><span class="name">${name}</span></div><div class="tool-body${bodyClass}">${escapeHtml(bodyText)}</div>`;msgBox.appendChild(d);scrollBottom();return d;}
function formatContent(t){if(!t)return '';t=t.replace(/```(\w*)\n([\s\S]*?)```/g,'<pre><code>$2</code></pre>');t=t.replace(/`([^`]+)`/g,'<code>$1</code>');t=t.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');return t;}
function escapeHtml(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function scrollBottom(){msgBox.scrollTop=msgBox.scrollHeight;}
async function send(){const text=input.value.trim();if(!text||streaming||!currentSession){if(!currentSession)newSession().then(()=>send());return;}streaming=true;input.value='';input.style.height='auto';document.getElementById('sendBtn').disabled=true;appendMsg('user',text);const respDiv=appendMsg('assistant','');const typing=document.createElement('div');typing.className='typing';typing.textContent='● 思考中...';msgBox.appendChild(typing);try{const r=await fetch(`/api/sessions/${currentSession}/chat`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:text})});const reader=r.body.getReader();const decoder=new TextDecoder();let fullText='',buffer='';while(true){const{done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});const lines=buffer.split('\n');buffer=lines.pop()||'';for(const line of lines){if(!line.startsWith('data: ')||line==='data: [DONE]')continue;try{const d=JSON.parse(line.slice(6));if(d.type==='content'){fullText+=d.content;respDiv.innerHTML=formatContent(fullText);scrollBottom();}else if(d.type==='tool_start'){typing.remove();appendTool(d.tool,d.args,null);}else if(d.type==='tool_result'){const blocks=msgBox.querySelectorAll('.tool-block');const last=blocks[blocks.length-1];if(last){const body=last.querySelector('.tool-body');let bodyText=JSON.stringify(d.result,null,2);if(d.result&&d.result.error)body.classList.add('error');body.textContent=bodyText;}}else if(d.type==='error'){fullText+='\n❌ '+d.content;respDiv.innerHTML=formatContent(fullText);}}catch{}}}}catch(e){respDiv.textContent='❌ 连接错误: '+e.message;}typing.remove();document.getElementById('sendBtn').disabled=false;streaming=false;scrollBottom();loadSessions();}
loadSessions();
</script>
</body>
</html>"""

TEMPLATES_DIR = os.path.join(HERE, "templates")
os.makedirs(TEMPLATES_DIR, exist_ok=True)
with open(os.path.join(TEMPLATES_DIR, "index.html"), "w") as f:
    f.write(HTML_TEMPLATE)

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5001))
    print("⚡ PyClaudeCode 启动中...")
    print(f"📍 http://localhost:{port}")
    print(f"📂 工作目录: {WORKSPACE}")
    try:
        ip = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True).stdout.strip()
        if ip:
            print(f"📱 http://{ip}:{port}")
    except:
        pass
    app.run(host="0.0.0.0", port=port, debug=False)
