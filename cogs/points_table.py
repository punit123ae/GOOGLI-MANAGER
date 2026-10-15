from discord.ext import commands


class PointsTable(commands.Cog):
    """Points-table commands will be added here after team/group rules are defined."""
    def __init__(self, bot):
        self.bot = bot


async def setup(bot):
    await bot.add_cog(PointsTable(bot))
