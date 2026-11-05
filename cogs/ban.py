import logging

import discord
from discord.ext import commands

logger = logging.getLogger("cogs.ban")


async def _try_dm(user: discord.abc.User, text: str):
    """ЛС могут быть закрыты: это не ошибка."""
    try:
        await user.send(text)
    except discord.Forbidden:
        pass
    except discord.HTTPException as e:
        logger.warning(f"[BAN] Не удалось отправить ЛС {user}: {e}")


def _check_hierarchy(ctx: commands.Context, target: discord.Member) -> str | None:
    """Возвращает текст отказа или None, если действие допустимо."""
    guild = ctx.guild
    if target.id == ctx.author.id:
        return "себя банить нельзя."
    if target.id == guild.owner_id:
        return "владельца сервера забанить нельзя."
    if target.top_role >= guild.me.top_role:
        return "роль этого участника не ниже роли бота, не могу его забанить."
    if ctx.author.id != guild.owner_id and target.top_role >= ctx.author.top_role:
        return "роль этого участника не ниже твоей."
    return None


class BanService(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _report_error(self, ctx, e: Exception, text: str):
        logger.error(f"[BAN Error] {e}", exc_info=e)
        if hasattr(self.bot, "supervisor"):
            await self.bot.supervisor.handle_cog_error("ban", e, ctx=ctx)
        else:
            await ctx.send(f"{text}: {e}")

    @commands.command(name="ban")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context, member: discord.Member, *, reason: str = "причина не указана"):
        if (denied := _check_hierarchy(ctx, member)) is not None:
            await ctx.send(denied)
            return

        try:
            # Проверки выше пройдены, значит бан почти наверняка удастся, и ЛС не соврёт
            await _try_dm(member, "вас забанили **НАХУЙ** модераторы сервера.")
            await member.ban(reason=f"{ctx.author}: {reason}")
            await ctx.send(f"пользователь {member.display_name} забанен.")
        except discord.Forbidden:
            await ctx.send("недостаточно прав для блокировки этого участника.")
        except Exception as e:
            await self._report_error(ctx, e, "ошибка при бане")

    @commands.command(name="unban")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx: commands.Context, user_id: int, *, reason: str = "причина не указана"):
        try:
            user = await self.bot.fetch_user(user_id)
            # Сначала разбан: если юзера нет в бан-листе, получим NotFound и не пошлём ложное ЛС
            await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
            await _try_dm(user, "ты разбанен.")
            await ctx.send(f"пользователь {user.name} разбанен.")
        except discord.NotFound:
            await ctx.send("пользователь с таким ID не найден или не находится в бан-листе.")
        except discord.Forbidden:
            await ctx.send("недостаточно прав для разбана этого участника.")
        except Exception as e:
            await self._report_error(ctx, e, "ошибка при разбане")


async def setup(bot: commands.Bot):
    await bot.add_cog(BanService(bot))
