import re
import os
import sqlite3
import zipfile
from pathlib import Path
from datetime import datetime, timezone
import discord
from discord.ext import commands

BOT_OWNER_ID = 1014568928682459198

RECOVERY_COMMANDS = {
    "toggleall", "untoggleall", "whitelist", "unwhitelist", "whitelistlist",
    "enableall", "enablechannel",
}


def _normalise_command_name(name):
    return str(name or "").strip().lower().lstrip("+!/").replace(" ", "")


def role_ids(member):
    return {int(role.id) for role in getattr(member, "roles", [])}


def has_any_whitelisted_role(bot, guild_id, member):
    if not guild_id or member is None:
        return False
    rows = bot.db.execute(
        "SELECT DISTINCT role_id FROM command_whitelist WHERE guild_id=?",
        (int(guild_id),),
    ).fetchall()
    allowed = {int(row[0]) for row in rows}
    return bool(role_ids(member) & allowed)


def has_command_whitelist(bot, guild_id, member, command_name):
    if not guild_id or member is None or not command_name:
        return False
    name = _normalise_command_name(command_name)
    rows = bot.db.execute(
        "SELECT role_id FROM command_whitelist WHERE guild_id=? AND command_name=?",
        (int(guild_id), name),
    ).fetchall()
    allowed = {int(row[0]) for row in rows}
    return bool(role_ids(member) & allowed)


async def owner_or_whitelisted(ctx, command_name=None, app_owner=False):
    """Allow the command's original owner check, plus a role whitelisted for this exact command.

    By default, preserve the configured BOT_OWNER_ID-only rule. Pass app_owner=True
    only for commands that originally used bot.is_owner().
    """
    bot = getattr(ctx, "bot", None)
    author = getattr(ctx, "author", None)
    if bot is None or author is None:
        return False
    if int(getattr(author, "id", 0)) == BOT_OWNER_ID:
        return True
    if app_owner:
        try:
            if await bot.is_owner(author):
                return True
        except (AttributeError, discord.DiscordException):
            pass
    utility = bot.get_cog("Utility")
    if utility is None:
        return False
    command = getattr(ctx, "command", None)
    name = command_name or getattr(command, "name", "")
    return await utility.command_allowed(ctx, name)


class PostApprovedTeamsView(discord.ui.View):
    """Owner-only confirmation before publishing approved team embeds."""
    def __init__(self, cog, guild_id, channel_id, author_id):
        super().__init__(timeout=60)
        self.cog = cog
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.author_id = author_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the user who started this action can confirm it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm and post", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the user who started this action can confirm it.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Posting approved team embeds… please wait.", view=self
        )
        count, error = await self.cog._post_approved_team_embeds(
            self.guild_id, self.channel_id
        )
        try:
            await interaction.edit_original_response(
                content=(f"✅ Posted **{count}** approved team embed(s)." if error is None
                         else f"⚠️ Posted **{count}** team embed(s), then stopped: {error}"),
                view=self,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Cancelled. No team embeds were posted.", view=self
        )


class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS command_whitelist (
            guild_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            command_name TEXT NOT NULL,
            PRIMARY KEY (guild_id, role_id, command_name)
        );
        CREATE TABLE IF NOT EXISTS utility_guild_state (
            guild_id INTEGER PRIMARY KEY,
            commands_locked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS utility_disabled_channels (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        );
        CREATE TABLE IF NOT EXISTS admin_activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            guild_id INTEGER,
            channel_id INTEGER,
            user_id INTEGER NOT NULL,
            command_name TEXT NOT NULL,
            command_text TEXT NOT NULL DEFAULT '',
            succeeded INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS bot_bans (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            banned_by INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            banned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        self.db.commit()
        # Repair historical approved teams whose captain is missing from the roster.
        # This is safe to run on every startup: INSERT OR IGNORE prevents duplicates.
        self._repair_missing_approved_captains()
        self.bot.add_check(self.global_command_check)
        # discord.py's app-command tree supports a global interaction check.
        self._previous_tree_check = getattr(self.bot.tree, "interaction_check", None)
        self.bot.tree.interaction_check = self.global_interaction_check

    def _repair_missing_approved_captains(self):
        """Add missing captains to approved team rosters without duplicating players."""
        db_path = Path("team_registration.db")
        if not db_path.is_file():
            return
        conn = None
        try:
            conn = sqlite3.connect(str(db_path), timeout=10)
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"registered_teams", "team_players"}.issubset(tables):
                return
            team_columns = {row[1] for row in conn.execute("PRAGMA table_info(team_players)")}
            required = {"team_id", "user_id"}
            if not required.issubset(team_columns):
                return
            # The current team schema has added_by/added_at; support older schemas too.
            insert_columns = ["team_id", "user_id"]
            select_values = ["t.id", "t.captain_id"]
            if "added_by" in team_columns:
                insert_columns.append("added_by")
                select_values.append("t.captain_id")
            if "added_at" in team_columns:
                insert_columns.append("added_at")
                select_values.append("CURRENT_TIMESTAMP")
            sql = (
                f"INSERT OR IGNORE INTO team_players ({', '.join(insert_columns)}) "
                f"SELECT {', '.join(select_values)} FROM registered_teams AS t "
                "WHERE t.status='approved' AND t.captain_id IS NOT NULL AND t.captain_id != 0 "
                "AND NOT EXISTS (SELECT 1 FROM team_players AS p "
                "WHERE p.team_id=t.id AND p.user_id=t.captain_id)"
            )
            cursor = conn.execute(sql)
            repaired = cursor.rowcount
            conn.commit()
            if repaired > 0:
                print(f"[Utility] Automatically added missing captains to {repaired} approved team roster(s).")
        except (sqlite3.Error, OSError) as exc:
            print(f"[Utility] Captain roster auto-repair skipped: {type(exc).__name__}: {exc}")
        finally:
            if conn is not None:
                conn.close()

    def cog_unload(self):
        self.bot.remove_check(self.global_command_check)
        # Restore the previous check if this cog is unloaded.
        if getattr(self.bot.tree, "interaction_check", None) == self.global_interaction_check:
            self.bot.tree.interaction_check = self._previous_tree_check

    @commands.Cog.listener()
    async def on_command_completion(self, ctx):
        """Record prefix-command activity for the owner audit log."""
        try:
            self.db.execute(
                "INSERT INTO admin_activity_log(guild_id, channel_id, user_id, command_name, command_text, succeeded) VALUES(?,?,?,?,?,1)",
                (ctx.guild.id if ctx.guild else None, ctx.channel.id if ctx.channel else None,
                 ctx.author.id, str(ctx.command.qualified_name if ctx.command else "unknown"),
                 str(ctx.message.content or "")[:500]),
            )
            self.db.commit()
        except sqlite3.Error:
            pass

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        # Ignore command-not-found noise; record actual attempted command errors.
        if isinstance(error, commands.CommandNotFound):
            return
        try:
            self.db.execute(
                "INSERT INTO admin_activity_log(guild_id, channel_id, user_id, command_name, command_text, succeeded) VALUES(?,?,?,?,?,0)",
                (ctx.guild.id if ctx.guild else None, ctx.channel.id if ctx.channel else None,
                 ctx.author.id, str(ctx.command.qualified_name if ctx.command else "unknown"),
                 str(ctx.message.content or "")[:500]),
            )
            self.db.commit()
        except sqlite3.Error:
            pass

    def _backup_dir(self):
        path = Path("backups")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _database_paths(self):
        # Both databases are used by this bot's loaded cogs.
        return [Path("matches.db"), Path("team_registration.db")]

    @commands.command(name="adminlog")
    @commands.guild_only()
    async def adminlog(self, ctx, limit: int = 15):
        """Owner-only recent command activity log."""
        if not await self._require_bot_owner(ctx):
            return
        limit = max(1, min(int(limit), 40))
        rows = self.db.execute(
            "SELECT occurred_at, guild_id, user_id, command_name, command_text, succeeded "
            "FROM admin_activity_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        if not rows:
            await ctx.reply("No command activity has been logged yet.", mention_author=False)
            return
        lines = []
        for row in rows:
            status = "OK" if row[5] else "ERROR"
            command_text = discord.utils.escape_markdown((row[4] or "")[:100]).replace("`", "ˋ")
            lines.append(f"`{row[0]}` **{status}** user `{row[2]}` · `+{row[3]}` · `{command_text}`")
        await ctx.reply("**Recent bot activity**\n" + "\n".join(lines), mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="backupdata")
    @commands.guild_only()
    async def backupdata(self, ctx):
        """Owner-only backup of both bot databases."""
        if not await self._require_bot_owner(ctx):
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_name = f"backup_{stamp}.zip"
        backup_path = self._backup_dir() / backup_name
        temp_paths = []
        try:
            with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for db_path in self._database_paths():
                    if not db_path.exists():
                        continue
                    snapshot = self._backup_dir() / f".{db_path.name}.{stamp}.snapshot"
                    temp_paths.append(snapshot)
                    source = sqlite3.connect(str(db_path))
                    target = sqlite3.connect(str(snapshot))
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                        source.close()
                    archive.write(snapshot, arcname=db_path.name)
            if not zipfile.is_zipfile(backup_path):
                raise RuntimeError("Backup archive validation failed")
            await ctx.reply(f"✅ Backup created: `{backup_name}`\nIncluded available database files. Use `+backuplist` to view backups.", mention_author=False)
        except Exception as exc:
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass
            await ctx.reply(f"❌ Backup failed ({type(exc).__name__}). No backup was reported as successful.", mention_author=False)
        finally:
            for path in temp_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    @commands.command(name="backuplist")
    @commands.guild_only()
    async def backuplist(self, ctx):
        """Owner-only list of available backups."""
        if not await self._require_bot_owner(ctx):
            return
        files = sorted(self._backup_dir().glob("backup_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            await ctx.reply("No backups found. Run `+backupdata` first.", mention_author=False)
            return
        lines = [f"`{p.name}` — {p.stat().st_size:,} bytes" for p in files[:30]]
        await ctx.reply("**Available backups**\n" + "\n".join(lines), mention_author=False)

    @commands.command(name="restoredata")
    @commands.guild_only()
    async def restoredata(self, ctx, backup_name: str, confirmation: str = ""):
        """Owner-only restore. Requires explicit CONFIRM and a prior backup name."""
        if not await self._require_bot_owner(ctx):
            return
        if confirmation.upper() != "CONFIRM":
            await ctx.reply(
                "⚠️ Restore replaces current database contents and may discard newer data.\n"
                f"To proceed, run `+restoredata {backup_name} CONFIRM`. Make a fresh `+backupdata` first if needed.",
                mention_author=False,
            )
            return
        if Path(backup_name).name != backup_name or not backup_name.startswith("backup_") or not backup_name.endswith(".zip"):
            await ctx.reply("Invalid backup name. Use an exact filename from `+backuplist`.", mention_author=False)
            return
        archive_path = self._backup_dir() / backup_name
        if not archive_path.is_file() or not zipfile.is_zipfile(archive_path):
            await ctx.reply("Backup not found or invalid. Check `+backuplist`.", mention_author=False)
            return
        expected = {p.name for p in self._database_paths()}
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                names = set(archive.namelist())
                if not names or not names.issubset(expected) or any("/" in n or "\\" in n for n in names):
                    raise ValueError("Unexpected files in backup")
                for name in names:
                    if not archive.getinfo(name).file_size or archive.getinfo(name).file_size > 200_000_000:
                        raise ValueError("Invalid database size")
                # Validate every database before touching current files.
                staged = {}
                for name in names:
                    temp = self._backup_dir() / f".restore_{name}"
                    with archive.open(name) as src, open(temp, "wb") as dst:
                        dst.write(src.read())
                    check = sqlite3.connect(str(temp))
                    try:
                        result = check.execute("PRAGMA integrity_check").fetchone()[0]
                        if result != "ok":
                            raise ValueError(f"Database integrity check failed for {name}")
                    finally:
                        check.close()
                    staged[name] = temp
            # Use SQLite's online backup API to restore into the existing database files.
            # This avoids replacing file handles used by the running bot and team cogs.
            restored = []
            for name, temp in staged.items():
                source = sqlite3.connect(str(temp))
                target = sqlite3.connect(name, timeout=30)
                try:
                    source.backup(target)
                    target.commit()
                    check = target.execute("PRAGMA integrity_check").fetchone()[0]
                    if check != "ok":
                        raise ValueError(f"Restored database integrity check failed for {name}")
                    restored.append(name)
                finally:
                    target.close()
                    source.close()
            for temp in staged.values():
                temp.unlink(missing_ok=True)
            await ctx.reply("✅ Restored database(s): " + ", ".join(f"`{n}`" for n in restored) + ". The bot may need a restart if any cog cached data in memory.", mention_author=False)
        except Exception as exc:
            await ctx.reply(f"❌ Restore validation failed ({type(exc).__name__}). Current databases were not intentionally changed.", mention_author=False)
            for temp in (self._backup_dir()).glob(".restore_*"):
                try: temp.unlink(missing_ok=True)
                except OSError: pass

    async def _audit_team_db(self):
        """Return team tables and columns needed by the audit/resolve commands."""
        db_path = Path("team_registration.db")
        if not db_path.is_file():
            raise FileNotFoundError("team_registration.db was not found")
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"registered_teams", "team_players"}.issubset(tables):
            conn.close()
            raise ValueError("The registered_teams/team_players tables are missing")
        return conn

    def _audit_username(self, guild, user_id):
        member = guild.get_member(int(user_id))
        if member:
            return f"{member.display_name} (@{member.name})"
        user = self.bot.get_user(int(user_id))
        if user:
            return f"{user.name} (not currently in this server)"
        return f"Unknown user (`{user_id}`)"

    @commands.command(name="auditteams")
    @commands.guild_only()
    async def auditteams(self, ctx):
        """Show active duplicate roster membership in this server, plus captain data issues."""
        if not await self._require_bot_owner(ctx):
            return
        conn = None
        try:
            conn = self._audit_team_db()
            # Match +teaminfo's roster logic: approved teams in this server, with
            # captain_id plus team_players. Removed/kicked/left players no longer
            # present in team_players are not counted unless they remain captain.
            teams = conn.execute(
                "SELECT id, name, captain_id FROM registered_teams "
                "WHERE guild_id=? AND status='approved' ORDER BY name COLLATE NOCASE",
                (ctx.guild.id,),
            ).fetchall()
            team_by_id = {int(t['id']): t['name'] for t in teams}
            memberships = {}
            for team in teams:
                tid = int(team['id'])
                ids = {int(team['captain_id'])} if team['captain_id'] else set()
                ids.update(int(r['user_id']) for r in conn.execute(
                    "SELECT user_id FROM team_players WHERE team_id=?", (tid,)
                ).fetchall())
                for uid in ids:
                    memberships.setdefault(uid, set()).add(tid)
            duplicates = [(uid, sorted(tids, key=lambda tid: team_by_id[tid].casefold()))
                          for uid, tids in memberships.items() if len(tids) > 1]
            lines = []
            for uid, tids in sorted(duplicates, key=lambda item: self._audit_username(ctx.guild, item[0]).casefold()):
                names = ", ".join(f"**{discord.utils.escape_markdown(team_by_id[tid])}**" for tid in tids)
                lines.append(f"• **{discord.utils.escape_markdown(self._audit_username(ctx.guild, uid))}** — {names}")

            missing_captains = conn.execute(
                "SELECT id, name, captain_id FROM registered_teams "
                "WHERE guild_id=? AND status='approved' AND (captain_id IS NULL OR captain_id=0)",
                (ctx.guild.id,),
            ).fetchall()
            if lines:
                header = f"**Active duplicate memberships — {discord.utils.escape_markdown(ctx.guild.name)}**\n"
                body = "\n".join(lines[:25])
                if len(lines) > 25:
                    body += f"\n…and {len(lines)-25} more player(s)."
                await ctx.reply(header + body, mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none())
            else:
                await ctx.reply(
                    f"✅ No players appear in multiple approved team rosters in **{discord.utils.escape_markdown(ctx.guild.name)}**.",
                    mention_author=False,
                )
            if missing_captains:
                extra = "\n".join(
                    f"• **{discord.utils.escape_markdown(r['name'])}** has no captain ID."
                    for r in missing_captains[:10]
                )
                await ctx.send("**Other team data issues**\n" + extra)
        except FileNotFoundError as exc:
            await ctx.reply(str(exc), mention_author=False)
        except (sqlite3.Error, ValueError) as exc:
            await ctx.reply(f"Team audit failed: {exc}", mention_author=False)
        finally:
            if conn is not None:
                conn.close()

    async def _wait_for_audit_answer(self, ctx, prompt):
        await ctx.reply(prompt, mention_author=False)
        def check(message):
            return message.author.id == ctx.author.id and message.channel.id == ctx.channel.id
        try:
            message = await self.bot.wait_for("message", check=check, timeout=90)
            return message.content.strip()
        except TimeoutError:
            await ctx.send("Timed out. Nothing was changed; run `+resolveauditteams` again.")
            return None

    async def _resolve_audit_user(self, ctx, raw):
        match = re.fullmatch(r"<@!?(\d+)>|\s*(\d+)\s*", raw)
        if match:
            uid = int(match.group(1) or match.group(2))
            return uid if ctx.guild.get_member(uid) or self.bot.get_user(uid) else None
        matches = [m for m in ctx.guild.members
                   if raw.casefold() in {m.name.casefold(), m.display_name.casefold(), str(m).casefold()}]
        if len(matches) == 1:
            return matches[0].id
        return None

    @commands.command(name="resolveauditteams")
    @commands.guild_only()
    async def resolve_audit_teams(self, ctx):
        """Move a player into one chosen approved team, removing other roster entries in this server."""
        if not await self._require_bot_owner(ctx):
            return
        raw_player = await self._wait_for_audit_answer(
            ctx, "**Which player should I resolve?** Send a server username, mention, or user ID."
        )
        if raw_player is None:
            return
        user_id = await self._resolve_audit_user(ctx, raw_player)
        if user_id is None:
            await ctx.reply("I couldn't identify one unique user in this server. Use their mention or user ID.", mention_author=False)
            return
        raw_team = await self._wait_for_audit_answer(
            ctx, f"Which **approved team in {ctx.guild.name}** should <@{user_id}> remain in? Send the exact team name."
        )
        if raw_team is None:
            return

        conn = None
        try:
            conn = self._audit_team_db()
            teams = conn.execute(
                "SELECT id, name, captain_id, vice_captain_id FROM registered_teams "
                "WHERE guild_id=? AND status='approved' ORDER BY name COLLATE NOCASE",
                (ctx.guild.id,),
            ).fetchall()
            selected = [t for t in teams if t['name'].casefold() == raw_team.casefold()]
            if len(selected) != 1:
                choices = ", ".join(t['name'] for t in teams[:20]) or "none"
                await ctx.reply(f"I couldn't find that exact approved team in this server. Available teams: {choices}", mention_author=False)
                return
            target = selected[0]
            # Never silently strip a captain from their team; leadership must be reassigned first.
            captain_elsewhere = [t['name'] for t in teams
                                 if int(t['id']) != int(target['id']) and int(t['captain_id'] or 0) == user_id]
            if captain_elsewhere:
                await ctx.reply(
                    f"I can't automatically move this player because they are captain of **{', '.join(captain_elsewhere)}**. "
                    "Reassign that captain role first, then run `+resolveauditteams` again. No data was changed.",
                    mention_author=False,
                )
                return

            # Only remove active roster rows from other approved teams in this server.
            # Historical/non-approved teams and other servers are left untouched.
            other_ids = [int(t['id']) for t in teams if int(t['id']) != int(target['id'])]
            removed = 0
            if other_ids:
                placeholders = ",".join("?" for _ in other_ids)
                cur = conn.execute(
                    f"DELETE FROM team_players WHERE user_id=? AND team_id IN ({placeholders})",
                    [user_id, *other_ids],
                )
                removed = max(cur.rowcount, 0)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(team_players)")}
            insert_cols = ["team_id", "user_id"]
            values = [int(target['id']), user_id]
            if "added_by" in columns:
                insert_cols.append("added_by")
                values.append(int(ctx.author.id))
            if "added_at" in columns:
                insert_cols.append("added_at")
                values.append(datetime.now(timezone.utc).isoformat())
            conn.execute(
                f"INSERT OR IGNORE INTO team_players ({', '.join(insert_cols)}) VALUES ({', '.join('?' for _ in insert_cols)})",
                values,
            )
            conn.commit()
            await ctx.reply(
                f"✅ Resolved **{discord.utils.escape_markdown(self._audit_username(ctx.guild, user_id))}**. "
                f"They are now listed in **{discord.utils.escape_markdown(target['name'])}** only among approved teams in this server. "
                f"Removed {removed} other active roster entry/entries.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (sqlite3.Error, FileNotFoundError, ValueError) as exc:
            await ctx.reply(f"Couldn't resolve the team memberships: {exc}", mention_author=False)
        finally:
            if conn is not None:
                conn.close()

    def is_locked(self, guild_id):
        if not guild_id:
            return False
        row = self.db.execute(
            "SELECT commands_locked FROM utility_guild_state WHERE guild_id=?",
            (int(guild_id),),
        ).fetchone()
        return bool(row[0]) if row else False

    def is_channel_disabled(self, guild_id, channel_id):
        if not guild_id or not channel_id:
            return False
        return self.db.execute(
            "SELECT 1 FROM utility_disabled_channels WHERE guild_id=? AND channel_id=?",
            (int(guild_id), int(channel_id)),
        ).fetchone() is not None

    async def command_allowed(self, ctx_or_interaction, command_name):
        """Permission gate for formerly admin-only commands in any supported cog.

        A member must have a role whitelisted for this exact command.
        The global lock and disabled-channel rules remain restrictive; they
        never turn one command whitelist into access to unrelated commands.
        """
        guild = getattr(ctx_or_interaction, "guild", None)
        guild_id = getattr(ctx_or_interaction, "guild_id", None) or getattr(guild, "id", None)
        member = getattr(ctx_or_interaction, "author", None) or getattr(ctx_or_interaction, "user", None)
        channel = getattr(ctx_or_interaction, "channel", None)
        channel_id = getattr(ctx_or_interaction, "channel_id", None) or getattr(channel, "id", None)
        name = _normalise_command_name(command_name)

        if member is not None and int(getattr(member, "id", 0)) == BOT_OWNER_ID:
            return True
        if not guild_id or member is None:
            return False
        if self.is_channel_disabled(guild_id, channel_id) and name not in RECOVERY_COMMANDS:
            return False
        # Lock mode still blocks ordinary commands, but whitelist access remains
        # command-specific so one rule can never unlock unrelated commands.
        return has_command_whitelist(self.bot, guild_id, member, name)

    def is_bot_banned(self, user_id):
        return self.db.execute(
            "SELECT 1 FROM bot_bans WHERE user_id=?", (int(user_id),)
        ).fetchone() is not None

    async def global_command_check(self, ctx):
        # Bot bans apply across every server and in DMs, not just one guild.
        if int(ctx.author.id) == BOT_OWNER_ID:
            return True
        if self.is_bot_banned(ctx.author.id):
            try:
                await ctx.reply(
                    "You are banned from using this bot's commands.",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
            return False
        if getattr(getattr(ctx, "command", None), "name", "") == "cancel":
            live_draws = self.bot.get_cog("LiveDraws")
            if live_draws is not None and live_draws.can_cancel_live_draw(ctx):
                return True
        if not ctx.guild:
            return True
        name = _normalise_command_name(getattr(ctx.command, "name", ""))
        if self.is_channel_disabled(ctx.guild.id, ctx.channel.id):
            if name in RECOVERY_COMMANDS and await self._can_manage_utility(ctx.guild.id, ctx.author):
                return True
            return False
        if self.is_locked(ctx.guild.id):
            return has_command_whitelist(self.bot, ctx.guild.id, ctx.author, name)
        return True

    async def global_interaction_check(self, interaction):
        # Preserve any interaction check that existed before this cog loaded.
        previous = self._previous_tree_check
        if previous is not None:
            result = previous(interaction)
            if hasattr(result, "__await__"):
                result = await result
            if not result:
                return False

        if int(interaction.user.id) == BOT_OWNER_ID:
            return True
        if self.is_bot_banned(interaction.user.id):
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "You are banned from using this bot's commands.", ephemeral=True
                    )
                except discord.HTTPException:
                    pass
            return False
        if interaction.guild_id is None:
            return True
        command = getattr(interaction, "command", None)
        name = _normalise_command_name(getattr(command, "name", ""))
        if self.is_channel_disabled(interaction.guild_id, interaction.channel_id):
            return False
        if self.is_locked(interaction.guild_id):
            return has_command_whitelist(self.bot, interaction.guild_id, interaction.user, name)
        return True

    async def _can_manage_utility(self, guild_id, member):
        # Only the configured bot owner may change whitelist and lock settings.
        # Discord Administrator permission does not grant this access.
        return member is not None and int(getattr(member, "id", 0)) == BOT_OWNER_ID

    async def _require_manager(self, ctx):
        if await self._can_manage_utility(ctx.guild.id, ctx.author):
            return True
        await ctx.reply(
            "You are not allowed to manage utility settings.",
            mention_author=False,
        )
        return False

    @commands.command(name="whitelist")
    @commands.guild_only()
    async def whitelist(self, ctx, role: str, *, command_name: str):
        if not await self._require_manager(ctx):
            return
        match = re.fullmatch(r"<@&?(\d+)>|@?(\d+)", role.strip())
        if not match:
            await ctx.reply(
                "Usage: `+whitelist @Role commandname` or `+whitelist ROLE_ID commandname`.",
                mention_author=False,
            )
            return
        role_id = int(next(group for group in match.groups() if group))
        discord_role = ctx.guild.get_role(role_id)
        if discord_role is None:
            await ctx.reply("That role ID doesn't exist in this server.", mention_author=False)
            return
        name = _normalise_command_name(command_name)
        # Store the registered command's canonical name so aliases grant the
        # same command permission without creating a separate permission scope.
        resolved = self.bot.get_command(name)
        if resolved is not None:
            name = _normalise_command_name(resolved.name)
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            await ctx.reply("Give one command name, without a prefix or arguments.", mention_author=False)
            return
        if name in {"whitelist", "unwhitelist", "toggleall", "untoggleall"}:
            await ctx.reply(
                "That utility control command cannot be individually whitelisted. Use the server lock carefully.",
                mention_author=False,
            )
            return
        self.db.execute(
            "INSERT OR IGNORE INTO command_whitelist(guild_id,role_id,command_name) VALUES(?,?,?)",
            (ctx.guild.id, role_id, name),
        )
        self.db.commit()
        await ctx.reply(
            f"Whitelisted **{discord.utils.escape_markdown(discord_role.name)}** (`{role_id}`) for `+{name}`.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="unwhitelist")
    @commands.guild_only()
    async def unwhitelist(self, ctx, role: str, *, command_name: str):
        if not await self._require_manager(ctx):
            return
        match = re.fullmatch(r"<@&?(\d+)>|@?(\d+)", role.strip())
        if not match:
            await ctx.reply(
                "Usage: `+unwhitelist @Role commandname` or `+unwhitelist ROLE_ID commandname`.",
                mention_author=False,
            )
            return
        role_id = int(next(group for group in match.groups() if group))
        name = _normalise_command_name(command_name)
        resolved = self.bot.get_command(name)
        if resolved is not None:
            name = _normalise_command_name(resolved.name)
        cur = self.db.execute(
            "DELETE FROM command_whitelist WHERE guild_id=? AND role_id=? AND command_name=?",
            (ctx.guild.id, role_id, name),
        )
        self.db.commit()
        await ctx.reply(
            "Whitelist rule removed." if cur.rowcount else "No matching whitelist rule was found.",
            mention_author=False,
        )

    @commands.command(name="whitelistlist")
    @commands.guild_only()
    async def whitelistlist(self, ctx):
        if not await self._require_manager(ctx):
            return
        rows = self.db.execute(
            "SELECT role_id, command_name FROM command_whitelist WHERE guild_id=? ORDER BY role_id, command_name",
            (ctx.guild.id,),
        ).fetchall()
        if not rows:
            await ctx.reply("No whitelist rules are configured.", mention_author=False)
            return
        lines = []
        for row in rows:
            role_obj = ctx.guild.get_role(row["role_id"])
            role_label = (
                f"{discord.utils.escape_markdown(role_obj.name)} (`{row['role_id']}`)"
                if role_obj else f"Missing role (`{row['role_id']}`)"
            )
            lines.append(f"{role_label} → `+{row['command_name']}`")
        await ctx.reply(
            "\n".join(lines)[:1900],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="toggleall")
    @commands.guild_only()
    async def toggleall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "INSERT INTO utility_guild_state(guild_id,commands_locked) VALUES(?,1) "
            "ON CONFLICT(guild_id) DO UPDATE SET commands_locked=1",
            (ctx.guild.id,),
        )
        self.db.commit()
        await ctx.reply(
            "🔒 Server-wide command lock enabled. Only members whose roles are whitelisted for the specific command can use it until unlocked.",
            mention_author=False,
        )

    @commands.command(name="untoggleall")
    @commands.guild_only()
    async def untoggleall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "INSERT INTO utility_guild_state(guild_id,commands_locked) VALUES(?,0) "
            "ON CONFLICT(guild_id) DO UPDATE SET commands_locked=0",
            (ctx.guild.id,),
        )
        self.db.commit()
        await ctx.reply("🔓 Server-wide command lock disabled.", mention_author=False)

    @commands.command(name="disableall")
    @commands.guild_only()
    async def disableall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "INSERT OR IGNORE INTO utility_disabled_channels(guild_id,channel_id) VALUES(?,?)",
            (ctx.guild.id, ctx.channel.id),
        )
        self.db.commit()
        await ctx.reply(
            "🔇 Bot commands are now disabled in this channel. Use `+enablechannel` here to restore them.",
            mention_author=False,
        )

    @commands.command(name="enablechannel")
    @commands.guild_only()
    async def enablechannel(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "DELETE FROM utility_disabled_channels WHERE guild_id=? AND channel_id=?",
            (ctx.guild.id, ctx.channel.id),
        )
        self.db.commit()
        await ctx.reply("🔊 Bot commands are enabled in this channel again.", mention_author=False)

    @commands.command(name="enableall")
    @commands.guild_only()
    async def enableall(self, ctx):
        if not await self._require_manager(ctx):
            return
        self.db.execute(
            "DELETE FROM utility_disabled_channels WHERE guild_id=?",
            (ctx.guild.id,),
        )
        self.db.commit()
        await ctx.reply("🔊 Bot commands are enabled in all channels again.", mention_author=False)


    async def _require_bot_owner(self, ctx):
        # Owner-only utility commands can be delegated individually, without
        # changing Bot.is_owner() or granting access to other commands.
        command_name = ctx.command.name if ctx.command else ""
        if await self.command_allowed(ctx, command_name):
            return True
        await ctx.reply(
            "Only the bot owner or a role whitelisted for this command can use it.",
            mention_author=False,
        )
        return False

    async def _resolve_botban_target(self, ctx, raw_target):
        """Resolve a mention/ID, or an exact username in the current server/cache."""
        value = raw_target.strip()
        match = re.fullmatch(r"<@!?(\d+)>|(\d+)", value)
        if match:
            user_id = int(match.group(1) or match.group(2))
            user = self.bot.get_user(user_id)
            if user is None:
                try:
                    user = await self.bot.fetch_user(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return user_id, None
            return user_id, user

        # Username resolution is intentionally exact to avoid banning the wrong person.
        candidates = {}
        if ctx.guild:
            for member in ctx.guild.members:
                if member.name.casefold() == value.casefold() or member.display_name.casefold() == value.casefold():
                    candidates[member.id] = member
        for user in self.bot.users:
            if user.name.casefold() == value.casefold():
                candidates[user.id] = user
        if len(candidates) == 1:
            user = next(iter(candidates.values()))
            return user.id, user
        if len(candidates) > 1:
            return None, "ambiguous"
        return None, None

    @commands.command(name="botban")
    @commands.guild_only()
    async def botban(self, ctx, target: str, *, reason: str = "No reason provided"):
        """Owner-only: prevent a user from using any bot command."""
        if not await self._require_bot_owner(ctx):
            return
        user_id, user = await self._resolve_botban_target(ctx, target)
        if user_id == BOT_OWNER_ID:
            await ctx.reply("The bot owner cannot be bot-banned.", mention_author=False)
            return
        if user_id is None and user == "ambiguous":
            await ctx.reply("That username matches multiple users. Use a mention or numeric user ID.", mention_author=False)
            return
        if user_id is None:
            await ctx.reply("I couldn't resolve that user. Use a mention, numeric user ID, or exact username of a visible/cached user.", mention_author=False)
            return
        if self.is_bot_banned(user_id):
            await ctx.reply(f"User `{user_id}` is already bot-banned.", mention_author=False)
            return
        display_name = getattr(user, "name", str(user_id))
        self.db.execute(
            "INSERT INTO bot_bans(user_id, username, banned_by, reason, banned_at) VALUES(?,?,?,?,?)",
            (user_id, str(display_name), ctx.author.id, reason[:500], discord.utils.utcnow().isoformat()),
        )
        self.db.commit()
        await ctx.reply(
            f"🔨 Bot-banned **{discord.utils.escape_markdown(str(display_name))}** (`{user_id}`).\nReason: {discord.utils.escape_markdown(reason[:500])}",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="botunban")
    @commands.guild_only()
    async def botunban(self, ctx, target: str):
        """Owner-only: restore a bot-banned user's command access."""
        if not await self._require_bot_owner(ctx):
            return
        match = re.fullmatch(r"<@!?(\d+)>|(\d+)", target.strip())
        if not match:
            await ctx.reply("Usage: `+botunban <user ID or mention>`", mention_author=False)
            return
        user_id = int(match.group(1) or match.group(2))
        cur = self.db.execute("DELETE FROM bot_bans WHERE user_id=?", (user_id,))
        self.db.commit()
        await ctx.reply(
            f"User `{user_id}` is unbanned from the bot." if cur.rowcount else f"User `{user_id}` was not bot-banned.",
            mention_author=False,
        )

    @commands.command(name="botbanlist")
    @commands.guild_only()
    async def botbanlist(self, ctx):
        """Owner-only: list persistent bot bans."""
        if not await self._require_bot_owner(ctx):
            return
        rows = self.db.execute(
            "SELECT user_id, username, reason, banned_at FROM bot_bans ORDER BY banned_at DESC"
        ).fetchall()
        if not rows:
            await ctx.reply("There are no bot-banned users.", mention_author=False)
            return
        lines = [
            f"`{row['user_id']}` — **{discord.utils.escape_markdown(row['username'] or 'Unknown user')}**"
            + (f" — {discord.utils.escape_markdown(row['reason'])}" if row['reason'] else "")
            for row in rows
        ]
        # Keep messages within Discord's normal message-size limit.
        text = "\n".join(lines)
        for start in range(0, len(text), 1800):
            await ctx.reply(text[start:start + 1800], mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    async def _post_approved_team_embeds(self, guild_id, channel_id):
        """Read approved teams from team_registration.db and post one embed per team."""
        import sqlite3

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return 0, "I couldn't access the selected channel."
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild_id:
            return 0, "The selected channel is not a text channel in this server."

        conn = None
        try:
            conn = sqlite3.connect("team_registration.db")
            conn.row_factory = sqlite3.Row
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS approved_team_post_settings (
                    guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL
                );
            """)
            # Track posts per channel, so a test post in one channel doesn't block
            # posting the same approved teams in the real announcement channel.
            posted_columns = [r[1] for r in conn.execute("PRAGMA table_info(approved_team_posted)").fetchall()]
            if posted_columns and "channel_id" not in posted_columns:
                conn.execute("ALTER TABLE approved_team_posted RENAME TO approved_team_posted_legacy")
            conn.execute("""CREATE TABLE IF NOT EXISTS approved_team_posted (
                guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, team_id INTEGER NOT NULL,
                message_id INTEGER, PRIMARY KEY(guild_id, channel_id, team_id)
            )""")
            # Legacy test posts have no reliable channel-specific record; don't let
            # them block posting in the selected channel.
            conn.execute("DROP TABLE IF EXISTS approved_team_posted_legacy")
            # Ensure old databases have permanent creation-order team numbers.
            team_columns = [r[1] for r in conn.execute("PRAGMA table_info(registered_teams)").fetchall()]
            if "team_number" not in team_columns:
                conn.execute("ALTER TABLE registered_teams ADD COLUMN team_number INTEGER")
            guild_ids = [r[0] for r in conn.execute("SELECT DISTINCT guild_id FROM registered_teams").fetchall()]
            for existing_guild_id in guild_ids:
                current = conn.execute(
                    "SELECT COALESCE(MAX(team_number), 0) FROM registered_teams WHERE guild_id=?",
                    (existing_guild_id,),
                ).fetchone()[0]
                missing = conn.execute(
                    "SELECT id FROM registered_teams WHERE guild_id=? AND team_number IS NULL ORDER BY created_at ASC, id ASC",
                    (existing_guild_id,),
                ).fetchall()
                for missing_row in missing:
                    current += 1
                    conn.execute("UPDATE registered_teams SET team_number=? WHERE id=?", (current, int(missing_row["id"])))
            conn.commit()
            rows = conn.execute(
                "SELECT id, name, captain_id, logo_url, team_number FROM registered_teams "
                "WHERE guild_id=? AND status='approved' ORDER BY team_number ASC, created_at ASC, id ASC",
                (guild_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            return 0, f"Couldn't read `team_registration.db` ({type(exc).__name__})."
        finally:
            if conn is not None:
                conn.close()

        if not rows:
            return 0, "No approved teams were found in this server."

        # Persist the chosen channel so later approvals can be announced automatically.
        try:
            conn = sqlite3.connect("team_registration.db")
            conn.execute(
                "INSERT INTO approved_team_post_settings(guild_id, channel_id) VALUES(?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
                (guild_id, channel_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Use the permanent team number assigned at creation, not the team's
        # alphabetical position or the count of posts in this channel.
        posted = 0
        for number, team in enumerate(rows, start=1):
            conn = sqlite3.connect("team_registration.db")
            exists = conn.execute(
                "SELECT 1 FROM approved_team_posted WHERE guild_id=? AND channel_id=? AND team_id=?",
                (guild_id, channel_id, int(team["id"])),
            ).fetchone()
            conn.close()
            if exists:
                continue
            # Match +teamlist: number approved teams in their displayed order.
            name = discord.utils.escape_markdown(str(team["name"] or "Unknown Team"))
            captain_id = int(team["captain_id"])
            logo_url = str(team["logo_url"] or "").strip()
            embed = discord.Embed(
                title="Kashmir Titans S1",
                description=(
                    f"{number}. **__Team name:__** **`{name}`**\n"
                    f"**__Team captain:__** <@{captain_id}>"
                ),
                color=discord.Color.from_rgb(55, 150, 255),
            )
            if logo_url.startswith(("https://", "http://")):
                # Thumbnail on the right and the full-size image at the bottom.
                embed.set_thumbnail(url=logo_url)
                embed.set_image(url=logo_url)
            try:
                # Captain mention is shown inside the embed only; no separate ping message.
                message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                conn = sqlite3.connect("team_registration.db")
                conn.execute(
                    "INSERT OR IGNORE INTO approved_team_posted(guild_id, channel_id, team_id, message_id) VALUES(?,?,?,?)",
                    (guild_id, channel_id, int(team["id"]), message.id),
                )
                conn.commit()
                conn.close()
                posted += 1
            except discord.HTTPException as exc:
                return posted, f"Discord returned {type(exc).__name__}."
        return posted, None

    @commands.command(name="sendteam")
    @commands.guild_only()
    async def send_single_approved_team(self, ctx, *, team_name: str):
        """Owner-only: post one approved team's announcement embed in this channel."""
        if not await self.command_allowed(ctx, ctx.command.name if ctx.command else ""):
            await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.", mention_author=False)
            return

        import sqlite3
        conn = None
        try:
            conn = sqlite3.connect("team_registration.db")
            conn.row_factory = sqlite3.Row
            columns = [row[1] for row in conn.execute("PRAGMA table_info(registered_teams)").fetchall()]
            if not columns:
                await ctx.reply("The registered teams table wasn't found in `team_registration.db`.", mention_author=False)
                return

            # Ensure permanent creation-order numbering exists for older databases.
            if "team_number" not in columns:
                conn.execute("ALTER TABLE registered_teams ADD COLUMN team_number INTEGER")
            guild_ids = [row[0] for row in conn.execute(
                "SELECT DISTINCT guild_id FROM registered_teams"
            ).fetchall()]
            for guild_id in guild_ids:
                current = conn.execute(
                    "SELECT COALESCE(MAX(team_number), 0) FROM registered_teams WHERE guild_id=?",
                    (guild_id,),
                ).fetchone()[0]
                missing = conn.execute(
                    "SELECT id FROM registered_teams WHERE guild_id=? AND team_number IS NULL "
                    "ORDER BY created_at ASC, id ASC", (guild_id,)
                ).fetchall()
                for row in missing:
                    current += 1
                    conn.execute(
                        "UPDATE registered_teams SET team_number=? WHERE id=?",
                        (current, int(row["id"]))
                    )
            conn.commit()
            approved_teams = conn.execute(
                "SELECT id, name, captain_id, logo_url, team_number FROM registered_teams "
                "WHERE guild_id=? AND status='approved' ORDER BY team_number ASC, created_at ASC, id ASC",
                (ctx.guild.id,),
            ).fetchall()
            team = next(
                (row for row in approved_teams if str(row["name"]).casefold() == team_name.strip().casefold()),
                None,
            )
            team_number = next(
                (index for index, row in enumerate(approved_teams, start=1) if team is not None and int(row["id"]) == int(team["id"])),
                None,
            )
        except sqlite3.Error as exc:
            await ctx.reply(f"Couldn't read `team_registration.db` ({type(exc).__name__}).", mention_author=False)
            return
        finally:
            if conn is not None:
                conn.close()

        if team is None:
            await ctx.reply(
                f"I couldn't find an approved team named **{discord.utils.escape_markdown(team_name)}**. Check the spelling and try again.",
                mention_author=False,
            )
            return

        number = team_number
        name = discord.utils.escape_markdown(str(team["name"] or "Unknown Team"))
        captain_id = int(team["captain_id"])
        logo_url = str(team["logo_url"] or "").strip()
        embed = discord.Embed(
            title="Kashmir Titans S1",
            description=(
                f"{number}. **__Team name:__** **`{name}`**\n"
                f"**__Team captain:__** <@{captain_id}>"
            ),
            color=discord.Color.from_rgb(55, 150, 255),
        )
        if logo_url.startswith(("https://", "http://")):
            embed.set_thumbnail(url=logo_url)
            embed.set_image(url=logo_url)

        try:
            await ctx.channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            await ctx.reply(f"Couldn't send the team embed ({type(exc).__name__}).", mention_author=False)
            return
        await ctx.reply(f"Posted the embed for **{name}** in this channel.", mention_author=False)

    @commands.command(name="setapprovedteamschannel", aliases=["setteamannouncechannel"])
    @commands.guild_only()
    async def set_approved_teams_channel(self, ctx, channel: discord.TextChannel):
        """Owner-only: choose the one channel for future approved-team announcements."""
        if not await self.command_allowed(ctx, ctx.command.name if ctx.command else ""):
            await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.", mention_author=False)
            return
        if channel.guild.id != ctx.guild.id:
            await ctx.reply("Choose a text channel from this server.", mention_author=False)
            return
        import sqlite3
        conn = None
        try:
            conn = sqlite3.connect("team_registration.db")
            conn.execute("""CREATE TABLE IF NOT EXISTS approved_team_post_settings (
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL
            )""")
            conn.execute(
                "INSERT INTO approved_team_post_settings(guild_id, channel_id) VALUES(?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
                (ctx.guild.id, channel.id),
            )
            conn.commit()
        except sqlite3.Error as exc:
            await ctx.reply(f"Couldn't save the channel ({type(exc).__name__}).", mention_author=False)
            return
        finally:
            if conn is not None:
                conn.close()
        await ctx.reply(
            f"Newly approved teams will be posted in {channel.mention} only. "
            "This setting is saved across bot restarts.",
            mention_author=False,
        )

    @commands.command(name="postapprovedteams", aliases=["postteams"])
    @commands.guild_only()
    async def post_approved_teams(self, ctx, channel: discord.TextChannel):
        """Owner-only: preview and confirm posting every approved team to a channel."""
        if not await self.command_allowed(ctx, ctx.command.name if ctx.command else ""):
            await ctx.reply("Only the bot owner or a role whitelisted for this command can use it.", mention_author=False)
            return

        import sqlite3
        conn = None
        try:
            conn = sqlite3.connect("team_registration.db")
            count = conn.execute(
                "SELECT COUNT(*) FROM registered_teams WHERE guild_id=? AND status='approved'",
                (ctx.guild.id,),
            ).fetchone()[0]
        except sqlite3.Error as exc:
            await ctx.reply(f"Couldn't read `team_registration.db` ({type(exc).__name__}).", mention_author=False)
            return
        finally:
            if conn is not None:
                conn.close()

        if count == 0:
            await ctx.reply("There are no approved teams to post.", mention_author=False)
            return
        view = PostApprovedTeamsView(self, ctx.guild.id, channel.id, ctx.author.id)
        await ctx.reply(
            f"**Confirm posting {count} approved team embed(s)** in {channel.mention}?\n"
            "Each team gets one embed with its logo as both the right-side thumbnail and the full-size image at the bottom.",
            view=view,
            mention_author=False,
        )


async def setup(bot):
    await bot.add_cog(Utility(bot))
