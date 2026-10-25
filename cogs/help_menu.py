"""Interactive, category-based help menu for Googli Manager."""
from __future__ import annotations

import inspect
from typing import Any

import discord
from discord.ext import commands

CATEGORY_NAMES = {
    "Scheduling": "Match Management",
    "Stats": "Player Statistics",
    "LeaderboardCog": "Leaderboards",
    "PointsTable": "Points Table",
    "TeamRegistration": "Team Management",
    "LiveDraws": "Live Draws",
    "CustomImageCommands": "Custom Image Commands",
    "MVP": "MVP",
    "ResultsCog": "Match Results",
    "DMExtension": "DM & Echo",
    "NoTeam": "No-Team Management",
    "Reserves": "Reserves & Matchups",
    "Groups": "Groups & Fixtures",
    "Utility": "Administration & Utility",
    "AutoLock": "AutoLock",
    "Predictions": "Predictions",
    "PointsTable": "Points Table",
}

CATEGORY_DESCRIPTIONS = {
    "Match Management": "Slash commands for scheduling, viewing, and cancelling matches.",
    "Player Statistics": "Import, view, and manage match statistics.",
    "Leaderboards": "Leaderboard and ranking commands.",
    "Points Table": "Points table commands.",
    "Team Management": "Team registration, rosters, roles, and team settings.",
    "Live Draws": "Configure and run live draws.",
    "Custom Image Commands": "Create and manage custom image commands.",
    "MVP": "MVP-related commands.",
    "Match Results": "Post and configure match results.",
    "DM & Echo": "Owner-restricted direct-message and echo utilities.",
    "No-Team Management": "No-team tracking and related controls.",
    "Reserves & Matchups": "Reserve players and matchup management.",
    "Groups & Fixtures": "Groups, formats, and fixture generation.",
    "Administration & Utility": "Bot administration, command whitelist, backups, and utilities.",
    "AutoLock": "Automatically lock configured channels after match statistics appear.",
    "Predictions": "Prediction-related commands.",
}


def _source_for(command: commands.Command) -> str:
    try:
        return inspect.getsource(command.callback).lower()
    except (OSError, TypeError):
        return ""


def _access_label(command: commands.Command) -> str:
    src = _source_for(command)
    name = command.name.lower()
    if "owner_or_whitelisted" in src:
        return "Bot owner or a role whitelisted for this command"
    if "_command_allowed(" in src or "_allowed(" in src or "_feature_blocked(" in src:
        return "Restricted by this command's built-in access checks"
    checks = " ".join(getattr(check, "__qualname__", repr(check)) for check in command.checks).lower()
    if "is_owner" in checks or "owner" in checks:
        return "Bot owner only (unless the command's checks say otherwise)"
    if "administrator" in checks or "has_permissions" in checks or "has_guild_permissions" in checks:
        return "Server administrators / members with the required permissions"
    if any(term in src for term in ("captain_id", "only an approved team captain", "team captain")):
        return "Eligible team captain or user meeting the command's team rules"
    if command.checks:
        return "Users who pass this command's built-in checks"
    if name in {"help", "lb", "leaderboard", "leaderboards", "stats", "teamlist", "teaminfo", "reservelist", "reservestatus", "match_list", "match_info"}:
        return "Everyone (subject to the command's built-in rules)"
    return "Check the command's own rules; additional restrictions may apply"


def _prefix_entry(command: commands.Command) -> dict[str, str]:
    usage = f"+{command.qualified_name}"
    if command.signature:
        usage += f" {command.signature}"
    description = (command.short_doc or command.help or "No description provided.").strip().split("\n", 1)[0]
    aliases = ", ".join(f"`+{alias}`" for alias in command.aliases) if command.aliases else "None"
    return {
        "sort": command.name.lower(),
        "usage": usage,
        "description": description[:240],
        "aliases": aliases,
        "access": _access_label(command),
    }


def _slash_entry(command: discord.app_commands.Command[Any, ..., Any]) -> dict[str, str]:
    usage = f"/{command.qualified_name}"
    params = []
    for parameter in getattr(command, "parameters", []):
        params.append(f"<{parameter.name}>" if parameter.required else f"[{parameter.name}]")
    if params:
        usage += " " + " ".join(params)
    description = (getattr(command, "description", None) or "No description provided.").strip()
    try:
        src = inspect.getsource(command.callback).lower()
    except (OSError, TypeError, AttributeError):
        src = ""
    if "command_allowed(" in src:
        access = "Bot owner or a role whitelisted for this exact command"
    elif "guild_permissions.administrator" in src or "has_permissions" in src:
        access = "Server administrators / members with the required permissions"
    else:
        access = "Command-specific restrictions may apply"
    return {
        "sort": command.name.lower(),
        "usage": usage,
        "description": description[:240],
        "aliases": "None — slash commands do not use prefix aliases",
        "access": access,
    }


class HelpCategorySelect(discord.ui.Select):
    def __init__(self, view: "HelpMenuView"):
        self.help_view = view
        options = []
        for category in view.categories[:25]:
            options.append(discord.SelectOption(
                label=category[:100],
                value=category,
                description=CATEGORY_DESCRIPTIONS.get(category, f"Commands in {category}")[:100],
                default=category == view.category,
            ))
        super().__init__(placeholder="Choose a command category…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.help_view.category = self.values[0]
        self.help_view.page = 0
        self.help_view.refresh_select()
        await interaction.response.edit_message(embed=self.help_view.make_embed(), view=self.help_view)


class HelpMenuView(discord.ui.View):
    PAGE_SIZE = 6

    def __init__(self, ctx: commands.Context, categories: dict[str, list[dict[str, str]]]):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.categories_data = categories
        self.categories = list(categories.keys())[:25]
        self.category = self.categories[0] if self.categories else "Help"
        self.page = 0
        self.message = None
        self.add_item(HelpCategorySelect(self))
        self._sync_buttons()

    def refresh_select(self):
        for item in self.children:
            if isinstance(item, HelpCategorySelect):
                self.remove_item(item)
                break
        self.insert_item(0, HelpCategorySelect(self))
        self._sync_buttons()

    def _sync_buttons(self):
        total = len(self.categories_data.get(self.category, []))
        pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == "gm_help_prev":
                    item.disabled = self.page <= 0
                elif item.custom_id == "gm_help_next":
                    item.disabled = self.page >= pages - 1

    def make_embed(self) -> discord.Embed:
        entries = self.categories_data.get(self.category, [])
        pages = max(1, (len(entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        start = self.page * self.PAGE_SIZE
        shown = entries[start:start + self.PAGE_SIZE]
        embed = discord.Embed(
            title=f"Googli Manager • {self.category}",
            description=CATEGORY_DESCRIPTIONS.get(self.category, "Commands in this category."),
            color=discord.Color.from_rgb(55, 150, 255),
        )
        if not shown:
            embed.add_field(name="No commands found", value="There are no visible commands in this category.", inline=False)
        for entry in shown:
            value = (
                f"{entry['description']}\n"
                f"**Aliases:** {entry['aliases']}\n"
                f"**Who can use it:** {entry['access']}"
            )
            embed.add_field(name=f"`{entry['usage']}`", value=value[:1024], inline=False)
        embed.set_footer(text=f"Category {self.categories.index(self.category) + 1}/{len(self.categories)} • Page {self.page + 1}/{pages} • Use the dropdown to switch categories")
        self._sync_buttons()
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Only the person who opened this help menu can use its controls.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="gm_help_prev", row=1)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, custom_id="gm_help_next", row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        total = len(self.categories_data.get(self.category, []))
        pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = min(pages - 1, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)


class HelpMenu(commands.Cog):
    """Category-based help for prefix and slash commands."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="help", help="Open the interactive command help menu.")
    async def help_menu(self, ctx: commands.Context):
        categories: dict[str, list[dict[str, str]]] = {}

        # Prefix commands are grouped by their actual loaded cog.
        for command in self.bot.walk_commands():
            if command.hidden or command.name == "help":
                continue
            cog_name = command.cog_name or "Other Commands"
            category = CATEGORY_NAMES.get(cog_name, cog_name)
            categories.setdefault(category, []).append(_prefix_entry(command))

        # Slash commands are grouped under Match Management (and any future app-command cogs).
        for command in self.bot.tree.walk_commands():
            if isinstance(command, discord.app_commands.Group):
                continue
            cog = getattr(command, "binding", None)
            cog_name = type(cog).__name__ if cog is not None else "Scheduling"
            category = CATEGORY_NAMES.get(cog_name, "Match Management" if cog_name == "Scheduling" else cog_name)
            categories.setdefault(category, []).append(_slash_entry(command))

        # Keep useful categories first, and list commands alphabetically within each category.
        preferred = [
            "Match Management", "Team Management", "Player Statistics", "Match Results",
            "Reserves & Matchups", "Groups & Fixtures", "Leaderboards", "Points Table",
            "AutoLock", "Administration & Utility", "DM & Echo", "No-Team Management",
            "Live Draws", "Custom Image Commands", "MVP", "Predictions",
        ]
        categories = {key: sorted(value, key=lambda item: item["sort"]) for key, value in categories.items()}
        ordered = {key: categories[key] for key in preferred if key in categories}
        ordered.update({key: value for key, value in categories.items() if key not in ordered})
        if not ordered:
            await ctx.send("No commands are available to display.")
            return

        view = HelpMenuView(ctx, ordered)
        await ctx.send(embed=view.make_embed(), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpMenu(bot))
