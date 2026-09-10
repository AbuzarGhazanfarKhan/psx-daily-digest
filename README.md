# PSX daily class notes

Free briefing for the Pakistan Stock Exchange. Written for someone who **just opened a broker account and is learning for a few weeks before buying**. Not financial advice.

Each trading day it:

- Explains in simple words whether the **whole market** went up or down
- Checks KSE-100 vs the **200-day average** (the slow trend line)
- Gives a **mood score** and a short lesson
- Tracks **your watchlist** (`watchlist.txt`)
- Lists healthier long-term KSE-100 names as a **study list**, not a shopping list
- Saves the notes in `reports/latest.md` and can send Telegram

## Schedule

GitHub runs this **Sunday–Thursday at 11:00 AM Pakistan time** (PSX trading days).

To change the time, edit `.github/workflows/psx_daily.yml` — the comment sits on the line above the cron. Pakistan is UTC+5, so 11:00 AM PKT = `0 6 * * 0-4`.

## Watchlist

Edit `watchlist.txt` (one symbol per line). The next run will report those names in simple language.

## Telegram — two people, two bots

Same GitHub Action, two BotFather bots.

**You (learner)** — secrets you already have:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

**Father (investor)** — new bot on his phone, then two more secrets:

- `TELEGRAM_BOT_TOKEN_INVESTOR`
- `TELEGRAM_CHAT_ID_INVESTOR`

He opens **his** bot, sends `/start` then `hi`, then `getUpdates` on **his** token to get chat id.

Each person gets:

1. A **short English** ping (numbers + one-line bias)
2. **Detailed Roman Urdu** (Urdu speech, English letters). Words like KSE-100, 200-day average, volume, breadth stay in English.

Learner note = study, do not buy yet.  
Investor note = tape, stance, quality screen — still not financial advice.

Files: `reports/latest.md` (you) and `reports/latest-investor.md` (him).
