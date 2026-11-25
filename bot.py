"""Run the bot, check configuration, or preview a fixture without any tokens."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import aiohttp
import discord
from dotenv import load_dotenv

from impbot.core import (
    DEFAULT_MATCH_COUNT, ConfigError, UserError, load_rules, load_unavailable,
    parse_friend_code, summarize, validate_match_count,
)
from impbot.discord_app import ImpClient, make_embed
from impbot.stratz import StratzClient, parse_valid_history

ROOT = Path(__file__).resolve().parent


def env_int(name: str, default: int | None, minimum: int, maximum: int) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须为整数。") from exc
    if not minimum <= number <= maximum:
        raise ConfigError(f"{name} 超出允许范围。")
    return number


def require_token(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"请在本地 .env 中填写 {name}。")
    return value


async def check_api(friend_code: str, match_count: int = DEFAULT_MATCH_COUNT) -> None:
    account_id = parse_friend_code(friend_code)
    async with aiohttp.ClientSession() as session:
        client = StratzClient(session, require_token("STRATZ_API_TOKEN"), cache_ttl=0)
        summary = summarize(await client.fetch_history(account_id, match_count))
    print(f"STRATZ 查询成功：好友代码 {account_id}，请求最近 {match_count} 把有效对局，"
          f"查到 {summary.valid_count} 把有 IMP。")
    print(f"平均 IMP：{summary.average if summary.average is not None else '暂无'}")


def main() -> int:
    # Keep Chinese diagnostics readable when stdout is captured on Windows.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Discord !查询 机器人")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="离线检查配置，不连接 Discord/STRATZ")
    group.add_argument("--check-api", metavar="好友代码", help="用本地 token 验证 STRATZ 实际查询")
    group.add_argument("--preview", metavar="JSON", help="使用离线 STRATZ 格式样例输出 Discord Embed JSON")
    parser.add_argument("--friend-code", default="123456789", help="离线样例中的好友代码")
    parser.add_argument("--matches", type=int, default=DEFAULT_MATCH_COUNT,
                        help=f"查询/预览的有效 IMP 场数，默认 {DEFAULT_MATCH_COUNT}，范围 1–100")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        match_count = validate_match_count(args.matches)
        rules = load_rules(ROOT / "config.json")
        _, unavailable_image = load_unavailable(ROOT / "config.json")
        if args.check:
            images = sum(rule.image is not None for rule in rules)
            print(f"配置正确：{len(rules)} 个分数区间，{images} 条已设置图片。")
            print(f"查不到/隐藏时的备用图片：{'已设置' if unavailable_image else '未设置'}。")
            if images < len(rules):
                print("未设置图片的区间会正常发送文字；图片到位后修改 config.json 即可。")
            return 0
        if args.preview:
            payload = json.loads(Path(args.preview).read_text(encoding="utf-8-sig"))
            summary = summarize(parse_valid_history(payload, parse_friend_code(args.friend_code), match_count))
            embed, _ = make_embed(summary, ROOT)
            print(json.dumps(embed.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.check_api:
            asyncio.run(check_api(args.check_api, match_count))
            return 0
        token = require_token("DISCORD_BOT_TOKEN")
        stratz_token = require_token("STRATZ_API_TOKEN")
        cache_ttl = env_int("CACHE_TTL_SECONDS", 60, 0, 3600)
        client = ImpClient(ROOT, stratz_token, int(cache_ttl))
        client.run(token, log_handler=None)
    except (ConfigError, UserError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except discord.PrivilegedIntentsRequired:
        print("Discord 拒绝 Message Content Intent，请在 Developer Portal 的 Bot 页面开启。", file=sys.stderr)
        return 1
    except discord.LoginFailure:
        print("Discord 登录失败，请检查 .env 中的 DISCORD_BOT_TOKEN。", file=sys.stderr)
        return 1
    except discord.Forbidden:
        print("Discord 拒绝机器人操作，请检查邀请和频道权限。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
