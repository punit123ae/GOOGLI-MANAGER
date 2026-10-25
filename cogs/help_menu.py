"""Interactive, public help menu for Googli Manager (discord.py 2.x)."""
import math
import discord
from discord.ext import commands
from discord import app_commands

PER_PAGE = 5
INTERFACE = "Interface"
CATEGORY_ORDER = [
    INTERFACE,
    "Match Management",
    "Team Management",
    "Statistics",
    "Leaderboards & Points Table",
    "Results",
    "Reserves",
    "Groups",
    "Predictions",
    "Draws",
    "MVP",
    "AutoLock",
    "Messaging",
    "Custom Image Commands",
    "Utility & Administration",
]
COG_CATEGORIES = {
    "scheduling": "Match Management",
    "teams1": "Team Management", "teams2": "Team Management", "teams3": "Team Management", "noteam": "Team Management",
    "stats": "Statistics", "lb": "Leaderboards & Points Table", "points_table": "Leaderboards & Points Table",
    "results": "Results", "reserves": "Reserves", "groups": "Groups",
    "predictions": "Predictions", "draws": "Draws", "mvp": "MVP", "autolock": "AutoLock",
    "dm": "Messaging", "custom_image_commands": "Custom Image Commands", "utility": "Utility & Administration",
}


def cog_key(command):
    cog = getattr(command, "cog", None)
    return (getattr(cog, "qualified_name", "") or "").lower().replace(" ", "_")


def pretty_cog_name(key):
    return " ".join(part.capitalize() for part in key.split("_") if part)


def category_for(command):
    key = cog_key(command)
    # Do not hide a loaded cog under a catch-all "Other" category.
    return COG_CATEGORIES.get(key, pretty_cog_name(key) or "General Commands")


def prefix_usage(command):
    args = []
    for param in getattr(command, "clean_params", {}).values():
        if param.kind.name == "KEYWORD_ONLY":
            args.append(f"[{param.name}...]")
        elif param.default is not param.empty:
            args.append(f"[{param.name}]")
        else:
            args.append(f"<{param.name}>")
    return "+" + command.qualified_name + (" " + " ".join(args) if args else "")


def access_for(command):
    notes = []
    for check in getattr(command, "checks", []):
        label = (getattr(check, "__qualname__", "") or getattr(check, "__name__", "")).lower()
        if "is_owner" in label:
            notes.append("Bot owner only")
        elif "has_permissions" in label or "has_guild_permissions" in label:
            notes.append("Requires Discord permissions")
        elif "guild_only" in label:
            notes.append("Server only")
    doc = (getattr(command, "help", None) or getattr(command, "description", None) or "").lower()
    if "owner only" in doc or "bot owner" in doc:
        notes.append("Bot owner only")
    return ", ".join(dict.fromkeys(notes)) or "Use depends on the command's existing restrictions"


def make_entry(name, description, usage, aliases="None", access="Existing command restrictions apply"):
    return {"name": name, "description": description or "No description provided.",
            "usage": usage, "aliases": aliases, "access": access}


def add_slash_command(result, command, parent=""):
    full_name = f"{parent} {command.name}".strip()
    if isinstance(command, app_commands.Group):
        for child in command.commands:
            add_slash_command(result, child, full_name)
        return
    description = getattr(command, "description", None) or "No description provided."
    result.setdefault("Match Management", []).append(
        make_entry("/" + full_name, description, "/" + full_name,
                   "None (slash command)", "Existing slash-command restrictions apply")
    )


class HelpView(discord.ui.View):
    def __init__(self, bot, categories, entries):
        super().__init__(timeout=180)
        self.bot = bot
        self.categories = categories
        self.entries = entries
        self.category = INTERFACE if INTERFACE in categories else categories[0]
        self.page = 0
        self.message = None
        options = [discord.SelectOption(label=c[:100], value=c, default=(c == self.category))
                   for c in categories[:25]]
        self.selector = discord.ui.Select(placeholder="Choose a command category", options=options, row=1)
        self.selector.callback = self.choose_category
        self.add_item(self.selector)
        self.refresh()

    def items(self):
        return self.entries.get(self.category, [])

    def page_count(self):
        return max(1, math.ceil(len(self.items()) / PER_PAGE))

    def refresh(self):
        self.page = max(0, min(self.page, self.page_count() - 1))
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page >= self.page_count() - 1

    def make_embed(self):
        if self.category == INTERFACE:
            embed = discord.Embed(
                title="Googli Manager | Interface",
                description=(
                    "Welcome to the command guide. Use the category menu to browse each part of the bot, "
                    "and use **Previous** / **Next** to move through longer lists.\n\n"
                    "**Created and maintained by:** google.18.18\n"
                    "**What it does:** Helps manage tournament matches, teams, statistics, results, "
                    "points tables, predictions, and server utilities.\n\n"
                    "**How to use it:** Prefix commands start with `+`. Match-management slash commands "
                    "start with `/` and can also be selected from Discord's slash-command menu.\n\n"
                    "Access notes describe existing restrictions; this help menu does not grant permissions."
                ), color=discord.Color.from_rgb(42, 52, 72)
            )
            avatar = getattr(getattr(self.bot, "user", None), "display_avatar", None)
            if avatar:
                embed.set_thumbnail(url=avatar.url)
            embed.add_field(name="Navigation", value="Pick a category below. Each command is numbered and separated for easier reading.", inline=False)
            embed.set_footer(text="GOOGLI MANAGER • COMMAND GUIDE")
            return embed

        items = self.items()
        pages = self.page_count()
        embed = discord.Embed(
            title=f"Googli Manager | {self.category}",
            description=f"Commands in this category  ·  Page {self.page + 1} of {pages}",
            color=discord.Color.from_rgb(42, 52, 72)
        )
        start = self.page * PER_PAGE
        for index, item in enumerate(items[start:start + PER_PAGE], start=start + 1):
            value = (
                f"{item['description']}\n"
                f"**Usage:** `{item['usage']}`\n"
                f"**Aliases:** {item['aliases']}\n"
                f"**Who can use it:** {item['access']}\n\u200b"
            )
            embed.add_field(name=f"{index}. `{item['name']}`", value=value[:1024], inline=False)
        if not items:
            embed.description = "No commands were found in this category."
        embed.set_footer(text=f"{len(items)} command(s) in this category • Use the menu to switch categories")
        return embed

    async def choose_category(self, interaction):
        self.category = self.selector.values[0]
        self.page = 0
        for option in self.selector.options:
            option.default = option.value == self.category
        await self.update(interaction)

    async def update(self, interaction):
        self.refresh()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction, button):
        self.page -= 1
        await self.update(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, row=0)
    async def next(self, interaction, button):
        self.page += 1
        await self.update(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class HelpMenu(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.remove_command("help")

    @commands.command(name="help", aliases=["commands"])
    async def help_menu(self, ctx):
        """Show the public interactive command guide."""
        entries = {INTERFACE: []}
        for command in self.bot.walk_commands():
            if command.hidden or command.name == "help":
                continue
            cat = category_for(command)
            aliases = ", ".join("+" + alias for alias in command.aliases) if command.aliases else "None"
            entries.setdefault(cat, []).append(make_entry(
                "+" + command.qualified_name, command.short_doc or command.help or "No description provided.",
                prefix_usage(command), aliases, access_for(command)
            ))
        for command in self.bot.tree.get_commands():
            add_slash_command(entries, command)

        # Preserve the requested order, then show any additional real cog categories by name.
        categories = [cat for cat in CATEGORY_ORDER if cat in entries]
        categories.extend(sorted(cat for cat in entries if cat not in categories and cat != "Other"))
        for cat, items in entries.items():
            items.sort(key=lambda item: item["name"].lower())
        view = HelpView(self.bot, categories, entries)
        view.message = await ctx.reply(embed=view.make_embed(), view=view, mention_author=False)


async def setup(bot):
    await bot.add_cog(HelpMenu(bot))
