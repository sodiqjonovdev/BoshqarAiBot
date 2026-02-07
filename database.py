import os
import logging
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import BigInteger, String, Integer, DateTime, Boolean, select, func, text, desc
from sqlalchemy.exc import IntegrityError

# --- CONFIG ---
# Railway yoki .env dan URL ni olamiz
DB_URL = os.getenv("DATABASE_URL")

# Railway 'postgres://' beradi, lekin SQLAlchemy ga 'postgresql+asyncpg://' kerak
if DB_URL and DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql+asyncpg://", 1)

if not DB_URL:
    raise ValueError("❌ DATABASE_URL topilmadi! .env faylni yoki Railway Variablesni tekshiring.")

# Engine va Session yaratish
engine = create_async_engine(DB_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Asosiy Model Class
class Base(DeclarativeBase):
    pass

# --- JADVALLAR (MODELS) ---

class User(Base):
    __tablename__ = "users"
    
    # Telegram ID lar katta bo'ladi, shuning uchun BigInteger
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str] = mapped_column(String, nullable=True)
    last_name: Mapped[str] = mapped_column(String, nullable=True)
    phone: Mapped[str] = mapped_column(String, nullable=True)
    username: Mapped[str] = mapped_column(String, nullable=True)
    plan: Mapped[str] = mapped_column(String, default="Free")
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_until: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

class Channel(Base):
    __tablename__ = "channels"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[str] = mapped_column(String)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    
    # Unique constraint (User bitta kanalni 2 marta qo'sha olmasligi uchun)
    # SQLAlchemy da buni __table_args__ bilan qilish mumkin, lekin oddiylik uchun
    # pastda add_channel funksiyasida tekshiramiz.

class AIRequest(Base):
    __tablename__ = "ai_requests"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    topic: Mapped[str] = mapped_column(String)
    request_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

# --- INIT ---

async def init_db():
    """Jadvallarni yaratish"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logging.info("✅ PostgreSQL jadvallari tayyorlandi.")

# --- FOYDALANUVCHI AMALLARI ---

async def save_user(data):
    """Userni saqlash yoki yangilash (Upsert)"""
    async with async_session() as session:
        # Avval user borligini tekshiramiz
        result = await session.execute(select(User).where(User.user_id == data['user_id']))
        user = result.scalar_one_or_none()

        if user:
            # Update (Yangilash)
            user.first_name = data.get('first_name', user.first_name)
            user.last_name = data.get('last_name', user.last_name)
            user.username = data.get('username', user.username)
            user.phone = data.get('phone', user.phone)
        else:
            # Insert (Yangi qo'shish)
            user = User(
                user_id=data['user_id'],
                first_name=data.get('first_name'),
                last_name=data.get('last_name'),
                username=data.get('username'),
                phone=data.get('phone'),
                plan=data.get('plan', 'Free')
            )
            session.add(user)
        
        await session.commit()

async def get_user(user_id):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        # Moslik uchun dict ko'rinishida yoki obyekt qaytarish mumkin. 
        # Hozirgi kodingiz ishlashi uchun obyektni o'zi qaytadi, 
        # lekin atributlarga nuqta bilan murojaat qilasiz (user.is_banned).
        return user

# --- ADMIN STATISTIKA ---

async def get_admin_dashboard_stats():
    async with async_session() as session:
        stats = {}
        
        # 1. Userlar soni
        stats['total_users'] = await session.scalar(select(func.count(User.user_id)))
        
        # Oylik userlar
        start_of_month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        stats['monthly_users'] = await session.scalar(
            select(func.count(User.user_id)).where(User.created_at >= start_of_month)
        )
        
        # 2. AI Requestlar
        stats['total_ai'] = await session.scalar(select(func.count(AIRequest.id)))
        stats['monthly_ai'] = await session.scalar(
            select(func.count(AIRequest.id)).where(AIRequest.request_at >= start_of_month)
        )
        
        # 3. Trendlar (Oxirgi 30 kun)
        thirty_days_ago = datetime.now() - timedelta(days=30)
        
        # Murakkab so'rov: Mavzularni sanash va kamayish tartibida saralash
        query = (
            select(AIRequest.topic, func.count(AIRequest.topic).label('cnt'))
            .where(AIRequest.request_at >= thirty_days_ago)
            .group_by(AIRequest.topic)
            .order_by(desc('cnt'))
            .limit(5)
        )
        result = await session.execute(query)
        stats['trends'] = result.all() # [(Mavzu, Soni), ...] qaytaradi
        
        # 4. Kanallar
        stats['total_channels'] = await session.scalar(select(func.count(Channel.id)))
        stats['monthly_channels'] = await session.scalar(
            select(func.count(Channel.id)).where(Channel.joined_at >= start_of_month)
        )
        
        return stats

# --- AI LOGLASH ---

async def log_ai_request(user_id, topic):
    async with async_session() as session:
        req = AIRequest(user_id=user_id, topic=topic)
        session.add(req)
        await session.commit()

# --- BAN TIZIMI ---

async def set_user_ban_status(user_id, status: bool, hours: int = 0):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        
        if user:
            if status:
                user.is_banned = True
                user.ban_until = datetime.now() + timedelta(hours=hours)
            else:
                user.is_banned = False
                user.ban_until = None
            await session.commit()

async def get_ban_info(user_id):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        
        if not user:
            return None
        
        if user.is_banned:
            if user.ban_until:
                # Muddat o'tganligini tekshirish
                if datetime.now() > user.ban_until:
                    user.is_banned = False
                    user.ban_until = None
                    await session.commit()
                    return None
                return user.ban_until
            # Agar ban bor lekin vaqt belgilanmagan bo'lsa (permaban)
            return True 
            
        return None

# --- LIST VA KANALLAR ---

async def get_users_list(limit=10, offset=0):
    async with async_session() as session:
        result = await session.execute(select(User).limit(limit).offset(offset))
        return result.scalars().all()

async def get_user_channel_count(user_id):
    async with async_session() as session:
        count = await session.scalar(
            select(func.count(Channel.id)).where(Channel.user_id == user_id)
        )
        return count or 0

async def add_channel(user_id, channel_id):
    async with async_session() as session:
        # Avval bu userda bu kanal borligini tekshiramiz
        existing = await session.scalar(
            select(Channel).where(
                (Channel.user_id == user_id) & (Channel.channel_id == str(channel_id))
            )
        )
        if existing:
            return False # Kanal allaqachon bor
            
        try:
            new_ch = Channel(user_id=user_id, channel_id=str(channel_id))
            session.add(new_ch)
            await session.commit()
            return True
        except Exception as e:
            logging.error(f"Kanal qo'shishda xatolik: {e}")
            return False

async def get_user_channels(user_id):
    async with async_session() as session:
        result = await session.execute(select(Channel.channel_id).where(Channel.user_id == user_id))
        return result.scalars().all()

async def get_all_user_ids():
    async with async_session() as session:
        result = await session.execute(select(User.user_id))
        return result.scalars().all()