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

GitHub runs the job **Monday–Friday at 11:00 AM Pakistan time**.

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

## Optional phone ping (still free)

If you already use Discord or Slack, create an incoming webhook and add a repo secret named `DIGEST_WEBHOOK_URL`. The next run will post the digest there.
