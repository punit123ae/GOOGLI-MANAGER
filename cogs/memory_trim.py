"""Keeps the bot's memory low on small hosts.

Every 5 minutes: garbage-collect and give freed memory back to the system (the same trim that
runs after each drawn image). When more than 10 MB comes back, one line is printed so you can
see it in the console. Nothing here has commands.
"""
from discord.ext import commands, tasks

from .h2h import trim_memory


def _rss_mb():
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


class MemoryTrim(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    @tasks.loop(minutes=5)
    async def loop(self):
        before = _rss_mb()
        if before is not None and before < 120:      # small already: a full collection would only cost CPU
            return
        trim_memory()
        after = _rss_mb()
        if before is not None and after is not None and before - after >= 10:
            print(f"[memory] {before:.0f} MB -> {after:.0f} MB after trim", flush=True)

    @loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(MemoryTrim(bot))
