# Discord 受教育程度查询机器人

在 Discord 输入 `!查询 123456789` 或 `!查询受教育程度 123456789`，机器人默认从 STRATZ 查询该玩家最近 20 把有有效 IMP 的对局，计算平均 IMP，再按区间选择指定图片。两条命令功能相同，也可以在好友码后添加可选的场数，范围为 1–100：

```text
!查询受教育程度 123456789
!查询 123456789
!查询受教育程度 123456789 5
!查询 123456789 5
```

命令各部分使用空格分隔；不填场数时查询最近 20 把有效对局。例如 `!查询 123456789 5` 表示最近 5 把有 IMP 的对局；中间没有 IMP 的比赛会跳过。好友码为必填的数字字符串。回复采用固定文案：

```text
目标玩家近期平均IMP为：+10.00
一眼盯真 鉴定为
【对应图片】
```

各区间使用同一文案，只替换平均值与图片。正常结果不显示额外标题、学历名称或对局明细；只有可查到的有效对局不足所选场数时，页脚说明实际样本数量。

查不到玩家、没有公开可查战绩或没有任何有效 IMP 时，回复固定为“神秘嘉宾来袭”，并显示抱猪图片。该回复不附带平均分或数据页脚。网络、token 或接口本身出错时仍显示查询错误提示。

Python 3.10+，使用 `discord.py` 和 STRATZ GraphQL。已接入用户提供的四张评分图片，从低到高依次为“苯猪 → 猪bee → 不赖猪 → 好猪好猪”，以及查不到时的抱猪备用图片。

应用当前名称为“IceTeee”，机器人账号为“一眼盯真”（ID `1559419206779412560`）。默认场数为最近 20 把有效对局。维护者（Discord 用户 ID `556537278118232066`）在 push 更新后可执行 `!reboot`，由托管面板拉取最新提交并重启 Bot。

## 1. 准备两个 token

1. 打开 [Discord Developer Portal](https://discord.com/developers/applications)，新建 Application，在 Bot 页面取得 **Bot Token**。
2. 在 Bot 页面启用 **Message Content Intent**。在 Installation 页面关闭 **User Install**，启用 **Guild Install（服务器安装）**；scope 需包含 `bot`，并给机器人 `View Channels`、`Send Messages`、`Embed Links`、`Attach Files` 权限。
3. 在 Discord 开启开发者模式，右键服务器复制服务器 ID。
4. 登录 [STRATZ API](https://stratz.com/api)，用 Steam 登录后取得 API token。
5. 复制 `.env.example` 为 `.env`，在本地填写：

```dotenv
DISCORD_BOT_TOKEN=你的Discord机器人token
STRATZ_API_TOKEN=你的STRATZ_API_token
CACHE_TTL_SECONDS=60
```

Token 填进本地文件即可。本机测试用 token 保存在本地 `.env`，交付 ZIP 不包含 `.env`；在其他机器解压后需自行填写。前缀命令需要 Message Content Intent。

现有机器人重新安装到“摸渔”可使用[固定服务器安装链接](https://discord.com/oauth2/authorize?client_id=1559419206779412560&scope=bot&permissions=52224&guild_id=821315791059943424&disable_guild_select=true&integration_type=0)，它包含所需权限，并固定选择“摸渔”。

前缀命令无需向 Discord 同步命令列表；新版首次启动会清除应用以前注册的 Slash Command。Bot 上线后即可在有权限读写的频道使用。机器人通过 Discord Gateway 连接，不需要配置 Interactions Endpoint URL 或公网 Web 服务器。

## 2. Windows 启动

在解压后的机器人目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

脚本会查找已安装的 Python 3.10+、创建 `.venv` 并安装依赖。若没有 `.env`，先创建空模板并退出；填写后再次运行。看到 `Bot connected` 后，在有机器人权限的频道输入 `!查询 好友码 [场数]`。

也可以手动启动（`py -3` 应指向 Python 3.10+）：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# 在编辑器中填写 .env 后：
.\.venv\Scripts\python.exe bot.py
```

Linux / macOS 可用 `python3 -m venv .venv`、`.venv/bin/python -m pip install -r requirements.txt`、`.venv/bin/python bot.py`。运行进程需要保持开启。本机后台测试进程依赖这台电脑持续运行，关机或休眠后机器人会离线；后续可部署到持续在线的云服务器。

## 3. 设置分数与图片

修改 `config.json`。四张图片已按用户指定的顺序接入，标签用于识别图片配置，不会显示在回复里；分数区间可以改：

| 平均 IMP | 图片 | 文件 |
|---|---|---|
| `分数 ≤ -10` | 苯猪 | `assets/苯猪_1.webp` |
| `-10 < 分数 < 0` | 猪bee | `assets/猪bee.webp` |
| `0 ≤ 分数 ≤ 10` | 不赖猪 | `assets/不赖猪.jpg` |
| `分数 > 10` | 好猪好猪 | `assets/好猪好猪.webp` |

第一条 `min` 必须为 `null`，其余严格递增。`include_min` 默认为 `true`，表示包含该最低分；设为 `false` 时只有严格大于 `min` 才进入该档，等于时归前一档。边界已补齐：−10 归苯猪，0 和 10 归不赖猪。判档使用未四舍五入的平均值，显示保留两位小数。

四张原图已放在 `assets/` 中。其中苯猪、猪bee 和好猪好猪原文件实际为 WebP，已仅改扩展名，图像内容不变。对应规则示例：

```json
{
  "min": 10,
  "include_min": false,
  "title": "好猪好猪",
  "text": "目标玩家近期平均IMP为：{average}\n一眼盯真 鉴定为",
  "image": "assets/好猪好猪.webp"
}
```

本地图片支持 PNG、JPG、JPEG、GIF、WEBP，单张最大 8 MiB。也可以将 `image` 写成公开的 HTTPS 图片直链，Discord 会展示远程图片；需要直链可直接访问。未设置图片时保持 `null`。

所有规则的 `text` 保持相同固定文案，其中 `{average}` 自动填入平均 IMP。配置在每次查询时重新读取，修改图片或区间后无需重启。

查不到时的备用配置为 `config.json` 顶层的 `unavailable`：

```json
"unavailable": {
  "text": "神秘嘉宾来袭",
  "image": "assets/抱猪_1.jpg"
}
```

## 4. 验证

离线检查配置，不需要 token：

```powershell
.\.venv\Scripts\python.exe bot.py --check
.\.venv\Scripts\python.exe bot.py --preview examples\sample.json --friend-code 123456789
.\.venv\Scripts\python.exe bot.py --preview examples\sample.json --friend-code 123456789 --matches 5
.\.venv\Scripts\python.exe bot.py --preview examples\unavailable.json --friend-code 123456789
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`examples/sample.json` 有 20 条虚构对局，其中 19 条有有效 IMP，用来查看固定文案的 Discord Embed 输出和缺失 IMP 的处理。默认请求最近 20 条有效对局，样例只能提供 19 条，结果按这 19 条平均，并说明数量不足；添加 `--matches 5` 时取最近 5 条有效对局。命令行的 `--matches` 同样接受 1–100 的整数，默认值为 20，`--preview` 和 `--check-api` 均按最近有效场数计算。填写 STRATZ token 后，可验证真实查询：

```powershell
.\.venv\Scripts\python.exe bot.py --check-api 你的好友码
.\.venv\Scripts\python.exe bot.py --check-api 你的好友码 --matches 5
```

此命令只连接 STRATZ，不登录 Discord，不发消息。本次已使用 token 实网验证最近 5 把和 20 把有效对局查询；更换 token 或部署环境后仍可用此命令检查。STRATZ 的实时 schema 需要 token 才能访问。若遇到 GraphQL 查询错误，可在 [官方 GraphiQL](https://api.stratz.com/graphiql/) 检查 `impbot/stratz.py` 中的查询。

## 数据计算与异常

- 查询 STRATZ 可查到的最近 N 把有有效 IMP 的对局，默认 N 为 20，场数可设为 1–100。不限定天梯或匹配模式，不增加 `isParsed` 筛选。查询使用 `matches(request: {take: N, orderBy: DESC, minImp: -32768})`，由 STRATZ 跳过无 IMP 的对局并按时间从新到旧返回。
- `0` 是有效分数；`null` / 缺少 IMP 会跳过，不补成 0。比如最新 5 场原始比赛中只有 1 场有 IMP，机器人会继续使用更早的有效对局，取最近 5 场有效分数计算平均。客户端及离线预览同样过滤无效 IMP，并按时间从新到旧取 N 场。
- STRATZ 返回玩家为 `null`、比赛列表为 `null` 或空列表时，视为没有公开可查数据，使用备用回复。这些状态可能来自玩家未收录或战绩隐藏，机器人不据此断言具体原因。响应缺少预期字段、类型不正确或有 GraphQL 错误时仍提示接口异常。
- 可查到的有效对局不足所选场数时，按实际有效场数平均并标明数量，例如请求 5 场只查到 3 场，则显示 `3 / 5` 的样本说明。一场有效分数都没有时显示“神秘嘉宾来袭”和抱猪图片。
- 仅使用目标好友码对应的玩家分数，排除其他九位玩家。对有效对局按开局时间倒序整理，不在回复中列出对局明细。
- 同一好友码、同一场数的成功查询默认缓存 60 秒；查询不同场数使用各自的缓存。同一用户每 10 秒可发起一次查询。多人同时查同一好友码且场数相同时合并请求。接口错误不作为成功结果缓存。
- 遇到 401/403、429、超时或 GraphQL 错误，返回相应提示，不用不完整的错误响应生成分数。

Dota 2 需要开启“公开比赛数据 / Expose Public Match Data”。STRATZ 数据受比赛公开状态、录像可用性和解析进度影响，所以这里的“最近 N 把”指 STRATZ 可查到且已有 IMP 的最近 N 场对局，可能滞后于游戏客户端记录。[官方公开数据说明](https://github.com/STRATZ-Esports/knowledge-base/issues/1)、[比赛缺失说明](https://github.com/STRATZ-Esports/knowledge-base/issues/24)

STRATZ 官方将 IMP 定义在 −100 至 +100 区间，正负表现评分可直接作为规则阈值。[IMP 说明](https://github.com/STRATZ-Esports/knowledge-base/issues/21)

命令前缀为英文半角感叹号 `!`，不接受全角 `！`。
