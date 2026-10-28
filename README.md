# Overlay

NFL (index.html) and NBA (nba.html) betting research, all on free data except optional NFL prop odds.

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
- Thursday and Saturday ~10am Pacific: fresh PrizePicks + sportsbook lines (~4 credits per remaining game, ~50-60 per pull, of the 500 free monthly Odds API credits, which reset on the 1st).
- If there aren't enough credits left for a pull, it's skipped and the last lines are kept until the reset.
- All other runs keep the last lines. For an extra pull: Actions → "Refresh Overlay data" → Run workflow → tick the odds box.
GitHub's scheduled runs can start a few hours late when GitHub is busy.

## NBA (free)
- Box scores and schedule: sportsdataverse (`nba_build.py`), refreshed with every run.
- Lines (DraftKings, via ESPN) and the injury report: ESPN's public site API. Lines usually appear the morning of each game, so a daily ~10:37am Pacific run picks them up.
- No Odds API credits are used for NBA.

## Pages
- `index.html`: **Today**: best NFL props, NBA games and leans, injury news, My bets (graded automatically) and Overlay's own track record.
- `nfl.html`: NFL props, slip, fantasy, pick'em, defenses, games.
- `nba.html`: NBA games, props, slip, defenses.

`tracker.py` logs every pick the site rates at or above break-even before kickoff (NFL props ≥54%, NBA leans) into `history.json`, grades them from box scores, and writes `today.json`. `python assemble.py all` rebuilds all three pages. Saved bets live in each browser's local storage. "Share" makes a link that loads your slip on a friend's phone. Add to Home Screen installs it like an app.
