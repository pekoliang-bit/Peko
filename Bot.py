import os
import re
import json
import random
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
DB_PATH = "word_counter_system.db"

# -------------------- 資料庫初始化 --------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. 伺服器字數計算模式設定表 (以 guild_id 為主鍵)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_count_settings (
                guild_id INTEGER PRIMARY KEY,
                mode TEXT DEFAULT '中文字加標點符號'
            )
        """)
        # 2. 抽獎獎項資料表 (以 guild_id 隔離)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS lottery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                title TEXT,               -- 獎項名稱（需與討論串名一致）
                role_id INTEGER DEFAULT 0, -- 0 表示無限制
                repeatable INTEGER DEFAULT 0, -- 1: 可重複, 0: 不可重複
                UNIQUE(guild_id, title)
            )
        """)
        # 3. 獎項內的具體獎品與庫存表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS lottery_prizes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                lottery_id INTEGER,
                prize_name TEXT,
                stock INTEGER,
                initial_stock INTEGER
            )
        """)
        await db.commit()

# -------------------- 字數統計核心函式 --------------------
def calculate_words(text: str, mode: str) -> int:
    # 移除 Discord 的 Mention 標籤（如 <@123456>）避免干擾字數
    cleaned_text = re.sub(r"<@!?[0-9]+>", "", text).strip()

    if mode == "僅中文字":
        # 僅匹配繁簡漢字
        matches = re.findall(r"[\u4e00-\u9fa5]", cleaned_text)
        return len(matches)

    elif mode == "中文字加標點符號":
        # 匹配漢字與全形/半形常見標點符號（不計英數字）
        pattern = r"[\u4e00-\u9fa5，。！？、；：「」『』（）《》〈〉【】—…～\.,!\?;:\(\)\"']"
        matches = re.findall(pattern, cleaned_text)
        return len(matches)

    elif mode == "中文字加英文字":
        # 漢字計字數，英文字以單詞 (Word) 計算
        chinese_count = len(re.findall(r"[\u4e00-\u9fa5]", cleaned_text))
        english_words = re.findall(r"[a-zA-Z]+", cleaned_text)
        return chinese_count + len(english_words)

    elif mode == "全部（含所有語言文字、符號、標點）":
        # 計算扣除空白與換行後的總字元數
        non_space_chars = re.sub(r"\s+", "", cleaned_text)
        return len(non_space_chars)

    return len(cleaned_text)

# -------------------- 監聽 @機器人 自動結算字數 --------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # 當訊息中 @提及了此機器人
    if bot.user in message.mentions:
        # 若是回覆訊息，優先統計被回覆的那則訊息；否則統計當前訊息
        target_text = message.content
        if message.reference and message.reference.message_id:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                target_text = ref_msg.content
            except Exception:
                pass

        # 讀取該伺服器設定的計算模式
        g_id = message.guild.id
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT mode FROM guild_count_settings WHERE guild_id = ?", (g_id,)) as cur:
                row = await cur.fetchone()
                mode = row[0] if row else "中文字加標點符號"

        count = calculate_words(target_text, mode)
        await message.reply(
            f"📝 **字數結算報告**\n"
            f"• 計算模式：`{mode}`\n"
            f"• 結算字數：**{count:,}** 字"
        )

    await bot.process_commands(message)

# -------------------- 指令 1：/計算方式調整 (管理員) --------------------
@bot.tree.command(name="計算方式調整", description="【管理員專用】調整本伺服器字數統計的判定規則")
@app_commands.default_permissions(administrator=True)
@app_commands.choices(計算內容=[
    app_commands.Choice(name="僅中文字", value="僅中文字"),
    app_commands.Choice(name="中文字加標點符號", value="中文字加標點符號"),
    app_commands.Choice(name="中文字加英文字", value="中文字加英文字"),
    app_commands.Choice(name="全部（含所有語言文字、符號、標點）", value="全部（含所有語言文字、符號、標點）"),
])
async def set_count_mode(interaction: discord.Interaction, 計算內容: app_commands.Choice[str]):
    g_id = interaction.guild_id
    chosen_mode = 計算內容.value

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO guild_count_settings (guild_id, mode) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET mode = ?
        """, (g_id, chosen_mode, chosen_mode))
        await db.commit()

    await interaction.response.send_message(
        f"✅ 本伺服器的字數統計模式已切換為：**【{chosen_mode}】**！\n"
        f"後續成員 @機器人 結算時將自動套用此規則。"
    )

# -------------------- 指令 2：/新增抽獎 (管理員) --------------------
@bot.tree.command(name="新增抽獎", description="【管理員專用】建立全新抽獎獎項與獎品庫")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    獎項名稱="抽獎主題（玩家抽獎時討論串名稱需完全相符）",
    可抽獎身分組="指定可參與的身分組，留空表示全體成員皆可抽",
    獎品內容與數量="格式：獎品A*數量, 獎品B*數量（例：流光幣5000*5, 特製胸針*1）",
    獎品可否重複="是否可重複抽取同一獎品（不可重複時，抽出一件庫存即減一）"
)
@app_commands.choices(獎品可否重複=[
    app_commands.Choice(name="可重複抽取", value=1),
    app_commands.Choice(name="不可重複（抽完扣除）", value=0)
])
async def add_lottery(
    interaction: discord.Interaction,
    獎項名稱: str,
    獎品內容與數量: str,
    獎品可否重複: app_commands.Choice[int],
    可抽獎身分組: discord.Role = None
):
    g_id = interaction.guild_id
    role_id = 可抽獎身分組.id if 可抽獎身分組 else 0
    repeatable_val = 獎品可否重複.value

    # 解析獎品格式（如：iPhone*1, 禮券*5 或 獎品A*2、獎品B*3）
    raw_prizes = re.split(r"[,，、\n]+", 獎品內容與數量.strip())
    prizes_to_insert = []
    for item in raw_prizes:
        if not item.strip():
            continue
        parts = re.split(r"[\*xX×]", item.strip())
        name = parts[0].strip()
        amt = 1
        if len(parts) > 1:
            try:
                amt = int(parts[1].strip())
            except ValueError:
                amt = 1
        prizes_to_insert.append((name, max(1, amt)))

    if not prizes_to_insert:
        await interaction.response.send_message("❌ 請填寫正確的獎品格式！例如：`流光幣5000*5, 簽名照*1`", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        # 新增或覆蓋該伺服器的該活動
        try:
            cur = await db.execute("""
                INSERT INTO lottery_events (guild_id, title, role_id, repeatable)
                VALUES (?, ?, ?, ?)
            """, (g_id, 獎項名稱.strip(), role_id, repeatable_val))
            lottery_id = cur.lastrowid
        except aiosqlite.IntegrityError:
            await interaction.response.send_message(f"❌ 獎項 **{獎項名稱}** 已存在於本伺服器！請先更換名稱。", ephemeral=True)
            return

        for p_name, p_stock in prizes_to_insert:
            await db.execute("""
                INSERT INTO lottery_prizes (guild_id, lottery_id, prize_name, stock, initial_stock)
                VALUES (?, ?, ?, ?, ?)
            """, (g_id, lottery_id, p_name, p_stock, p_stock))

        await db.commit()

    role_str = 可抽獎身分組.mention if 可抽獎身分組 else "無限制（全體可抽）"
    repeat_str = "可重複" if repeatable_val == 1 else "不可重複（抽完即止）"
    preview = "\n".join([f"• {n} × {s}" for n, s in prizes_to_insert])

    embed = discord.Embed(title=f"🎉 抽獎獎項發布成功：【{獎項名稱}】", color=0x2ECC71)
    embed.add_field(name="資格門檻", value=role_str, inline=True)
    embed.add_field(name="重複規則", value=repeat_str, inline=True)
    embed.add_field(name="獎品庫存明細", value=preview, inline=False)
    embed.set_footer(text=f"玩家請至名為【{獎項名稱}】的討論串內輸入 /我要抽獎 參與")

    await interaction.response.send_message(embed=embed)

# -------------------- 指令 3：/查看獎項 --------------------
class PrizeDetailSelect(discord.ui.Select):
    def __init__(self, events):
        options = [
            discord.SelectOption(
                label=ev[1][:100], 
                description=f"抽獎資格: {'特定身分組' if ev[2] != 0 else '無限制'}",
                value=str(ev[0])
            )
            for ev in events[:25]
        ]
        super().__init__(placeholder="點選以查看該獎項目前的抽獎與庫存情況...", options=options)

    async def callback(self, interaction: discord.Interaction):
        chosen_id = int(self.values[0])
        g_id = interaction.guild_id

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT title, role_id, repeatable FROM lottery_events WHERE guild_id = ? AND id = ?", (g_id, chosen_id)) as cur:
                ev_row = await cur.fetchone()
            async with db.execute("SELECT prize_name, stock, initial_stock FROM lottery_prizes WHERE lottery_id = ?", (chosen_id,)) as cur:
                prizes = await cur.fetchall()

        if not ev_row:
            await interaction.response.send_message("❌ 查無此獎項資訊！", ephemeral=True)
            return

        title, role_id, repeatable = ev_row
        role_txt = f"<@&{role_id}>" if role_id != 0 else "所有人皆可參與"
        mode_txt = "可重複抽取" if repeatable == 1 else "不可重複（抽完扣除）"

        embed = discord.Embed(title=f"🎁【{title}】抽獎進度與庫存", color=0x3498DB)
        embed.add_field(name="抽獎資格", value=role_txt, inline=True)
        embed.add_field(name="機制說明", value=mode_txt, inline=True)

        lines = []
        for p_name, stock, init_stock in prizes:
            status = f"剩餘 `{stock}/{init_stock}`" if repeatable == 0 else f"總量 `{init_stock}` (無限)"
            bar = "🔴 已抽完" if stock <= 0 and repeatable == 0 else "🟢 尚有名額"
            lines.append(f"• **{p_name}** ｜ {status} ｜ {bar}")

        embed.description = "\n".join(lines) if lines else "暫無獎品配置"
        await interaction.response.send_message(embed=embed, ephemeral=True)

class PrizeView(discord.ui.View):
    def __init__(self, events):
        super().__init__(timeout=180)
        self.add_item(PrizeDetailSelect(events))

@bot.tree.command(name="查看獎項", description="查看本伺服器所有已發布的獎項與即時庫存進度")
async def view_prizes(interaction: discord.Interaction):
    g_id = interaction.guild_id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, title, role_id FROM lottery_events WHERE guild_id = ?", (g_id,)) as cur:
            events = await cur.fetchall()

    if not events:
        await interaction.response.send_message("📋 目前本伺服器尚未發布任何抽獎活動！", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎰 流光城抽獎活動列表",
        description="以下為目前進行中的獎項，請透過下方選單調閱具體剩餘名額：",
        color=0xF1C40F
    )
    for _, title, role_id in events:
        req = f"<@&{role_id}>" if role_id != 0 else "無限制"
        embed.add_field(name=f"🏆 {title}", value=f"資格：{req}", inline=False)

    await interaction.response.send_message(embed=embed, view=PrizeView(events), ephemeral=True)

# -------------------- 指令 4：/我要抽獎 --------------------
@bot.tree.command(name="我要抽獎", description="在對應獎項的討論串內執行抽獎")
async def draw_lottery(interaction: discord.Interaction):
    g_id = interaction.guild_id
    channel = interaction.channel

    # 1. 檢驗是否在討論串 (Thread) 內發動
    if not isinstance(channel, discord.Thread):
        await interaction.response.send_message("❌ 此指令僅限在對應獎項的**討論串**內使用！", ephemeral=True)
        return

    thread_name = channel.name.strip()

    async with aiosqlite.connect(DB_PATH) as db:
        # 2. 自動比對討論串名稱與獎項名稱
        async with db.execute(
            "SELECT id, title, role_id, repeatable FROM lottery_events WHERE guild_id = ? AND title = ?",
            (g_id, thread_name)
        ) as cur:
            ev_row = await cur.fetchone()

        if not ev_row:
            await interaction.response.send_message(
                f"❌ 找不到與討論串名稱 `【{thread_name}】` 完全吻合的獎項！\n"
                f"請確認討論串名是否與管理員設定的獎項名一致。",
                ephemeral=True
            )
            return

        lottery_id, title, role_id, repeatable = ev_row

        # 3. 身分組資格檢查
        if role_id != 0:
            user_role_ids = [r.id for r in interaction.user.roles]
            if role_id not in user_role_ids and not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message(f"❌ 資格不符！本抽獎僅限持有 <@&{role_id}> 身分組的成員參與。", ephemeral=True)
                return

        # 4. 取得可抽取的獎品庫
        if repeatable == 1:
            # 可重複抽：取得全部獎品
            async with db.execute("SELECT id, prize_name, stock FROM lottery_prizes WHERE lottery_id = ?", (lottery_id,)) as cur:
                prizes = await cur.fetchall()
        else:
            # 不可重複抽：僅取得庫存 > 0 的獎品
            async with db.execute("SELECT id, prize_name, stock FROM lottery_prizes WHERE lottery_id = ? AND stock > 0", (lottery_id,)) as cur:
                prizes = await cur.fetchall()

        if not prizes:
            await interaction.response.send_message(f"⚠️ 抱歉，【{title}】的所有獎品已經全數被抽完了！", ephemeral=True)
            return

        # 5. 隨機抽選獎品
        chosen_prize = random.choice(prizes)
        prize_id, prize_name, cur_stock = chosen_prize

        # 6. 不可重複時扣除庫存
        if repeatable == 0:
            new_stock = cur_stock - 1
            await db.execute("UPDATE lottery_prizes SET stock = ? WHERE id = ?", (new_stock, prize_id))
            await db.commit()
            remain_msg = f"（本獎品剩餘數量：`{new_stock}`）"
        else:
            remain_msg = ""

    # 公開在討論串中宣布得獎
    embed = discord.Embed(
        title="🎊 幸運抽獎結果出爐！",
        description=f"恭喜 {interaction.user.mention} 於 **【{title}】** 幸運抽中：\n\n🎉 **{prize_name}** {remain_msg}",
        color=0xE74C3C
    )
    await interaction.response.send_message(embed=embed)

# -------------------- 開機與斜線指令同步 --------------------
@bot.event
async def on_ready():
    await init_db()
    try:
        synced = await bot.tree.sync()
        print(f"🎉 成功同步 {len(synced)} 個 Slash 指令！")
    except Exception as e:
        print(f"❌ 同步失敗: {e}")
    print(f"字數統計與抽獎系統已就緒：{bot.user}")

# 填入你的 Discord Token 或 Render 環境變數
TOKEN = os.getenv("DISCORD_TOKEN")
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ 請設定 DISCORD_TOKEN 環境變數！")

