import nextcord
from nextcord.ext import commands

from .service import ServerBoostService
from .views import ServerBoostPanelView


class ServerBoostCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.service = ServerBoostService(bot)

    def create_panel_view(self, show_admin_back: bool = False) -> ServerBoostPanelView:
        return ServerBoostPanelView(self, show_admin_back=show_admin_back)

    @commands.Cog.listener()
    async def on_member_update(self, before: nextcord.Member, after: nextcord.Member) -> None:
        if after.guild is None:
            return

        if before.premium_since is None and after.premium_since is not None:
            settings = await self.service.get_settings(after.guild.id)
            if settings is not None and bool(settings["enabled"]):
                await self.service.handle_boost_started(after, settings=settings)
            return

        if before.premium_since is not None and after.premium_since is None:
            settings = await self.service.get_settings(after.guild.id)
            if settings is not None:
                await self.service.handle_boost_expired(after, settings=settings)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(ServerBoostCog(bot))
