# yunpancuncuBOT

Telegram 网盘文件分享机器人。用户上传的文件会转存至私有频道，并生成可分享的机器人链接。

## 功能

- 多文件及相册上传、分页获取与个人文件管理
- 指定群组成员验证
- 首页、文件查看页、上传完成页三个独立广告位
- 仅指定 Telegram 用户 ID 可访问的广告管理面板
- PostgreSQL 持久化存储

## 环境变量

复制 `.env.example` 中的变量到部署平台。必填变量如下：

| 变量 | 说明 |
| --- | --- |
| `BOT_TOKEN` | BotFather 提供的机器人 Token |
| `PRIVATE_CHANNEL_ID` | 用于保存文件的私有频道 ID，机器人需为管理员 |
| `DATABASE_URL` | PostgreSQL 连接地址 |
| `REQUIRED_GROUP_ID` | 用户必须加入的群组 ID，机器人需能读取成员状态 |
| `GROUP_INVITE_LINK` | 群组邀请链接 |
| `ADMIN_IDS` | 广告管理员的 Telegram 数字用户 ID；多个 ID 用英文逗号分隔 |
| `PROXY_URL` | 可选的 HTTP/SOCKS 代理地址 |

不知道自己的 Telegram 用户 ID 时，部署后在机器人私聊发送 `/id`。

## 广告管理

1. 将管理员 Telegram 用户 ID 填入 `ADMIN_IDS` 并重启部署。
2. 管理员在机器人私聊发送 `/ads`。
3. 选择广告位，设置文案、链接及按钮文字，然后点击“启用 / 停用”。

广告设置保存在 PostgreSQL 的 `ad_slots` 表中，重启或重新部署不会丢失。广告文案为空时不会展示。跳转链接允许 `http://`、`https://` 和 `tg://`。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python public_share_bot.py
```

部署启动命令已写入 `Procfile`：

```text
worker: python public_share_bot.py
```
