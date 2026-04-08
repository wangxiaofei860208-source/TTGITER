# ⚡ PyClaudeCode — Python版Claude Code

本地部署的AI编程Agent，支持手机/外网远程访问。

## 快速启动

```bash
pip install -r requirements.txt
cp .env.example .env
python3 app.py
```

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| DEEPSEEK_API_KEY | ✅ | API密钥 |
| MODEL | ❌ | 默认 DeepSeek-V3 |
| PORT | ❌ | 默认 5001 |
| AUTH_TOKEN | ❌ | API认证token |

## 功能

- 7个编程工具 + Agentic工具循环
- 流式SSE输出
- 多会话管理 + 手机适配UI
- 命令白名单 + SSRF防护 + 限流 + 429自动重试
