"""Overlay data builder.

Pulls free NFL data from nflverse (schedule, weekly player stats, snap counts, injury
reports), computes defense-vs-position numbers, usage trends, teammate-injury splits and
game environment for the upcoming week, and (optionally) pulls PrizePicks + sportsbook
prop lines from The Odds API when ODDS_API_KEY is set. Writes data.json for the page.

Usage:  python build_data.py [--week N] [--out data.json] [--local DIR]
"""
import warnings; warnings.filterwarnings("ignore")
import argparse, io, json, os, sys, urllib.request
from datetime import datetime, timezone
import pandas as pd

SEASON = 2026
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
FILES = {
    "games": f"{NFLVERSE}/schedules/games.csv",
    "stats": f"{NFLVERSE}/stats_player/stats_player_week_{SEASON}.csv",
    "inj": f"{NFLVERSE}/injuries/injuries_{SEASON}.csv",
    "snaps": f"{NFLVERSE}/snap_counts/snap_counts_{SEASON}.csv",
}
# Which props we track for each position: (label, stat column, Odds API market key)
MARKETS = {
    "QB": [("Pass Yds", "passing_yards", "player_pass_yds"), ("Rush Yds", "rushing_yards", "player_rush_yds")],
    "RB": [("Rush Yds", "rushing_yards", "player_rush_yds"), ("Rec Yds", "receiving_yards", "player_reception_yds"), ("Receptions", "receptions", "player_receptions")],
    "WR": [("Rec Yds", "receiving_yards", "player_reception_yds"), ("Receptions", "receptions", "player_receptions")],
    "TE": [("Rec Yds", "receiving_yards", "player_reception_yds"), ("Receptions", "receptions", "player_receptions")],
}
TAKE = {"QB": 1, "RB": 2, "WR": 3, "TE": 1}  # top players per team by usage
SHARP_BOOKS = ["pinnacle", "fanduel", "draftkings"]  # priority order for the fair price
DFS_BOOKS = ["prizepicks", "underdog"]
SKILL = {"QB", "RB", "WR", "TE"}


def load(name, local_dir):
    path = os.path.join(local_dir, os.path.basename(FILES[name])) if local_dir else None
    if path and os.path.exists(path):
        return pd.read_csv(path, low_memory=False)
    with urllib.request.urlopen(FILES[name], timeout=120) as r:
        return pd.read_csv(io.BytesIO(r.read()), low_memory=False)


def r1(x): return None if x is None or pd.isna(x) else round(float(x), 1)
def r3(x): return None if x is None or pd.isna(x) else round(float(x), 3)
def key(name): return "".join(ch for ch in str(name).lower() if ch.isalpha())


def build(week=None, local_dir=None):
    all_games = load("games", local_dir); games = all_games[all_games.season == SEASON]
    stats = load("stats", local_dir); stats = stats[stats.season_type == "REG"].copy()
    inj = load("inj", local_dir)
    try: snaps = load("snaps", local_dir); snaps = snaps[snaps.game_type == "REG"]
    except Exception: snaps = pd.DataFrame(columns=["player", "team", "week", "offense_pct"])
    for c in ["passing_yards", "rushing_yards", "receiving_yards", "receptions", "attempts", "carries", "targets", "target_share"]:
        stats[c] = stats[c].fillna(0)
    stats["u"] = stats.apply(lambda r: r.attempts if r.position == "QB" else (r.carries + r.targets if r.position == "RB" else r.targets), axis=1)
    team_carries = stats.groupby(["team", "week"]).carries.sum()
    stats["carry_share"] = [c / team_carries.get((t, w), 0) if team_carries.get((t, w), 0) else 0 for c, t, w in zip(stats.carries, stats.team, stats.week)]
    snap_pct = {(key(r.player), r.team, int(r.week)): float(r.offense_pct) for r in snaps.itertuples()}

    if week is None:  # first week with unplayed games
        week = int(games[games.result.isna()].week.min())
    wk = games[games.week == week].sort_values(["gameday", "gametime"])
    played_through = int(stats.week.max())
    team_weeks = {}  # weeks each team actually played
    for g in games[games.result.notna() & (games.week < week)].itertuples():
        team_weeks.setdefault(g.home_team, []).append(int(g.week)); team_weeks.setdefault(g.away_team, []).append(int(g.week))

    # ---- defense vs position: what each defense allows per game to each position ----
    defense, league = {}, {}
    for pos, mk in MARKETS.items():
        sub = stats[stats.position == pos]
        for label, col, _ in mk:
            per_game = sub.groupby(["opponent_team", "week"])[col].sum().reset_index()
            avg = per_game.groupby("opponent_team")[col].mean()
            rank = avg.rank(method="min").astype(int)  # 1 = allows least (toughest), 32 = allows most (softest)
            league[f"{pos}|{label}"] = round(float(avg.mean()), 1)
            for team in avg.index:
                last = per_game[per_game.opponent_team == team].sort_values("week").tail(5)
                defense.setdefault(team, {})[f"{pos}|{label}"] = {
                    "avg": round(float(avg[team]), 1), "rank": int(rank[team]), "n": int(len(avg)),
                    "last": [{"wk": int(w), "v": round(float(v), 1)} for w, v in zip(last.week, last[col])],
                }

    # ---- latest injury report per team ----
    injuries = {}
    latest = inj[inj.season_type == "REG"]
    latest = latest[latest.week == latest.groupby("team").week.transform("max")]
    for r in latest[latest.report_status.isin(["Out", "Doubtful", "Questionable"])].itertuples():
        injuries.setdefault(r.team, []).append({
            "name": r.full_name, "pos": r.position, "status": r.report_status,
            "injury": r.report_primary_injury if isinstance(r.report_primary_injury, str) else "",
            "practice": r.practice_status if isinstance(r.practice_status, str) else "", "week": int(r.week)})
    status_of = {(key(i["name"]), t): i["status"] for t, lst in injuries.items() for i in lst}

    current_team = stats.sort_values("week").groupby("player_id").team.last()
    stats["cur"] = stats.player_id.map(current_team)

    # ---- teammates ruled out (Out/Doubtful) who had a real role: what they leave behind ----
    absences = {}  # team -> list of {pid, name, pos, tgt, car, without_weeks}
    for team, lst in injuries.items():
        for i in lst:
            if i["status"] not in ("Out", "Doubtful") or i["pos"] not in SKILL: continue
            rows = stats[(stats.cur == team) & (stats.player_display_name.map(key) == key(i["name"]))]
            if rows.empty or rows.u.mean() < 3: continue
            played = set(rows.week.astype(int))
            absences.setdefault(team, []).append({
                "pid": rows.player_id.iloc[0], "name": i["name"], "pos": i["pos"], "status": i["status"],
                "tgt": r1(rows.targets.mean()), "car": r1(rows.carries.mean()), "games": len(played),
                "without": [w for w in team_weeks.get(team, []) if w not in played]})

    # ---- players to watch in each game ----
    players = []
    for g in wk.itertuples():
        for team, opp, qb_name in ((g.away_team, g.home_team, g.away_qb_name), (g.home_team, g.away_team, g.home_qb_name)):
            for pos, n in TAKE.items():
                pool = stats[(stats.position == pos) & (stats.cur == team)]
                if pool.empty: continue
                recent = pool[pool.week >= played_through - 2].groupby("player_id").u.sum().sort_values(ascending=False)
                pids = list(recent.head(n).index)
                if pos == "QB" and isinstance(qb_name, str):  # use nflverse's expected starter when we have his stats
                    match = pool[pool.player_display_name.map(key) == key(qb_name)]
                    if not match.empty: pids = [match.player_id.iloc[0]]
                for pid in pids:
                    logs = pool[pool.player_id == pid].sort_values("week")
                    name = logs.player_display_name.iloc[-1]
                    props = []
                    for label, col, mkey in MARKETS[pos]:
                        d = defense.get(opp, {}).get(f"{pos}|{label}")
                        lg = league[f"{pos}|{label}"]
                        vals = logs[col].astype(float)
                        avg = float(vals.mean()); sd = float(vals.std()) if len(vals) > 1 else avg * 0.45
                        proj = avg * (d["avg"] / lg) ** 0.5 if d and lg else avg  # soft adjustment
                        props.append({"stat": label, "col": col, "market": mkey, "avg": round(avg, 1),
                                      "proj": round(proj, 1), "sd": round(sd, 1),
                                      "log": [{"wk": int(w), "v": round(float(v), 1)} for w, v in zip(logs.week, vals)]})
                    usage = [{"wk": int(r.week), "snap": r3(snap_pct.get((key(name), team, int(r.week)))),
                              "tgt": int(r.targets), "tshare": r3(r.target_share), "car": int(r.carries),
                              "cshare": r3(r.carry_share), "att": int(r.attempts)} for r in logs.itertuples()]
                    # with/without splits for each teammate who is out
                    splits = []
                    for a in absences.get(team, []):
                        if a["pid"] == pid: continue
                        played = set(logs.week.astype(int))
                        wo = [w for w in a["without"] if w in played]
                        w_ = [w for w in played if w not in a["without"]]
                        def avg_of(ws, col):
                            x = logs[logs.week.isin(ws)][col]; return r1(x.mean()) if len(x) else None
                        main = MARKETS[pos][0][1]
                        splits.append({"name": a["name"], "pos": a["pos"], "status": a["status"], "tgt": a["tgt"], "car": a["car"],
                                       "nWith": len(w_), "nWithout": len(wo),
                                       "useWith": avg_of(w_, "u"), "useWithout": avg_of(wo, "u"),
                                       "statWith": avg_of(w_, main), "statWithout": avg_of(wo, main), "stat": MARKETS[pos][0][0]})
                    players.append({"id": pid, "name": name, "pos": pos, "team": team, "opp": opp,
                                    "game": g.game_id, "status": status_of.get((key(name), team), ""),
                                    "props": props, "usage": usage, "splits": splits})

    # ---- game environment ----
    def last_qb(team):
        q = stats[(stats.team == team) & (stats.position == "QB")].sort_values(["week", "attempts"]).groupby("week").tail(1)
        return q.player_display_name.iloc[-1] if len(q) else None
    games_out = []
    for g in wk.itertuples():
        e = {"id": g.game_id, "away": g.away_team, "home": g.home_team, "day": g.gameday, "time": g.gametime,
             "spread": r1(g.spread_line), "total": r1(g.total_line),
             "roof": g.roof if isinstance(g.roof, str) else None, "temp": r1(g.temp), "wind": r1(g.wind),
             "div": bool(g.div_game), "restAway": int(g.away_rest), "restHome": int(g.home_rest),
             "stadium": g.stadium if isinstance(g.stadium, str) else ""}
        if e["spread"] is not None and e["total"] is not None:  # implied team totals
            e["impHome"] = round(e["total"] / 2 + e["spread"] / 2, 1); e["impAway"] = round(e["total"] / 2 - e["spread"] / 2, 1)
        e["qbAway"], e["qbHome"] = (g.away_qb_name if isinstance(g.away_qb_name, str) else None), (g.home_qb_name if isinstance(g.home_qb_name, str) else None)
        e["qbAwayLast"], e["qbHomeLast"] = last_qb(g.away_team), last_qb(g.home_team)
        games_out.append(e)

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "season": SEASON, "week": week, "statsThrough": played_through,
        "games": games_out, "league": league, "defense": defense, "injuries": injuries,
        "absences": absences, "players": players,
        "lines": fetch_lines(wk, players) if os.environ.get("ODDS_API_KEY") else [],
    }


# ---------------- The Odds API (optional, needs free key) ----------------
TEAM_FULL = {"ARI":"Arizona Cardinals","ATL":"Atlanta Falcons","BAL":"Baltimore Ravens","BUF":"Buffalo Bills","CAR":"Carolina Panthers","CHI":"Chicago Bears","CIN":"Cincinnati Bengals","CLE":"Cleveland Browns","DAL":"Dallas Cowboys","DEN":"Denver Broncos","DET":"Detroit Lions","GB":"Green Bay Packers","HOU":"Houston Texans","IND":"Indianapolis Colts","JAX":"Jacksonville Jaguars","KC":"Kansas City Chiefs","LA":"Los Angeles Rams","LAC":"Los Angeles Chargers","LV":"Las Vegas Raiders","MIA":"Miami Dolphins","MIN":"Minnesota Vikings","NE":"New England Patriots","NO":"New Orleans Saints","NYG":"New York Giants","NYJ":"New York Jets","PHI":"Philadelphia Eagles","PIT":"Pittsburgh Steelers","SEA":"Seattle Seahawks","SF":"San Francisco 49ers","TB":"Tampa Bay Buccaneers","TEN":"Tennessee Titans","WAS":"Washington Commanders"}


def _get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        print("odds credits remaining:", r.headers.get("x-requests-remaining"), file=sys.stderr)
        return json.loads(r.read())


def fetch_lines(wk, players):
    """One request per game. Passing up to 10 bookmakers counts as a single region,
    so each game costs (number of markets) credits: 4 markets x ~14 games = ~56 credits."""
    key = os.environ["ODDS_API_KEY"]; base = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
    events = _get(f"{base}/events?apiKey={key}")
    want = {(TEAM_FULL[g.away_team], TEAM_FULL[g.home_team]): g.game_id for g in wk.itertuples()}
    markets = sorted({m for p in players for pr in p["props"] for m in [pr["market"]]})
    books = ",".join(SHARP_BOOKS + DFS_BOOKS)
    lines = []
    for ev in events:
        gid = want.get((ev["away_team"], ev["home_team"]))
        if not gid: continue
        data = _get(f"{base}/events/{ev['id']}/odds?apiKey={key}&bookmakers={books}&markets={','.join(markets)}&oddsFormat=american")
        for bk in data.get("bookmakers", []):
            for m in bk["markets"]:
                for o in m["outcomes"]:
                    lines.append({"game": gid, "book": bk["key"], "market": m["key"], "player": o.get("description"),
                                  "side": o["name"], "line": o.get("point"), "price": o.get("price")})
    return lines


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int); ap.add_argument("--out", default="data.json"); ap.add_argument("--local")
    a = ap.parse_args()
    d = build(a.week, a.local)
    with open(a.out, "w") as f: json.dump(d, f, separators=(",", ":"))
    print(f"week {d['week']}: {len(d['games'])} games, {len(d['players'])} players, {len(d['lines'])} lines -> {a.out}")
