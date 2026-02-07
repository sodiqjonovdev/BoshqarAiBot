import asyncio
import datetime
import logging
import os
import sys

from aiogram.filters import BaseFilter
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove
)

try:
    from aiogram.client.default_bot_properties import DefaultBotProperties
    from aiogram.enums import ParseMode
except ImportError:
    from aiogram.client.bot import DefaultBotProperties
    from aiogram.enums.parse_mode import ParseMode

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from aiogram import BaseMiddleware

import database as db
from services import NewsService, AIService, ImageService

# --- Konfiguratsiya ---
load_dotenv()
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

bot = Bot(
    token=os.getenv("BOT_TOKEN"),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()
scheduler = AsyncIOScheduler()

# --- Tugmalar ---
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📢 Kanallar"), KeyboardButton(text="📝 Post yaratish")],
        [KeyboardButton(text="👤 Profil")] # Profil tugmasi qo'shildi
    ],
    resize_keyboard=True
)

contact_markup = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
    resize_keyboard=True
)

cancel_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🔙 Bekor qilish")]],
    resize_keyboard=True
)

# --- Adminlar ---
ADMIN = [int(os.getenv("ADMIN_ID"))]
logging.Filter
class IsAdmin(BaseFilter): # logging.Filter emas, BaseFilter bo'lishi shart!
    async def __call__(self, message: types.Message) -> bool:
        # ADMIN o'zgaruvchisi ro'yxat (list) ekanligiga ishonch hosil qil
        return message.from_user.id in ADMIN

# --- Holatlar ---
class Form(StatesGroup):
    waiting_for_name = State()
    waiting_for_surname = State()
    waiting_for_phone = State()
    waiting_for_channel = State()
    waiting_for_post_content = State()
    waiting_for_length_choice = State() 
    waiting_for_ai_image_choice = State()
    
    # Yangi professional holatlar
    waiting_for_approval = State()  # Tasdiqlash va tahrirlash bosqichi
    waiting_for_edit_text = State() # Matnni qo'lda tahrirlash uchun

    # Profil tahrirlash uchun holatlar
    edit_field_choice = State()
    editing_name = State()
    editing_surname = State()
    editing_phone = State()

    waiting_for_ban_id = State()
    waiting_for_unban_id = State()
    # Broadcast states
    broadcasting = State()
    asking_extra_photo = State()
    asking_extra_text = State()
    confirm_broadcast = State()

# --- Yordamchi Funksiya: Post yuborish ---
async def send_post_to_channels(user_id, text, photo_bytes=None):
    # Faqat shu userga tegishli kanallarni bazadan olamiz
    channels = await db.get_user_channels(user_id)
    
    if not channels:
        return 0

    count = 0
    CAPTION_LIMIT = 1024 

    for ch_id in channels:
        try:
            chat = await bot.get_chat(chat_id=ch_id)
            
            # Har bir kanal uchun o'z linkini yasaymiz
            if chat.username:
                link_text = f"\n\n👉 <a href='https://t.me/{chat.username}'>{chat.title}</a> kanaliga obuna bo'ling!"
            else:
                link_text = f"\n\n👉 <b>{chat.title}</b> kanaliga obuna bo'ling!"
            
            final_text = text + link_text

            if photo_bytes:
                photo_file = BufferedInputFile(photo_bytes, filename="ai_image.png")
                if len(final_text) <= CAPTION_LIMIT:
                    await bot.send_photo(chat_id=ch_id, photo=photo_file, caption=final_text)
                else:
                    await bot.send_photo(chat_id=ch_id, photo=photo_file)
                    await bot.send_message(chat_id=ch_id, text=final_text, disable_web_page_preview=True)
            else:
                await bot.send_message(chat_id=ch_id, text=final_text, disable_web_page_preview=True)
            
            count += 1
            await asyncio.sleep(0.3) 
            
        except Exception as e:
            logging.error(f"Kanalga yuborishda xato ({ch_id}): {e}")
            
    return count

class BanCheckMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        # Adminlar uchun tekshiruvni o'tkazib yuboramiz
        user_id = event.from_user.id
        if user_id in ADMIN:
            return await handler(event, data)

        # Ban holatini tekshirish
        ban_until_str = await db.get_ban_info(user_id)
        
        if ban_until_str:
            now = datetime.now()
            # SQLite dan kelgan stringni datetime obyektiga o'giramiz
            ban_time = datetime.strptime(ban_until_str, '%Y-%m-%d %H:%M:%S')
            
            if now < ban_time:
                remaining = ban_time - now
                days = remaining.days
                hours, remainder = divmod(remaining.seconds, 3600)
                minutes, _ = divmod(remainder, 60)
                
                time_str = f"{days} kun, {hours} soat va {minutes} daqiqa"
                await event.answer(
                    f"⚠️ <b>Sizning accountingiz bloklangan!</b>\n\n"
                    f"⏳ Blok tugashiga qoldi: <code>{time_str}</code>\n"
                    f"❗️ Qoidalarni buzmang.",
                    parse_mode="HTML"
                )
                return # Handlerga o'tmaydi, bot javob qaytarmaydi
            else:
                # Muddat o'tgan bo'lsa ban'dan ochish (db.get_ban_info buni o'zi ham qiladi)
                await db.set_user_ban_status(user_id, False)

        return await handler(event, data)

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    # BU YERDAN db.init_db() OLIB TASHLANDI - chunki u main() da bir marta ishlaydi
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer(
            "👋 Assalomu alaykum! <b>BoshqarAI</b> ga xush kelibsiz.\n"
            "Ro'yxatdan o'tish uchun ismingizni kiriting:", 
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(Form.waiting_for_name)
    else:
        await message.answer(f"Xush kelibsiz, {user.first_name}!", reply_markup=main_menu)

@dp.message(Form.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(first_name=message.text)
    await message.answer("Familiyangizni kiriting:")
    await state.set_state(Form.waiting_for_surname)

@dp.message(Form.waiting_for_surname)
async def process_surname(message: types.Message, state: FSMContext):
    await state.update_data(last_name=message.text)
    await message.answer("Telefon raqamingizni yuboring:", reply_markup=contact_markup)
    await state.set_state(Form.waiting_for_phone)

@dp.message(Form.waiting_for_phone, F.contact)
async def process_phone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_data = {
        "user_id": message.from_user.id,
        "first_name": data['first_name'],
        "last_name": data['last_name'],
        "phone": message.contact.phone_number,
        "username": message.from_user.username,
        "plan": "Free"
    }
    await db.save_user(user_data)
    await message.answer("✅ Ro'yxatdan muvaffaqiyatli o'tdingiz!", reply_markup=main_menu)
    await state.clear()

# --- PROFESSIONAL POST TAYYORLASH VA PREVIEW ---
@dp.message(F.text == "📝 Post yaratish") # Menyudagi matn bilan bir xil bo'lishi kerak
async def btn_manual_post(message: types.Message, state: FSMContext):
    # 1. Bazadan foydalanuvchi kanallarini olamiz
    user_channels = await db.get_user_channels(message.from_user.id)
    
    # 2. Kanal qo'shilmagan bo'lsa, post yaratishga yo'l qo'ymaymiz
    if not user_channels:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="start_add_channel")]
        ])
        await message.answer(
            "⚠️ <b>Xatolik:</b> Sizda hali ulangan kanallar mavjud emas.\n"
            "Post yaratish uchun avval kamida bitta kanal ulashingiz shart!",
            reply_markup=kb
        )
        return # Funksiyani shu yerda to'xtatadi

    # 3. Agar hammasi joyida bo'lsa, mavzuni so'raymiz
    await message.answer(
        "📝 <b>Yangi post yaratish</b>\n\n"
        "Post nima haqida bo'lishini qisqacha yozing:", 
        reply_markup=cancel_menu
    )
    await state.set_state(Form.waiting_for_post_content)

@dp.message(Form.waiting_for_post_content)
async def process_manual_topic(message: types.Message, state: FSMContext):
    if message.text == "🔙 Bekor qilish":
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=main_menu)
        return

    # --- TREND VA STATISTIKA UCHUN LOG ---
    topic = message.text
    user_id = message.from_user.id
    
    # Bazaga yozish funksiyasini chaqiramiz
    await db.log_ai_request(user_id, topic)
    # -------------------------------------

    await state.update_data(topic=topic)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Qisqa (SMM)", callback_data="len_short"),
         InlineKeyboardButton(text="📖 Batafsil (Maqola)", callback_data="len_long")]
    ])
    
    await message.answer("Post qanday uslubda bo'lsin?", reply_markup=kb)
    await state.set_state(Form.waiting_for_length_choice)
@dp.callback_query(F.data.startswith("len_"), Form.waiting_for_length_choice)
async def process_length_choice(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(is_long=(callback.data == "len_long"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 AI Rasm qidirish", callback_data="gen_ai_img")],
        [InlineKeyboardButton(text="📝 Faqat matn", callback_data="text_only")]
    ])
    await callback.message.edit_text("Post uchun mos rasm ham qidiramiz?", reply_markup=kb)
    await state.set_state(Form.waiting_for_ai_image_choice)

def get_post_approval_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Jo'natish", callback_data="post_confirm"),
         InlineKeyboardButton(text="🏷 Hashtag", callback_data="post_hashtag")],
        [InlineKeyboardButton(text="📝 Tahrirlash", callback_data="post_edit_text"),
         InlineKeyboardButton(text="🔄 Rasm", callback_data="post_regen_img")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="post_cancel")]
    ])

# --- PREVIEW VA MA'LUMOTLARNI TAYYORLASH ---
@dp.callback_query(Form.waiting_for_ai_image_choice)
async def process_preview(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_text("⏳ BoshqarAI matn tayyorlamoqda...")
    
    # AIService'dan post va rasm promptini olish
    post_text, img_prompt = await AIService.generate_post_and_prompt(data['topic'], is_long=data.get('is_long', False))
    
    img_bytes = None
    if callback.data == "gen_ai_img":
        await callback.message.edit_text("🎨 Rasm chizilmoqda...")
        img_bytes = await ImageService.generate_image(img_prompt)

    # Ma'lumotlarni saqlaymiz
    await state.update_data(final_text=post_text, final_image=img_bytes, img_prompt=img_prompt)

    kb = get_post_approval_kb() # 2x2 tartibidagi tugmalar

    if img_bytes:
        photo = BufferedInputFile(img_bytes, filename="preview.png")
        await callback.message.answer_photo(photo=photo, caption=f"🔍 <b>Post ko'rinishi:</b>\n\n{post_text}", reply_markup=kb)
        await callback.message.delete()
    else:
        await callback.message.answer(f"🔍 <b>Post ko'rinishi:</b>\n\n{post_text}", reply_markup=kb)

    await state.set_state(Form.waiting_for_approval)

# --- AI HASHTAG GENERATSIYASI ---
@dp.callback_query(F.data == "post_hashtag", Form.waiting_for_approval)
async def post_hashtag(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    # Agar matnda hashtag bo'lsa, qayta qo'shmaymiz
    if "#" in data['final_text'][-20:]: 
        await callback.answer("Hashtaglar allaqachon mavjud!", show_alert=True)
        return

    await callback.answer("🏷 AI hashtaglar o'ylamoqda...")
    
    # AIService orqali mavzuga mos hashtag generatsiya qilish (Servisda bor deb hisoblaymiz)
    # Agar servisda yo'q bo'lsa, AIService.generate_hashtags(data['topic']) funksiyasini qo'shish kerak
    try:
        ai_hashtags = await AIService.get_hashtags_for_topic(data['topic'])
    except:
        ai_hashtags = "\n\n#BoshqarAI #Texnologiya #Uzbekistan"

    new_text = data['final_text'] + "\n\n" + ai_hashtags
    await state.update_data(final_text=new_text)
    
    kb = get_post_approval_kb()
    if callback.message.photo:
        await callback.message.edit_caption(caption=f"🔍 <b>Yangi ko'rinish:</b>\n\n{new_text}", reply_markup=kb)
    else:
        await callback.message.edit_text(text=f"🔍 <b>Yangi ko'rinish:</b>\n\n{new_text}", reply_markup=kb)

# --- MATNNI TAHRIRLASH ---
@dp.callback_query(F.data == "post_edit_text", Form.waiting_for_approval)
async def post_edit_text(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    # Foydalanuvchi nusxa olishi oson bo'lishi uchun mono shriftda yuboramiz
    await callback.message.answer(
        f"📝 <b>Matnni nusxalab oling va tahrirlab botga qayta yuboring:</b>\n\n"
        f"<code>{data['final_text']}</code>",
        parse_mode="HTML"
    )
    await state.set_state(Form.waiting_for_edit_text)
    await callback.answer()

@dp.message(Form.waiting_for_edit_text)
async def save_edited_text(message: types.Message, state: FSMContext):
    # Yangi tahrirlangan matnni saqlaymiz
    await state.update_data(final_text=message.text)
    data = await state.get_data()
    
    # Tahrirdan keyin yana o'sha 5ta tugma chiqadi
    kb = get_post_approval_kb()
    
    if data.get('final_image'):
        photo = BufferedInputFile(data['final_image'], filename="preview.png")
        await message.answer_photo(photo=photo, caption=f"🔍 <b>Tahrirlangan post:</b>\n\n{message.text}", reply_markup=kb)
    else:
        await message.answer(f"🔍 <b>Tahrirlangan post:</b>\n\n{message.text}", reply_markup=kb)
    
    await state.set_state(Form.waiting_for_approval)

# --- RASMNI YANGILASH ---
@dp.callback_query(F.data == "post_regen_img", Form.waiting_for_approval)
async def post_regen_img(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.answer("🔄 AI yangi rasm chizmoqda, kuting...")
    
    # Yangi rasm generatsiya qilish
    new_img = await ImageService.generate_image(data['img_prompt'])
    await state.update_data(final_image=new_img)
    
    kb = get_post_approval_kb()
    photo = BufferedInputFile(new_img, filename="preview_new.png")
    
    # Faqat rasmni o'zini yangilaymiz (MediaGroup emas, bitta rasm)
    await callback.message.edit_media(
        media=types.InputMediaPhoto(media=photo, caption=f"🔍 <b>Yangi rasm bilan:</b>\n\n{data['final_text']}"), 
        reply_markup=kb
    )

# --- TASDIQLASH VA BEKOR QILISH ---

@dp.callback_query(F.data == "post_confirm", Form.waiting_for_approval)
async def post_confirm(callback: types.CallbackQuery, state: FSMContext):
    # Holatdagi ma'lumotlarni olamiz
    data = await state.get_data()
    user_id = callback.from_user.id  # Tugmani bosgan foydalanuvchi IDsi
    
    # Yuklanish xabarini ko'rsatamiz
    await callback.message.answer("🚀 Kanallaringizga yuborish boshlandi...")
    
    # MUHIM: Funksiyaga user_id uzatiladi, shunda bot begona kanallarga yubormaydi
    count = await send_post_to_channels(
        user_id=user_id, 
        text=data['final_text'], 
        photo_bytes=data.get('final_image')
    )
    
    if count > 0:
        await callback.message.answer(
            f"✅ Tayyor! Post muvaffaqiyatli {count} ta kanalingizga yuborildi.", 
            reply_markup=main_menu
        )
    else:
        await callback.message.answer(
            "⚠️ Xatolik: Kanallaringiz topilmadi yoki bot admin emas. "
            "Iltimos, avval kanal qo'shing!", 
            reply_markup=main_menu
        )
    
    # Post yuborilgach, jarayonni yakunlab holatni tozalaymiz
    await state.clear()
    await callback.answer() # Telegram "soat" belgisini olib tashlash uchun

@dp.callback_query(F.data == "post_cancel", Form.waiting_for_approval)
async def post_cancel(callback: types.CallbackQuery, state: FSMContext):
    # Jarayonni bekor qilamiz va xotirani tozalaymiz
    await state.clear()
    
    # Oldingi ko'rinish (preview) xabarini o'chirib tashlaymiz
    try:
        await callback.message.delete()
    except:
        pass # Agar xabar allaqachon o'chgan bo'lsa xato bermaydi
        
    await callback.message.answer("❌ Post bekor qilindi.", reply_markup=main_menu)
    await callback.answer("Bekor qilindi")

# --- KANALLARNI BOSHQARISH (MENU) ---
@dp.message(F.text == "📢 Kanallar")
async def btn_channels_management(message: types.Message):
    user_channels = await db.get_user_channels(message.from_user.id)
    
    if not user_channels:
        # Kanal ulanmagan bo'lsa
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="start_add_channel")]
        ])
        await message.answer("Sizda hali kanallar ulanmagan. Birinchi kanalingizni qo'shishingiz mumkin:", reply_markup=kb)
    else:
        # Kanal ulanib bo'lingan bo'lsa (Hozircha faqat 1-sini ko'rsatamiz)
        ch_id = user_channels[0]
        try:
            chat = await bot.get_chat(ch_id)
            ch_title = chat.title
        except:
            ch_title = "Noma'lum kanal (Adminlikni tekshiring)"

        text = (f"📢 <b>Sizning kanallaringiz:</b>\n\n"
                f"✅ 1. <b>{ch_title}</b> (<code>{ch_id}</code>)\n\n"
                f"⚠️ <i>Bepul tarifda faqat 1 ta kanal ulay olasiz.</i>")
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Kanalni o'chirish", callback_data=f"remove_ch_{ch_id}")]
        ])
        await message.answer(text, reply_markup=kb)

# --- KANAL QO'SHISH BOSQICHI (LIMIT TEKSHIRUVI BILAN) ---
@dp.callback_query(F.data == "start_add_channel")
async def start_add_channel_callback(callback: types.CallbackQuery, state: FSMContext):
    user_channels = await db.get_user_channels(callback.from_user.id)
    
    if len(user_channels) >= 1: # MANA SHU YERDA LIMIT (1 ta)
        await callback.answer("⚠️ Bepul tarifda faqat 1 ta kanal qo'shish mumkin!", show_alert=True)
        return

    await callback.message.answer("Kanal username (@kanal) yoki ID sini yuboring:", reply_markup=cancel_menu)
    await state.set_state(Form.waiting_for_channel)
    await callback.answer()

# --- KANALNI BAZAGA SAQLASH ---
@dp.message(Form.waiting_for_channel)
async def process_channel_id(message: types.Message, state: FSMContext):
    if message.text == "🔙 Bekor qilish":
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=main_menu)
        return
    
    try:
        chat = await bot.get_chat(message.text.strip())
        
        # Adminlikni tekshirish (Bot adminmi?)
        member = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
        if member.status not in ['administrator', 'creator']:
            await message.answer("❌ Bot bu kanalda admin emas! Avval admin qiling.")
            return

        if await db.add_channel(message.from_user.id, str(chat.id)):
            await message.answer(f"✅ <b>{chat.title}</b> muvaffaqiyatli qo'shildi!", reply_markup=main_menu)
            await state.clear()
        else:
            await message.answer("⚠️ Bu kanal ro'yxatingizda bor.")
            
    except Exception as e:
        await message.answer("❌ Kanal topilmadi. Kanal username to'g'riligini va bot adminligini tekshiring.")

# --- KANALNI O'CHIRISH (CALLBACK) ---
@dp.callback_query(F.data.startswith("remove_ch_"))
async def remove_channel_callback(callback: types.CallbackQuery):
    channel_id = callback.data.replace("remove_ch_", "")
    await db.remove_channel(callback.from_user.id, channel_id)
    
    await callback.message.edit_text("✅ Kanal o'chirildi. Endi yangi kanal ulanishi mumkin.")
    await callback.answer("O'chirildi")

# --- PROFIL ---
@dp.message(F.text == "👤 Profil")
async def btn_profile(message: types.Message):
    user = await db.get_user(message.from_user.id)
    # Faqat shu foydalanuvchining kanallarini olamiz
    user_channels = await db.get_user_channels(message.from_user.id)
    
    first_name = user.first_name if user else "Kiritilmagan"
    last_name = user.last_name if user else "Kiritilmagan"
    phone = user.phone if user else "Kiritilmagan"
    plan = user.plan if user else "Free"
    
    text = (
        f"<b>👤 Profilingiz</b>\n\n"
        f"📝 <b>Ism:</b> {first_name} {last_name}\n"
        f"📞 <b>Tel:</b> {phone}\n"
        f"💎 <b>Plan:</b> {plan}\n"
        f"📢 <b>Sizning kanallaringiz:</b> {len(user_channels)} ta"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Tahrirlash", callback_data="edit_profile")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "edit_profile")
async def edit_profile_menu(callback: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ism", callback_data="edit_name"), InlineKeyboardButton(text="Familiya", callback_data="edit_surname")],
        [InlineKeyboardButton(text="Telefon", callback_data="edit_phone")],
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="back_to_profile")]
    ])
    await callback.message.edit_text("Nimani o'zgartiramiz?", reply_markup=kb)
    await state.set_state(Form.edit_field_choice)

@dp.callback_query(F.data == "edit_name", Form.edit_field_choice)
async def edit_name_call(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Yangi ism kiriting:", reply_markup=cancel_menu)
    await state.set_state(Form.editing_name)

@dp.callback_query(F.data == "edit_surname", Form.edit_field_choice)
async def edit_surname_call(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Yangi familiya kiriting:", reply_markup=cancel_menu)
    await state.set_state(Form.editing_surname)

@dp.callback_query(F.data == "edit_phone", Form.edit_field_choice)
async def edit_phone_call(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Yangi telefon raqamni yuboring:", reply_markup=contact_markup)
    await state.set_state(Form.editing_phone)

@dp.callback_query(F.data == "back_to_profile")
async def back_to_profile_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await btn_profile(callback.message)

@dp.message(Form.editing_name)
async def update_name(message: types.Message, state: FSMContext):
    if message.text == "🔙 Bekor qilish":
        await message.answer("❌ O'zgartirish bekor qilindi.", reply_markup=main_menu)
    else:
        await db.update_user_field(message.from_user.id, "first_name", message.text)
        await message.answer("✅ Ism o'zgardi!", reply_markup=main_menu)
    await state.clear()

@dp.message(Form.editing_surname)
async def update_surname(message: types.Message, state: FSMContext):
    if message.text == "🔙 Bekor qilish":
        await message.answer("❌ O'zgartirish bekor qilindi.", reply_markup=main_menu)
    else:
        await db.update_user_field(message.from_user.id, "last_name", message.text)
        await message.answer("✅ Familiya o'zgardi!", reply_markup=main_menu)
    await state.clear()

@dp.message(Form.editing_phone, F.contact | F.text)
async def update_phone(message: types.Message, state: FSMContext):
    if message.text == "🔙 Bekor qilish":
        await message.answer("❌ O'zgartirish bekor qilindi.", reply_markup=main_menu)
    else:
        new_phone = message.contact.phone_number if message.contact else message.text
        await db.update_user_field(message.from_user.id, "phone", new_phone)
        await message.answer(f"✅ Tel yangilandi: {new_phone}", reply_markup=main_menu)
    await state.clear()

@dp.message(Command("admin"), IsAdmin())
async def admin_main(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stats")],
        [InlineKeyboardButton(text="👤 Userlar", callback_data="adm_users_0")],
        [InlineKeyboardButton(text="🚫 Ban / Unban", callback_data="adm_set_ban")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="adm_broadcast")]
    ])
    await message.answer("🛠 <b>Admin Dashboard</b>\nKerakli bo'limni tanlang:", reply_markup=kb)

@dp.callback_query(F.data == "adm_back", IsAdmin())
async def back_to_admin_main(callback: types.CallbackQuery, state: FSMContext):
    # Har qanday holatni (FSM) tozalaymiz, aks holda keyingi tugmalar ishlamay qolishi mumkin
    await state.clear()
    
    # Sening asosiy admin menyuing (o'zing yozganingdek)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stats")],
        [InlineKeyboardButton(text="👤 Userlar", callback_data="adm_users_0")],
        [InlineKeyboardButton(text="🚫 Ban / Unban", callback_data="adm_set_ban")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="adm_broadcast")]
    ])
    
    # Xabarni o'zgartiramiz
    await callback.message.edit_text(
        "🛠 <b>Admin Dashboard</b>\nKerakli bo'limni tanlang:", 
        reply_markup=kb,
        parse_mode="HTML"
    )
    
    # Telegramga callback qabul qilinganini bildiramiz (soat belgisi ketishi uchun)
    await callback.answer()

@dp.callback_query(F.data == "adm_stats", IsAdmin())
async def show_stats(callback: types.CallbackQuery):
    s = await db.get_admin_dashboard_stats()
    
    # Trendlarni chiroyli formatlash
    # t[0] - bu topic (mavzu), t[1] - bu count (soni)
    trends_text = "\n".join([f"🔥 {t[0]} ({t[1]} ta)" for t in s['trends']]) or "Hozircha yo'q"
    
    text = (f"📊 <b>Umumiy statistika:</b>\n\n"
            f"👤 <b>Userlar:</b>\n"
            f"  • Jami: {s['total_users']} ta\n"
            f"  • Shu oyda: +{s['monthly_users']} ta\n\n"
            f"🧠 <b>AI Requestlar:</b>\n"
            f"  • Jami: {s['total_ai']} ta\n"
            f"  • Shu oyda: {s['monthly_ai']} ta\n\n"
            f"📢 <b>Kanallar:</b>\n"
            f"  • Jami: {s['total_channels']} ta\n"
            f"  • Shu oyda: +{s['monthly_channels']} ta\n\n"
            f"📈 <b>Trenddagi mavzular:</b>\n{trends_text}")
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm_back")]
    ]))

@dp.callback_query(F.data.startswith("adm_users_"), IsAdmin())
async def list_users_paged(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[2])
    limit = 10
    offset = page * limit
    
    users = await db.get_users_list(limit, offset)
    if not users and page > 0:
        await callback.answer("Boshqa foydalanuvchi yo'q")
        return

    text = "👤 <b>Foydalanuvchilar ro'yxati:</b>\n\n"
    for i, u in enumerate(users, offset + 1):
        ch_count = await db.get_user_channel_count(u.user_id) # Nuqta bilan!
        text += f"{i}. {u.first_name} - {u.plan} - 📢 {ch_count} ta\n"

    nav_btns = []
    if page > 0:
        nav_btns.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_users_{page-1}"))
    nav_btns.append(InlineKeyboardButton(text=f"📄 {page+1}", callback_data="ignore"))
    if len(users) == limit:
        nav_btns.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_users_{page+1}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[nav_btns, [InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm_back")]])
    await callback.message.edit_text(text, reply_markup=kb)

# bot.py dagi handlerlar

@dp.callback_query(F.data == "adm_set_ban", IsAdmin())
async def ask_ban_id(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("🚫 Bloklamoqchi bo'lgan foydalanuvchi <b>ID</b> sini yuboring:", parse_mode="HTML")
    await state.set_state(Form.waiting_for_ban_id)

@dp.message(Form.waiting_for_ban_id, IsAdmin())
async def process_ban_id(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ ID faqat raqamlardan iborat bo'ladi!")
        return
    
    await state.update_data(ban_user_id=int(message.text))
    
    # Muddat tanlash tugmalari
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 soat", callback_data="ban_h_1"),
         InlineKeyboardButton(text="10 soat", callback_data="ban_h_10")],
        [InlineKeyboardButton(text="1 kun (24s)", callback_data="ban_h_24"),
         InlineKeyboardButton(text="3 kun (72s)", callback_data="ban_h_72")],
        [InlineKeyboardButton(text="7 kun (168s)", callback_data="ban_h_168"),
         InlineKeyboardButton(text="Abadiy (10 yil)", callback_data="ban_h_87600")],
        [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="adm_back")]
    ])
    
    await message.answer(f"⏳ User {message.text} uchun bloklash muddatini tanlang:", reply_markup=kb)
    await state.set_state(Form.confirm_broadcast) # Shunchaki holatni o'zgartiramiz

@dp.callback_query(F.data.startswith("ban_h_"), IsAdmin())
async def finalize_ban(callback: types.CallbackQuery, state: FSMContext):
    hours = int(callback.data.split("_")[2])
    data = await state.get_data()
    user_id = data.get('ban_user_id')
    
    await db.set_user_ban_status(user_id, True, hours)
    
    # Ban tugash vaqtini hisoblash (taxminiy ko'rsatish uchun)
    await callback.message.edit_text(f"✅ User <code>{user_id}</code> {hours} soatga bloklandi.", parse_mode="HTML")
    
    try:
        await bot.send_message(user_id, f"⚠️ Sizning accountingiz ma'lum muddatga bloklandi!\n⏳ Blok muddati: {hours} soat.")
    except:
        pass
    
    await state.clear()

@dp.callback_query(F.data == "adm_broadcast", IsAdmin())
async def bc_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Reklama xabarini yuboring (Matn yoki rasm):")
    await state.set_state(Form.broadcasting)

@dp.message(Form.broadcasting, IsAdmin())
async def bc_content(message: types.Message, state: FSMContext):
    if message.photo:
        await state.update_data(photo=message.photo[-1].file_id, text=message.caption or "")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Matnni o'zgartirish", callback_data="bc_add_text")],
            [InlineKeyboardButton(text="✅ Shunday yuborish", callback_data="bc_confirm")]
        ])
        await message.answer("Rasm qabul qilindi. Matn qo'shasizmi?", reply_markup=kb)
    else:
        await state.update_data(text=message.text, photo=None)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🖼 Rasm qo'shish", callback_data="bc_add_photo")],
            [InlineKeyboardButton(text="✅ Shunday yuborish", callback_data="bc_confirm")]
        ])
        await message.answer("Matn qabul qilindi. Rasm qo'shasizmi?", reply_markup=kb)

@dp.callback_query(F.data == "bc_confirm", IsAdmin())
async def bc_final_check(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = f"📢 <b>Reklamani yuboraymi?</b>\n\nContent: {data.get('text', 'Faqat rasm')[:50]}..."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 HA, JO'NAT!", callback_data="bc_send_now")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="adm_back")]
    ])
    await callback.message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "bc_send_now", IsAdmin())
async def bc_send_all(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    users = await db.get_all_user_ids()
    count, err = 0, 0
    
    await callback.message.edit_text(f"⏳ {len(users)} ta userga yuborilmoqda...")
    
    for uid in users:
        try:
            if data['photo']:
                await bot.send_photo(uid, data['photo'], caption=data['text'])
            else:
                await bot.send_message(uid, data['text'])
            count += 1
        except:
            err += 1
            
    await callback.message.answer(f"✅ Yakunlandi!\nQabul qildi: {count}\nXatolik: {err}")
    await state.clear()

# Bu handler barcha boshqa handlerlarga tushmagan xabarlarni tutib oladi
@dp.message()
async def unknown_message(message: types.Message):
    # Foydalanuvchiga xushmuomalalik bilan javob beramiz
    await message.answer(
        "🧐 <b>Tushunarsiz buyruq.</b>\n\n"
        "Iltimos, bot xizmatlaridan foydalanish uchun pastdagi menyu tugmalaridan foydalaning yoki to'gri buyruq yuboring.",
        reply_markup=main_menu, # Sening asosiy menyuing nomi
        parse_mode="HTML"
    )

# --- BOTNI ISHGA TUSHIRISH ---
async def main():
    # Baza faqat shu yerda, bot yoqilganda bir marta tekshiriladi
    await db.init_db()

    # Middleware'ni ulash
    dp.message.outer_middleware(BanCheckMiddleware())
    
    # Terminalga xabar chiqarish
    print("-" * 30)
    print("🚀 BoshqarAI bot ishga tushdi!")
    print(f"🤖 Bot: @{(await bot.get_me()).username}")
    print("✅ Baza ulandi va tekshirildi.")
    print("-" * 30)
    
    scheduler.start()
    
    # Eski xabarlarni o'chirib yuborish (webhook tozalash)
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Pollingni boshlash
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n🛑 Bot to'xtatildi!")