"""Overlay NBA data builder (free data only, no odds credits).

- Box scores and schedule: sportsdataverse (ESPN data, updated daily on GitHub)
- Today's lines (DraftKings via ESPN) and injury report: ESPN's public site API

Usage: python nba_build.py [--out nba.json] [--local DIR] [--espn DIR]
  --local DIR  read player_box_*.csv / team_box_*.csv / nba_schedule_*.csv from DIR instead of downloading
  --espn DIR   read nba_scoreboard_today.json / nba_scoreboard_tomorrow.json / nba_injuries.json from DIR
"""
import warnings; warnings.filterwarnings("ignore")
import argparse, io, json, math, os, sys, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd

SEASON, PRIOR = 2027, 2026  # ESPN names a season by the year it ends (2026-27 = 2027)
SDV = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
PT = ZoneInfo("America/Los_Angeles")
STATS = [("Points", "points"), ("Rebounds", "rebounds"), ("Assists", "assists"), ("3-Pointers", "three_point_field_goals_made"), ("Pts+Reb+Ast", "pra")]
PRIOR_GAMES = 10  # early in the season, blend in last season as if it were this many games


def r1(x): return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 1)
def key(s): return "".join(ch for ch in str(s).lower() if ch.isalpha())


def load_csv(name, local):
    if local and os.path.exists(os.path.join(local, name)):
        return pd.read_csv(os.path.join(local, name), low_memory=False)
    tag = {"player_box": "espn_nba_player_boxscores", "team_box": "espn_nba_team_boxscores", "nba_schedule": "espn_nba_schedules"}[name.rsplit("_", 1)[0]]
    with urllib.request.urlopen(f"{SDV}/{tag}/{name}", timeout=300) as r:
        return pd.read_csv(io.BytesIO(r.read()), low_memory=False)


def get_json(url, local_dir, fname):
    if local_dir:
        p = os.path.join(local_dir, fname)
        return json.load(open(p)) if os.path.exists(p) else {}
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 overlay"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def blend(cur_vals, prior_mean):
    """Mean of this season's values, shrunk toward last season early on."""
    n = len(cur_vals)
    if prior_mean is None or (isinstance(prior_mean, float) and math.isnan(prior_mean)):
        return float(sum(cur_vals) / n) if n else None
    w = PRIOR_GAMES * max(0, 1 - n / 25)  # prior fades out by ~25 games
    return (sum(cur_vals) + prior_mean * w) / (n + w) if (n + w) else None


def odds_num(x):
    try: return int(str(x).replace("+", "")) if x not in (None, "", "OFF", "EVEN") else (100 if x == "EVEN" else None)
    except ValueError: return None


def line_num(x):
    try: return float(str(x).replace("o", "").replace("u", "").replace("+", ""))
    except (TypeError, ValueError): return None


def parse_odds(comp):
    od = (comp.get("odds") or [None])[0]
    if not od: return None
    def side(block, k, part):
        b = (od.get(block) or {}).get(k) or {}
        return (b.get("close") or {}).get(part), (b.get("open") or {}).get(part)
    mlH, mlHo = side("moneyline", "home", "odds"); mlA, mlAo = side("moneyline", "away", "odds")
    spH, spHo = side("pointSpread", "home", "line"); spHodds, _ = side("pointSpread", "home", "odds"); spAodds, _ = side("pointSpread", "away", "odds")
    tot, toto = side("total", "over", "line"); ovOdds, _ = side("total", "over", "odds"); unOdds, _ = side("total", "under", "odds")
    return {"book": (od.get("provider") or {}).get("displayName", "ESPN"),
            "mlH": odds_num(mlH), "mlA": odds_num(mlA), "mlHOpen": odds_num(mlHo), "mlAOpen": odds_num(mlAo),
            "spreadH": line_num(spH) if spH else od.get("spread"), "spreadHOpen": line_num(spHo),
            "spreadHOdds": odds_num(spHodds) or -110, "spreadAOdds": odds_num(spAodds) or -110,
            "total": line_num(tot) if tot else od.get("overUnder"), "totalOpen": line_num(toto),
            "overOdds": odds_num(ovOdds) or -110, "underOdds": odds_num(unOdds) or -110}


def build(local=None, espn_dir=None):
    pb = load_csv(f"player_box_{SEASON}.csv", local); tb = load_csv(f"team_box_{SEASON}.csv", local)
    pb0 = load_csv(f"player_box_{PRIOR}.csv", local); tb0 = load_csv(f"team_box_{PRIOR}.csv", local)
    pb = pb[(pb.season_type == 2) & (pb.did_not_play != True) & pb.minutes.notna() & (pb.minutes > 0)].copy()
    pb0 = pb0[(pb0.season_type == 2) & (pb0.did_not_play != True) & pb0.minutes.notna() & (pb0.minutes > 0)].copy()
    tb = tb[tb.season_type == 2].copy(); tb0 = tb0[tb0.season_type == 2].copy()
    for df in (pb, pb0):
        df["pra"] = df.points.fillna(0) + df.rebounds.fillna(0) + df.assists.fillna(0)
        df["pos"] = df.athlete_position_abbreviation.fillna("F").str[0].map({"G": "G", "F": "F", "C": "C"}).fillna("F")
        df["game_date"] = df.game_date.astype(str)
    names = dict(zip(pb.team_abbreviation, pb.team_display_name)) | dict(zip(pb0.team_abbreviation, pb0.team_display_name))
    abbr_by_name = {v: k for k, v in names.items()}

    # ---- team ratings (per 100 possessions), blended with last season early on ----
    def team_frame(t):
        t = t.copy(); tov = t["total_turnovers"] if "total_turnovers" in t else t["turnovers"]
        t["poss"] = t.field_goals_attempted - t.offensive_rebounds + tov.fillna(0) + 0.44 * t.free_throws_attempted
        t = t.merge(t[["game_id", "team_abbreviation", "poss", "offensive_rebounds", "defensive_rebounds"]].rename(columns={"team_abbreviation": "opponent_team_abbreviation", "poss": "opp_poss", "offensive_rebounds": "opp_oreb", "defensive_rebounds": "opp_dreb"}), on=["game_id", "opponent_team_abbreviation"], how="left")
        t["g_poss"] = (t.poss + t.opp_poss) / 2
        t["ortg"] = t.team_score / t.g_poss * 100; t["drtg"] = t.opponent_team_score / t.g_poss * 100
        t["fg3_rate"] = t.three_point_field_goals_attempted / t.field_goals_attempted
        t["tov_pct"] = tov / t.poss * 100; t["oreb_pct"] = t.offensive_rebounds / (t.offensive_rebounds + t.opp_dreb) * 100
        return t
    T, T0 = team_frame(tb), team_frame(tb0)
    metrics = [("ppg", "team_score"), ("oppg", "opponent_team_score"), ("ortg", "ortg"), ("drtg", "drtg"), ("pace", "g_poss"),
               ("fg3_rate", "fg3_rate"), ("fg3_pct", "three_point_field_goal_pct"), ("tov_pct", "tov_pct"), ("oreb_pct", "oreb_pct")]
    teams = {}
    played = T.sort_values("game_date")
    for ab in sorted(set(T.team_abbreviation) | set(T0.team_abbreviation)):
        cur, pri = played[played.team_abbreviation == ab], T0[T0.team_abbreviation == ab]
        row = {"name": names.get(ab, ab), "games": int(len(cur))}
        for k, col in metrics:
            row[k] = r1(blend(list(cur[col].dropna()), pri[col].mean() if len(pri) else None))
        row["net"] = r1((row["ortg"] or 0) - (row["drtg"] or 0))
        w = int((cur.team_winner == True).sum()); row["record"] = f"{w}-{len(cur) - w}"
        row["last"] = [{"date": r.game_date, "opp": r.opponent_team_abbreviation, "home": r.team_home_away == "home",
                        "pts": int(r.team_score), "oppPts": int(r.opponent_team_score), "won": bool(r.team_winner)} for r in cur.tail(5).itertuples()]
        row["lastDate"] = cur.game_date.max() if len(cur) else None
        row["priorNet"] = r1(pri.ortg.mean() - pri.drtg.mean()) if len(pri) else None
        teams[ab] = row
    real = [t for t in teams if t in set(T.team_abbreviation)]
    for k, hi_good in [("ortg", True), ("drtg", False), ("net", True), ("pace", True), ("ppg", True), ("oppg", False)]:
        order = sorted(real, key=lambda t: (teams[t][k] or 0), reverse=hi_good)
        for i, t in enumerate(order): teams[t][k + "Rank"] = i + 1  # 1 = best (or fastest, for pace)
    league = {k: r1(sum(teams[t][k] for t in real) / len(real)) for k in ("pace", "ortg", "ppg")}

    # ---- defense vs position (what each defense allows per game to G / F / C) ----
    def dvp_frame(df):
        g = df.groupby(["opponent_team_abbreviation", "game_id", "pos"])[[c for _, c in STATS]].sum().reset_index()
        return g
    D, D0 = dvp_frame(pb), dvp_frame(pb0)
    dvp, dvp_league = {}, {}
    for pos in ("G", "F", "C"):
        for label, col in STATS:
            vals = {}
            for t in real:
                cur = D[(D.opponent_team_abbreviation == t) & (D.pos == pos)][col]
                pri = D0[(D0.opponent_team_abbreviation == t) & (D0.pos == pos)][col]
                vals[t] = blend(list(cur), pri.mean() if len(pri) else None)
            lg = sum(vals.values()) / len(vals); dvp_league[f"{pos}|{label}"] = r1(lg)
            order = sorted(real, key=lambda t: vals[t])
            for i, t in enumerate(order):
                tg = T[T.team_abbreviation == t][["game_id", "game_date", "team_home_away", "opponent_team_abbreviation"]]
                last = D[(D.opponent_team_abbreviation == t) & (D.pos == pos)][["game_id", col]].merge(tg, on="game_id").sort_values("game_date").tail(5)
                dvp.setdefault(t, {})[f"{pos}|{label}"] = {"avg": r1(vals[t]), "rank": i + 1,
                    "last": [{"wk": r.game_date[5:], "v": r1(getattr(r, col)), "o": ("vs " if r.team_home_away == "home" else "@ ") + r.opponent_team_abbreviation} for r in last.itertuples()]}

    # ---- today's and tomorrow's slate with ESPN (DraftKings) lines ----
    today = datetime.now(PT).date()
    slate = []
    for i, fname in enumerate(["nba_scoreboard_today.json", "nba_scoreboard_tomorrow.json"]):
        d = today + timedelta(days=i)
        sb = get_json(f"{ESPN}/scoreboard?dates={d:%Y%m%d}", espn_dir, fname)
        for ev in sb.get("events", []):
            c = ev["competitions"][0]; tm = {x["homeAway"]: x for x in c["competitors"]}
            h, a = tm["home"]["team"]["abbreviation"], tm["away"]["team"]["abbreviation"]
            st = c["status"]["type"]
            start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            g = {"id": ev["id"], "start": ev["date"], "day": start.astimezone(PT).strftime("%Y-%m-%d"), "home": h, "away": a,
                 "state": st.get("state"), "detail": st.get("shortDetail"),
                 "scoreH": int(tm["home"].get("score") or 0) if st.get("state") != "pre" else None,
                 "scoreA": int(tm["away"].get("score") or 0) if st.get("state") != "pre" else None,
                 "recH": ((tm["home"].get("records") or [{}])[0]).get("summary"), "recA": ((tm["away"].get("records") or [{}])[0]).get("summary"),
                 "odds": parse_odds(c)}
            for side, ab in (("H", h), ("A", a)):
                last = teams.get(ab, {}).get("lastDate")
                g["rest" + side] = (start.astimezone(PT).date() - datetime.strptime(last, "%Y-%m-%d").date()).days - 1 if last else None
            slate.append(g)
    slate.sort(key=lambda g: g["start"])

    # ---- injuries (ESPN) ----
    inj = get_json(f"{ESPN}/injuries", espn_dir, "nba_injuries.json")
    recent = pb[pb.game_date >= pb.game_date.max()] if len(pb) else pb
    avg_cur = pb.groupby("athlete_display_name").agg(min=("minutes", "mean"), pts=("points", "mean"), n=("minutes", "size"))
    avg_pri = pb0.groupby("athlete_display_name").agg(min=("minutes", "mean"), pts=("points", "mean"))
    injuries = {}
    for team in inj.get("injuries", []):
        ab = abbr_by_name.get(team.get("displayName"))
        if not ab: continue
        for x in team.get("injuries", []):
            nm = x["athlete"]["displayName"]; src = avg_cur if nm in avg_cur.index else (avg_pri if nm in avg_pri.index else None)
            det = x.get("details") or {}
            injuries.setdefault(ab, []).append({
                "name": nm, "pos": ((x["athlete"].get("position") or {}).get("abbreviation") or ""), "status": x.get("status", ""),
                "injury": " ".join(v for v in [det.get("side") if det.get("side") not in (None, "Not Specified") else "", det.get("type"), det.get("detail")] if v).strip(),
                "returnDate": det.get("returnDate"), "note": x.get("shortComment", "")[:220],
                "min": r1(src.loc[nm, "min"]) if src is not None else None, "pts": r1(src.loc[nm, "pts"]) if src is not None else None})
        injuries.setdefault(ab, []).sort(key=lambda r: -(r["min"] or 0))
    status_of = {(key(r["name"]), t): r["status"] for t, lst in injuries.items() for r in lst}

    # ---- players to watch for each game on the slate ----
    players = []
    lg_pace = league["pace"] or 100
    for g in slate:
        for team, opp, home in ((g["away"], g["home"], False), (g["home"], g["away"], True)):
            cur = pb[pb.team_abbreviation == team]; pri = pb0[pb0.team_abbreviation == team]
            rec = cur.groupby("athlete_display_name").minutes.apply(lambda s: s.tail(5).mean()).sort_values(ascending=False)
            if len(rec) < 5:  # very early: fall back to last season's rotation for names on this roster
                rec = pd.concat([rec, pri.groupby("athlete_display_name").minutes.mean()]).groupby(level=0).first().sort_values(ascending=False)
            game_pace = ((teams.get(team, {}).get("pace") or lg_pace) + (teams.get(opp, {}).get("pace") or lg_pace)) / 2
            for nm in [n for n, m in rec.items() if m >= 20][:8]:
                if status_of.get((key(nm), team)) == "Out": continue
                logs = cur[cur.athlete_display_name == nm].sort_values("game_date"); prior = pb0[pb0.athlete_display_name == nm]
                if logs.empty and prior.empty: continue
                pos = (logs.pos.iloc[-1] if len(logs) else prior.pos.iloc[-1])
                props = []
                for label, col in STATS:
                    vals = list(logs[col].astype(float)); pm = float(prior[col].mean()) if len(prior) else None
                    base = blend(vals, pm)
                    if base is None: continue
                    dv = dvp.get(opp, {}).get(f"{pos}|{label}"); lg = dvp_league.get(f"{pos}|{label}")
                    mf = (dv["avg"] / lg) ** 0.5 if dv and lg else 1.0
                    pf = (game_pace / lg_pace) ** 0.5
                    proj = base * min(1.2, max(0.83, mf)) * min(1.06, max(0.94, pf))
                    allv = vals or list(prior[col].astype(float).tail(10))
                    sd = float(pd.Series(allv).std()) if len(allv) > 1 else base * 0.35
                    props.append({"stat": label, "avg": r1(sum(vals) / len(vals)) if vals else None, "prior": r1(pm), "base": r1(base), "proj": r1(proj), "sd": r1(sd),
                                  "log": [{"wk": r.game_date[5:], "v": r1(getattr(r, col)), "o": ("vs " if r.home_away == "home" else "@ ") + r.opponent_team_abbreviation} for r in logs.tail(10).itertuples()]})
                if not props: continue
                usage = [{"wk": r.game_date[5:], "min": r1(r.minutes), "fga": int(r.field_goals_attempted or 0), "starter": bool(r.starter),
                          "o": ("vs " if r.home_away == "home" else "@ ") + r.opponent_team_abbreviation} for r in logs.tail(10).itertuples()]
                players.append({"id": f"{key(nm)}-{team}", "name": nm, "pos": pos, "team": team, "opp": opp, "home": home, "game": g["id"],
                                "status": status_of.get((key(nm), team), ""), "props": props, "usage": usage,
                                "minPrior": r1(prior.minutes.mean()) if len(prior) else None})

    return {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "season": "2026-27",
            "statsThrough": pb.game_date.max() if len(pb) else None, "slateDays": sorted({g["day"] for g in slate}),
            "games": slate, "teams": teams, "league": league, "dvp": dvp, "dvpLeague": dvp_league,
            "injuries": injuries, "players": players}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default="nba.json"); ap.add_argument("--local"); ap.add_argument("--espn")
    a = ap.parse_args()
    d = build(a.local, a.espn)
    json.dump(d, open(a.out, "w"), separators=(",", ":"))
    print(f"NBA: {len(d['games'])} games ({', '.join(d['slateDays'])}), {len(d['players'])} players, stats through {d['statsThrough']} -> {a.out}")
