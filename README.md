# PSX daily digest

Free weekday briefing for the Pakistan Stock Exchange. No Cursor Automation, no paid API.

Once a day it:

- Pulls delayed **KSE-100** data from the public PSX data portal (Yahoo Finance is the fallback)
- Says whether the market looks **up, down, or mixed** vs the 20 / 50 / 200-day averages
- Gives a **conservative stance** (not a buy/sell order)
- Lists **public headlines** from Dawn Business, Business Recorder, and Express Tribune
- Saves the write-up in `reports/latest.md`

This is **not financial advice**. You can lose money.

## Schedule

GitHub runs the job **Monday–Friday at 11:00 AM Pakistan time**. Each run writes a long markdown brief (sentiment, what to do today, historically strong KSE-100 names, movers, sectors, news) to `reports/latest.md`. Telegram gets the same brief as three messages.

To change the time, edit `.github/workflows/psx_daily.yml` and change the cron line **under the comment**. GitHub cron is UTC (Pakistan is UTC+5), so 11:00 AM PKT = `0 6 * * 1-5`.

PSX actually trades **Sunday–Thursday**. If you want trading days instead of Mon–Fri, use `0 6 * * 0-4`.

## One-time setup (still $0)

1. Create a free GitHub account if you do not have one.
2. Create a new **empty** repository named `psx-daily-digest` (public is easiest; Actions minutes are free on public repos).
3. In this folder, run:

```bash
git add .
git commit -m "Add free daily PSX digest"
git remote add origin https://github.com/YOUR_USER/psx-daily-digest.git
git push -u origin main
```

4. On GitHub: **Actions → Daily PSX digest → Run workflow**.
5. After it finishes, open the run for the report, or read `reports/latest.md` in the repo.

GitHub can delay scheduled jobs by a few minutes. The first scheduled run happens after the file is on the default branch.

## Telegram (still free)

The daily job can DM you on Telegram through a bot you own. GitHub never needs your Telegram password.

1. In Telegram, open [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name and username.
2. Copy the bot token BotFather gives you. Do not put it in the repo or in chat.
3. Open your new bot and tap **Start** (or send `/start`). The bot cannot message you until you do this.
4. In a browser, open:
   `https://api.telegram.org/botPASTE_TOKEN_HERE/getUpdates`
5. Find `"chat":{"id":` and copy that number. For a private chat it looks like `123456789`.
6. On the GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**. Add two secrets:
   - `TELEGRAM_BOT_TOKEN` = the BotFather token
   - `TELEGRAM_CHAT_ID` = the chat id number
7. **Actions → Daily PSX digest → Run workflow**. You should get a Telegram message within a minute.

If `getUpdates` shows `"ok":true,"result":[]`, send the bot another message and refresh.

## Optional Discord / Slack

Create an incoming webhook and add a repo secret named `DIGEST_WEBHOOK_URL`. The next run will post the digest there.
