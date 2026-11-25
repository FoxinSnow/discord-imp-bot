from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

from .core import (
    DEFAULT_MATCH_COUNT, MAX_MATCH_COUNT, ConfigError, Summary, UserError, is_remote_image,
    load_rules, load_unavailable, local_image, parse_friend_code, render_text, select_rule, summarize,
    validate_match_count,
)
from .stratz import StratzClient

LOGGER = logging.getLogger(__name__)
COMMAND_PREFIX = "!"
COMMAND_NAME = "查询受教育程度"
REBOOT_COMMAND_NAME = "reboot"
REBOOT_ADMIN_USER_ID = 556537278118232066


def config_path_for_guild(root: Path, guild_id: int | None) -> Path:
    default_path = root / "config.json"
    if guild_id is not None:
        guild_path = root / f"config_{guild_id}.json"
        if guild_path.is_file():
            return guild_path
    return default_path


def make_embed(
    summary: Summary,
    root: Path,
    cache_ttl: int = 60,
    config_path: Path | None = None,
) -> tuple[discord.Embed, Path | None]:
    # Reload on each query so threshold/image changes do not require restart.
    if config_path is None:
        config_path = root / "config.json"
    rules = load_rules(config_path)
    history = summary.history
    embed = discord.Embed(colour=0x5865F2)
    image_path = None
    if summary.average is not None:
        rule = select_rule(rules, summary.average)
        embed.description = discord.utils.escape_mentions(render_text(summary, rule))
        image_reference = rule.image
    else:
        text, image_reference = load_unavailable(config_path)
        embed.description = discord.utils.escape_mentions(text)
    if image_reference:
        if is_remote_image(image_reference):
            embed.set_image(url=image_reference)
        else:
            image_path = local_image(root, image_reference)
            embed.set_image(url=f"attachment://rating{image_path.suffix.lower()}")
    if summary.average is None:
        return embed, image_path
    notes = []
    if len(history.matches) < history.requested_count:
        notes.append(f"只查到 {len(history.matches)}/{history.requested_count} 把有效 IMP 对局")
    if summary.missing_count:
        notes.append(f"{summary.valid_count}/{len(history.matches)} 把有 IMP，平均仅用有效分数")
    if notes:
        embed.set_footer(text="；".join(notes))
    return embed, image_path


class ImpClient(commands.Bot):
    def __init__(self, root: Path, stratz_token: str, cache_ttl: int):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.message_content = True
        super().__init__(
            command_prefix=COMMAND_PREFIX,
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            help_command=None,
        )
        self.root = root
        self.stratz_token = stratz_token
        self.cache_ttl = cache_ttl
        self.session: aiohttp.ClientSession | None = None
        self.stratz: StratzClient | None = None
        self._guild_slash_commands_cleared = False
        self._register_commands()

    def _register_commands(self) -> None:
        @self.command(name=COMMAND_NAME, aliases=("查询",))
        @commands.cooldown(1, 10, commands.BucketType.user)
        async def query_education(
            ctx: commands.Context,
            friend_code: str,
            match_count: int = DEFAULT_MATCH_COUNT,
        ) -> None:
            try:
                account_id = parse_friend_code(friend_code)
                match_count = validate_match_count(match_count)
            except UserError as exc:
                await ctx.reply(str(exc), mention_author=False)
                return
            assert self.stratz is not None
            try:
                summary = summarize(await self.stratz.fetch_history(account_id, match_count))
                guild_id = ctx.guild.id if ctx.guild else None
                config_path = config_path_for_guild(self.root, guild_id)
                embed, image_path = make_embed(
                    summary,
                    self.root,
                    self.cache_ttl,
                    config_path=config_path,
                )
            except UserError as exc:
                await ctx.reply(str(exc), mention_author=False)
                return
            except ConfigError:
                LOGGER.exception("Invalid rule or image configuration")
                await ctx.reply("评价或图片配置有误，请让管理员检查 config.json。", mention_author=False)
                return
            if image_path:
                try:
                    attachment = discord.File(image_path, filename=f"rating{image_path.suffix.lower()}")
                    try:
                        await ctx.reply(embed=embed, file=attachment, mention_author=False)
                    finally:
                        attachment.close()
                except (OSError, discord.HTTPException):
                    # A file can disappear or fail to upload between validation and send.
                    embed.set_image(url=None)
                    await ctx.reply(
                        content="图片暂时发送失败，先显示鉴定结果。",
                        embed=embed,
                        mention_author=False,
                    )
            else:
                await ctx.reply(embed=embed, mention_author=False)

        @self.command(name=REBOOT_COMMAND_NAME)
        @commands.guild_only()
        async def reboot(ctx: commands.Context) -> None:
            if ctx.author.id != REBOOT_ADMIN_USER_ID:
                return
            await ctx.reply("正在拉取更新并重启机器人。", mention_author=False)
            await asyncio.sleep(1)
            # The hosting panel pulls the latest commit when exit code 1 triggers a restart.
            os._exit(1)

    async def setup_hook(self) -> None:
        load_unavailable(self.root / "config.json")
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        self.session = aiohttp.ClientSession()
        self.stratz = StratzClient(self.session, self.stratz_token, self.cache_ttl)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CommandOnCooldown):
            message = f"查询太快了，请 {error.retry_after:.0f} 秒后再试。"
        elif isinstance(error, commands.MissingRequiredArgument):
            message = f"用法：{COMMAND_PREFIX}查询 好友码 [场数]"
        elif isinstance(error, commands.BadArgument):
            message = f"场数必须是 1–{MAX_MATCH_COUNT} 之间的整数。"
        elif isinstance(error, commands.NoPrivateMessage):
            message = "该命令只能在服务器中使用。"
        else:
            LOGGER.error("Command failed", exc_info=(type(error), error, error.__traceback__))
            message = "查询出现异常，请稍后重试或查看机器人运行日志。"
        await ctx.reply(message, mention_author=False)

    async def on_ready(self) -> None:
        if not self._guild_slash_commands_cleared:
            for guild in self.guilds:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
            self._guild_slash_commands_cleared = True
            LOGGER.info("Legacy slash commands removed")
        LOGGER.info("Bot connected: %s", self.user)

    async def close(self) -> None:
        if self.session:
            await self.session.close()
        await super().close()
