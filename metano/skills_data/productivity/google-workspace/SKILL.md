---
name: google-workspace
description: "Gmail, Calendar, Drive, Docs, Sheets via gws CLI or Python."
version: 1.1.0
author: Nous Research
license: MIT
platforms: [linux, macos, windows]
required_credential_files:
  - path: google_token.json
    description: Google OAuth2 token (created by setup script)
  - path: google_client_secret.json
    description: Google OAuth2 client credentials (downloaded from Google Cloud Console)
metadata:
  hermes:
    tags: [Google, Gmail, Calendar, Drive, Sheets, Docs, Contacts, Email, OAuth]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [himalaya]
---


# Google Workspace

Gmail, Calendar, Drive, Contacts, Sheets, and Docs via Google REST APIs.

> **metano 说明：** 本技能最初依赖 Hermes 自带的 `scripts/setup.py` /
> `scripts/google_api.py` CLI 辅助脚本，metano 发行版**不包含**这些脚本，
> 因此需要手动配置 Google API 访问（OAuth 凭据）。配置完成后，用
> `code_run(language="shell")` 调用 curl 或 Python（`google-api-python-client`）
> 访问各服务 REST API。所有写操作（发邮件、删日历事件、改文档等）必须先与用户确认。

## 手动配置（一次性）

1. 打开 Google Cloud Console：https://console.cloud.google.com/projectselector2/home/dashboard
2. 启用所需 API（API Library → 启用）：
   Gmail API、Google Calendar API、Google Drive API、Google Sheets API、Google Docs API、People API
3. 创建 OAuth 客户端凭据：
   https://console.cloud.google.com/apis/credentials
   Credentials → Create Credentials → OAuth 2.0 Client ID → Application type: "Desktop app"
4. 若应用处于 Testing 状态，把用户账号加为测试用户：
   https://console.cloud.google.com/auth/audience
5. 下载 `client_secret.json`，告知用户保存路径（建议 `~/.claude/metano/credentials/google_client_secret.json`）。

## 获取访问令牌（code_run）

安装依赖并用 Python 完成 OAuth 授权（首次会输出授权 URL，需用户浏览器打开并回贴 code）：

```python
# pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client
import os, json
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = [
  "https://www.googleapis.com/auth/gmail.send",
  "https://www.googleapis.com/auth/calendar",
  "https://www.googleapis.com/auth/drive",
  "https://www.googleapis.com/auth/spreadsheets",
  "https://www.googleapis.com/auth/documents",
]
cred_dir = Path(os.environ.get("METANO_HOME", str(Path.home()/".claude"/"metano")))/"credentials"
cred_dir.mkdir(parents=True, exist_ok=True)
secret = cred_dir/"google_client_secret.json"
token = cred_dir/"google_token.json"

creds = None
if token.exists():
    from google.oauth2.credentials import Credentials
    creds = Credentials.from_authorized_user_file(str(token), SCOPES)
if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
        creds = flow.run_local_server(port=0)
    token.write_text(creds.to_json())
print("AUTHENTICATED")
print("ACCESS_TOKEN=" + creds.token)
```

## 服务调用

拿到 access token 后，用 `code_run(language="shell")` 直接调 REST API：

```bash
TOKEN="<access_token>"

# Gmail：搜索（返回 JSON 数组）
curl -s -H "Authorization: Bearer $TOKEN"   "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=is:unread&maxResults=10"

# Gmail：读取单封邮件
curl -s -H "Authorization: Bearer $TOKEN"   "https://gmail.googleapis.com/gmail/v1/users/me/messages/<MESSAGE_ID>?format=full"

# Calendar：列出未来 7 天事件
curl -s -H "Authorization: Bearer $TOKEN"   "https://www.googleapis.com/calendar/v3/calendars/primary/events?timeMin=$(date -u -Iseconds)&maxResults=25"

# Drive：搜索文件
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://www.googleapis.com/drive/v3/files?q=name contains 'report'&fields=files(id,name,mimeType)"

# Sheets：读取单元格
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://sheets.googleapis.com/v4/spreadsheets/<SHEET_ID>/values/Sheet1!A1:D10"

# Docs：读取文档正文
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://docs.googleapis.com/v1/documents/<DOC_ID>"
```

写操作（发邮件 / 建日历事件 / 上传文件 / 改表格 / 改文档）通过 POST 到对应端点，
但**必须先向用户展示收件人、内容、文件等并取得确认**。

## Gmail 搜索语法速查

| 操作符 | 含义 | 示例 |
|---|---|---|
| `is:unread` | 未读 | `is:unread` |
| `from:` | 发件人 | `from:boss@example.com` |
| `to:` / `cc:` / `bcc:` | 收件人 | `to:me` |
| `subject:` | 主题 | `subject:report` |
| `after:` / `before:` | 日期（YYYY/MM/DD） | `after:2026/01/01` |
| `newer_than:` / `older_than:` | 相对时间 | `newer_than:7d` |
| `has:attachment` | 含附件 | `has:attachment filename:pdf` |
| `in:` | 标签/文件夹 | `in:trash in:spam` |

复杂查询可组合：`from:a@gmail.com has:attachment newer_than:3d`。

## Rules

1. **禁止未经确认执行写操作**：发邮件、创建/删除日历事件、删除 Drive 文件、分享文件、
   修改 Docs/Sheets 前，先展示将执行的内容（收件人、文件 ID、内容、分享角色）并请求批准。
   Drive 删除默认走回收站（可恢复），勿直接 `--permanent`。
2. 首次使用前检查 `~/.claude/metano/credentials/google_token.json` 是否存在；缺失则引导用户完成上面的手动配置。
3. 日历时间必须带时区（ISO 8601 偏移或 UTC `Z`）。
4. 尊重速率限制，批量读取、避免连续快速调用。

## Troubleshooting

| 问题 | 处理 |
|------|------|
| 403 `insufficient permissions` | 缺少 scope，重新授权（删除 token 文件后重跑授权流程） |
| 403 `Access Not Configured` | 对应 Google API 未在 Console 启用 |
| 401 `Invalid Credentials` | token 过期，重新授权 |
| `ModuleNotFoundError` | `code_run` 里先 `pip install google-auth-oauthlib google-api-python-client` |

## 撤销访问

删除 `~/.claude/metano/credentials/google_token.json`，再到 Google 账号的
「第三方应用访问权限」页撤销授权即可。
