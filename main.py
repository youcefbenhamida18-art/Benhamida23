import asyncio
import json
import os
import random
import re
import time
import edge_tts
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from moviepy.editor import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    ImageClip,
    VideoFileClip,
    concatenate_videoclips,
)
import PIL.Image
import requests

# إصلاح خطأ Pillow الشهير في GitHub Actions
if not hasattr(PIL.Image, "ANTIALIAS"):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

# ==========================================
# 1. استدعاء الـ Secrets كما هي في صورتك بالضبط
# ==========================================
GEMINI_KEYS = [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
]
GEMINI_KEYS = [k for k in GEMINI_KEYS if k]  # تصفية المفاتيح الفارغة

CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_TOKEN = os.getenv("CF_API_TOKEN")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")

YT_CLIENT_ID = os.getenv("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.getenv("YT_REFRESH_TOKEN")

# قائمة ترندات كوميدية عشوائية لضمان تنوع المحتوى وعدم التكرار
TREND_TOPICS = [
    "تقليد ساخر لفيديوهات الروتين الصباحي للأثرياء مقابل الواقع المضحك للشاب العربي",
    "تقليد مضحك جداً لخبراء التنمية البشرية اللي يقولك استيقظ الساعة 3 الفجر واستحم بماء متجمد",
    "سخرية كوميدية من ترندات أول يوم في الجيم والتصوير أمام المرآة مقابل الهروب ثاني يوم",
    "تقليد درامي مضحك لرد فعل الأم العربية لما يضيع غرض في البيت وهي التي خبأته",
    "تقليد ساخر لمشاهير السوشيال ميديا لما يعلنون عن منتج فاشل وكأنه معجزة",
    "كوميديا ساخرة عن ترند الدايت الصحي اللي ينتهي بأكل شاورما وبروستد الساعة 2 بالليل",
    "تقليد مضحك لأصحاب الفيديوهات الحزينة والدرامية على تيك توك على أشياء تافهة مثل انتهاء الشاحن",
    "سخرية مضحكة من ترند المقابلات في الشارع والأسئلة الغبية والردود الصادمة",
]


# ==========================================
# 2. توليد السكريبت الكوميدي عبر Gemini
# ==========================================
def generate_comedy_script():
    selected_trend = random.choice(TREND_TOPICS)
    print(f"🎭 جاري صنع فيديو كوميدي عن: {selected_trend}")

    prompt = f"""
    أنت أشهر صانع محتوى كوميدي ساخر (Parody Creator) على يوتيوب شورتس وتيك توك.
    مهمتك كتابة سكريبت قصير ومضحك جداً (يموت من الضحك) يقلد ويسخر من هذا الترند:
    "{selected_trend}"

    الشروط:
    1. اللهجة: عربية بسيطة مطعمة بإفيهات مصرية/عربية دارجة ومضحكة جداً وسريعة الإيقاع.
    2. المدة: يجب أن يكون النص الصوتي بين 45 إلى 65 كلمة (حوالي 25-35 ثانية).
    3. بدون أي مقدمات مملة (لا تقل "مرحبا بكم")، ادخل في الإفيه والتقليد المضحك فوراً من أول ثانية!
    4. أرجع النتيجة بصيغة JSON فقط بدون أي نص إضافي خارج الـ JSON.

    هيكل الـ JSON المطلوب:
    {{
      "title": "عنوان فيديو مضحك وجذاب جداً لا يتعدى 60 حرف مع إيموجي 😂🔥 #shorts",
      "description": "وصف مضحك للفيديو مع هاشتاجات الترند والكوميديا #ضحك #كوميديا #ترند #shorts",
      "tags": ["ضحك", "كوميديا", "ترند", "تقليد", "مواقف_مضحكة", "shorts"],
      "voiceover": "النص الكوميدي كامل الذي سيقرأه المعلق الصوتي بدون أي رموز أو إيموجي، فقط كلام مضحك ومباشر.",
      "pixabay_queries": ["funny cat", "laughing man", "crazy dance", "shocked face"],
      "image_prompts": [
        "3D Pixar style funny cartoon parody of an exhausted Arab guy waking up with messy hair holding coffee, hilarious expression, vibrant colors, vertical 9:16",
        "3D Pixar style funny cartoon character flexing tiny muscles in gym mirror, comedic exaggeration, vibrant lighting, vertical 9:16"
      ]
    }}
    """

    for idx, api_key in enumerate(GEMINI_KEYS):
        try:
            print(f"🔑 تجربة مفتاح Gemini رقم {idx + 1}...")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.9},
            }
            res = requests.post(url, json=payload, timeout=30)
            if res.status_code == 200:
                text = res.json()["candidates"][0]["content"]["parts"][0][
                    "text"
                ]
                # تنظيف النص لاستخراج JSON فقط
                json_match = re.search(r"\{.*\}", text, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group(0))
                    print("✅ تم توليد السكريبت الكوميدي بنجاح!")
                    return data
            else:
                print(f"⚠️ فشل المفتاح {idx + 1}: {res.text}")
        except Exception as e:
            print(f"⚠️ خطأ في المفتاح {idx + 1}: {e}")

    raise Exception("❌ فشلت جميع مفاتيح Gemini! تأكد من صلاحيتها.")


# ==========================================
# 3. توليد التعليق الصوتي الكوميدي (Edge-TTS)
# ==========================================
async def create_voiceover(text, output_file="voice.mp3"):
    # استخدام الصوت المصري "شاكر" مع تسريع بسيط (+10%) لإعطاء طابع كوميدي حماسي
    voice = "ar-EG-ShakirNeural"
    clean_text = re.sub(r"[^\w\s\.,!؟،]", "", text)
    communicate = edge_tts.Communicate(clean_text, voice, rate="+10%")
    await communicate.save(output_file)
    print("🎙️ تم توليد الصوت الكوميدي بنجاح!")


# ==========================================
# 4. توليد صور مضحكة عبر Cloudflare AI
# ==========================================
def generate_cf_image(prompt, filename):
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        return None
    try:
        print(f"🎨 توليد صورة ميمز بالذكاء الاصطناعي: {prompt[:40]}...")
        url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/@cf/stabilityai/stable-diffusion-xl-base-1.0"
        headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
        payload = {"prompt": prompt, "width": 768, "height": 1344}
        res = requests.post(url, headers=headers, json=payload, timeout=45)
        if res.status_code == 200:
            with open(filename, "wb") as f:
                f.write(res.content)
            return filename
    except Exception as e:
        print(f"⚠️ تخطي صورة Cloudflare بسبب: {e}")
    return None


# ==========================================
# 5. جلب مقاطع فيديو مضحكة من Pixabay
# ==========================================
def fetch_pixabay_videos(queries, max_videos=3):
    downloaded = []
    if not PIXABAY_API_KEY:
        return downloaded

    search_terms = queries + ["funny", "laugh", "monkey", "cat funny"]
    for idx, q in enumerate(search_terms):
        if len(downloaded) >= max_videos:
            break
        try:
            url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={requests.utils.quote(q)}&per_page=5"
            res = requests.get(url, timeout=20).json()
            hits = res.get("hits", [])
            if hits:
                video_url = random.choice(hits)["videos"]["medium"]["url"]
                v_Data = requests.get(video_url, timeout=30).content
                fname = f"pix_{idx}.mp4"
                with open(fname, "wb") as f:
                    f.write(v_Data)
                downloaded.append(fname)
                print(f"🎬 تم تحميل فيديو مضحك من Pixabay لـ: {q}")
        except Exception as e:
            print(f"⚠️ خطأ أثناء جلب فيديو Pixabay: {e}")
    return downloaded


# ==========================================
# 6. مونتاج الفيديو النهائي (9:16 Shorts)
# ==========================================
def build_final_video(audio_path, image_files, video_files, output_path="final_short.mp4"):
    print("🎬 جاري مونتاج الفيديو الكوميدي...")
    audio = AudioFileClip(audio_path)
    total_duration = audio.duration

    visual_assets = []
    # دمج الصور والفيديوهات بالتناوب لزيادة التشويق والضحك
    max_len = max(len(image_files), len(video_files))
    for i in range(max_len):
        if i < len(image_files) and image_files[i]:
            visual_assets.append(("img", image_files[i]))
        if i < len(video_files) and video_files[i]:
            visual_assets.append(("vid", video_files[i]))

    if not visual_assets:
        # خلفية احتياطية في حال عدم توفر إنترنت للصور
        clip = ColorClip(size=(1080, 1920), color=(30, 30, 30), duration=total_duration)
        clip = clip.set_audio(audio)
        clip.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac")
        return output_path

    segment_duration = total_duration / len(visual_assets)
    clips = []

    for v_type, path in visual_assets:
        try:
            if v_type == "img":
                c = ImageClip(path).set_duration(segment_duration)
                c = c.resize(height=1920)
                if c.w < 1080:
                    c = c.resize(width=1080)
                c = c.crop(x_center=c.w / 2, y_center=c.h / 2, width=1080, height=1920)
                clips.append(c)
            else:
                v = VideoFileClip(path).without_audio()
                if v.duration < segment_duration:
                    v = v.loop(duration=segment_duration)
                else:
                    v = v.subclip(0, segment_duration)
                v = v.resize(height=1920)
                if v.w < 1080:
                    v = v.resize(width=1080)
                v = v.crop(x_center=v.w / 2, y_center=v.h / 2, width=1080, height=1920)
                clips.append(v)
        except Exception as e:
            print(f"⚠️ تخطي مقطع بسبب خطأ بسيط: {e}")

    final_clip = concatenate_videoclips(clips, method="compose")
    final_clip = final_clip.set_audio(audio).set_duration(total_duration)

    final_clip.write_videofile(
        output_path,
        fps=24,
        codec="libx264",
        audio_codec="aac",
        threads=4,
        preset="ultrafast"
    )
    print("✅ تم إنتاج الفيديو بنجاح!")
    return output_path


# ==========================================
# 7. الرفع التلقائي على يوتيوب (قسم الكوميديا 23)
# ==========================================
def upload_to_youtube(video_path, metadata):
    print("🚀 جاري رفع الفيديو إلى قناتك على يوتيوب...")
    creds = Credentials(
        None,
        refresh_token=YT_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YT_CLIENT_ID,
        client_secret=YT_CLIENT_SECRET,
    )
    creds.refresh(Request())
    youtube = build("youtube", "v3", credentials=creds)

    title = metadata.get("title", "مواقف مضحكة وتقليد الترند 😂 #shorts")
    if "#shorts" not in title.lower():
        title = f"{title[:85]} #shorts"

    body = {
        "snippet": {
            "title": title[:100],
            "description": metadata.get("description", "فيديو كوميدي ساخر 😂 #shorts"),
            "tags": metadata.get("tags", ["ضحك", "كوميديا", "shorts"]),
            "categoryId": "23",  # رقم 23 مخصص لقسم الكوميديا (Comedy) في يوتيوب!
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    print(f"🎉 تم الرفع بنجاح! رابط الفيديو: https://youtu.be/{response['id']}")


# ==========================================
# التشغيل الرئيسي
# ==========================================
if __name__ == "__main__":
    # 1. كتابة السكريبت الساخر
    script_data = generate_comedy_script()

    # 2. تحويل النص إلى صوت مضحك
    audio_file = "voice.mp3"
    asyncio.run(create_voiceover(script_data["voiceover"], audio_file))

    # 3. توليد صور الكوميديا من Cloudflare
    img_files = []
    for idx, p in enumerate(script_data.get("image_prompts", [])[:2]):
        img = generate_cf_image(p, f"cf_img_{idx}.png")
        if img:
            img_files.append(img)

    # 4. تحميل مقاطع مضحكة من Pixabay
    vid_files = fetch_pixabay_videos(script_data.get("pixabay_queries", []), max_videos=3)

    # 5. المونتاج والدمج
    final_video = build_final_video(audio_file, img_files, vid_files)

    # 6. النشر على يوتيوب
    upload_to_youtube(final_video, script_data)
