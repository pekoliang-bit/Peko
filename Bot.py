import os
import re
import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

def count_text_and_money(text: str) -> dict:
    cleaned = re.sub(r'<@!?[0-9]+>', '', text)
    non_space_chars = re.sub(r'\s+', '', cleaned)
    char_count = len(non_space_chars)
    return {
        "total_chars": char_count,
        "money": char_count
    }

@bot.event
async def on_ready():
    print(f"流光城統計機器人已上線：{bot.user}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if bot.user in message.mentions:
        channel = message.channel

        if not isinstance(channel, discord.Thread):
            await message.reply("⚠️ 此結算指令僅限在【討論串（Thread）】或【論壇貼文】內使用！")
            return

        target_user = message.author
        total_word_count = 0
        post_count = 0

        async with channel.typing():
            async for msg in channel.history(limit=None, oldest_first=True):
                if msg.author.id == target_user.id and msg.id != message.id:
                    stats = count_text_and_money(msg.content)
                    if stats["total_chars"] > 0:
                        total_word_count += stats["total_chars"]
                        post_count += 1

        if post_count == 0:
            await message.reply("⚠️ 未在此討論串中檢測到你在結算前的有效發言內容。")
            return

        reply_content = (
            f"累計段落：{post_count} 則\n"
            f"個人總字數：{total_word_count:,}字\n"
            f"✨本次收入：${total_word_count:,}\n"
            f"‼️總字數含標點符號直接折算為金錢，請至財務登記區登記。"
        )

        await message.reply(reply_content)

    await bot.process_commands(message)

# ⚠️ 請將下方的引號內容替換為你的機器人 Token
bot.run("MTU1NDg3MzU2MzU4Mjk2Mzc5Mg.Gctmz9.klT0RGzSThGS9r-20yPMyPcW86ApgFbJqwC7Kg")
