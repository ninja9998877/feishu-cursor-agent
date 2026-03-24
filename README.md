# feishu-cursor-agent

在本机用**飞书长连接**接收消息，转发给 **Cursor CLI**（`agent`）执行，并通过**飞书卡片实时更新**执行状态和结果。

## 前置条件

1. Python 3.10+
2. 已安装并登录 [Cursor CLI](https://cursor.com/docs/cli/overview)，且 `agent` 在 PATH 中（PowerShell 执行 `where.exe agent` 能看到路径）
3. 飞书自建应用已开启机器人，事件订阅为 **使用长连接接收事件**，并已订阅 `im.message.receive_v1`
4. 权限至少包含：`im:message`、`im:message:send_as_bot`

## 安装

```powershell
cd D:\Project\feishu-cursor-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# 编辑 .env，填入 FEISHU_APP_ID / FEISHU_APP_SECRET 等
python main.py
```

控制台出现连接成功日志后，在飞书里对机器人发消息即可（群聊建议 **@机器人**）。

## 用法说明

| 消息 | 行为 |
|------|------|
| `/help` | 显示帮助 |
| `/cd <绝对路径>` | 绑定当前会话的工作目录（仅内存，重启失效） |
| `/pwd` | 查看当前会话工作目录 |
| 其他文本 | 作为 `agent -p "..."` 的提示词执行 |

## 卡片模式

- 收到任务后先发一张互动卡片（状态：已接收）
- 执行过程中按间隔更新同一卡片（状态：执行中）
- 完成或失败后更新为最终状态并展示结果摘要

## 安全提示

- 本工具会在本机执行 Cursor Agent，**等价于你在终端里跑 agent**，请勿把机器人加到不可信群，务必配置 `ALLOWED_*` 白名单（按需）。
- 长连接模式下事件回调仍需在 **约 3 秒内返回**；本实现将重活放到后台线程，避免飞书重复推送。
- 严禁提交 `.env`、日志、凭证到仓库；请阅读 [SECURITY.md](SECURITY.md)。

## 需要你提供给维护者的信息

- `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（勿提交到 git）
- 是否群聊必须 @：若是，提供 `FEISHU_BOT_OPEN_ID`
- 可选：`ALLOWED_CHAT_IDS` / `ALLOWED_SENDER_OPEN_IDS`

## 开源协作

- 贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)
- 安全问题上报见 [SECURITY.md](SECURITY.md)
- 协议见 [LICENSE](LICENSE)
