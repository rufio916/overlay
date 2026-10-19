"""Overlay data builder.

Pulls free NFL data from nflverse (schedule, weekly player stats, snap counts, injury
reports), computes defense-vs-position numbers, usage trends, teammate-injury splits and
game environment for the upcoming week, and (optionally) pulls PrizePicks + sportsbook
prop lines from The Odds API when ODDS_API_KEY is set. Writes data.json for the page.

Usage:  python build_data.py [--week N] [--out data.json] [--local DIR]
"""
import warnings; warnings.filterwarnings("ignore")
import argparse, io, json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone
import pandas as pd

SEASON = 2026
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
FILES = {
    "games": f"{NFLVERSE}/schedules/games.csv",
    "stats": f"{NFLVERSE}/stats_player/stats_player_week_{SEASON}.csv",
    "inj": f"{NFLVERSE}/injuries/injuries_{SEASON}.csv",
    "snaps": f"{NFLVERSE}/snap_counts/snap_counts_{SEASON}.csv",
    "team": f"{NFLVERSE}/stats_team/stats_team_week_{SEASON}.csv",
}
# Which props we track for each position: (label, stat column, Odds API market key)
MARKETS = {
    "QB": [("Pass Yds", "passing_yards", "player_pass_yds"), ("Rush Yds", "rushing_yards", "player_rush_yds")],
    "RB": [("Rush Yds", "rushing_yards", "player_rush_yds"), ("Rec Yds", "receiving_yards", "player_reception_yds"), ("Receptions", "receptions", "player_receptions")],
    "WR": [("Rec Yds", "receiving_yards", "player_reception_yds"), ("Receptions", "receptions", "player_receptions")],
    "TE": [("Rec Yds", "receiving_yards", "player_reception_yds"), ("Receptions", "receptions", "player_receptions")],
}
TAKE = {"QB": 1, "RB": 2, "WR": 3, "TE": 1}  # top players per team by usage
# Up to 10 bookmakers in one request cost the same as 1, so we use exactly 10.
SHARP_BOOKS = ["pinnacle", "fanduel", "draftkings", "betmgm", "espnbet", "betrivers", "bovada"]  # sportsbooks for the consensus
DFS_BOOKS = ["prizepicks", "underdog", "pick6"]  # pick'em apps you'd actually play
SKILL = {"QB", "RB", "WR", "TE"}


def load(name, local_dir):
    path = os.path.join(local_dir, os.path.basename(FILES[name])) if local_dir else None
    if path and os.path.exists(path):
        return pd.read_csv(path, low_memory=False)
    with urllib.request.urlopen(FILES[name], timeout=120) as r:
        return pd.read_csv(io.BytesIO(r.read()), low_memory=False)


VS = {}  # (team, week) -> "@ DEN" / "vs SF"


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
    stats["half"] = (stats.fantasy_points.fillna(0) + stats.fantasy_points_ppr.fillna(0)) / 2
    stats["u"] = stats.apply(lambda r: r.attempts if r.position == "QB" else (r.carries + r.targets if r.position == "RB" else r.targets), axis=1)
    team_carries = stats.groupby(["team", "week"]).carries.sum()
    stats["carry_share"] = [c / team_carries.get((t, w), 0) if team_carries.get((t, w), 0) else 0 for c, t, w in zip(stats.carries, stats.team, stats.week)]
    snap_pct = {(key(r.player), r.team, int(r.week)): float(r.offense_pct) for r in snaps.itertuples()}

    if week is None:  # first week with unplayed games
        week = int(games[games.result.isna()].week.min())
    wk = games[games.week == week].sort_values(["gameday", "gametime"])
    played_through = int(stats.week.max())
    VS.clear()
    for g in games[games.game_type == "REG"].itertuples():
        VS[(g.home_team, int(g.week))] = f"vs {g.away_team}"; VS[(g.away_team, int(g.week))] = f"@ {g.home_team}"
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
                    "last": [{"wk": int(w), "v": round(float(v), 1), "o": VS.get((team, int(w)), "")} for w, v in zip(last.week, last[col])],
                }

    # ---- half-PPR fantasy points each defense allows to each position ----
    defense_fp, league_fp = {}, {}
    for pos in ["QB", "RB", "WR", "TE"]:
        per_game = stats[stats.position == pos].groupby(["opponent_team", "week"]).half.sum().reset_index()
        avg = per_game.groupby("opponent_team").half.mean(); rank = avg.rank(method="min").astype(int)
        league_fp[pos] = round(float(avg.mean()), 1)
        for t in avg.index: defense_fp.setdefault(t, {})[pos] = {"avg": round(float(avg[t]), 1), "rank": int(rank[t])}

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

    def make_player(pid, pos, team, opp, game_id):
        pool = stats[(stats.position == pos) & (stats.cur == team)]
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
                          "log": [{"wk": int(w), "v": round(float(v), 1), "o": VS.get((t, int(w)), "")} for w, v, t in zip(logs.week, vals, logs.team)]})
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
        return ({"id": pid, "name": name, "pos": pos, "team": team, "opp": opp,
                        "game": game_id, "status": status_of.get((key(name), team), ""),
                        "props": props, "usage": usage, "splits": splits})


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
                    players.append(make_player(pid, pos, team, opp, g.game_id))

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

    odds = odds_block(wk, players)
    tracked = {key(p["name"]) for p in players}
    games_by_team = {}
    for g in wk.itertuples():
        games_by_team[g.away_team] = (g.home_team, g.game_id); games_by_team[g.home_team] = (g.away_team, g.game_id)
    for nm in sorted({key(l["player"]) for l in odds["lines"] if l["book"] == "prizepicks"} - tracked):
        cand = stats[(stats.player_display_name.map(key) == nm) & stats.position.isin(list(MARKETS)) & stats.cur.isin(list(games_by_team))]
        if cand.empty: continue
        r = cand.sort_values("week").iloc[-1]
        opp, gid = games_by_team[r.cur]
        players.append(make_player(r.player_id, r.position, r.cur, opp, gid)); tracked.add(nm)

    fantasy = build_fantasy(stats, games, wk, week, defense_fp, league_fp, status_of, absences, snap_pct)

    # ---- pick'em: this week and next, with moneylines (free, from the nflverse schedule) ----
    pickem = []
    for g in games[games.week.isin([week, week + 1]) & (games.game_type == "REG")].sort_values(["gameday", "gametime"]).itertuples():
        pickem.append({"id": g.game_id, "week": int(g.week), "day": g.gameday, "time": g.gametime, "away": g.away_team, "home": g.home_team,
                       "mlA": None if pd.isna(g.away_moneyline) else int(g.away_moneyline), "mlH": None if pd.isna(g.home_moneyline) else int(g.home_moneyline),
                       "spread": r1(g.spread_line), "total": r1(g.total_line),
                       "qbA": g.away_qb_name if isinstance(g.away_qb_name, str) else None, "qbH": g.home_qb_name if isinstance(g.home_qb_name, str) else None,
                       "qbALast": last_qb(g.away_team), "qbHLast": last_qb(g.home_team),
                       "scoreA": None if pd.isna(g.away_score) else int(g.away_score), "scoreH": None if pd.isna(g.home_score) else int(g.home_score)})

    return {
        "pickem": pickem,
        "fantasy": fantasy,
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "season": SEASON, "week": week, "statsThrough": played_through,
        "games": games_out, "league": league, "defense": defense, "injuries": injuries,
        "absences": absences, "players": players,
        **odds,
    }


# ---------------- fantasy (half-PPR) ----------------
def sleeper_trending():
    """Sleeper's free trending adds/drops (last 48h), keyed by name+team. Works on GitHub; skipped where Sleeper is unreachable."""
    try:
        def get(u):
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "overlay"}), timeout=90) as r:
                return json.loads(r.read())
        players = get("https://api.sleeper.app/v1/players/nfl")
        adds = get("https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=48&limit=200")
        drops = get("https://api.sleeper.app/v1/players/nfl/trending/drop?lookback_hours=48&limit=200")
        out = {}
        for kind, rows in (("adds", adds), ("drops", drops)):
            for r in rows:
                pl = players.get(str(r["player_id"]))
                if not pl or not pl.get("full_name"): continue
                out.setdefault(key(pl["full_name"]), {})[kind] = int(r["count"])
        return {"ok": True, "players": out}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200], "players": {}}


def build_fantasy(stats, games, wk, week, defense_fp, league_fp, status_of, absences, snap_pct):
    team_pts = {}
    for g in games[games.result.notna() & (games.week < week)].itertuples():
        team_pts.setdefault(g.home_team, []).append(g.home_score); team_pts.setdefault(g.away_team, []).append(g.away_score)
    team_avg = {t: sum(v) / len(v) for t, v in team_pts.items()}
    opp_of, imp_of, game_of = {}, {}, {}
    for g in wk.itertuples():
        opp_of[g.away_team], opp_of[g.home_team] = g.home_team, g.away_team
        game_of[g.away_team] = game_of[g.home_team] = g.game_id
        if not pd.isna(g.spread_line) and not pd.isna(g.total_line):
            imp_of[g.home_team] = g.total_line / 2 + g.spread_line / 2; imp_of[g.away_team] = g.total_line / 2 - g.spread_line / 2
    trending = sleeper_trending()
    out = []
    pool = stats[stats.position.isin(["QB", "RB", "WR", "TE"])]
    for pid, logs in pool.groupby("player_id"):
        logs = logs.sort_values("week"); team = logs.cur.iloc[-1]; pos = logs.position.iloc[-1]; name = logs.player_display_name.iloc[-1]
        fp = logs.half.astype(float)
        if fp.mean() < 3 and logs.u.iloc[-1] < 4: continue
        base = 0.5 * fp.mean() + 0.5 * fp.tail(3).mean()
        opp = opp_of.get(team); status = status_of.get((key(name), team), "")
        if opp:
            d = defense_fp.get(opp, {}).get(pos)
            mf = min(1.2, max(0.8, (d["avg"] / league_fp[pos]) ** 0.5)) if d else 1.0
            ef = min(1.15, max(0.85, (imp_of[team] / team_avg[team]) ** 0.4)) if team in imp_of and team_avg.get(team) else 1.0
            proj = base * mf * ef
            if status in ("Out",): proj = 0.0
            elif status == "Doubtful": proj *= 0.25
        else:
            d, proj = None, 0.0
        sd = max(float(fp.std()) if len(fp) > 1 else 0, 0.35 * base)
        u = logs.u.astype(float).tolist()
        rise = (sum(u[-2:]) / len(u[-2:]) - sum(u[:-2]) / len(u[:-2])) if len(u) >= 3 else 0.0
        snaps = [snap_pct.get((key(name), team, int(w))) for w in logs.week]
        tr = trending["players"].get(key(name), {})
        out.append({
            "id": pid, "name": name, "pos": pos, "team": team, "opp": opp, "game": game_of.get(team), "status": status,
            "proj": round(proj, 1), "floor": round(max(0, proj - 0.8 * sd), 1) if opp else 0, "ceil": round(proj + 0.8 * sd, 1) if opp else 0,
            "avg": round(float(fp.mean()), 1), "games": int(len(fp)),
            "matchRank": d["rank"] if d else None, "matchAllowed": d["avg"] if d else None,
            "implied": r1(imp_of.get(team)), "log": [{"wk": int(w), "v": round(float(v), 1), "o": VS.get((t, int(w)), "")} for w, v, t in zip(logs.week, fp, logs.team)],
            "opp_log": [int(x) for x in u], "snaps": [r3(x) for x in snaps], "rise": round(rise, 1),
            "adds": tr.get("adds", 0), "drops": tr.get("drops", 0),
            "teammateOut": [a["name"] for a in absences.get(team, []) if a["pid"] != pid and ((a["tgt"] or 0) >= 4 or (a["car"] or 0) >= 8 or a["pos"] == "QB")],
        })
    try:
        team_stats = load("team", LOCAL_DIR)
        out += build_k_dst(stats, team_stats, games, week, {"opp": opp_of, "imp": imp_of, "game": game_of, "team_avg": team_avg, "status": status_of})
    except Exception as e:
        print("kickers/defenses skipped:", e, file=sys.stderr)
    out.sort(key=lambda x: -x["proj"])
    return {"players": out, "leagueAllowed": league_fp, "defense": defense_fp,
            "sleeper": {"ok": trending["ok"], "error": trending.get("error")}}


# ---------------- kickers and team defenses (Yahoo default scoring) ----------------
TEAM_NAME = {"ARI":"Arizona","ATL":"Atlanta","BAL":"Baltimore","BUF":"Buffalo","CAR":"Carolina","CHI":"Chicago","CIN":"Cincinnati","CLE":"Cleveland","DAL":"Dallas","DEN":"Denver","DET":"Detroit","GB":"Green Bay","HOU":"Houston","IND":"Indianapolis","JAX":"Jacksonville","KC":"Kansas City","LA":"LA Rams","LAC":"LA Chargers","LV":"Las Vegas","MIA":"Miami","MIN":"Minnesota","NE":"New England","NO":"New Orleans","NYG":"NY Giants","NYJ":"NY Jets","PHI":"Philadelphia","PIT":"Pittsburgh","SEA":"Seattle","SF":"San Francisco","TB":"Tampa Bay","TEN":"Tennessee","WAS":"Washington"}


def pa_points(pa):
    return 10 if pa <= 0 else 7 if pa <= 6 else 4 if pa <= 13 else 1 if pa <= 20 else 0 if pa <= 27 else -1 if pa <= 34 else -4


def expected_pa_points(implied, sd=9.5):
    import math
    pts, tot = 0.0, 0.0
    for x in range(0, 61):  # discrete approximation of a normal around the implied score
        w = math.exp(-0.5 * ((x - implied) / sd) ** 2); pts += w * pa_points(x); tot += w
    return pts / tot


def build_k_dst(stats, team_stats, games, week, ctx):
    out = []
    played = games[games.result.notna() & (games.week < week)]
    pa = {}
    for g in played.itertuples():
        pa[(g.home_team, int(g.week))] = g.away_score; pa[(g.away_team, int(g.week))] = g.home_score

    def spread(vals, proj):
        import statistics
        sd = max(statistics.pstdev(vals) if len(vals) > 1 else 0, 0.35 * max(proj, 1))
        return round(max(0, proj - 0.8 * sd), 1), round(proj + 0.8 * sd, 1)

    # --- kickers ---
    k = stats[(stats.position == "K") & (stats.season_type == "REG")].copy()
    for c in ["fg_made_0_19", "fg_made_20_29", "fg_made_30_39", "fg_made_40_49", "fg_made_50_59", "fg_made_60_", "pat_made", "fg_made", "fg_att", "pat_att"]:
        k[c] = k[c].fillna(0)
    k["kp"] = 3 * (k.fg_made_0_19 + k.fg_made_20_29 + k.fg_made_30_39) + 4 * k.fg_made_40_49 + 5 * (k.fg_made_50_59 + k.fg_made_60_) + k.pat_made
    allowed_k = k.groupby(["opponent_team", "week"]).kp.sum().groupby("opponent_team").mean()
    rank_k = allowed_k.rank(method="min").astype(int)
    for pid, logs in k.groupby("player_id"):
        logs = logs.sort_values("week"); team = logs.cur.iloc[-1] if "cur" in logs else logs.team.iloc[-1]
        if logs.week.iloc[-1] < week - 3: continue  # no longer kicking
        base = 0.5 * logs.kp.mean() + 0.5 * logs.kp.tail(3).mean(); opp = ctx["opp"].get(team)
        if opp:
            ef = min(1.2, max(0.8, (ctx["imp"][team] / ctx["team_avg"][team]) ** 0.6)) if team in ctx["imp"] and ctx["team_avg"].get(team) else 1.0
            mf = min(1.1, max(0.9, (allowed_k.get(opp, allowed_k.mean()) / allowed_k.mean()) ** 0.3))
            proj = base * ef * mf
        else:
            proj = 0.0
        fl, ce = spread(list(logs.kp), proj) if opp else (0, 0)
        name = logs.player_display_name.iloc[-1]
        out.append({"id": pid, "name": name, "pos": "K", "team": team, "opp": opp, "game": ctx["game"].get(team),
                    "status": ctx["status"].get((key(name), team), ""), "proj": round(proj, 1), "floor": fl, "ceil": ce,
                    "avg": round(float(logs.kp.mean()), 1), "games": int(len(logs)),
                    "matchRank": int(rank_k[opp]) if opp in rank_k else None, "matchAllowed": round(float(allowed_k[opp]), 1) if opp in allowed_k else None,
                    "implied": r1(ctx["imp"].get(team)), "log": [{"wk": int(w), "v": float(v), "o": VS.get((t, int(w)), "")} for w, v, t in zip(logs.week, logs.kp, logs.team)],
                    "breakdown": [{"wk": int(r.week), "txt": f"{int(r.fg_made)}/{int(r.fg_att)} FG, {int(r.pat_made)}/{int(r.pat_att)} XP" + (f", long {int(r.fg_long)}" if r.fg_made and not pd.isna(r.fg_long) else "")} for r in logs.itertuples()],
                    "opp_log": [], "snaps": [], "rise": 0, "adds": 0, "drops": 0, "teammateOut": []})

    # --- team defenses ---
    t = team_stats[team_stats.season_type == "REG"].copy()
    for c in ["def_sacks", "def_interceptions", "fumble_recovery_opp", "def_tds", "special_teams_tds", "def_safeties", "def_punt_blocks", "def_pat_blocks", "def_fg_blocks"]:
        t[c] = t[c].fillna(0)
    t["ev"] = t.def_sacks + 2 * t.def_interceptions + 2 * t.fumble_recovery_opp + 6 * (t.def_tds + t.special_teams_tds) + 2 * t.def_safeties + 2 * (t.def_punt_blocks + t.def_pat_blocks + t.def_fg_blocks)
    t["pa"] = [pa.get((tm, int(w))) for tm, w in zip(t.team, t.week)]
    t = t[t.pa.notna()]
    t["dp"] = t.ev + t.pa.map(pa_points)
    # what each offense gives up to opposing defenses (sacks, turnovers, scores against)
    given = t.groupby(["opponent_team"]).dp.mean(); given_ev = t.groupby("opponent_team").ev.mean()
    rank_off = given.rank(method="min").astype(int)  # 32 = offense that feeds defenses the most
    for team, logs in t.groupby("team"):
        logs = logs.sort_values("week"); opp = ctx["opp"].get(team)
        if opp:
            ev = 0.5 * logs.ev.mean() + 0.5 * given_ev.get(opp, given_ev.mean())
            opp_imp = ctx["imp"].get(opp)
            pa_pts = expected_pa_points(opp_imp) if opp_imp is not None else float(logs.pa.map(pa_points).mean())
            proj = ev + pa_pts
        else:
            proj = 0.0
        fl, ce = spread(list(logs.dp), proj) if opp else (0, 0)
        out.append({"id": "DEF-" + team, "name": f"{TEAM_NAME.get(team, team)} D/ST", "pos": "DEF", "team": team, "opp": opp, "game": ctx["game"].get(team),
                    "status": "", "proj": round(proj, 1), "floor": fl, "ceil": ce, "avg": round(float(logs.dp.mean()), 1), "games": int(len(logs)),
                    "matchRank": int(rank_off[opp]) if opp in rank_off else None, "matchAllowed": round(float(given[opp]), 1) if opp in given else None,
                    "implied": r1(ctx["imp"].get(opp)), "log": [{"wk": int(w), "v": float(v), "o": VS.get((team, int(w)), "")} for w, v in zip(logs.week, logs.dp)],
                    "breakdown": [{"wk": int(r.week), "txt": f"{int(r.pa)} pts allowed, {r.def_sacks:g} sk, {int(r.def_interceptions)} INT, {int(r.fumble_recovery_opp)} FR" + (f", {int(r.def_tds + r.special_teams_tds)} TD" if r.def_tds + r.special_teams_tds else "")} for r in logs.itertuples()],
                    "opp_log": [], "snaps": [], "rise": 0, "adds": 0, "drops": 0, "teammateOut": []})
    return out


# ---------------- The Odds API (optional, needs free key) ----------------
TEAM_FULL = {"ARI":"Arizona Cardinals","ATL":"Atlanta Falcons","BAL":"Baltimore Ravens","BUF":"Buffalo Bills","CAR":"Carolina Panthers","CHI":"Chicago Bears","CIN":"Cincinnati Bengals","CLE":"Cleveland Browns","DAL":"Dallas Cowboys","DEN":"Denver Broncos","DET":"Detroit Lions","GB":"Green Bay Packers","HOU":"Houston Texans","IND":"Indianapolis Colts","JAX":"Jacksonville Jaguars","KC":"Kansas City Chiefs","LA":"Los Angeles Rams","LAC":"Los Angeles Chargers","LV":"Las Vegas Raiders","MIA":"Miami Dolphins","MIN":"Minnesota Vikings","NE":"New England Patriots","NO":"New Orleans Saints","NYG":"New York Giants","NYJ":"New York Jets","PHI":"Philadelphia Eagles","PIT":"Pittsburgh Steelers","SEA":"Seattle Seahawks","SF":"San Francisco 49ers","TB":"Tampa Bay Buccaneers","TEN":"Tennessee Titans","WAS":"Washington Commanders"}


ODDS_STATUS = {"errors": [], "remaining": None, "used": None, "calls": 0}


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            ODDS_STATUS["calls"] += 1
            ODDS_STATUS["remaining"] = r.headers.get("x-requests-remaining"); ODDS_STATUS["used"] = r.headers.get("x-requests-used")
            print("odds credits remaining:", ODDS_STATUS["remaining"], file=sys.stderr)
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        safe = url.split("?")[0]  # never record the key
        ODDS_STATUS["errors"].append(f"{e.code} {safe}: {body}")
        print("odds error:", ODDS_STATUS["errors"][-1], file=sys.stderr)
        return None


LOCAL_DIR = None
PREV_PATH = None  # previous data.json, so runs without odds keep the last lines for the same week


CREDITS_PER_GAME = 4  # one credit per stat type per game; passing up to 10 books counts once


def _carry_over(wk, status):
    try:
        prev = json.load(open(PREV_PATH)) if PREV_PATH and os.path.exists(PREV_PATH) else {}
        if prev.get("lines") and prev.get("week") == int(wk.week.iloc[0]):
            o = dict(prev.get("odds") or {}); o["status"] = status
            o["fetched"] = o.get("fetched") or prev.get("generated")
            out = {"lines": prev["lines"], "odds": o}
            if prev.get("linesPrev"): out.update({"linesPrev": prev["linesPrev"], "oddsPrev": prev.get("oddsPrev")})
            return out
    except Exception as e:
        print("could not reuse previous lines:", e, file=sys.stderr)
    return None


def _credits_left_guess():
    """Credits left after the last pull, or None if unknown or a new month has started (credits reset on the 1st)."""
    try:
        prev = json.load(open(PREV_PATH))
        o = prev.get("odds") or {}
        last = o.get("fetched") or ""
        if o.get("remaining") is None or last[:7] != datetime.now(timezone.utc).strftime("%Y-%m"):
            return None
        return int(o["remaining"])
    except Exception:
        return None


def odds_block(wk, players):
    if os.environ.get("ODDS_API_KEY"):
        left = _credits_left_guess()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cost = CREDITS_PER_GAME * int((wk.gameday >= today).sum())  # games already played aren't pulled
        print(f"odds pull estimate: ~{cost} credits; credits left: {left if left is not None else 'unknown (new month)'}", file=sys.stderr)
        if left is not None and left < cost + 2:
            print(f"only {left} odds credits left; keeping last lines until the monthly reset", file=sys.stderr)
            return _carry_over(wk, "skipped: low credits") or {"lines": [], "odds": {"status": "skipped: low credits", "remaining": left}}
    if not os.environ.get("ODDS_API_KEY"):
        try:
            prev = json.load(open(PREV_PATH)) if PREV_PATH and os.path.exists(PREV_PATH) else {}
            if prev.get("lines") and prev.get("week") == int(wk.week.iloc[0]):
                o = dict(prev.get("odds") or {}); o["status"] = "carried over"
                o["fetched"] = o.get("fetched") or prev.get("generated")
                return {"lines": prev["lines"], "odds": o}
        except Exception as e:
            print("could not reuse previous lines:", e, file=sys.stderr)
        return {"lines": [], "odds": {"status": "no key"}}
    try:
        lines = fetch_lines(wk, players)
        status = "ok" if lines else ("error" if ODDS_STATUS["errors"] else "no lines yet")
    except Exception as e:
        lines, status = [], "error"; ODDS_STATUS["errors"].append(f"{type(e).__name__}: {e}"[:300])
    if not lines:  # a failed or empty pull keeps the last good lines instead of blanking the page
        kept = _carry_over(wk, f"{status}, kept last lines")
        if kept:
            kept["odds"].update({"errors": ODDS_STATUS["errors"][:5], "remaining": ODDS_STATUS["remaining"] or kept["odds"].get("remaining")})
            return kept
    out = {"lines": lines, "odds": {"status": status, "fetched": datetime.now(timezone.utc).isoformat(timespec="minutes"), **ODDS_STATUS, "errors": ODDS_STATUS["errors"][:5]}}
    try:  # remember the previous pull from the same week so the page can show line movement
        prev = json.load(open(PREV_PATH)) if PREV_PATH and os.path.exists(PREV_PATH) else {}
        if prev.get("lines") and prev.get("week") == int(wk.week.iloc[0]):
            keep = set(SHARP_BOOKS + DFS_BOOKS)
            out["linesPrev"] = [l for l in prev["lines"] if l["book"] in keep]
            out["oddsPrev"] = {"fetched": (prev.get("odds") or {}).get("fetched") or prev.get("generated")}
    except Exception as e:
        print("could not keep previous lines:", e, file=sys.stderr)
    return out


def fetch_lines(wk, players):
    """One request per game. Passing up to 10 bookmakers counts as a single region,
    so each game costs (number of markets) credits: 4 markets x ~14 games = ~56 credits."""
    key = os.environ["ODDS_API_KEY"]; base = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
    events = _get(f"{base}/events?apiKey={key}") or []
    want = {(TEAM_FULL[g.away_team], TEAM_FULL[g.home_team]): g.game_id for g in wk.itertuples()}
    markets = sorted({m for p in players for pr in p["props"] for m in [pr["market"]]})
    books = ",".join(SHARP_BOOKS + DFS_BOOKS)
    lines = []
    for ev in events:
        gid = want.get((ev["away_team"], ev["home_team"]))
        if not gid: continue
        data = _get(f"{base}/events/{ev['id']}/odds?apiKey={key}&bookmakers={books}&markets={','.join(markets)}&oddsFormat=american") or {}
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
    PREV_PATH = a.out; LOCAL_DIR = a.local
    d = build(a.week, a.local)
    with open(a.out, "w") as f: json.dump(d, f, separators=(",", ":"))
    print(f"week {d['week']}: {len(d['games'])} games, {len(d['players'])} players, {len(d['lines'])} lines -> {a.out}")
