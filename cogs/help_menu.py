"""Interactive, public help menu for Googli Manager (discord.py 2.x).

Every command is listed with a plain description, usage, aliases and who can use it.
Descriptions live in CURATED below; any command that is not in that table (for example a
command you add later) is still discovered automatically and listed with generic text.
"""
import math

import discord
from discord import app_commands
from discord.ext import commands

PER_PAGE = 5
INTERFACE = "Interface"
EMBED_COLOR = discord.Color.from_rgb(35, 105, 145)

CATEGORY_ORDER = [
    INTERFACE,
    "Match Management",
    "Team Management",
    "Team Administration",
    "Statistics",
    "Leaderboards",
    "Fielding",
    "Results",
    "Reserves",
    "Groups & Schedules",
    "Predictions",
    "Draws",
    "MVP",
    "AutoLock",
    "Messaging",
    "Custom Image Commands",
    "Utility & Administration",
]

CATEGORY_EMOJI = {
    INTERFACE: "📖", "Match Management": "🗓️", "Team Management": "🏏",
    "Team Administration": "🛠️", "Statistics": "📊", "Leaderboards": "🏆",
    "Fielding": "🧤", "Results": "📢", "Reserves": "📅", "Groups & Schedules": "🧩",
    "Predictions": "🔮", "Draws": "🎲", "MVP": "⭐", "AutoLock": "🔒",
    "Messaging": "✉️", "Custom Image Commands": "🖼️", "Utility & Administration": "⚙️",
}

# Who can use a command (plain words).
EVERYONE = "Everyone"
WHITELIST = "Bot owner, or a role whitelisted for this command"
OWNER = "Bot owner only"
CAPTAIN = "Captain of the team"

# name -> (category, description, usage, access)
CURATED = {
    # ---------------------------------------------------------------- Statistics
    "stats": ("Statistics", "Show a player's batting, bowling and fantasy stats card. Without a user it shows your own.",
              "+stats [@user | USER_ID]", EVERYONE),
    "as": ("Statistics", "Import match statistics. Reply to the raw-statistics message with this command.",
           "+as  (reply to the raw stats message)", WHITELIST),
    "rs": ("Statistics", "Remove a previously imported match. Reply to the original raw-statistics message.",
           "+rs  (reply to the raw stats message)", WHITELIST),
    "resetstats": ("Statistics", "Reset ALL imported batting/bowling stats for this server.",
                   "+resetstats", WHITELIST),
    "setdesign": ("Statistics", "Choose which design the +stats card uses (1 or 2).",
                  "+setdesign <1|2>", WHITELIST),
    # ---------------------------------------------------------------- Leaderboards
    "lb": ("Leaderboards", "Paginated leaderboard with menus for Batting, Bowling, Fantasy Points and Fielding.",
           "+lb", EVERYONE),
    # ---------------------------------------------------------------- Fielding
    "fieldingstats": ("Fielding", "Show a player's catches caught, catches dropped and catch efficiency. "
                                  "Without a user it shows a server-wide overview.",
                      "+fieldingstats [@user]", EVERYONE),
    "changefieldingstats": ("Fielding", "Set a player's exact total catches caught and dropped. "
                                        "The bot asks for each number; type `cancel` to stop.",
                            "+changefieldingstats @user", WHITELIST),
    "resetfieldingstats": ("Fielding", "Reset all fielding stats for this server after a button confirmation. "
                                       "Batting and bowling data are not touched.",
                           "+resetfieldingstats", WHITELIST),
    "starttracking": ("Fielding", "Turn on automatic catch tracking in this channel (saved across restarts).",
                      "+starttracking", WHITELIST),
    "stoptracking": ("Fielding", "Turn off automatic catch tracking in this channel. Recorded stats are kept.",
                     "+stoptracking", WHITELIST),
    "fieldingtrackingstatus": ("Fielding", "List every channel in this server where catch tracking is on.",
                               "+fieldingtrackingstatus", EVERYONE),
    # ---------------------------------------------------------------- Team Management
    "createteam": ("Team Management", "Apply to register a new team. The bot walks you through it in DMs "
                                      "and sends the application for approval.",
                   "+createteam", EVERYONE),
    "jointeam": ("Team Management", "Request to join an approved team. The captain approves or declines.",
                 "+jointeam <team name>", EVERYONE),
    "leaveteam": ("Team Management", "Leave your current team (with a confirmation). Captains and vice-captains cannot leave.",
                  "+leaveteam", "Team members"),
    "teaminfo": ("Team Management", "Show a team's roster. The first 4 letters of the name are enough.",
                 "+teaminfo <team name>", EVERYONE),
    "teamlist": ("Team Management", "List all approved teams in the server.",
                 "+teamlist", EVERYONE),
    "vc": ("Team Management", "Set your team's vice-captain.",
           "+vc <@user | ID> <team name>", CAPTAIN),
    "teamchat": ("Team Management", "DM a message (and any attached image or file) to everyone on your team.",
                 "+teamchat <message>", CAPTAIN),
    "pingteam": ("Team Management", "Ping your team's role. Subject to a per-team cooldown.",
                 "+pingteam", "Captain of an approved team"),
    # ---------------------------------------------------------------- Team Administration
    "setteamapproval": ("Team Administration", "Set the channel where new team applications are posted for approval.",
                        "+setteamapproval #channel", WHITELIST),
    "cancelrequests": ("Team Administration", "Cancel a user's pending join request.",
                       "+cancelrequests <USER_ID>", WHITELIST),
    "disbandteam": ("Team Administration", "Disband a team.",
                    "+disbandteam <team name>", WHITELIST),
    "addplayer": ("Team Administration", "Add a player directly to a team.",
                  "+addplayer <@user | ID> <team name>", WHITELIST),
    "addallplayers": ("Team Administration", "Add every username in a squad-list message to a team. "
                                             "Reply to that message when you run it.",
                      "+addallplayers <team name>  (reply to the squad list)", WHITELIST),
    "bulkadd": ("Team Administration", "Add every member of a role to a team.",
                "+bulkadd <team name> <ROLE_ID>", WHITELIST),
    "kickplayer": ("Team Administration", "Remove a player from a team.",
                   "+kickplayer <@user | ID> <team name>", WHITELIST),
    "setcaptain": ("Team Administration", "Appoint a new captain for a team.",
                   "+setcaptain <@user | ID> <team name>", WHITELIST),
    "setteamname": ("Team Administration", "Rename an approved team (names can contain spaces).",
                    "+setteamname <current name> <new name>", WHITELIST),
    "setmaxplayers": ("Team Administration", "Set the maximum number of players per team.",
                      "+setmaxplayers <number>", WHITELIST),
    "setpingcooldown": ("Team Administration", "Set the cooldown between team-role pings.",
                        "+setpingcooldown <seconds>", "Server owner or bot owner"),
    "setbaserole": ("Team Administration", "Set the base role given to team members.",
                    "+setbaserole <ROLE_ID>", WHITELIST),
    "syncbaserole": ("Team Administration", "Give the base role to all current team members who are missing it.",
                     "+syncbaserole", WHITELIST),
    "setteamrole": ("Team Administration", "Link a team to its Discord role.",
                    "+setteamrole <team name> <ROLE_ID>", WHITELIST),
    "teamroles": ("Team Administration", "List the configured role for every approved team (no pings).",
                  "+teamroles", WHITELIST),
    "syncteamroles": ("Team Administration", "Give existing team members their configured team roles when missing.",
                      "+syncteamroles", WHITELIST),
    "stopteamroles": ("Team Administration", "Stop automatic role assignment for one team.",
                      "+stopteamroles <team name>", WHITELIST),
    "startteamroles": ("Team Administration", "Resume automatic role assignment for one team.",
                       "+startteamroles <team name>", WHITELIST),
    "stopallteamroles": ("Team Administration", "Stop automatic team-role assignment for all teams.",
                         "+stopallteamroles", WHITELIST),
    "startallteamroles": ("Team Administration", "Resume automatic team-role assignment for all teams.",
                          "+startallteamroles", WHITELIST),
    "toggle": ("Team Administration", "Turn a team feature on or off for the server.",
               "+toggle <jointeam|leaveteam|createteam>", WHITELIST),
    "deleteallteams": ("Team Administration", "Delete every team in the server (asks for confirmation).",
                       "+deleteallteams", WHITELIST),
    "noteam": ("Team Administration", "Create (or refresh) the live list of members who are not on any team. "
                                      "It keeps updating automatically.",
               "+noteam", WHITELIST),
    "stopnoteam": ("Team Administration", "Stop the automatic updates of the no-team list. The current list stays.",
                   "+stopnoteam", WHITELIST),
    # ---------------------------------------------------------------- Results
    "setresultchannel": ("Results", "Set the channel where match results are posted.",
                         "+setresultchannel #channel", WHITELIST),
    "result": ("Results", "Publish a match result. The bot asks for each detail step by step.",
               "+result", WHITELIST),
    # ---------------------------------------------------------------- Reserves
    "setreserves": ("Reserves", "Set how many reserve matches each team may have (1-100).",
                    "+setreserves <number>", WHITELIST),
    "reservematch": ("Reserves", "Record a reserve match. The bot asks for the reserving and opposing team.",
                     "+reservematch", WHITELIST),
    "reservelist": ("Reserves", "Show all reserved matches, 10 per page.",
                    "+reservelist", EVERYONE),
    "reservestatus": ("Reserves", "Show one team's reservations and how many it has left.",
                      "+reservestatus <team name>", EVERYONE),
    "deletereserve": ("Reserves", "Delete one reservation by its number (asks for confirmation).",
                      "+deletereserve <match number>", WHITELIST),
    "deleteallreserves": ("Reserves", "Delete every reservation and restart numbering at 1.",
                          "+deleteallreserves", WHITELIST),
    "creatematchups": ("Reserves", "Generate the reserve match-up schedule.",
                       "+creatematchups", WHITELIST),
    "resetmatchups": ("Reserves", "Clear the generated match-up schedule so it can be created again.",
                      "+resetmatchups", WHITELIST),
    "reserveaudit": ("Reserves", "Check reservations for problems (limits exceeded, duplicates, and so on).",
                     "+reserveaudit", WHITELIST),
    # ---------------------------------------------------------------- Groups & Schedules
    "openmatch": ("Groups & Schedules", "Reply to the daily fixtures: unlocks each stadium, pings only the two "
                  "captains, then tracks their messages and confirms the match time once both agree.",
                  "+openmatch [match number]  (reply to the fixtures message)", WHITELIST),
    "setconfirmrole": ("Groups & Schedules", "Set the role that is pinged when both captains agree on a match time.",
                       "+setconfirmrole @role", WHITELIST),
    "groupstyle": ("Groups & Schedules", "Set the allowed group names, for example A, B, C, D.",
                   "+groupstyle", WHITELIST),
    "setteamgroups": ("Groups & Schedules", "Assign each approved team to a group (interactive).",
                      "+setteamgroups", WHITELIST),
    "audittgroup": ("Groups & Schedules", "Check every approved team is in exactly one valid group.",
                    "+audittgroup", WHITELIST),
    "deletegroupteams": ("Groups & Schedules", "Remove all team assignments from one group (asks for confirmation).",
                         "+deletegroupteams <group>", WHITELIST),
    "setformat": ("Groups & Schedules", "Set how many times each pair of teams plays in a group.",
                  "+setformat", WHITELIST),
    "generateschedule": ("Groups & Schedules", "Generate and save a round-robin schedule for a group.",
                         "+generateschedule <group>", WHITELIST),
    # ---------------------------------------------------------------- Draws
    "setuplivedraws": ("Draws", "Save a live-draw setting for this server (interactive).",
                       "+setuplivedraws", WHITELIST),
    "startlivedraw": ("Draws", "Run a live draw one team at a time using a saved setting.",
                      "+startlivedraw <setting name>", WHITELIST),
    # ---------------------------------------------------------------- MVP
    "setmvp": ("MVP", "Create an MVP image. The bot asks for the player name, stats and avatar.",
               "+setmvp", WHITELIST),
    # ---------------------------------------------------------------- AutoLock
    "autolock": ("AutoLock", "Turn on raw-statistics auto-locking for a channel (this channel by default).",
                 "+autolock [#channel]", WHITELIST),
    "stopautolock": ("AutoLock", "Turn off auto-locking for a channel and restore a lock this feature made.",
                     "+stopautolock [#channel]", WHITELIST),
    "statusautolock": ("AutoLock", "Show auto-lock status for this channel, a chosen channel, or the whole server.",
                       "+statusautolock [channel | #channel | server]", WHITELIST),
    # ---------------------------------------------------------------- Messaging
    "dm": ("Messaging", "Send a DM (text and/or image URL) to a user or everyone in a role.",
           "+dm <user ID | username | role ID> <text or image URL>", WHITELIST),
    "echo": ("Messaging", "Make the bot send a message in a channel.",
             "+echo <message> <#channel>", WHITELIST),
    # ---------------------------------------------------------------- Custom Image Commands
    "createimagecommand": ("Custom Image Commands", "Create a custom command that replies with an image (interactive).",
                           "+createimagecommand", WHITELIST),
    "editimagecommand": ("Custom Image Commands", "Change a custom command's image or colour.",
                         "+editimagecommand", WHITELIST),
    "deleteimagecommand": ("Custom Image Commands", "Delete a custom image command.",
                           "+deleteimagecommand <name>", WHITELIST),
    "listimagecommands": ("Custom Image Commands", "List all custom image commands in this server.",
                          "+listimagecommands", EVERYONE),
    # ---------------------------------------------------------------- Utility & Administration
    "whitelist": ("Utility & Administration", "Let a role use one specific command.",
                  "+whitelist <@Role | ROLE_ID> <command name>", OWNER),
    "unwhitelist": ("Utility & Administration", "Remove a role's access to one command.",
                    "+unwhitelist <@Role | ROLE_ID> <command name>", OWNER),
    "whitelistlist": ("Utility & Administration", "Show all role-to-command whitelist entries.",
                      "+whitelistlist", OWNER),
    "toggleall": ("Utility & Administration", "Lock the server: only whitelisted roles can use each command.",
                  "+toggleall", OWNER),
    "untoggleall": ("Utility & Administration", "Unlock the server-wide command lock.",
                    "+untoggleall", OWNER),
    "disableall": ("Utility & Administration", "Disable bot commands in this channel.",
                   "+disableall", OWNER),
    "enablechannel": ("Utility & Administration", "Enable bot commands again in this channel.",
                      "+enablechannel", OWNER),
    "enableall": ("Utility & Administration", "Enable bot commands again in every channel.",
                  "+enableall", OWNER),
    "botban": ("Utility & Administration", "Stop a user from using any bot command.",
               "+botban <@user | ID> [reason]", WHITELIST),
    "botunban": ("Utility & Administration", "Let a bot-banned user use commands again.",
                 "+botunban <@user | ID>", WHITELIST),
    "botbanlist": ("Utility & Administration", "List everyone who is bot-banned.",
                   "+botbanlist", WHITELIST),
    "adminlog": ("Utility & Administration", "Show the recent command activity log.",
                 "+adminlog [number]", WHITELIST),
    "backupdata": ("Utility & Administration", "Back up the bot's databases.",
                   "+backupdata", WHITELIST),
    "backuplist": ("Utility & Administration", "List the available backups.",
                   "+backuplist", WHITELIST),
    "restoredata": ("Utility & Administration", "Restore a backup. Needs the backup name and the word CONFIRM.",
                    "+restoredata <backup name> CONFIRM", WHITELIST),
    "auditteams": ("Utility & Administration", "Find players on more than one roster and captain data problems.",
                   "+auditteams", WHITELIST),
    "resolveauditteams": ("Utility & Administration", "Fix duplicate roster entries by moving a player into one chosen team.",
                          "+resolveauditteams", WHITELIST),
    "sendteam": ("Utility & Administration", "Post one approved team's announcement embed in this channel.",
                 "+sendteam <team name>", WHITELIST),
    "setapprovedteamschannel": ("Utility & Administration", "Choose the channel for approved-team announcements.",
                                "+setapprovedteamschannel #channel", WHITELIST),
    "postapprovedteams": ("Utility & Administration", "Preview, then post every approved team to a channel.",
                          "+postapprovedteams <#channel>", WHITELIST),
}

# Slash commands: name -> access text.
SLASH_ACCESS = {
    "set_match_logs": WHITELIST,
    "match_schedule": WHITELIST,
    "match_list": WHITELIST,
    "match_info": WHITELIST,
    "match_delete": WHITELIST,
}

# Fallback only: cog class name (lower-case, no spaces/underscores) -> category.
COG_CATEGORIES = {
    "scheduling": "Match Management",
    "teamregistration": "Team Management",
    "stats": "Statistics", "leaderboardcog": "Leaderboards", "catchtracker": "Fielding",
    "resultscog": "Results", "reserves": "Reserves", "groups": "Groups & Schedules",
    "predictions": "Predictions", "livedraws": "Draws", "mvp": "MVP", "autolock": "AutoLock",
    "dmextension": "Messaging", "customimagecommands": "Custom Image Commands",
    "utility": "Utility & Administration", "noteam": "Team Administration",
}


def cog_key(command):
    cog = getattr(command, "cog", None)
    return (getattr(cog, "qualified_name", "") or "").lower().replace(" ", "").replace("_", "")


def pretty_cog_name(key):
    return key.capitalize()


def category_for(command):
    key = cog_key(command)
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
    doc = (getattr(command, "help", None) or getattr(command, "description", None) or "").lower()
    if "owner only" in doc or "bot owner" in doc:
        notes.append("Bot owner only")
    return ", ".join(dict.fromkeys(notes)) or "Depends on the command's own restrictions"


def make_entry(name, description, usage, aliases="None", access=EVERYONE, kind="Prefix"):
    return {"name": name, "description": description or "No description provided.",
            "usage": usage, "aliases": aliases, "access": access, "kind": kind}


def add_slash_command(result, command, parent=""):
    full_name = f"{parent} {command.name}".strip()
    if isinstance(command, app_commands.Group):
        for child in command.commands:
            add_slash_command(result, child, full_name)
        return
    description = getattr(command, "description", None) or "No description provided."
    params = []
    for param in getattr(command, "parameters", []):
        params.append(f"<{param.name}>" if getattr(param, "required", True) else f"[{param.name}]")
    usage = "/" + full_name + (" " + " ".join(params) if params else "")
    access = SLASH_ACCESS.get(command.name, "Depends on the command's own restrictions")
    result.setdefault("Match Management", []).append(
        make_entry("/" + full_name, description, usage, "None (slash command)", access, kind="Slash")
    )


def build_entries(bot, guild=None):
    """Collect every command currently loaded on the bot, grouped by category."""
    entries = {}
    for command in bot.walk_commands():
        if command.name == "help":
            continue
        aliases = ", ".join("+" + a for a in command.aliases) if command.aliases else "None"
        curated = CURATED.get(command.qualified_name)
        if curated:
            cat, desc, usage, access = curated
        else:
            cat = category_for(command)
            desc = command.help or command.short_doc or "No description provided."
            usage = prefix_usage(command)
            access = access_for(command)
        entries.setdefault(cat, []).append(make_entry("+" + command.qualified_name, desc, usage, aliases, access))
    for command in bot.tree.get_commands():
        add_slash_command(entries, command)

    # Saved custom image commands for this server (created with +createimagecommand).
    cog = bot.get_cog("CustomImageCommands")
    if cog is not None and guild is not None:
        try:
            rows = cog.db.execute(
                "SELECT trigger FROM custom_image_commands WHERE guild_id=? ORDER BY trigger", (guild.id,)
            ).fetchall()
            triggers = [f"`+{r['trigger']}`" for r in rows]
        except Exception:
            triggers = []
        if triggers:
            text = ", ".join(triggers)
            if len(text) > 800:
                text = text[:800].rsplit(", ", 1)[0] + ", …"
            entries.setdefault("Custom Image Commands", []).insert(0, make_entry(
                "Saved image commands", f"Created in this server: {text}", "+<name>", "None", EVERYONE
            ))
    for cat, items in entries.items():
        items.sort(key=lambda item: (item["name"] != "Saved image commands", item["name"].lower()))
    return entries


def ordered_categories(entries):
    categories = [cat for cat in CATEGORY_ORDER if cat in entries or cat == INTERFACE]
    categories.extend(sorted(cat for cat in entries if cat not in categories and cat != "Other"))
    return categories


class HelpView(discord.ui.View):
    def __init__(self, bot, categories, entries, owner_id=None):
        super().__init__(timeout=180)
        self.bot = bot
        self.categories = categories
        self.entries = entries
        self.owner_id = owner_id
        self.category = INTERFACE if INTERFACE in categories else categories[0]
        self.page = 0
        self.message = None
        options = []
        for c in categories[:25]:
            count = len(entries.get(c, []))
            options.append(discord.SelectOption(
                label=c[:100], value=c, default=(c == self.category),
                emoji=CATEGORY_EMOJI.get(c), description=(f"{count} command(s)" if c != INTERFACE else "Start here"),
            ))
        self.selector = discord.ui.Select(placeholder="Choose a command category", options=options, row=1)
        self.selector.callback = self.choose_category
        self.add_item(self.selector)
        self.refresh()

    async def interaction_check(self, interaction):
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran `+help` can use this menu. Run `+help` for your own.", ephemeral=True)
            return False
        return True

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
            total = sum(len(v) for v in self.entries.values())
            embed = discord.Embed(
                title="Googli Manager | Interface",
                description=(
                    "Welcome to the command guide. Use the category menu to browse each part of the bot, "
                    "and use **Previous** / **Next** to move through longer lists.\n\n"
                    "**Created and maintained by:** .q2727. / punitaec\n"
                    "**What it does:** Manages tournament matches, teams, statistics, leaderboards, fielding, "
                    "results, reserves, groups and schedules, draws, and server utilities.\n\n"
                    "**How to use it:** Prefix commands start with `+`. Match-management slash commands "
                    "start with `/` and can also be selected from Discord's slash-command menu.\n"
                    "**Tip:** `+help <command>` shows full details for one command, e.g. `+help fieldingstats`.\n\n"
                    "Access notes describe existing restrictions; this help menu does not grant permissions."
                ), color=EMBED_COLOR
            )
            avatar = getattr(getattr(self.bot, "user", None), "display_avatar", None)
            if avatar:
                embed.set_thumbnail(url=avatar.url)
            lines = [
                f"{CATEGORY_EMOJI.get(c, '•')} **{c}** — {len(self.entries.get(c, []))}"
                for c in self.categories if c != INTERFACE
            ]
            embed.add_field(name=f"Categories ({total} commands)", value="\n".join(lines)[:1024] or "None", inline=False)
            embed.add_field(name="Access guide",
                            value=(f"**{EVERYONE}** — anyone can run it.\n"
                                   f"**{WHITELIST}** — the owner can give a role access with `+whitelist`.\n"
                                   f"**{OWNER}** — cannot be delegated."),
                            inline=False)
            embed.set_footer(text="GOOGLI MANAGER • COMMAND GUIDE")
            return embed

        items = self.items()
        pages = self.page_count()
        embed = discord.Embed(
            title=f"{CATEGORY_EMOJI.get(self.category, '')} Googli Manager | {self.category}".strip(),
            description=f"Commands in this category  ·  Page {self.page + 1} of {pages}",
            color=EMBED_COLOR
        )
        start = self.page * PER_PAGE
        for index, item in enumerate(items[start:start + PER_PAGE], start=start + 1):
            value = (
                f"{item['description']}\n"
                f"**Usage:** `{item['usage']}`\n"
                f"**Aliases:** {item['aliases']}\n"
                f"**Who can use it:** {item['access']}\n​"
            )
            embed.add_field(name=f"{index}. `{item['name']}`"[:256], value=value[:1024], inline=False)
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
    async def help_menu(self, ctx, *, command_name: str = None):
        """Show the public interactive command guide, or details for one command."""
        entries = build_entries(self.bot, ctx.guild)

        if command_name:
            wanted = command_name.strip().lower().lstrip("+/").replace("  ", " ")
            for items in entries.values():
                for item in items:
                    names = {item["name"].lower().lstrip("+/")}
                    names.update(a.strip().lstrip("+").lower() for a in item["aliases"].split(",") if a.strip() != "None")
                    if wanted in names:
                        embed = discord.Embed(title=f"Googli Manager | {item['name']}",
                                              description=item["description"], color=EMBED_COLOR)
                        embed.add_field(name="Usage", value=f"`{item['usage']}`", inline=False)
                        embed.add_field(name="Aliases", value=item["aliases"], inline=False)
                        embed.add_field(name="Who can use it", value=item["access"], inline=False)
                        embed.set_footer(text="Run +help for the full command guide")
                        await ctx.reply(embed=embed, mention_author=False)
                        return
            await ctx.reply(f"I couldn't find a command called `{command_name}`. Run `+help` to browse all commands.",
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return

        categories = ordered_categories(entries)
        view = HelpView(self.bot, categories, entries, owner_id=ctx.author.id)
        view.message = await ctx.reply(embed=view.make_embed(), view=view, mention_author=False)


async def setup(bot):
    await bot.add_cog(HelpMenu(bot))
