
# Telegram Challenge Bot (Uzbek)
Guruh+DM challenge bot: Reading15, Wake6, Sport20; check-in, streak, stats, leaderboard, APScheduler eslatmalar.

## Fayllar
- bot.py
- requirements.txt
- .env.example

## Ishga tushirish (lokal)
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # .env ichiga BOT_TOKEN va ALLOWED_GROUP_ID kiriting
python bot.py
```

## 24/7 (Railway/Render)
- GitHub repo yarating va ushbu fayllarni yuklang
- Environment Variables:
  - BOT_TOKEN
  - ALLOWED_GROUP_ID
  - TZ=Asia/Tashkent
- Start Command: `python bot.py`
