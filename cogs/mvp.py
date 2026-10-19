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
# Coordinates for the updated tropical MVP background (1024 x 1536).
NAME_BOX = (165, 850, 860, 950)
AVATAR_BOX = (305, 420, 719, 830)
# Values sit on the template's dotted underline in each information box.
VALUE_BOXES = {
    "runs": (105, 1090, 455, 1150),
    "wickets": (595, 1090, 925, 1150),
    "against": (105, 1285, 435, 1345),
    "awarded for": (620, 1285, 925, 1345),
}


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a scalable italic font. Never silently use Pillow's tiny bitmap font."""
    candidates = [
        # Common Linux hosting paths
        "/usr/share/fonts/opentype/inter/InterDisplay-ExtraBoldItalic.otf",
        "/usr/share/fonts/opentype/inter/InterDisplay-BoldItalic.otf",
        "/usr/share/fonts/opentype/inter/Inter-ExtraBoldItalic.otf",
        "/usr/share/fonts/opentype/inter/Inter-BoldItalic.otf",
        "/usr/share/fonts/opentype/cabin/Cabin-BoldItalic.otf",
        "/usr/share/fonts/truetype/lato/Lato-HeavyItalic.ttf",
        "/usr/share/fonts/truetype/lato/Lato-BoldItalic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
    ] if bold else [
        "/usr/share/fonts/opentype/inter/InterDisplay-MediumItalic.otf",
        "/usr/share/fonts/opentype/inter/Inter-MediumItalic.otf",
        "/usr/share/fonts/opentype/cabin/Cabin-Italic.otf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
    ]
    if os.name == "nt":
        candidates = ([r"C:\Windows\Fonts\arialbi.ttf", r"C:\Windows\Fonts\calibrib.ttf"] if bold else
                      [r"C:\Windows\Fonts\ariali.ttf", r"C:\Windows\Fonts\calibri.ttf"]) + candidates

    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, size=size)
        except (OSError, ValueError):
            continue

    # Newer Pillow supports a scalable default font; older versions do not.
    # If no system font is available, try Pillow's sized default before the
    # legacy bitmap font, which ignores requested sizes and causes tiny text.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        try:
            return ImageFont.truetype("DejaVuSans-BoldOblique.ttf" if bold else "DejaVuSans-Oblique.ttf", size=size)
        except OSError as exc:
            raise RuntimeError(
                "No scalable TrueType/OpenType font found. Install a font such as Inter Bold Italic or DejaVu Sans."
            ) from exc


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, max_height: int,
              start_size: int, minimum: int = 16) -> ImageFont.ImageFont:
    for size in range(start_size, minimum - 1, -1):
        font = _font(size)
        box = draw.textbbox((0, 0), text, font=font, stroke_width=1)
        if box[2] - box[0] <= max_width and box[3] - box[1] <= max_height:
            return font
    return _font(minimum)


def _draw_centered(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str,
                   color: tuple[int, int, int], start_size: int, min_size: int = 16, bold: bool = True) -> None:
    x1, y1, x2, y2 = box
    text = str(text).strip()
    if not text:
        return

    # Keep a generous usable area and calculate using the exact font that is drawn.
    max_width = x2 - x1 - 20
    max_height = y2 - y1 - 6
    font = _font(min_size, bold=bold)
    for size in range(start_size, min_size - 1, -1):
        candidate = _font(size, bold=bold)
        bbox = draw.textbbox((0, 0), text, font=candidate, stroke_width=1)
        if (bbox[2] - bbox[0]) <= max_width and (bbox[3] - bbox[1]) <= max_height:
            font = candidate
            break

    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = x1 + ((x2 - x1) - w) / 2 - bbox[0]
    y = y1 + ((y2 - y1) - h) / 2 - bbox[1]

    # Thin dark shadow and light outline; avoid a thick stroke swallowing small text.
    draw.text((x + 2, y + 2), text, font=font, fill=(10, 42, 73, 190), stroke_width=2,
              stroke_fill=(10, 42, 73, 190))
    draw.text((x, y), text, font=font, fill=color, stroke_width=1,
              stroke_fill=(255, 255, 255, 235))


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

    async def _make_banner(self, name: str, values: dict[str, str], member: Optional[discord.Member], custom_avatar: Optional[bytes] = None) -> io.BytesIO:
        if not BACKGROUND_PATH.exists():
            raise FileNotFoundError(f"Background image not found: {BACKGROUND_PATH}")

        image = Image.open(BACKGROUND_PATH).convert("RGBA")
        draw = ImageDraw.Draw(image, "RGBA")

        # Keep only the player name in the blue strip; no playing-for text is drawn here.
        _draw_centered(draw, NAME_BOX, name, (255, 255, 255), 68, 32)

        # Prefer the image sent in the final prompt; otherwise use the matched Discord member avatar.
        if custom_avatar is not None or member is not None:
            try:
                avatar_bytes = custom_avatar
                if avatar_bytes is None and member is not None:
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
            "awarded for": (13, 145, 69),
        }
        # Larger bold-italic values sit directly over the dotted guide lines.
        for key in ("runs", "wickets", "against", "awarded for"):
            _draw_centered(draw, VALUE_BOXES[key], values[key], colors[key], 64, 28)

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

        name = await self._ask(ctx, "**MVP setup — 1/5**\nEnter the player name (or mention a Discord member):")
        if name is None:
            return

        fields = [
            ("runs", "**MVP setup — 2/5**\nEnter the player's runs:"),
            ("wickets", "**MVP setup — 3/5**\nEnter the player's wickets:"),
            ("against", "**MVP setup — 4/5**\nEnter the team/player they played against:"),
            ("awarded for", "**MVP setup — 5/5**\nEnter what they were awarded for:"),
        ]
        values: dict[str, str] = {}
        for key, prompt in fields:
            answer = await self._ask(ctx, prompt)
            if answer is None:
                return
            values[key] = answer

        await ctx.send("**MVP setup — 6/6**\nUpload the avatar image you want to use as an attachment, or type `skip` to use the matched Discord member's avatar/default silhouette.\n*Type `cancel` to stop.*")
        def avatar_check(message: discord.Message) -> bool:
            return message.author.id == ctx.author.id and message.channel.id == ctx.channel.id and (bool(message.attachments) or message.content.strip().lower() in {"skip", "cancel"})
        try:
            avatar_message = await self.bot.wait_for("message", check=avatar_check, timeout=120)
        except TimeoutError:
            await ctx.send("MVP setup timed out. Run `+setmvp` again.")
            return
        if avatar_message.content.strip().lower() == "cancel":
            await ctx.send("MVP setup cancelled.")
            return
        custom_avatar = None
        if avatar_message.content.strip().lower() != "skip":
            attachment = next((a for a in avatar_message.attachments if a.content_type and a.content_type.startswith("image/")), None)
            if attachment is None:
                await ctx.send("Please upload an image attachment. Run `+setmvp` again.")
                return
            try:
                custom_avatar = await attachment.read()
                Image.open(io.BytesIO(custom_avatar)).verify()
            except Exception:
                await ctx.send("That avatar file could not be opened as an image. Run `+setmvp` again.")
                return

        member = self._find_member(ctx.guild, name)
        display_name = member.display_name if member is not None else name
        try:
            banner = await self._make_banner(display_name, values, member, custom_avatar)
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
