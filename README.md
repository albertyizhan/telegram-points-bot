# Telegram 多群积分机器人

一个轻量的 Telegram 群组积分机器人：聊天、签到获得积分，管理员负责调整规则和成员积分。

## 1. 安装

服务器需要 Python 3.9+。从 GitHub 拉取并安装依赖：

```bash
git clone git@github.com:albertyizhan/telegram-points-bot.git
cd telegram-points-bot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod +x manage.sh
```

编辑 `.env`：

```dotenv
BOT_TOKEN=从 BotFather 获取的 Token
OWNER_ID=总管理员的 Telegram 数字 ID
# DB_PATH=points.db
```

启动和管理：

```bash
./manage.sh start       # 后台启动
./manage.sh status      # 查看状态和最近日志
./manage.sh stop        # 正常关闭
./manage.sh restart     # 重启
./manage.sh update      # 拉取 GitHub 更新、安装依赖并重启
```

`manage.sh` 会自动进入项目目录并读取 `.env`。`run` 命令可前台运行用于调试；进程卡住时使用 `./manage.sh stop --force`。

数据库使用 SQLite，已启用 WAL、正常同步和写入等待，适合每天几千条群消息的小型服务。请使用 `./manage.sh stop` 正常关闭后再复制或迁移数据库；不要单独复制 `points.db-wal` 或 `points.db-shm` 文件。

## 2. 激活群组

1. 总管理员私聊机器人发送 `/start`，点击“生成群组激活码”。
2. 把激活码发给群主。
3. 群主在目标群发送 `/activate 激活码`。

激活码第一次使用后会绑定该 Telegram 用户。每个用户最多激活 3 个群；机器人拥有者不受此限制。停用群组只停止积分统计，不删除已有数据。

## 3. 日常使用

群成员可以使用：

```text
/score       查看自己的总积分、今日聊天积分和签到状态
/checkin     签到并领取每日奖励
/rank        查看累计积分排名
/today       查看今日积分排名
```

群管理员可以使用：

```text
/addpoints 10 @username   增加成员积分
/subpoints 10 @username   扣除成员积分
```

也可以回复成员消息后发送 `/addpoints 10` 或 `/subpoints 10`。机器人只会给已经在群里发过言的成员调整积分。

## 4. 私聊管理

私聊机器人发送 `/start`，先选择群组。群组主页直接提供：

- 语言
- 地区（时区）
- 成员积分
- 今日排名和累计排名

进入群组后，管理员还可以：

- 积分规则
- 自定义命令
- 积分调整记录

语言和时区按群组分别保存。每日结算时区会影响签到和每日聊天积分上限的日期边界。

## 测试

```bash
.venv/bin/python -m unittest -v
```

测试只覆盖本地 SQLite 规则，不访问 Telegram 网络。
