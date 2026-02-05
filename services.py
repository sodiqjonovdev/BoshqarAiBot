import feedparser
import logging
import os
import re
import requests
from groq import Groq
from dotenv import load_dotenv
from duckduckgo_search import DDGS

load_dotenv()

# API konfiguratsiyalari
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

class NewsService:
    @staticmethod
    def get_latest_news(rss_url):
        try:
            feed = feedparser.parse(rss_url)
            if feed.entries:
                entry = feed.entries[0]
                image_url = None
                
                if 'media_content' in entry:
                    image_url = entry.media_content[0]['url']
                elif 'links' in entry:
                    for link in entry.links:
                        if hasattr(link, 'type') and link.type.startswith('image/'):
                            image_url = link.href
                            break
                
                return {
                    "title": entry.title,
                    "link": entry.link,
                    "summary": entry.summary if 'summary' in entry else "",
                    "image": image_url
                }
        except Exception as e:
            logging.error(f"RSS Xatosi: {e}")
        return None

class AIService:
    @staticmethod
    async def generate_post_and_prompt(topic_or_title, context_text="", is_long=False):
        """Groq yordamida tartibli post matni va rasm qidiruv promptini yaratadi."""
        try:
            style_instruction = (
                "Batafsil maqola uslubida yoz. Har bir abzasdan keyin 2 ta yangi qator qoldir. Matn o'qishga juda qulay bo'lsin." if is_long 
                else "Qisqa, lo'nda va vizual chiroyli SMM post yoz. Har bir gap yoki punkt alohida qatorda bo'lsin."
            )

            system_instruction = (
                "Sen professional o'zbek SMM kopirayterisan. "
                "Telegram uchun chiroyli postlar yozasan. "
                f"{style_instruction} "
                "MUHIM QOIDALAR:\n"
                "1. Matn zich bo'lmasin, abzaslar orasida bo'sh qatorlar (double newline) bo'lsin.\n"
                "2. Har bir punkt yoki yangi fikrni yangi qatordan, emoji bilan boshla.\n"
                "3. Sarlavhani doim qalin (<b>...</b>) qilib yoz.\n"
                "4. Jami matn 900 ta belgidan oshmasin (Telegram caption limiti uchun).\n"
                "5. Javobing oxirida rasm uchun inglizcha tavsifni mana bu formatda yoz: 'IMAGE_PROMPT: [tavsif]'"
            )

            completion = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": f"Mavzu: {topic_or_title}\nMa'lumot: {context_text}"}
                ],
                temperature=0.6
            )

            full_response = completion.choices[0].message.content
            
            if "IMAGE_PROMPT:" in full_response:
                parts = full_response.split("IMAGE_PROMPT:")
                post_text = parts[0].strip()
                image_prompt = parts[1].strip().replace("[", "").replace("]", "")
            else:
                post_text = full_response
                image_prompt = topic_or_title

            post_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', post_text).replace("*", "")
            post_text = re.sub(r'([.!?])\s*([\U00010000-\U0010ffff])', r'\1\n\n\2', post_text)
            
            lines = post_text.split('\n')
            if len(lines) > 0 and "<b>" in lines[0]:
                if len(lines) > 1 and lines[1].strip() != "":
                    lines.insert(1, "")
            post_text = '\n'.join(lines)

            if len(post_text) > 1000:
                post_text = post_text[:997] + "..."
            
            return post_text, f"{image_prompt} high resolution professional"

        except Exception as e:
            logging.error(f"AI Xatosi: {e}")
            return f"<b>{topic_or_title}</b>\n\n{context_text}"[:1000], topic_or_title

    # 🔥 SEN SO'RAGAN YANGI FUNKSIYA SHU YERDA:
    @staticmethod
    async def get_hashtags_for_topic(topic):
        """Mavzu asosida AI orqali hashtaglar generatsiya qilish."""
        try:
            system_prompt = (
                "Sen hashtaglar bo'yicha mutaxassisan. Foydalanuvchi bergan mavzu uchun "
                "eng ommabop va mos 5-8 ta hashtaglarni o'zbek yoki ingliz tilida (sohaga qarab) yaratib ber. "
                "Faqat hashtaglarni o'zini qaytar, orasida bo'sh joy bo'lsin. "
                "Masalan: #texnologiya #ai #uzbekistan"
            )
            
            completion = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Mavzu: {topic}"}
                ],
                temperature=0.5
            )
            
            hashtags = completion.choices[0].message.content.strip()
            # AI ba'zida ortiqcha gap qo'shib yuborsa, tozalaymiz
            clean_hashtags = " ".join(re.findall(r'#\w+', hashtags))
            return clean_hashtags if clean_hashtags else "#BoshqarAI #Texnologiya"
            
        except Exception as e:
            logging.error(f"Hashtag AI Xatosi: {e}")
            return "#BoshqarAI #Texnologiya #Uzbekistan"

class ImageService:
    @staticmethod
    async def generate_image(prompt_text):
        """
        DuckDuckGo orqali rasmni qidiradi va yuklab oladi.
        Yangilangan ddgs versiyasiga moslangan.
        """
        # Sifatsiz stok saytlarni qidiruvdan chiqarib tashlash
        excluded_sites = "-stock -shutterstock -gettyimages -adobestock -dreamstime -depositphotos -vector -alamy"
        search_query = f"{prompt_text} {excluded_sites}"
        
        try:
            # Kutubxonani har ehtimolga qarshi shu yerda chaqiramiz
            from duckduckgo_search import DDGS
            
            with DDGS() as ddgs:
                # timelimit parametrini olib tashladik, chunki u natijani cheklab qo'yishi mumkin
                results = list(ddgs.images(
                    keywords=search_query,
                    region="wt-wt",
                    safesearch="moderate",
                    max_results=15  # Ko'proq natija olamiz, tanlash imkoniyati bo'lishi uchun
                ))
                
                if not results:
                    logging.warning(f"Rasm topilmadi: {prompt_text}")
                    return None
                
                # Birinchi yaxshi rasmni tanlash logikasi
                best_image_url = results[0]['image']
                
                # Ishonchli manbalardan rasm qidirish
                trusted_sources = ['unsplash', 'pixabay', 'pexels', 'static', 'wp-content', 'cloudinary', 'images.unsplash']
                for res in results:
                    source_url = res['image'].lower()
                    if any(site in source_url for site in trusted_sources):
                        best_image_url = res['image']
                        break
                
                logging.info(f"Rasm yuklanmoqda: {best_image_url}")
                
                # Brauzer simulyatsiyasi (Headers) - Bu juda muhim!
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                }
                
                # Rasmni yuklab olish
                response = requests.get(best_image_url, headers=headers, timeout=15)
                
                if response.status_code == 200:
                    # Rasm hajmi juda kichik emasligini tekshirish (masalan, 5KB dan katta bo'lsin)
                    if len(response.content) > 5000:
                        return response.content
                    else:
                        logging.warning("Rasm hajmi juda kichik, boshqasini sinab ko'ring.")
                
            return None
            
        except Exception as e:
            logging.error(f"ImageService xatosi: {e}")
            return None