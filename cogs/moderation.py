"""
Moderation cog (discord.py 2.x): ban, unban, kick, mute, unmute, timeout, untimeout

Key behaviour
-------------
* Owner-locked punishments: if the BOT OWNER mutes/times out/bans someone, only the
  bot owner can undo it with the bot. If anyone else removes it (manually, from
  another bot, from Discord's UI...), this bot automatically re-applies it for the
  time that was LEFT.
* Punishments by normal moderators are not locked - anyone with the permission can undo.
* Permission checks + role hierarchy on every action (mod vs target, bot vs target).
* Mute = "Muted" role (like Carl). Timeout = Discord's native timeout.
* Punishments survive restarts (SQLite) and are re-applied if the user leaves and rejoins.

Required intents: guilds, members, moderation (bans). Enable "Server Members Intent"
in the Developer Portal. Bot needs: Kick, Ban, Moderate Members, Manage Roles.
"""

import re
import sqlite3
import time
from datetime import timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks

DB_PATH = "moderation.db"
MAX_TIMEOUT_SECONDS = 28 * 24 * 3600  # Discord limit

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
DUR_FULL = re.compile(r"^(?:\d+\s*[smhdw])+$", re.I)
DUR_PART = re.compile(r"(\d+)\s*([smhdw])", re.I)


def parse_duration(text: str) -> Optional[int]:
    """'10m', '1h30m', '2d' -> seconds. None if not a duration."""
    if not text or not DUR_FULL.match(text.strip()):
        return None
    return sum(int(n) * UNITS[u.lower()] for n, u in DUR_PART.findall(text))


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    parts = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{name}")
            seconds %= size
    return " ".join(parts) or "0s"


def mod_perms(**perms):
    """Permission check. The bot owner always passes."""
    async def predicate(ctx: commands.Context):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if await ctx.bot.is_owner(ctx.author):
            return True
        have = ctx.author.guild_permissions
        missing = [p for p, v in perms.items() if getattr(have, p) != v]
        if missing:
            raise commands.MissingPermissions(missing)
        return True
    return commands.check(predicate)


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS punishments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,              -- mute | timeout | ban
                expires_at REAL,                 -- NULL = permanent
                moderator_id INTEGER NOT NULL,
                locked INTEGER NOT NULL DEFAULT 0,  -- 1 = applied by bot owner
                reason TEXT,
                created_at REAL NOT NULL,
                UNIQUE (guild_id, user_id, kind)
            );
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                muted_role_id INTEGER
            );
            """
        )
        self.db.commit()

    async def cog_load(self):
        self.expiry_loop.start()

    async def cog_unload(self):
        self.expiry_loop.cancel()
        self.db.close()

    # ------------------------------------------------------------------ db helpers
    def get_active(self, guild_id: int, user_id: int, kind: str):
        row = self.db.execute(
            "SELECT * FROM punishments WHERE guild_id=? AND user_id=? AND kind=?",
            (guild_id, user_id, kind),
        ).fetchone()
        if row and row["expires_at"] is not None and row["expires_at"] <= time.time():
            return None
        return row

    def save(self, guild_id, user_id, kind, expires_at, moderator_id, locked, reason):
        self.db.execute(
            """INSERT INTO punishments
               (guild_id,user_id,kind,expires_at,moderator_id,locked,reason,created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(guild_id,user_id,kind) DO UPDATE SET
                 expires_at=excluded.expires_at, moderator_id=excluded.moderator_id,
                 locked=excluded.locked, reason=excluded.reason, created_at=excluded.created_at""",
            (guild_id, user_id, kind, expires_at, moderator_id, int(locked), reason, time.time()),
        )
        self.db.commit()

    def drop(self, guild_id: int, user_id: int, kind: str):
        self.db.execute(
            "DELETE FROM punishments WHERE guild_id=? AND user_id=? AND kind=?",
            (guild_id, user_id, kind),
        )
        self.db.commit()

    def muted_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        row = self.db.execute(
            "SELECT muted_role_id FROM guild_config WHERE guild_id=?", (guild.id,)
        ).fetchone()
        return guild.get_role(row["muted_role_id"]) if row and row["muted_role_id"] else None

    @staticmethod
    def remaining(row) -> Optional[float]:
        """Seconds left, None if permanent."""
        if row["expires_at"] is None:
            return None
        return max(0.0, row["expires_at"] - time.time())

    # ------------------------------------------------------------------ check helpers
    async def hierarchy_error(self, ctx: commands.Context, target) -> Optional[str]:
        """Return an error string if ctx.author may not act on target, else None."""
        guild = ctx.guild
        if target.id == ctx.author.id:
            return "You can't do that to yourself."
        if target.id == self.bot.user.id:
            return "I won't do that to myself."
        is_owner = await self.bot.is_owner(ctx.author)
        if await self.bot.is_owner(target) and not is_owner:
            return "You can't act on my owner."
        if target.id == guild.owner_id and not is_owner:
            return "You can't act on the server owner."

        member = target if isinstance(target, discord.Member) else guild.get_member(target.id)
        if member is None:
            return None  # not in server (e.g. banning by ID) -> no hierarchy to check

        if member.id == guild.owner_id:
            return "I can't act on the server owner."
        if not is_owner and ctx.author.id != guild.owner_id:
            if ctx.author.top_role <= member.top_role:
                return "Your highest role must be above the target's highest role."
        if guild.me.top_role <= member.top_role:
            return "My highest role must be above the target's highest role."
        return None

    async def locked_error(self, ctx, rec) -> Optional[str]:
        """Block non-owners from touching an owner-locked punishment."""
        if rec and rec["locked"] and not await self.bot.is_owner(ctx.author):
            return "That punishment was applied by the bot owner - only the bot owner can change or remove it."
        return None

    async def _dm(self, user, text: str):
        try:
            await user.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @staticmethod
    def split_duration(duration: Optional[str], reason: Optional[str]):
        """Allow `!mute @user spamming` (no duration) as well as `!mute @user 10m spamming`."""
        secs = parse_duration(duration) if duration else None
        if duration and secs is None:
            reason = f"{duration} {reason or ''}".strip()
        return secs, (reason or "No reason provided")

    # ------------------------------------------------------------------ setup
    @commands.hybrid_command(name="setupmute", description="Create/configure the Muted role")
    @commands.guild_only()
    @mod_perms(manage_roles=True)
    async def setupmute(self, ctx: commands.Context):
        role = self.muted_role(ctx.guild) or discord.utils.get(ctx.guild.roles, name="Muted")
        async with ctx.typing():
            if role is None:
                role = await ctx.guild.create_role(name="Muted", reason="Mute setup")
            if ctx.guild.me.top_role <= role:
                return await ctx.send("Move my role above the Muted role first.")
            for ch in ctx.guild.channels:
                try:
                    await ch.set_permissions(
                        role, send_messages=False, add_reactions=False, speak=False,
                        send_messages_in_threads=False, create_public_threads=False,
                        create_private_threads=False, reason="Mute setup",
                    )
                except discord.HTTPException:
                    pass
            self.db.execute(
                "INSERT INTO guild_config (guild_id, muted_role_id) VALUES (?,?) "
                "ON CONFLICT(guild_id) DO UPDATE SET muted_role_id=excluded.muted_role_id",
                (ctx.guild.id, role.id),
            )
            self.db.commit()
        await ctx.send(f"Muted role set to {role.mention}.")

    # ------------------------------------------------------------------ kick
    @commands.hybrid_command(name="kick", description="Kick a member")
    @commands.guild_only()
    @mod_perms(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if err := await self.hierarchy_error(ctx, member):
            return await ctx.send(err)
        await self._dm(member, f"You were kicked from **{ctx.guild.name}**: {reason}")
        await member.kick(reason=f"{ctx.author} | {reason}")
        await ctx.send(f"Kicked **{member}**. Reason: {reason}")

    # ------------------------------------------------------------------ ban / unban
    @commands.hybrid_command(name="ban", description="Ban a user (optional duration)")
    @commands.guild_only()
    @mod_perms(ban_members=True)
    async def ban(self, ctx, user: discord.User, duration: Optional[str] = None, *, reason: Optional[str] = None):
        if err := await self.hierarchy_error(ctx, user):
            return await ctx.send(err)
        existing = self.get_active(ctx.guild.id, user.id, "ban")
        if err := await self.locked_error(ctx, existing):
            return await ctx.send(err)

        secs, reason = self.split_duration(duration, reason)
        expires = time.time() + secs if secs else None
        locked = await self.bot.is_owner(ctx.author)

        await self._dm(user, f"You were banned from **{ctx.guild.name}**: {reason}")
        # Save first so the unban listener never mistakes our own action for tampering.
        self.save(ctx.guild.id, user.id, "ban", expires, ctx.author.id, locked, reason)
        await ctx.guild.ban(user, reason=f"{ctx.author} | {reason}", delete_message_seconds=0)
        await ctx.send(
            f"Banned **{user}** {'for ' + fmt_duration(secs) if secs else 'permanently'}. Reason: {reason}"
        )

    @commands.hybrid_command(name="unban", description="Unban a user")
    @commands.guild_only()
    @mod_perms(ban_members=True)
    async def unban(self, ctx, user: discord.User, *, reason: str = "No reason provided"):
        rec = self.get_active(ctx.guild.id, user.id, "ban")
        if err := await self.locked_error(ctx, rec):
            return await ctx.send(err)
        self.drop(ctx.guild.id, user.id, "ban")  # drop BEFORE unbanning
        try:
            await ctx.guild.unban(user, reason=f"{ctx.author} | {reason}")
        except discord.NotFound:
            return await ctx.send("That user isn't banned.")
        await ctx.send(f"Unbanned **{user}**.")

    # ------------------------------------------------------------------ mute / unmute
    @commands.hybrid_command(name="mute", description="Mute a member with the Muted role")
    @commands.guild_only()
    @mod_perms(moderate_members=True)
    async def mute(self, ctx, member: discord.Member, duration: Optional[str] = None, *, reason: Optional[str] = None):
        role = self.muted_role(ctx.guild)
        if role is None:
            return await ctx.send("No Muted role configured. Run `setupmute` first.")
        if ctx.guild.me.top_role <= role:
            return await ctx.send("My highest role must be above the Muted role.")
        if err := await self.hierarchy_error(ctx, member):
            return await ctx.send(err)
        existing = self.get_active(ctx.guild.id, member.id, "mute")
        if err := await self.locked_error(ctx, existing):
            return await ctx.send(err)

        secs, reason = self.split_duration(duration, reason)
        expires = time.time() + secs if secs else None
        locked = await self.bot.is_owner(ctx.author)

        self.save(ctx.guild.id, member.id, "mute", expires, ctx.author.id, locked, reason)
        await member.add_roles(role, reason=f"{ctx.author} | {reason}")
        await self._dm(member, f"You were muted in **{ctx.guild.name}**: {reason}")
        await ctx.send(
            f"Muted **{member}** {'for ' + fmt_duration(secs) if secs else 'permanently'}. Reason: {reason}"
        )

    @commands.hybrid_command(name="unmute", description="Remove a mute")
    @commands.guild_only()
    @mod_perms(moderate_members=True)
    async def unmute(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        rec = self.get_active(ctx.guild.id, member.id, "mute")
        if err := await self.locked_error(ctx, rec):
            return await ctx.send(err)
        if err := await self.hierarchy_error(ctx, member):
            return await ctx.send(err)
        role = self.muted_role(ctx.guild)
        if not rec and not (role and role in member.roles):
            return await ctx.send("That member isn't muted.")
        self.drop(ctx.guild.id, member.id, "mute")  # drop BEFORE removing the role
        if role and role in member.roles:
            await member.remove_roles(role, reason=f"{ctx.author} | {reason}")
        await ctx.send(f"Unmuted **{member}**.")

    # ------------------------------------------------------------------ timeout / untimeout
    @commands.hybrid_command(name="timeout", aliases=["to"], description="Timeout a member (max 28d)")
    @commands.guild_only()
    @mod_perms(moderate_members=True)
    async def timeout(self, ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
        secs = parse_duration(duration)
        if not secs:
            return await ctx.send("Invalid duration. Examples: `10m`, `2h`, `1d12h`.")
        if secs > MAX_TIMEOUT_SECONDS:
            return await ctx.send("Discord timeouts can't exceed 28 days.")
        if err := await self.hierarchy_error(ctx, member):
            return await ctx.send(err)
        existing = self.get_active(ctx.guild.id, member.id, "timeout")
        if err := await self.locked_error(ctx, existing):
            return await ctx.send(err)

        locked = await self.bot.is_owner(ctx.author)
        self.save(ctx.guild.id, member.id, "timeout", time.time() + secs, ctx.author.id, locked, reason)
        await member.timeout(timedelta(seconds=secs), reason=f"{ctx.author} | {reason}")
        await self._dm(member, f"You were timed out in **{ctx.guild.name}** for {fmt_duration(secs)}: {reason}")
        await ctx.send(f"Timed out **{member}** for {fmt_duration(secs)}. Reason: {reason}")

    @commands.hybrid_command(name="untimeout", aliases=["rto", "removetimeout"], description="Remove a timeout")
    @commands.guild_only()
    @mod_perms(moderate_members=True)
    async def untimeout(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        rec = self.get_active(ctx.guild.id, member.id, "timeout")
        if err := await self.locked_error(ctx, rec):
            return await ctx.send(err)
        if err := await self.hierarchy_error(ctx, member):
            return await ctx.send(err)
        if not member.is_timed_out() and not rec:
            return await ctx.send("That member isn't timed out.")
        self.drop(ctx.guild.id, member.id, "timeout")  # drop BEFORE removing
        await member.timeout(None, reason=f"{ctx.author} | {reason}")
        await ctx.send(f"Removed timeout from **{member}**.")

    # ------------------------------------------------------------------ enforcement listeners
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = after.guild

        # 1) Timeout removed/shortened by someone else -> re-apply remaining time
        if before.timed_out_until != after.timed_out_until:
            rec = self.get_active(guild.id, after.id, "timeout")
            if rec and rec["locked"]:
                left = self.remaining(rec)
                until = after.timed_out_until.timestamp() if after.timed_out_until else 0
                if left and left > 2 and until < rec["expires_at"] - 5:
                    try:
                        await after.timeout(
                            timedelta(seconds=min(left, MAX_TIMEOUT_SECONDS)),
                            reason="Owner-locked timeout re-applied",
                        )
                    except discord.HTTPException:
                        pass

        # 2) Muted role removed by someone else -> give it back
        role = self.muted_role(guild)
        if role and role in before.roles and role not in after.roles:
            rec = self.get_active(guild.id, after.id, "mute")
            if rec and rec["locked"]:
                try:
                    await after.add_roles(role, reason="Owner-locked mute re-applied")
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        rec = self.get_active(guild.id, user.id, "ban")
        if rec and rec["locked"]:  # our own unban command drops the record first
            try:
                await guild.ban(user, reason="Owner-locked ban re-applied", delete_message_seconds=0)
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Rejoin evasion: re-apply any active mute/timeout."""
        role = self.muted_role(member.guild)
        if role and self.get_active(member.guild.id, member.id, "mute"):
            try:
                await member.add_roles(role, reason="Active mute re-applied on rejoin")
            except discord.HTTPException:
                pass
        rec = self.get_active(member.guild.id, member.id, "timeout")
        if rec:
            left = self.remaining(rec)
            if left and left > 2:
                try:
                    await member.timeout(
                        timedelta(seconds=min(left, MAX_TIMEOUT_SECONDS)),
                        reason="Active timeout re-applied on rejoin",
                    )
                except discord.HTTPException:
                    pass

    # ------------------------------------------------------------------ expiry
    @tasks.loop(seconds=20)
    async def expiry_loop(self):
        rows = self.db.execute(
            "SELECT * FROM punishments WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (time.time(),),
        ).fetchall()
        for r in rows:
            self.drop(r["guild_id"], r["user_id"], r["kind"])  # drop first = no re-apply
            guild = self.bot.get_guild(r["guild_id"])
            if guild is None:
                continue
            try:
                if r["kind"] == "mute":
                    member, role = guild.get_member(r["user_id"]), self.muted_role(guild)
                    if member and role and role in member.roles:
                        await member.remove_roles(role, reason="Mute expired")
                elif r["kind"] == "ban":
                    await guild.unban(discord.Object(id=r["user_id"]), reason="Tempban expired")
                # timeouts expire on Discord's side automatically
            except discord.HTTPException:
                pass

    @expiry_loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ errors
    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        error = getattr(error, "original", error)
        if isinstance(error, commands.MissingPermissions):
            need = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
            await ctx.send(f"You need the **{need}** permission to do that.")
        elif isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
            await ctx.send("I couldn't find that user.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This only works in a server.")
        elif isinstance(error, discord.Forbidden):
            await ctx.send("I don't have permission to do that (check my role position).")
        elif isinstance(error, commands.CheckFailure):
            pass
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
