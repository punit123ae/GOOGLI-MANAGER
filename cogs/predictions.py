from discord.ext import commands


class Predictions(commands.Cog):
    """Prediction commands will be added here."""
    def __init__(self, bot):
        self.bot = bot


async def setup(bot):
    await bot.add_cog(Predictions(bot))
