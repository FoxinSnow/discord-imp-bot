from __future__ import annotations

import logging
from pathlib import Path

import aiohttp
import discord
from discord import app_commands

from .core import (
    DEFAULT_MATCH_COUNT, MAX_MATCH_COUNT, ConfigError, Summary, UserError, is_remote_image,
    load_rules, load_unavailable, local_image, parse_friend_code, render_text, select_rule, summarize,
    validate_match_count,
)
from .stratz import StratzClient

LOGGER = logging.getLogger(__name__)
COMMAND_NAME = "查询受教育程度"
COMMAND_NAMES = (COMMAND_NAME, "查询")


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


class ImpClient(discord.Client):
    def __init__(self, root: Path, stratz_token: str, guild_id: int | None, cache_ttl: int):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.root = root
        self.stratz_token = stratz_token
        self.guild_id = guild_id
        self.cache_ttl = cache_ttl
        self.session: aiohttp.ClientSession | None = None
        self.stratz: StratzClient | None = None
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    def _register_commands(self) -> None:
        @app_commands.rename(friend_code="好友码", match_count="场数")
        @app_commands.describe(
            friend_code="Dota 2 数字好友码（也支持 SteamID64）",
            match_count=f"查询最近多少把有有效 IMP 的对局，不填默认 {DEFAULT_MATCH_COUNT} 把（1–100）",
        )
        @app_commands.checks.cooldown(1, 10, key=lambda interaction: interaction.user.id)
        async def query_education(
            interaction: discord.Interaction,
            friend_code: str,
            match_count: app_commands.Range[int, 1, MAX_MATCH_COUNT] = DEFAULT_MATCH_COUNT,
        ) -> None:
            try:
                account_id = parse_friend_code(friend_code)
                match_count = validate_match_count(match_count)
            except UserError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            assert self.stratz is not None
            try:
                summary = summarize(await self.stratz.fetch_history(account_id, match_count))
                config_path = config_path_for_guild(self.root, interaction.guild_id)
                embed, image_path = make_embed(
                    summary,
                    self.root,
                    self.cache_ttl,
                    config_path=config_path,
                )
            except UserError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            except ConfigError:
                LOGGER.exception("Invalid rule or image configuration")
                await interaction.followup.send("评价或图片配置有误，请让管理员检查 config.json。", ephemeral=True)
                return
            if image_path:
                try:
                    attachment = discord.File(image_path, filename=f"rating{image_path.suffix.lower()}")
                    try:
                        await interaction.followup.send(embed=embed, file=attachment)
                    finally:
                        attachment.close()
                except (OSError, discord.HTTPException):
                    # A file can disappear or fail to upload between validation and send.
                    embed.set_image(url=None)
                    await interaction.followup.send(content="图片暂时发送失败，先显示鉴定结果。", embed=embed)
            else:
                await interaction.followup.send(embed=embed)

        # Both aliases use the same callback and cooldown predicate.
        for name in COMMAND_NAMES:
            self.tree.add_command(app_commands.Command(
                name=name,
                description=f"根据 STRATZ 最近有效 IMP 鉴定受教育程度（默认 {DEFAULT_MATCH_COUNT} 把有效对局）",
                callback=query_education,
            ))

        @self.tree.error
        async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
            if isinstance(error, app_commands.CommandOnCooldown):
                message = f"查询太快了，请 {error.retry_after:.0f} 秒后再试。"
            else:
                LOGGER.error("Command failed", exc_info=(type(error), error, error.__traceback__))
                message = "查询出现异常，请稍后重试或查看机器人运行日志。"
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def setup_hook(self) -> None:
        load_unavailable(self.root / "config.json")
        self.session = aiohttp.ClientSession()
        self.stratz = StratzClient(self.session, self.stratz_token, self.cache_ttl)
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            LOGGER.info("Command synced to guild %d", self.guild_id)
        else:
            await self.tree.sync()
            LOGGER.info("Command synced globally")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != self.guild_id:
            return
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception as exc:
            # Exception bodies can contain remote response details. Keep the
            # failure actionable without printing credentials or raw payloads.
            LOGGER.error(
                "Failed to sync commands on guild join: guild=%d error=%s status=%s code=%s",
                guild.id, type(exc).__name__, getattr(exc, "status", None), getattr(exc, "code", None),
            )
        else:
            LOGGER.info("Commands synced on guild join: guild=%d", guild.id)

    async def on_ready(self) -> None:
        LOGGER.info("Bot connected: %s", self.user)

    async def close(self) -> None:
        if self.session:
            await self.session.close()
        await super().close()
