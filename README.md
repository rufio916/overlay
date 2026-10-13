# Overlay

NFL prop matchup tool for PrizePicks. Free stack:

- **nflverse** (free, no key): schedule, weekly player stats, injury reports
- **The Odds API** free tier (500 credits/month): PrizePicks lines + sharp book prices. Optional; the site works without it.
- **GitHub Actions** (free): refreshes stats and injuries Wed and Fri, and adds PrizePicks lines Sat and Sun mornings
- **GitHub Pages** (free): hosts the site at a link you can send friends

## Setup (one time, ~10 minutes)
1. Create a free GitHub account and a new **public** repo named `overlay`.
2. Upload every file in this folder (keep the `.github/workflows` folder).
3. Repo **Settings → Pages**: Source = "Deploy from a branch", branch = `main`, folder = `/ (root)`.
4. (Optional) Get a free key at the-odds-api.com, then **Settings → Secrets and variables → Actions → New secret** named `ODDS_API_KEY`.
5. **Actions** tab → "Refresh Overlay data" → **Run workflow** once.

Your site is at `https://<your-username>.github.io/overlay/`.

## Odds credits
Each odds refresh costs about 4 credits per game (~55 for a full slate). Fresh lines are pulled only on the
Sunday-morning run (~240 credits a month, inside the free 500). Every other run keeps the last lines.
For an extra pull (say, before a Thursday game): Actions → "Refresh Overlay data" → Run workflow → tick the odds box.
