import aiosqlite
import logging
from datetime import datetime

# Bitta markaziy baza nomi
DB_NAME = "boshqar_ai.db"

async def init_db():
    """Ma'lumotlar bazasini va barcha jadvallarni yaratish."""
    async with aiosqlite.connect(DB_NAME) as db:
        # 1. Foydalanuvchilar jadvali
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                username TEXT,
                plan TEXT DEFAULT 'Free',
                is_banned INTEGER DEFAULT 0,
                ban_until TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 2. Kanallar jadvali
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_id TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, channel_id) 
            )
        """)
        
        # 3. AI Requestlar jadvali (Topic loglari)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                topic TEXT,
                request_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await db.commit()
        logging.info("✅ Baza modernizatsiya qilindi va barcha jadvallar tayyor.")

# --- FOYDALANUVCHI AMALLARI ---

async def save_user(data):
    """Yangi foydalanuvchini saqlash yoki yangilash"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """INSERT INTO users (user_id, first_name, last_name, phone, username, plan) 
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET 
               first_name=excluded.first_name, last_name=excluded.last_name, 
               username=excluded.username, phone=excluded.phone""",
            (data['user_id'], data['first_name'], data['last_name'], data['phone'], data['username'], data.get('plan', 'Free'))
        )
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

# --- ADMIN STATISTIKA AMALLARI (YANGILANGAN) ---

async def get_admin_dashboard_stats():
    """Dashboard uchun barcha statistikalar"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        stats = {}
        
        # 1. Userlar statistikasi
        async with db.execute("SELECT COUNT(*) as total FROM users") as c:
            stats['total_users'] = (await c.fetchone())['total']
            
        async with db.execute("SELECT COUNT(*) as monthly FROM users WHERE created_at >= date('now', 'start of month')") as c:
            stats['monthly_users'] = (await c.fetchone())['monthly']
            
        # 2. AI Requestlar statistikasi
        async with db.execute("SELECT COUNT(*) as total FROM ai_requests") as c:
            stats['total_ai'] = (await c.fetchone())['total']
            
        async with db.execute("SELECT COUNT(*) as monthly FROM ai_requests WHERE request_at >= date('now', 'start of month')") as c:
            stats['monthly_ai'] = (await c.fetchone())['monthly']
            
        # 3. Trendlar (Oxirgi 30 kun ichidagi eng mashhur 5 ta mavzu)
        # BU YERDA 'request_at' orqali vaqt filtrlanadi
        async with db.execute("""
            SELECT topic, COUNT(topic) as cnt 
            FROM ai_requests 
            WHERE request_at >= datetime('now', '-30 days')
            GROUP BY topic 
            ORDER BY cnt DESC 
            LIMIT 5
        """) as c:
            stats['trends'] = await c.fetchall()
            
        # 4. Kanallar statistikasi
        async with db.execute("SELECT COUNT(*) as total FROM channels") as c:
            stats['total_channels'] = (await c.fetchone())['total']
            
        async with db.execute("SELECT COUNT(*) as monthly FROM channels WHERE joined_at >= date('now', 'start of month')") as c:
            stats['monthly_channels'] = (await c.fetchone())['monthly']
            
        return stats

# --- AI LOGLASH ---

async def log_ai_request(user_id, topic):
    """Har bir post so'rovini bazaga yozib boradi"""
    async with aiosqlite.connect(DB_NAME) as db:
        # request_at avtomatik hozirgi vaqtni oladi
        await db.execute("INSERT INTO ai_requests (user_id, topic) VALUES (?, ?)", (user_id, topic))
        await db.commit()

# --- BAN TIZIMI (DEBUG QILINGAN) ---

async def set_user_ban_status(user_id, status: bool, hours: int = 0):
    """Userni bloklash (ma'lum soatga) yoki ochish"""
    async with aiosqlite.connect(DB_NAME) as db:
        if status:
            # Soatni qo'shib, 'localtime' da saqlaymiz
            await db.execute(
                f"UPDATE users SET is_banned = 1, ban_until = datetime('now', 'localtime', '+{hours} hours') WHERE user_id = ?",
                (user_id,)
            )
        else:
            await db.execute("UPDATE users SET is_banned = 0, ban_until = NULL WHERE user_id = ?", (user_id,))
        await db.commit()

async def get_ban_info(user_id):
    """Ban holatini tekshirish: Agar ban muddati o'tgan bo'lsa avtomatik ochadi"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT is_banned, ban_until FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row: return None
            
            if row['is_banned'] == 1:
                # Ban muddati tugaganini tekshirish
                if row['ban_until']:
                    now = datetime.now()
                    try:
                        # SQLite dan keladigan vaqt formati: '2023-10-25 14:30:00'
                        ban_time = datetime.strptime(row['ban_until'], '%Y-%m-%d %H:%M:%S')
                        
                        if now > ban_time:
                            # Muddat o'tgan bo'lsa, ban'dan ochamiz
                            await set_user_ban_status(user_id, False)
                            return None
                        else:
                            return row['ban_until'] # Ban vaqti qaytadi
                    except ValueError:
                        # Agar vaqt formati buzilgan bo'lsa, banni ochib yuboramiz (xavfsizlik uchun)
                        await set_user_ban_status(user_id, False)
                        return None
            return None

# --- USER LIST VA KANAL AMALLARI ---

async def get_users_list(limit=10, offset=0):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users LIMIT ? OFFSET ?", (limit, offset)) as cursor:
            return await cursor.fetchall()

async def get_user_channel_count(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM channels WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def add_channel(user_id, channel_id):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT INTO channels (user_id, channel_id) VALUES (?, ?)", (user_id, str(channel_id)))
            await db.commit()
            return True
        except: 
            return False

async def get_user_channels(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT channel_id FROM channels WHERE user_id = ?", (user_id,)) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def get_all_user_ids():
    """Broadcast uchun barcha user ID larini olish"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]