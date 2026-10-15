# Overlay

NFL prop matchup tool for PrizePicks. Free stack:

- **nflverse** (free, no key): schedule, weekly player stats, injury reports
- **The Odds API** free tier (500 credits/month): PrizePicks lines + sharp book prices. Optional; the site works without it.
- **GitHub Actions** (free): refreshes stats and injuries daily and PrizePicks lines on Saturday mornings
- **GitHub Pages** (free): hosts the site at a link you can send friends

## Setup (one time, ~10 minutes)
1. Create a free GitHub account and a new **public** repo named `overlay`.
2. Upload every file in this folder (keep the `.github/workflows` folder).
3. Repo **Settings → Pages**: Source = "Deploy from a branch", branch = `main`, folder = `/ (root)`.
4. (Optional) Get a free key at the-odds-api.com, then **Settings → Secrets and variables → Actions → New secret** named `ODDS_API_KEY`.
5. **Actions** tab → "Refresh Overlay data" → **Run workflow** once.

Your site is at `https://<your-username>.github.io/overlay/`.

## Schedule and odds credits
- Every day ~5pm Pacific: stats and injury reports (free).
- Saturday ~10am and Sunday ~6am Pacific: fresh PrizePicks + sportsbook lines (~50-55 each of the 500 free monthly Odds API credits, which reset on the 1st).
- If fewer than 70 credits are left, odds pulls are skipped and the last lines are kept until the reset.
- All other runs keep the last lines. For an extra pull: Actions → "Refresh Overlay data" → Run workflow → tick the odds box.
GitHub's scheduled runs can start a few hours late when GitHub is busy.
