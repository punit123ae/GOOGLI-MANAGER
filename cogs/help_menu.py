import math
import discord
from discord.ext import commands
from discord import app_commands

PER_PAGE = 6
CATEGORY_ORDER = ["Match Management", "Team Management", "Statistics", "Leaderboards", "Results", "Reserves", "Groups & Scheduling", "Predictions & Draws", "MVP", "AutoLock", "DM & Messaging", "Utility & Administration", "Other"]
COG_CATEGORIES = {
    "scheduling": "Match Management", "teams1": "Team Management", "teams2": "Team Management", "teams3": "Team Management", "noteam": "Team Management",
    "stats": "Statistics", "lb": "Leaderboards", "points_table": "Leaderboards", "results": "Results", "reserves": "Reserves",
    "groups": "Groups & Scheduling", "predictions": "Predictions & Draws", "draws": "Predictions & Draws", "mvp": "MVP", "autolock": "AutoLock",
    "dm": "DM & Messaging", "custom_image_commands": "DM & Messaging", "utility": "Utility & Administration"
}

def category_for(command):
    cog = getattr(command, "cog", None)
    key = (getattr(cog, "qualified_name", "") or "").lower().replace(" ", "_")
    return COG_CATEGORIES.get(key, "Other")

def usage(command):
    args = []
    for p in getattr(command, "clean_params", {}).values():
        if p.kind.name == "KEYWORD_ONLY": args.append(f"[{p.name}...]")
        elif p.default is not p.empty: args.append(f"[{p.name}]")
        else: args.append(f"<{p.name}>")
    return "+" + command.qualified_name + (" " + " ".join(args) if args else "")

def access(command):
    found = []
    for check in getattr(command, "checks", []):
        name = getattr(check, "__qualname__", getattr(check, "__name__", "")).lower()
        if "is_owner" in name: found.append("Bot owner")
        elif "has_permissions" in name or "has_guild_permissions" in name: found.append("Requires Discord permissions")
        elif "guild_only" in name: found.append("Server only")
    doc = (getattr(command, "help", None) or getattr(command, "description", None) or "").lower()
    if "owner only" in doc or "bot owner" in doc: found.append("Bot owner only")
    return ", ".join(dict.fromkeys(found)) or "Existing command checks apply"

def prefix_entries(bot):
    result = {}
    for cmd in bot.walk_commands():
        if cmd.hidden or cmd.name == "help": continue
        cat = category_for(cmd)
        result.setdefault(cat, []).append({"name": "+" + cmd.qualified_name, "usage": usage(cmd), "description": (cmd.short_doc or "No description provided.")[:160], "aliases": ", ".join("+" + a for a in cmd.aliases) if cmd.aliases else "None", "access": access(cmd)})
    return result

def slash_entries(bot):
    result = {}
    for cmd in bot.tree.get_commands():
        if isinstance(cmd, app_commands.Group):
            for sub in cmd.commands:
                result.setdefault("Match Management", []).append({"name": f"/{cmd.name} {sub.name}", "usage": f"/{cmd.name} {sub.name}", "description": (sub.description or "No description provided.")[:160], "aliases": "None (slash command)", "access": "Existing command checks apply"})
        else:
            result.setdefault("Match Management", []).append({"name": "/" + cmd.name, "usage": "/" + cmd.name, "description": (cmd.description or "No description provided.")[:160], "aliases": "None (slash command)", "access": "Existing command checks apply"})
    return result

class HelpView(discord.ui.View):
    def __init__(self, author_id, entries, categories):
        super().__init__(timeout=180)
        self.author_id, self.entries, self.categories = author_id, entries, categories
        self.category, self.page, self.message = categories[0], 0, None
        options = [discord.SelectOption(label=c[:100], value=c, default=(c == self.category)) for c in categories[:25]]
        self.selector = discord.ui.Select(placeholder="Jump to a command category…", options=options, row=1)
        self.selector.callback = self.choose_category
        self.add_item(self.selector)
        self.refresh()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Run `+help` to open your own help menu.", ephemeral=True)
            return False
        return True

    def items(self): return self.entries.get(self.category, [])
    def refresh(self):
        pages = max(1, math.ceil(len(self.items()) / PER_PAGE))
        self.page = max(0, min(self.page, pages - 1))
        self.first.disabled = self.prev.disabled = self.page == 0
        self.next.disabled = self.last.disabled = self.page >= pages - 1

    def embed(self):
        items = self.items()
        embed = discord.Embed(title="🤖 Googli Manager • Help", description=f"**Category:** {self.category}\nChoose a category below or use the page buttons.", color=discord.Color.blurple())
        start = self.page * PER_PAGE
        for item in items[start:start + PER_PAGE]:
            value = f"{item['description']}\n**Usage:** `{item['usage']}`\n**Aliases:** {item['aliases']}\n**Who can use it:** {item['access']}"
            embed.add_field(name="🔹 " + item['name'], value=value[:1024], inline=False)
        if not items: embed.add_field(name="No commands", value="No commands are listed in this category.", inline=False)
        embed.set_footer(text=f"Page {self.page + 1}/{max(1, math.ceil(len(items) / PER_PAGE))} • {len(items)} commands")
        return embed

    async def choose_category(self, interaction):
        self.category = self.selector.values[0]
        self.page = 0
        for opt in self.selector.options: opt.default = opt.value == self.category
        await self.update(interaction)

    async def update(self, interaction):
        self.refresh()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="⏮ First", style=discord.ButtonStyle.secondary, row=0)
    async def first(self, interaction, button): self.page = 0; await self.update(interaction)
    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction, button): self.page -= 1; await self.update(interaction)
    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary, row=0)
    async def next(self, interaction, button): self.page += 1; await self.update(interaction)
    @discord.ui.button(label="Last ⏭", style=discord.ButtonStyle.secondary, row=0)
    async def last(self, interaction, button): self.page = max(0, math.ceil(len(self.items()) / PER_PAGE) - 1); await self.update(interaction)

    async def on_timeout(self):
        for child in self.children: child.disabled = True
        if self.message:
            try: await self.message.edit(view=self)
            except discord.HTTPException: pass

class HelpMenu(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.remove_command("help")

    @commands.command(name="help", aliases=["commands"])
    async def help_menu(self, ctx):
        """Show commands organised by category in one interactive message."""
        entries = {}
        for source in (slash_entries(self.bot), prefix_entries(self.bot)):
            for cat, items in source.items(): entries.setdefault(cat, []).extend(items)
        categories = [c for c in CATEGORY_ORDER if c in entries]
        categories.extend(sorted(c for c in entries if c not in categories))
        if not categories:
            await ctx.reply("No commands are currently loaded.", mention_author=False); return
        for cat in entries: entries[cat].sort(key=lambda x: (not x["name"].startswith("/"), x["name"].lower()))
        view = HelpView(ctx.author.id, entries, categories)
        view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False)

async def setup(bot):
    await bot.add_cog(HelpMenu(bot))
