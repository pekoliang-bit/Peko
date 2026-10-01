import os
import json
import time
import threading
from datetime import datetime
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask

# -------------------- 輕量假網頁（防 Render 斷線） --------------------
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Bot is alive and running!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()

# -------------------- 機器人初始化 --------------------
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
DB_PATH = "game_database.db"

# -------------------- 資料庫初始化 --------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS characters (
                user_id INTEGER,
                name TEXT PRIMARY KEY,
                age INTEGER,
                gender TEXT,
                job TEXT,
                faction TEXT,
                cash INTEGER DEFAULT 0,
                bank INTEGER DEFAULT 0,
                works TEXT DEFAULT '[]',
                created_at REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_name TEXT,
                item_name TEXT,
                amount INTEGER,
                giver TEXT DEFAULT '系統商城'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shop_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_name TEXT,
                name TEXT,
                price INTEGER,
                description TEXT,
                stat_bonus TEXT,
                affinity_bonus INTEGER DEFAULT 0,
                usable INTEGER DEFAULT 1,
                seller TEXT DEFAULT '官方'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS npcs (
                name TEXT PRIMARY KEY,
                age INTEGER,
                gender TEXT,
                identity TEXT,
                avatar_url TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS npc_affection (
                char_name TEXT,
                npc_name TEXT,
                affection INTEGER DEFAULT 0,
                PRIMARY KEY (char_name, npc_name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('interest_rate', '0.05')")
        await db.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('last_rate_update', '0')")
        await db.commit()

# -------------------- 定時任務 --------------------
@tasks.loop(hours=24)
async def daily_interest():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM system_settings WHERE key = 'interest_rate'") as cur:
            row = await cur.fetchone()
            rate = float(row[0]) if row else 0.05

        await db.execute(f"UPDATE characters SET bank = CAST(bank * (1 + {rate}) AS INTEGER) WHERE bank > 0")
        await db.commit()

@tasks.loop(hours=24)
async def monthly_age_up():
    now = time.time()
    one_month = 30 * 86400
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name, age, created_at FROM characters") as cur:
            rows = await cur.fetchall()
            for name, age, created_at in rows:
                months_passed = int((now - created_at) // one_month)
                if months_passed > 0:
                    await db.execute("UPDATE characters SET age = age + 1, created_at = created_at + ? WHERE name = ?", (one_month, name))
        await db.commit()

# -------------------- UI 互動元件 --------------------
class BuyModal(discord.ui.Modal, title="購買商品確認"):
    def __init__(self, item_id: int, item_name: str, price: int, buyer: str):
        super().__init__()
        self.item_id = item_id
        self.item_name = item_name
        self.price = price
        self.buyer = buyer

    amount = discord.ui.TextInput(label="購買數量", default="1", min_length=1, max_length=4)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(self.amount.value)
            if qty <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ 請輸入大於 0 的整數！", ephemeral=True)
            return

        total_cost = self.price * qty
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT cash FROM characters WHERE name = ?", (self.buyer,)) as cur:
                user = await cur.fetchone()
                if not user or user[0] < total_cost:
                    await interaction.response.send_message(f"❌ 現金不足！需要 ${total_cost:,}，當前僅有 ${user[0] if user else 0:,}。", ephemeral=True)
                    return

            async with db.execute("SELECT seller FROM shop_items WHERE id = ?", (self.item_id,)) as cur:
                seller_row = await cur.fetchone()
                seller = seller_row[0] if seller_row else '官方'

            await db.execute("UPDATE characters SET cash = cash - ? WHERE name = ?", (total_cost, self.buyer))
            
            if seller != '官方':
                await db.execute("UPDATE characters SET cash = cash + ? WHERE name = ?", (total_cost, seller))

            async with db.execute("SELECT id, amount FROM inventory WHERE owner_name = ? AND item_name = ? AND giver = '系統商城'", (self.buyer, self.item_name)) as cur:
                inv_row = await cur.fetchone()
                if inv_row:
                    await db.execute("UPDATE inventory SET amount = amount + ? WHERE id = ?", (qty, inv_row[0]))
                else:
                    await db.execute("INSERT INTO inventory (owner_name, item_name, amount, giver) VALUES (?, ?, ?, '系統商城')", (self.buyer, self.item_name, qty))

            if seller != '官方':
                await db.execute("DELETE FROM shop_items WHERE id = ?", (self.item_id,))

            await db.commit()

        await interaction.response.send_message(f"✅ 成功購買 **{self.item_name}** ×{qty}！共扣款 **${total_cost:,}**。", ephemeral=True)

class ShopSelectView(discord.ui.View):
    def __init__(self, char_name: str, shops: list):
        super().__init__(timeout=120)
        self.char_name = char_name
        for shop in shops:
            button = discord.ui.Button(label=shop, style=discord.ButtonStyle.primary)
            button.callback = self.make_callback(shop)
            self.add_item(button)

    def make_callback(self, shop_name):
        async def callback(interaction: discord.Interaction):
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT id, name, price, description, stat_bonus, seller FROM shop_items WHERE shop_name = ?", (shop_name,)) as cur:
                    items = await cur.fetchall()

            if not items:
                await interaction.response.send_message(f"🏪 **{shop_name}** 目前無任何商品。", ephemeral=True)
                return

            embed = discord.Embed(title=f"🏪 商城 — 【{shop_name}】", color=0xD4AF37)
            item_select_view = discord.ui.View(timeout=120)

            options = []
            for item in items[:25]:
                item_id, i_name, price, desc, stat, seller = item
                label_extra = f" (賣家: {seller})" if seller != '官方' else ""
                embed.add_field(name=f"📦 {i_name} — ${price:,}{label_extra}", value=f"簡介: {desc}\n加成: {stat}", inline=False)
                options.append(discord.SelectOption(label=f"{i_name} (${price:,})", value=str(item_id), description=desc[:50]))

            select = discord.ui.Select(placeholder="選擇欲購買商品...", options=options)
            
            async def select_callback(inter: discord.Interaction):
                selected_id = int(select.values[0])
                for it in items:
                    if it[0] == selected_id:
                        await inter.response.send_modal(BuyModal(selected_id, it[1], it[2], self.char_name))
                        break

            select.callback = select_callback
            item_select_view.add_item(select)
            await interaction.response.send_message(embed=embed, view=item_select_view, ephemeral=True)

        return callback

class WorkRegisterModal(discord.ui.Modal, title="作品檔案登記"):
    def __init__(self, char_name: str, category: str):
        super().__init__()
        self.char_name = char_name
        self.category = category

        self.work_title = discord.ui.TextInput(label="作品名稱", placeholder="輸入作品名稱")
        self.add_item(self.work_title)

        if category in ["電視劇", "電影", "短劇", "舞台劇"]:
            self.role_info = discord.ui.TextInput(label="番位 & 角色名", placeholder="例如：領銜主演 / 飾演 顧淮安")
            self.add_item(self.role_info)
        elif category in ["廣告", "代言", "其他"]:
            self.desc_info = discord.ui.TextInput(label="代言商品 / 簡介", placeholder="例如：品牌全球代言人 / 香水推廣", style=discord.TextStyle.paragraph)
            self.add_item(self.desc_info)

    async def on_submit(self, interaction: discord.Interaction):
        extra_val = getattr(self, 'role_info', getattr(self, 'desc_info', None))
        work_entry = {
            "category": self.category,
            "title": self.work_title.value,
            "extra": extra_val.value if extra_val else ""
        }

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT works FROM characters WHERE name = ?", (self.char_name,)) as cur:
                row = await cur.fetchone()
                works = json.loads(row[0]) if row and row[0] else []
            
            works.append(work_entry)
            await db.execute("UPDATE characters SET works = ? WHERE name = ?", (json.dumps(works, ensure_ascii=False), self.char_name))
            await db.commit()

        await interaction.response.send_message(f"✅ 已成功為 **{self.char_name}** 登記作品：【{self.category}】《{self.work_title.value}》！", ephemeral=True)

# -------------------- 指令註冊 --------------------
@bot.tree.command(name="檔案建立", description="建立自己的角色檔案")
async def create_profile(interaction: discord.Interaction, 名字: str, 年齡: int, 性別: str, 職業: str, 勢力: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM characters WHERE name = ?", (名字,)) as cur:
            if await cur.fetchone():
                await interaction.response.send_message(f"❌ 角色名 **{名字}** 已存在！", ephemeral=True)
                return
        
        await db.execute("""
            INSERT INTO characters (user_id, name, age, gender, job, faction, cash, bank, works, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, '[]', ?)
        """, (interaction.user.id, 名字, 年齡, 性別, 職業, 勢力, time.time()))
        await db.commit()

    await interaction.response.send_message(f"🎉 角色 **{名字}** 檔案建立成功！")

@bot.tree.command(name="商城_選擇自己的角色", description="選擇商城並購買商品")
async def shop(interaction: discord.Interaction, 角色名: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM characters WHERE name = ?", (角色名,)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message("❌ 查無此角色檔案！", ephemeral=True)
                return

        async with db.execute("SELECT DISTINCT shop_name FROM shop_items") as cur:
            shops = [row[0] for row in await cur.fetchall()]

    if "二手" not in shops:
        shops.append("二手")

    view = ShopSelectView(角色名, shops)
    await interaction.response.send_message(f"🏬 請選擇 **{角色名}** 想進入的商店：", view=view, ephemeral=True)

@bot.tree.command(name="背包_選擇自己的角色", description="查看角色背包物品與來源")
async def inventory(interaction: discord.Interaction, 角色名: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT item_name, amount, giver FROM inventory WHERE owner_name = ?", (角色名,)) as cur:
            items = await cur.fetchall()

    if not items:
        await interaction.response.send_message(f"🎒 **{角色名}** 的背包目前沒有物品。", ephemeral=True)
        return

    embed = discord.Embed(title=f"🎒【{角色名}】的背包清單", color=0x3498DB)
    for name, amt, giver in items:
        embed.add_field(name=f"📦 {name} × {amt}", value=f"來源/贈予人: {giver}", inline=True)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="使用_選擇自己的角色", description="使用背包物品獲得數值")
async def use_item(interaction: discord.Interaction, 角色名: str, 物品名: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, amount FROM inventory WHERE owner_name = ? AND item_name = ?", (角色名, 物品名)) as cur:
            inv = await cur.fetchone()
            if not inv or inv[1] <= 0:
                await interaction.response.send_message("❌ 背包中沒有該物品！", ephemeral=True)
                return

        async with db.execute("SELECT usable, stat_bonus FROM shop_items WHERE name = ?", (物品名,)) as cur:
            item_info = await cur.fetchone()
            if not item_info or item_info[0] == 0:
                await interaction.response.send_message(f"⚠️ **{物品名}** 為不可使用之物品！", ephemeral=True)
                return
            bonus = item_info[1]

        if inv[1] == 1:
            await db.execute("DELETE FROM inventory WHERE id = ?", (inv[0],))
        else:
            await db.execute("UPDATE inventory SET amount = amount - 1 WHERE id = ?", (inv[0],))
        await db.commit()

    await interaction.response.send_message(f"✨ **{角色名}** 使用了 **{物品名}**！獲得加成：【{bonus}】（剩餘數量: {inv[1] - 1}）")

@bot.tree.command(name="贈予_選擇自己的角色", description="贈送物品給其他玩家或 NPC")
async def gift_item(interaction: discord.Interaction, 角色名: str, 對象名: str, 物品名: str, 數量: int):
    if 數量 <= 0:
        await interaction.response.send_message("❌ 數量需大於 0！", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, amount FROM inventory WHERE owner_name = ? AND item_name = ?", (角色名, 物品名)) as cur:
            inv = await cur.fetchone()
            if not inv or inv[1] < 數量:
                await interaction.response.send_message("❌ 背包中該物品數量不足！", ephemeral=True)
                return

        async with db.execute("SELECT name FROM npcs WHERE name = ?", (對象名,)) as cur:
            is_npc = await cur.fetchone() is not None

        if inv[1] == 數量:
            await db.execute("DELETE FROM inventory WHERE id = ?", (inv[0],))
        else:
            await db.execute("UPDATE inventory SET amount = amount - ? WHERE id = ?", (數量, inv[0]))

        extra_msg = ""
        if is_npc:
            async with db.execute("SELECT affinity_bonus FROM shop_items WHERE name = ?", (物品名,)) as cur:
                item_row = await cur.fetchone()
                single_aff = item_row[0] if item_row else 10
            total_aff = single_aff * 數量

            await db.execute("""
                INSERT INTO npc_affection (char_name, npc_name, affection)
                VALUES (?, ?, ?)
                ON CONFLICT(char_name, npc_name) DO UPDATE SET affection = affection + ?
            """, (角色名, 對象名, total_aff, total_aff))
            extra_msg = f"\n💖 NPC **{對象名}** 對你的好感度增加了 **+{total_aff}**！"
        else:
            async with db.execute("SELECT id, amount FROM inventory WHERE owner_name = ? AND item_name = ? AND giver = ?", (對象名, 物品名, 角色名)) as cur:
                target_inv = await cur.fetchone()
                if target_inv:
                    await db.execute("UPDATE inventory SET amount = amount + ? WHERE id = ?", (數量, target_inv[0]))
                else:
                    await db.execute("INSERT INTO inventory (owner_name, item_name, amount, giver) VALUES (?, ?, ?, ?)", (對象名, 物品名, 數量, 角色名))

        await db.commit()

    await interaction.response.send_message(f"🎁 **{角色名}** 贈送了 **{物品名}** ×{數量} 給 **{對象名}**！{extra_msg}")

@bot.tree.command(name="售物", description="將擁有物品放上［二手］商城販售")
async def sell_item(interaction: discord.Interaction, 角色名: str, 物品名: str, 價格: int):
    if 價格 <= 0:
        await interaction.response.send_message("❌ 販售價格必須大於 0！", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, amount FROM inventory WHERE owner_name = ? AND item_name = ?", (角色名, 物品名)) as cur:
            inv = await cur.fetchone()
            if not inv or inv[1] <= 0:
                await interaction.response.send_message("❌ 背包中查無此物品！", ephemeral=True)
                return

        if inv[1] == 1:
            await db.execute("DELETE FROM inventory WHERE id = ?", (inv[0],))
        else:
            await db.execute("UPDATE inventory SET amount = amount - 1 WHERE id = ?", (inv[0],))

        await db.execute("""
            INSERT INTO shop_items (shop_name, name, price, description, stat_bonus, affinity_bonus, usable, seller)
            VALUES ('二手', ?, ?, '玩家自售物品', '無加成', 0, 1, ?)
        """, (物品名, 價格, 角色名))
        await db.commit()

    await interaction.response.send_message(f"🏷️ 已將 **{物品名}** 上架至【二手】商城，定價為 **${價格:,}**！")

@bot.tree.command(name="存錢_帳戶名", description="將現金存入銀行")
async def deposit(interaction: discord.Interaction, 角色名: str, 金額: int):
    if 金額 <= 0:
        await interaction.response.send_message("❌ 金額需大於 0！", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT cash FROM characters WHERE name = ?", (角色名,)) as cur:
            user = await cur.fetchone()
            if not user or user[0] < 金額:
                await interaction.response.send_message(f"❌ 現金不足！當前僅有 ${user[0] if user else 0:,}。", ephemeral=True)
                return

        await db.execute("UPDATE characters SET cash = cash - ?, bank = bank + ? WHERE name = ?", (金額, 金額, 角色名))
        await db.commit()

    await interaction.response.send_message(f"🏦 **{角色名}** 成功存入 **${金額:,}**！")

@bot.tree.command(name="取錢_帳戶名", description="從銀行取出金錢")
async def withdraw(interaction: discord.Interaction, 角色名: str, 金額: int):
    if 金額 <= 0:
        await interaction.response.send_message("❌ 金額需大於 0！", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT bank FROM characters WHERE name = ?", (角色名,)) as cur:
            user = await cur.fetchone()
            if not user or user[0] < 金額:
                await interaction.response.send_message(f"❌ 存款不足！當前僅有 ${user[0] if user else 0:,}。", ephemeral=True)
                return

        await db.execute("UPDATE characters SET bank = bank - ?, cash = cash + ? WHERE name = ?", (金額, 金額, 角色名))
        await db.commit()

    await interaction.response.send_message(f"💵 **{角色名}** 成功自銀行提款 **${金額:,}**！")

@bot.tree.command(name="轉錢_帳戶名", description="轉帳給其他角色")
async def transfer(interaction: discord.Interaction, 角色名: str, 對象名: str, 金額: int):
    if 金額 <= 0:
        await interaction.response.send_message("❌ 金額需大於 0！", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT cash FROM characters WHERE name = ?", (角色名,)) as cur:
            sender = await cur.fetchone()
            if not sender or sender[0] < 金額:
                await interaction.response.send_message("❌ 現金不足！", ephemeral=True)
                return

        async with db.execute("SELECT name FROM characters WHERE name = ?", (對象名,)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message(f"❌ 找不到受款對象 **{對象名}**！", ephemeral=True)
                return

        await db.execute("UPDATE characters SET cash = cash - ? WHERE name = ?", (金額, 角色名))
        await db.execute("UPDATE characters SET cash = cash + ? WHERE name = ?", (金額, 對象名))
        await db.commit()

    await interaction.response.send_message(f"💸 **{角色名}** 成功轉帳 **${金額:,}** 給 **{對象名}**！")

# -------------------- 管理員指令 --------------------
@bot.tree.command(name="商店與物品增加", description="［管理員］新增商店商品")
@app_commands.checks.has_permissions(administrator=True)
async def add_shop_item(interaction: discord.Interaction, 商店: str, 商品名: str, 價格: int, 簡介: str, 增加數值: str, 增加好感: int, 可否使用: bool):
    usable_val = 1 if 可否使用 else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO shop_items (shop_name, name, price, description, stat_bonus, affinity_bonus, usable, seller)
            VALUES (?, ?, ?, ?, ?, ?, ?, '官方')
        """, (商店, 商品名, 價格, 簡介, 增加數值, 增加好感, usable_val))
        await db.commit()

    await interaction.response.send_message(f"✅ 管理員已在【{商店}】新增商品：**{商品名}**！")

@bot.tree.command(name="npc登記", description="［管理員］登記 NPC 資料")
@app_commands.checks.has_permissions(administrator=True)
async def register_npc(interaction: discord.Interaction, 名字: str, 年齡: int, 性別: str, 身分: str, 圖像連結: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO npcs (name, age, gender, identity, avatar_url)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET age=?, gender=?, identity=?, avatar_url=?
        """, (名字, 年齡, 性別, 身分, 圖像連結, 年齡, 性別, 身分, 圖像連結))
        await db.commit()

    embed = discord.Embed(title=f"👤 NPC 資料登記完成：{名字}", color=0x9B59B6)
    embed.add_field(name="基本信息", value=f"{年齡} 歲 / {性別} / {身分}", inline=False)
    embed.set_thumbnail(url=圖像連結)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="利息調整", description="［管理員］每 168 小時調整一次利率")
@app_commands.checks.has_permissions(administrator=True)
async def set_interest(interaction: discord.Interaction, 利率百分比: float):
    now = time.time()
    one_week = 168 * 3600

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM system_settings WHERE key = 'last_rate_update'") as cur:
            row = await cur.fetchone()
            last_update = float(row[0]) if row else 0

        if now - last_update < one_week:
            remain_h = int((one_week - (now - last_update)) / 3600)
            await interaction.response.send_message(f"⏳ 利率調整冷卻中！還需等待約 **{remain_h}** 小時。", ephemeral=True)
            return

        new_rate = 利率百分比 / 100.0
        await db.execute("UPDATE system_settings SET value = ? WHERE key = 'interest_rate'", (str(new_rate),))
        await db.execute("UPDATE system_settings SET value = ? WHERE key = 'last_rate_update'", (str(now),))
        await db.commit()

    await interaction.response.send_message(f"📈 銀行利率已調整為 **{利率百分比}%**！")

# -------------------- 檔案更新與查看 --------------------
class UpdateProfileSelect(discord.ui.Select):
    def __init__(self, char_name: str):
        self.char_name = char_name
        options = [
            discord.SelectOption(label="更新基礎資料 (名字/性別/職業/勢力)", value="base"),
            discord.SelectOption(label="電視劇", value="電視劇"),
            discord.SelectOption(label="電影", value="電影"),
            discord.SelectOption(label="短劇", value="短劇"),
            discord.SelectOption(label="舞台劇", value="舞台劇"),
            discord.SelectOption(label="MV", value="MV"),
            discord.SelectOption(label="演唱會", value="演唱會"),
            discord.SelectOption(label="廣告", value="廣告"),
            discord.SelectOption(label="代言", value="代言"),
            discord.SelectOption(label="其他", value="其他"),
        ]
        super().__init__(placeholder="選擇欲修改項或登記作品類別...", options=options)

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice == "base":
            class BaseModal(discord.ui.Modal, title="修改角色基本資料"):
                new_name = discord.ui.TextInput(label="名字", default=self.char_name)
                gender = discord.ui.TextInput(label="性別")
                job = discord.ui.TextInput(label="職業")
                faction = discord.ui.TextInput(label="勢力")

                async def on_submit(m_self, inter: discord.Interaction):
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE characters SET name = ?, gender = ?, job = ?, faction = ? WHERE name = ?",
                                         (m_self.new_name.value, m_self.gender.value, m_self.job.value, m_self.faction.value, self.char_name))
                        await db.commit()
                    await inter.response.send_message("✅ 角色資料更新完成！", ephemeral=True)

            await interaction.response.send_modal(BaseModal())
        else:
            await interaction.response.send_modal(WorkRegisterModal(self.char_name, choice))

@bot.tree.command(name="檔案更新", description="更新角色資料或登記演藝作品")
async def update_profile(interaction: discord.Interaction, 角色名: str):
    view = discord.ui.View()
    view.add_item(UpdateProfileSelect(角色名))
    await interaction.response.send_message(f"📝 請選擇 **{角色名}** 要登記或更新的項目：", view=view, ephemeral=True)

@bot.tree.command(name="查看", description="查看角色完整檔案與 NPC 好感")
async def view_profile(interaction: discord.Interaction, 角色名: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT age, gender, job, faction, cash, bank, works FROM characters WHERE name = ?", (角色名,)) as cur:
            char = await cur.fetchone()
            if not char:
                await interaction.response.send_message(f"❌ 查無角色 **{角色名}**！", ephemeral=True)
                return
            age, gender, job, faction, cash, bank, works_json = char

        async with db.execute("SELECT npc_name, affection FROM npc_affection WHERE char_name = ?", (角色名,)) as cur:
            aff_rows = await cur.fetchall()

    embed = discord.Embed(title=f"🪪【{角色名}】個人檔案", color=0xE67E22)
    embed.add_field(name="基本信息", value=f"• 年齡：{age} 歲\n• 性別：{gender}\n• 職業：{job}\n• 勢力：{faction}", inline=True)
    embed.add_field(name="財務狀態", value=f"• 現金：${cash:,}\n• 銀行：${bank:,}", inline=True)

    works = json.loads(works_json) if works_json else []
    if works:
        work_texts = [f"• 【{w['category']}】《{w['title']}》({w.get('extra', '')})" for w in works[-5:]]
        embed.add_field(name="代表作品 (最新5筆)", value="\n".join(work_texts), inline=False)
    else:
        embed.add_field(name="代表作品", value="尚無作品紀錄", inline=False)

    if aff_rows:
        aff_texts = [f"• **{npc}**：💖 {aff}" for npc, aff in aff_rows]
        embed.add_field(name="NPC 好感度", value="\n".join(aff_texts), inline=False)
    else:
        embed.add_field(name="NPC 好感度", value="尚無任何 NPC 好感度記錄", inline=False)

    await interaction.response.send_message(embed=embed)

# -------------------- 啟動與同步 --------------------
@bot.event
async def on_ready():
    await init_db()
    if not daily_interest.is_running():
        daily_interest.start()
    if not monthly_age_up.is_running():
        monthly_age_up.start()
    await bot.tree.sync()
    print(f"機器人已上線：{bot.user}，Slash 指令同步完成！")

TOKEN = os.getenv("DISCORD_TOKEN")
bot.run(TOKEN)
