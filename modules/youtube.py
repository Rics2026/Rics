import os
import re
import random
import ollama
import asyncio
import edge_tts
import yt_dlp
from dotenv import load_dotenv
from googleapiclient.discovery import build

# MoviePy 2.x Importe
from moviepy import TextClip, AudioFileClip, CompositeVideoClip, VideoFileClip, ColorClip, CompositeAudioClip
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

load_dotenv()

# --- SYSTEM SETUP ---
os.environ["IMAGEMAGICK_BINARY"] = os.getenv("IMAGEMAGICK_BINARY", "/usr/bin/convert")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YT_BASE_DIR = os.path.join(PROJECT_DIR, "workspace", "YouTube")
YT_ASSETS_DIR = os.path.join(YT_BASE_DIR, "assets")

for d in [YT_BASE_DIR, YT_ASSETS_DIR]: 
    os.makedirs(d, exist_ok=True)

def escape_md(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text))

# --- MATCHING GAMEPLAY DOWNLOADER ---
def download_gaming_video(keyword):
    clean_keyword = re.sub(r'[^a-zA-Z0-9 ]', '', keyword).strip()
    search_query = f"{clean_keyword} gameplay 4k no commentary"
    output_path = os.path.join(YT_ASSETS_DIR, f"match_{clean_keyword.lower().replace(' ', '_')}.mp4")
    
    if os.path.exists(output_path): return output_path

    ydl_opts = {
        'format': 'bestvideo[height<=1080][ext=mp4]/best',
        'default_search': 'ytsearch1',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'download_ranges': lambda info_dict, ydl: [{'start_time': 30, 'end_time': 120}],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([search_query])
    return output_path

# --- BEFEHLE ---

async def yt_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        r"🎮 *GAMING STORY BOT*" + "\n\n"
        r"🚀 `/yt_trends` \- Erstellt Gaming\-Skript" + "\n"
        r"🎬 `/make_video` \- Rendert das Video"
    )
    await update.message.reply_text(help_text, parse_mode='MarkdownV2')

async def get_yt_trends(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key: return await update.message.reply_text("❌ API Key fehlt!")
    
    await update.message.reply_chat_action("typing")
    try:
        youtube = build('youtube', 'v3', developerKey=api_key)
        res = youtube.videos().list(part="snippet", chart="mostPopular", regionCode="DE", videoCategoryId="20", maxResults=5).execute()
        trend_titles = "\n".join([f"- {i['snippet']['title']}" for i in res.get('items', [])])

        prompt = (
            f"Trends:\n{trend_titles}\n\n"
            "Wähle EIN Spiel. Erstelle ein Voiceover (20s).\n"
            "TRENNE alle 5 Wörter mit einem '|'.\n"
            "ANTWORTE STRENG NUR SO:\n"
            "KEYWORD: [Spielname]\n"
            "VOICEOVER: [Text mit |]"
        )
        
        resp = ollama.chat(model=os.getenv("OLLAMA_MODEL", "qwen3:8b"), messages=[{'role': 'user', 'content': prompt}])
        content = resp['message']['content'].strip()
        
        with open(os.path.join(YT_BASE_DIR, "concept_latest.txt"), "w", encoding="utf-8") as f:
            f.write(content)
            
        await update.message.reply_text(escape_md(f"🎮 *SKRIPT BEREIT*\n\n{content}\n\n🚀 /make_video"), parse_mode='MarkdownV2')
    except Exception as e: await update.message.reply_text(f"❌ Fehler: {str(e)}")

async def make_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎬 Rendere Video...")
    try:
        concept_path = os.path.join(YT_BASE_DIR, "concept_latest.txt")
        if not os.path.exists(concept_path): return await update.message.reply_text("❌ Erst /yt_trends nutzen!")
        
        with open(concept_path, "r", encoding="utf-8") as f: 
            script_raw = f.read()

        # SICHERER PARSER (verhindert 'NoneType' Fehler)
        kw_match = re.search(r"KEYWORD:\s*(.*)", script_raw, re.I)
        vo_match = re.search(r"VOICEOVER:\s*(.*)", script_raw, re.S | re.I)

        if not kw_match or not vo_match:
            return await update.message.reply_text("❌ KI hat das Format verhunzt. Bitte /yt_trends nochmal!")

        keyword = kw_match.group(1).strip().split('\n')[0]
        voice_text = vo_match.group(1).strip()

        audio_path = os.path.join(YT_BASE_DIR, "voice.mp3")
        clean_voice = voice_text.replace("|", " ").strip()
        await edge_tts.Communicate(clean_voice, "de-DE-KillianNeural", rate="+5%").save(audio_path)
        voice_clip = AudioFileClip(audio_path)

        video_file = download_gaming_video(keyword)
        full_clip = VideoFileClip(video_file)
        
        duration = voice_clip.duration + 1.5
        start_t = random.uniform(10, max(10, full_clip.duration - duration - 5))
        
        bg_clip = full_clip.subclipped(start_t, start_t + duration).resized(height=1920)
        
        if bg_clip.audio is not None:
            game_atmo = bg_clip.audio.with_volume_scaled(0.15)
        else:
            game_atmo = None

        w, h = bg_clip.size
        bg_clip = bg_clip.cropped(x1=(w-1080)//2, y1=0, x2=(w+1080)//2, y2=1920)

        ui_clips = []
        parts = [p.strip() for p in voice_text.split("|") if p.strip()]
        part_dur = voice_clip.duration / len(parts)

        for i, part in enumerate(parts):
            bg_rect = ColorClip(size=(900, 140), color=(0,0,0)).with_opacity(0.5).with_start(i * part_dur).with_duration(part_dur).with_position(('center', 1500))
            txt = TextClip(text=part.upper(), font_size=40, color='white', font='Arial', method='caption', size=(850, None)).with_start(i * part_dur).with_duration(part_dur).with_position(('center', 1520))
            ui_clips.extend([bg_rect, txt])

        audio_list = [voice_clip]
        if game_atmo: audio_list.append(game_atmo)
        
        final_video = CompositeVideoClip([bg_clip] + ui_clips).with_audio(CompositeAudioClip(audio_list))
        output_path = os.path.join(YT_BASE_DIR, "final_short.mp4")
        final_video.write_videofile(output_path, fps=30, codec="libx264", audio_codec="aac")

        with open(output_path, 'rb') as v:
            await context.bot.send_video(chat_id=update.effective_chat.id, video=v, caption=f"✅ {keyword} fertig!")
        
        if os.path.exists(audio_path): os.remove(audio_path)

    except Exception as e: 
        await update.message.reply_text(f"❌ Fehler: {str(e)}")

# Metadaten
get_yt_trends.description = "Zeigt aktuelle YouTube-Trends & Top-Videos"
get_yt_trends.category    = "Content"
make_video.description    = "Erstellt ein Gaming-Video aus einem Keyword (/make_video minecraft)"
make_video.category       = "Content"
yt_help.description       = "Hilfe & Übersicht aller YouTube-Funktionen"
yt_help.category          = "Content"

def setup(app):
    app.add_handler(CommandHandler("yt_trends", get_yt_trends))
    app.add_handler(CommandHandler("make_video", make_video))
    app.add_handler(CommandHandler("yt_help", yt_help))