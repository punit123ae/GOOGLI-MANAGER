"""
Kashmir Titans DM extension
---------------------------
Owner-only command:
    +dm <user_id_or_role_id> <message_or_image_url>

Setup:
1. Save this file as dm.py beside bot.py.
2. In your existing bot.py, load this extension once, e.g.:
       await bot.load_extension("dm")
   inside your bot's async setup_hook, OR:
       await bot.load_extension("dm")
   inside an async function before the bot starts.
3. The bot must use discord.ext.commands.Bot with the '+' command prefix.
4. Install discord.py: python -m pip install -U discord.py

Image support: provide an image URL (https://...) as the message, optionally followed
by text, or use an image URL by itself. Discord attachment uploads are not handled
by this command.

Role DMs are sent only to cached members who currently have the role. Discord may
block DMs or rate-limit sends; failures are reported. A confirmation is required.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urlparse

import discord
from discord.ext import commands

OWNER_ID = 1014568928682459198
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _looks_like_image_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
            and (
                parsed.path.lower().endswith(IMAGE_EXTENSIONS)
                or "cdn.discordapp.com" in parsed.netloc.lower()
                or "media.discordapp.net" in parsed.netloc.lower()
                or "images.unsplash.com" in parsed.netloc.lower()
                or "i.imgur.com" in parsed.netloc.lower()
            )
        )
    except Exception:
        return False


def _split_message_and_image(raw: str) -> tuple[str, Optional[str]]:
    """Treat the first token as an image if it looks like an image URL."""
    raw = raw.strip()
    if not raw:
        return "", None

    parts = raw.split(maxsplit=1)
    if _looks_like_image_url(parts[0]):
        image_url = parts[0]
        text = parts[1].strip() if len(parts) > 1 else ""
        return text, image_url

    # A URL without a recognized file extension may still be an image CDN link.
    if len(parts) == 1 and parts[0].startswith(("https://", "http://")):
        return "", parts[0]

    return raw, None


class DMConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        requester_id: int,
        target_kind: str,
        target_id: int,
        content: str,
        image_url: Optional[str],
        recipients: list[discord.Member | discord.User],
    ):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.target_kind = target_kind
        self.target_id = target_id
        self.content = content
        self.image_url = image_url
        self.recipients = recipients
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who started this DM command can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Yes, send DMs", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.finished:
            await interaction.response.send_message(
                "This confirmation has already been used.", ephemeral=True
            )
            return

        self.finished = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="DMs are being sent. Please wait for the final report...",
            view=self,
        )

        success: list[str] = []
        failed: list[str] = []

        # Process sequentially to reduce burst rate limits. Discord may still rate-limit.
        for recipient in self.recipients:
            try:
                embed = None
                if self.image_url:
                    embed = discord.Embed()
                    embed.set_image(url=self.image_url)

                await recipient.send(
                    content=self.content or None,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                success.append(f"• {recipient} (`{recipient.id}`)")
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                failed.append(f"• {recipient} (`{recipient.id}`)")
            except Exception:
                failed.append(f"• {recipient} (`{recipient.id}`)")

            # Small pause between DMs. This is not a guarantee against platform limits.
            await asyncio.sleep(1.0)

        # Discord messages have a 2000-character limit. Split long reports into chunks.
        header = (
            f"**DM PROCESS COMPLETED**\n"
            f"Successful DMs: **{len(success)}**\n"
            f"Failed DMs: **{len(failed)}**\n"
            f"Total attempted: **{len(self.recipients)}**"
        )
        await interaction.followup.send(header, ephemeral=True)

        async def send_list(title: str, entries: list[str]):
            if not entries:
                await interaction.followup.send(f"**{title} (0)**\nNone", ephemeral=True)
                return
            chunk = f"**{title} ({len(entries)})**\n"
            for entry in entries:
                if len(chunk) + len(entry) + 1 > 1850:
                    await interaction.followup.send(chunk, ephemeral=True)
                    chunk = f"**{title} (continued)**\n"
                chunk += entry + "\n"
            if chunk.strip():
                await interaction.followup.send(chunk, ephemeral=True)

        await send_list("Successful DMs", success)
        await send_list("Failed DMs", failed)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if self.finished:
            await interaction.response.send_message(
                "This confirmation has already been used.", ephemeral=True
            )
            return

        self.finished = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="DM operation cancelled. No messages were sent.", view=self
        )


class DMExtension(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="dm")
    async def dm_command(self, ctx: commands.Context, target_id: int, *, raw_message: str):
        """Owner-only: +dm <user_id_or_role_id> <text_or_image_url>"""
        if ctx.author.id != OWNER_ID:
            await ctx.reply("You are not authorized to use this command.", mention_author=False)
            return

        if ctx.guild is None:
            await ctx.reply("Use this command inside the server.", mention_author=False)
            return

        text, image_url = _split_message_and_image(raw_message)
        if not text and not image_url:
            await ctx.reply(
                "Provide text or an image URL.\nUsage: `+dm <user_id_or_role_id> <text_or_image_url>`",
                mention_author=False,
            )
            return

        # Resolve a role first; if no role exists, resolve as a user.
        role = ctx.guild.get_role(target_id)
        target_user: Optional[discord.User | discord.Member] = None

        if role is not None:
            recipients: list[discord.Member | discord.User] = [
                member
                for member in role.members
                if not member.bot
            ]
            target_kind = "role"
            target_label = f"{role.name} (`{role.id}`)"
            if not recipients:
                await ctx.reply(
                    "That role has no cached, non-bot members to DM.",
                    mention_author=False,
                )
                return
        else:
            target_user = ctx.guild.get_member(target_id)
            if target_user is None:
                try:
                    target_user = await self.bot.fetch_user(target_id)
                except (discord.NotFound, discord.HTTPException):
                    target_user = None

            if target_user is None:
                await ctx.reply(
                    "I couldn't find that user ID or role ID in this server.",
                    mention_author=False,
                )
                return

            recipients = [target_user]
            target_kind = "user"
            target_label = f"{target_user} (`{target_user.id}`)"

        # Avoid sending an accidental role-wide DM to a huge group without showing count.
        preview_text = text if text else "(image only)"
        if len(preview_text) > 900:
            preview_text = preview_text[:897] + "..."

        embed = None
        if image_url:
            embed = discord.Embed(title="Image preview")
            embed.set_image(url=image_url)

        confirmation = (
            f"**Are you sure you want to DM this {target_kind}?**\n\n"
            f"**Target:** {target_label}\n"
            f"**Message:** {preview_text}\n"
            f"**This will DM:** {target_label}\n"
            f"**Total members:** {len(recipients)}\n"
        )

        view = DMConfirmView(
            requester_id=ctx.author.id,
            target_kind=target_kind,
            target_id=target_id,
            content=text,
            image_url=image_url,
            recipients=recipients,
        )

        await ctx.reply(
            content=confirmation,
            embed=embed,
            view=view,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DMExtension(bot))
