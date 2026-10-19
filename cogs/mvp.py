from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Put mvp.py and mvp_background.png in the same cogs folder.
BACKGROUND_PATH = Path(__file__).with_name("mvp_background.png")

# Coordinates are designed for the supplied 1024 x 1536 Kashmir Titans banner.
NAME_BOX = (170, 864, 854, 946)
AVATAR_BOX = (405, 456, 619, 670)
VALUE_BOXES = {
    "runs": (80, 1070, 480, 1147),
    "wickets": (542, 1070, 942, 1147),
    "against": (76, 1240, 327, 1320),
    "playing for": (391, 1240, 640, 1320),
    "awarded for": (704, 1240, 953, 1320),
}


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = []
    if os.name == "nt":
        candidates += [r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf"]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, max_height: int,
              start_size: int, minimum: int = 16) -> ImageFont.ImageFont:
    for size in range(start_size, minimum - 1, -1):
        font = _font(size)
        box = draw.textbbox((0, 0), text, font=font, stroke_width=1)
        if box[2] - box[0] <= max_width and box[3] - box[1] <= max_height:
            return font
    return _font(minimum)


def _draw_centered(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str,
                   color: tuple[int, int, int], start_size: int, min_size: int = 16) -> None:
    x1, y1, x2, y2 = box
    font = _fit_font(draw, text, x2 - x1 - 22, y2 - y1 - 8, start_size, min_size)
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = x1 + ((x2 - x1) - w) / 2 - bbox[0]
    y = y1 + ((y2 - y1) - h) / 2 - bbox[1]
    # A subtle dark outline keeps text readable over the bright template.
    draw.text((x + 2, y + 3), text, font=font, fill=(10, 42, 73, 110), stroke_width=2, stroke_fill=(10, 42, 73, 90))
    draw.text((x, y), text, font=font, fill=color, stroke_width=1, stroke_fill=(255, 255, 255, 170))


def _avatar_from_member(member: Optional[discord.Member], size: tuple[int, int]) -> Optional[Image.Image]:
    if member is None:
        return None
    try:
        with io.BytesIO() as buffer:
            # Prefer the user's actual Discord avatar; display_avatar also handles defaults.
            # The URL is fetched asynchronously before this helper is called.
            pass
        return None
    except Exception:
        return None


class MVP(commands.Cog):
    """Owner-only interactive MVP banner generator."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _ask(self, ctx: commands.Context, prompt: str) -> Optional[str]:
        await ctx.send(prompt + "\n*Type `cancel` to stop.*")

        def check(message: discord.Message) -> bool:
            return message.author.id == ctx.author.id and message.channel.id == ctx.channel.id

        try:
            message = await self.bot.wait_for("message", check=check, timeout=120)
        except TimeoutError:
            await ctx.send("MVP setup timed out. Run `+setmvp` again.")
            return None
        answer = message.content.strip()
        if answer.lower() == "cancel":
            await ctx.send("MVP setup cancelled.")
            return None
        if not answer:
            await ctx.send("That answer can't be empty. Run `+setmvp` again.")
            return None
        return answer

    def _find_member(self, guild: Optional[discord.Guild], value: str) -> Optional[discord.Member]:
        if guild is None:
            return None
        match = re.fullmatch(r"<@!?([0-9]+)>", value)
        if match:
            return guild.get_member(int(match.group(1)))
        if value.isdigit():
            member = guild.get_member(int(value))
            if member:
                return member
        lowered = value.casefold().lstrip("@")
        for member in guild.members:
            if member.name.casefold() == lowered or member.display_name.casefold() == lowered:
                return member
        return None

    async def _make_banner(self, name: str, values: dict[str, str], member: Optional[discord.Member]) -> io.BytesIO:
        if not BACKGROUND_PATH.exists():
            raise FileNotFoundError(f"Background image not found: {BACKGROUND_PATH}")

        image = Image.open(BACKGROUND_PATH).convert("RGBA")
        draw = ImageDraw.Draw(image, "RGBA")

        # Place the player's name in the blue name strip.
        _draw_centered(draw, NAME_BOX, name, (255, 255, 255), 48, 22)

        # Overlay a rounded avatar crop in the central portrait area when a Discord member was found.
        if member is not None:
            try:
                avatar_bytes = await member.display_avatar.replace(size=256, static_format="png").read()
                avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
                x1, y1, x2, y2 = AVATAR_BOX
                target_size = (x2 - x1, y2 - y1)
                avatar = ImageOps.fit(avatar, target_size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.35))
                mask = Image.new("L", target_size, 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.rounded_rectangle((0, 0, target_size[0] - 1, target_size[1] - 1), radius=28, fill=255)
                image.alpha_composite(Image.composite(avatar, Image.new("RGBA", target_size, (0, 0, 0, 0)), mask), (x1, y1))
                draw = ImageDraw.Draw(image, "RGBA")
                draw.rounded_rectangle((x1, y1, x2, y2), radius=28, outline=(255, 255, 255, 245), width=5)
            except Exception:
                # Keep the template's built-in silhouette if the avatar cannot be fetched.
                pass

        colors = {
            "runs": (0, 126, 190),
            "wickets": (222, 26, 91),
            "against": (209, 112, 0),
            "playing for": (13, 145, 69),
            "awarded for": (112, 35, 174),
        }
        for key, value in values.items():
            _draw_centered(draw, VALUE_BOXES[key], value, colors[key], 38 if key in ("runs", "wickets") else 28, 15)

        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        output.seek(0)
        return output

    @commands.command(name="setmvp")
    @commands.guild_only()
    async def set_mvp(self, ctx: commands.Context) -> None:
        # Uses Discord's configured application owner/team owner check; no hard-coded user ID.
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("Only the bot owner can use this command.")
            return

        name = await self._ask(ctx, "**MVP setup — 1/6**\nEnter the player name (or mention a Discord member):")
        if name is None:
            return

        fields = [
            ("runs", "**MVP setup — 2/6**\nEnter the player's runs:"),
            ("wickets", "**MVP setup — 3/6**\nEnter the player's wickets:"),
            ("against", "**MVP setup — 4/6**\nEnter the team/player they played against:"),
            ("playing for", "**MVP setup — 5/6**\nEnter the team they were playing for:"),
            ("awarded for", "**MVP setup — 6/6**\nEnter what they were awarded for:"),
        ]
        values: dict[str, str] = {}
        for key, prompt in fields:
            answer = await self._ask(ctx, prompt)
            if answer is None:
                return
            values[key] = answer

        member = self._find_member(ctx.guild, name)
        try:
            banner = await self._make_banner(name, values, member)
        except FileNotFoundError as exc:
            await ctx.send(f"Couldn't find the MVP background. Put `mvp_background.png` beside `mvp.py` in the `cogs` folder.\n`{exc}`")
            return
        except Exception as exc:
            await ctx.send(f"Couldn't generate the MVP image: `{type(exc).__name__}: {exc}`")
            return

        # Final response contains only the generated image attachment.
        await ctx.send(file=discord.File(banner, filename="kashmir_titans_mvp.png"))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MVP(bot))
