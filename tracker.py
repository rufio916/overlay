"""Overlay track record + Today page data (free data only).

Every run:
  1. logs Overlay's picks before games start (NFL props that beat a 6-pick Flex break-even,
     NBA spread/total leans from the rough team model), updating them until kickoff;
  2. grades logged picks once games are final, from nflverse / sportsdataverse results;
  3. writes today.json for the Today page (best picks, games, injury news, track record,
     and recent results so saved bets can be graded in the browser).

Usage: python tracker.py [--nfl data.json] [--nba nba.json] [--history history.json] [--out today.json] [--local DIR]
"""
import warnings; warnings.filterwarnings("ignore")
import argparse, io, json, math, os, sys, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd

ET, PT = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
BREAK_EVEN = 0.542  # 6-pick Flex
SHARP = ["pinnacle", "fanduel", "draftkings", "betmgm", "espnbet", "betrivers", "bovada"]
BOOKW = {"pinnacle": 3, "fanduel": 1.5, "draftkings": 1.5, "betmgm": 1, "espnbet": 1, "betrivers": 1, "bovada": .5}
NFL_COL = {"Pass Yds": "passing_yards", "Rush Yds": "rushing_yards", "Rec Yds": "receiving_yards", "Receptions": "receptions"}
NBA_COL = {"Points": "points", "Rebounds": "rebounds", "Assists": "assists", "3-Pointers": "three_point_field_goals_made", "Pts+Reb+Ast": "pra"}
NFLV = "https://github.com/nflverse/nflverse-data/releases/download"
SDV = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"


def now(): return datetime.fromisoformat(os.environ["OVERLAY_NOW"]) if os.environ.get("OVERLAY_NOW") else datetime.now(timezone.utc)  # override only for testing
def iso(t): return t.astimezone(timezone.utc).isoformat(timespec="minutes")
def norm(s): return "".join(ch for ch in str(s or "").lower().replace(" jr", "").replace(" sr", "").replace(" iii", "").replace(" ii", "") if ch.isalpha())
def imp(o): return (-o) / (-o + 100) if o < 0 else 100 / (o + 100)
def cdf(z): return 0.5 * (1 + math.erf(z / math.sqrt(2)))
def zinv(p):
    lo, hi = -8.0, 8.0
    for _ in range(60):
        m = (lo + hi) / 2
        if cdf(m) < p: lo = m
        else: hi = m
    return (lo + hi) / 2


def load_csv(url, local, name):
    if local and os.path.exists(os.path.join(local, name)):
        return pd.read_csv(os.path.join(local, name), low_memory=False)
    with urllib.request.urlopen(url, timeout=300) as r:
        return pd.read_csv(io.BytesIO(r.read()), low_memory=False)


def load_json(path, default):
    try: return json.load(open(path))
    except Exception: return default


# ---------------- NFL: same hit-chance math as the NFL page ----------------
def nfl_sd(pr):
    k = .42 if pr["stat"] == "Receptions" else .24 if pr["stat"] == "Pass Yds" else .5
    return max(math.sqrt(.5 * pr["sd"] ** 2 + .5 * (k * max(pr["proj"], 1)) ** 2), 1 if pr["stat"] == "Receptions" else 5)


def nfl_lines_index(lines):
    idx = {}
    for l in lines:
        idx.setdefault((norm(l["player"]), l["market"]), []).append(l)
    return idx


def nfl_price(p, pr, idx):
    ls = idx.get((norm(p["name"]), pr["market"]))
    if not ls: return None
    pp = next((l for l in ls if l["book"] == "prizepicks" and l["side"] == "Over"), None)
    if not pp: return None
    line = pp["line"]; sd = nfl_sd(pr); lim = max(3, .12 * line); wsum = psum = 0; n = 0
    for b in SHARP:
        by = {}
        for l in ls:
            if l["book"] == b: by.setdefault(l["line"], {})[l["side"]] = l["price"]
        two = [(ln, o) for ln, o in by.items() if "Over" in o and "Under" in o]
        if not two: continue
        ln, o = min(two, key=lambda x: abs(x[0] - line))
        if abs(ln - line) > lim: continue
        a, c = imp(o["Over"]), imp(o["Under"]); fair = a / (a + c)
        over = fair if abs(ln - line) < 1e-9 else 1 - cdf((line - (ln + sd * zinv(fair))) / sd)
        w = BOOKW.get(b, 1); wsum += w; psum += w * over; n += 1
    if not wsum: return {"line": line, "fair": None, "n": 0}
    return {"line": line, "fair": psum / wsum, "n": n}


def nfl_kickoff(g):
    return datetime.strptime(f"{g['day']} {g['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)


# ---------------- NBA: same rough team model as the NBA page ----------------
def nba_model(g, nba):
    H, A = nba["teams"].get(g["home"]), nba["teams"].get(g["away"])
    if not H or not A or H.get("ortg") is None or A.get("ortg") is None: return None
    pace = (H["pace"] + A["pace"]) / 2; hca = 2.4
    pH = (H["ortg"] + A["drtg"]) / 2 * pace / 100 + hca / 2; pA = (A["ortg"] + H["drtg"]) / 2 * pace / 100 - hca / 2
    if g.get("restH") == 0: pH -= 1.2
    if g.get("restA") == 0: pA -= 1.2
    for t, side in ((g["home"], "H"), (g["away"], "A")):
        for x in nba["injuries"].get(t, []):
            if x["status"] == "Out" and (x.get("min") or 0) >= 24:
                hit = min(3, (x.get("pts") or 0) * .12)
                if side == "H": pH -= hit
                else: pA -= hit
    return {"ptsH": pH, "ptsA": pA, "margin": pH - pA, "total": pH + pA, "pace": pace}


def nba_win(g):
    o = g.get("odds") or {}
    if o.get("mlH") is not None and o.get("mlA") is not None:
        a, h = imp(o["mlA"]), imp(o["mlH"]); return h / (a + h)
    if o.get("spreadH") is not None: return cdf(-o["spreadH"] / 12)
    return None


def nba_leans(g, nba):
    o = g.get("odds"); m = nba_model(g, nba)
    if not o or not m or o.get("spreadH") is None: return []
    out = []; mkt = -o["spreadH"]; diff = m["margin"] - mkt
    if abs(diff) >= 3:
        side = "H" if diff > 0 else "A"; line = o["spreadH"] if side == "H" else -o["spreadH"]
        out.append({"type": "spread", "side": side, "team": g["home"] if side == "H" else g["away"], "line": line, "by": round(abs(diff), 1)})
    if o.get("total") is not None and abs(m["total"] - o["total"]) >= 5:
        out.append({"type": "total", "side": "Over" if m["total"] > o["total"] else "Under", "line": o["total"], "by": round(abs(m["total"] - o["total"]), 1)})
    return out


def run(a):
    nfl = load_json(a.nfl, {}); nba = load_json(a.nba, {})
    hist = load_json(a.history, {"picks": {}, "injurySnap": {}, "injuryNews": []})
    picks = hist["picks"]; t_now = now()

    # ---------- 1. log picks (update until kickoff, then freeze) ----------
    nfl_games = {g["id"]: g for g in nfl.get("games", [])}
    idx = nfl_lines_index(nfl.get("lines", []))
    nfl_rows = []
    fetched = (nfl.get("odds") or {}).get("fetched")
    for p in nfl.get("players", []):
        g = nfl_games.get(p["game"])
        if not g: continue
        ko = nfl_kickoff(g)
        for pr in p["props"]:
            pc = nfl_price(p, pr, idx) if idx else None
            row = {"sport": "nfl", "name": p["name"], "team": p["team"], "opp": p["opp"], "pos": p["pos"], "stat": pr["stat"], "proj": pr["proj"],
                   "avg": pr["avg"], "game": g["id"], "week": nfl.get("week"), "kickoff": iso(ko), "img": p.get("img", ""), "status": p.get("status", "")}
            if pc and pc["fair"] is not None:
                side = "More" if pc["fair"] >= .5 else "Less"; ch = max(pc["fair"], 1 - pc["fair"])
                row.update({"line": pc["line"], "side": side, "chance": round(ch, 4), "books": pc["n"]})
                if ch >= BREAK_EVEN and ko > t_now and fetched and p.get("status") not in ("Out", "Doubtful"):
                    k = f"nfl|{nfl['week']}|{norm(p['name'])}|{pr['stat']}"
                    if k not in picks or picks[k].get("result") is None and datetime.fromisoformat(picks[k]["kickoff"]) > t_now:
                        picks[k] = {**{x: row[x] for x in ("sport", "name", "team", "opp", "stat", "line", "side", "chance", "game", "week", "kickoff")}, "logged": iso(t_now), "result": None}
            nfl_rows.append(row)
    nba_games = nba.get("games", [])
    for g in nba_games:
        start = datetime.fromisoformat(g["start"].replace("Z", "+00:00"))
        if start <= t_now or g.get("state") != "pre": continue
        for l in nba_leans(g, nba):
            k = f"nba|{g['id']}|{l['type']}"
            picks[k] = {"sport": "nba", "game": g["id"], "away": g["away"], "home": g["home"], **l, "kickoff": g["start"], "logged": iso(t_now), "result": None}

    # ---------- 2. grade finished picks ----------
    try:
        st = load_csv(f"{NFLV}/stats_player/stats_player_week_{nfl.get('season', 2026)}.csv", a.local, f"stats_player_week_{nfl.get('season', 2026)}.csv")
        gm = load_csv(f"{NFLV}/schedules/games.csv", a.local, "games.csv")
        gm = gm[gm.season == nfl.get("season", 2026)]
        final_nfl = {r.game_id: r for r in gm[gm.result.notna()].itertuples()}
        st["k"] = st.player_display_name.map(norm)
        for k, pk in picks.items():
            if pk["sport"] != "nfl" or pk.get("result") is not None or pk["game"] not in final_nfl: continue
            row = st[(st.k == norm(pk["name"])) & (st.week == pk["week"])]
            if row.empty: pk["result"] = "void"; continue
            v = float(row.iloc[0][NFL_COL[pk["stat"]]] or 0); pk["actual"] = v
            pk["result"] = "push" if v == pk["line"] else ("win" if (v > pk["line"]) == (pk["side"] == "More") else "loss")
    except Exception as e:
        print("NFL grading skipped:", e, file=sys.stderr); st = None; final_nfl = {}
    try:
        sch = load_csv(f"{SDV}/espn_nba_schedules/nba_schedule_2027.csv", a.local, "nba_schedule_2027.csv")
        done = sch[sch.status_type_completed == True]
        final_nba = {str(r.game_id): r for r in done.itertuples()}
        for k, pk in picks.items():
            if pk["sport"] != "nba" or pk.get("result") is not None or str(pk["game"]) not in final_nba: continue
            r = final_nba[str(pk["game"])]; h, aw = float(r.home_score), float(r.away_score); pk["final"] = f"{pk['away']} {int(aw)} · {pk['home']} {int(h)}"
            if pk["type"] == "spread":
                margin = (h - aw) if pk["side"] == "H" else (aw - h); x = margin + pk["line"]
            else:
                x = (h + aw - pk["line"]) * (1 if pk["side"] == "Over" else -1)
            pk["result"] = "push" if x == 0 else ("win" if x > 0 else "loss")
    except Exception as e:
        print("NBA grading skipped:", e, file=sys.stderr); sch = None

    # ---------- 3. injury news (what changed since the last run) ----------
    snap = hist.get("injurySnap", {}); news = [n for n in hist.get("injuryNews", []) if datetime.fromisoformat(n["at"]) > t_now - timedelta(days=3)]
    cur = {}
    for t, lst in (nfl.get("injuries") or {}).items():
        for x in lst: cur[f"nfl|{t}|{x['name']}"] = x["status"]
    for t, lst in (nba.get("injuries") or {}).items():
        for x in lst:
            if (x.get("min") or 0) >= 18: cur[f"nba|{t}|{x['name']}"] = x["status"]
    if snap:  # only report changes after the first run
        for k, s in cur.items():
            if snap.get(k) != s and s in ("Out", "Doubtful", "Questionable", "Day-To-Day"):
                sport, team, name = k.split("|", 2)
                news.append({"sport": sport, "team": team, "name": name, "status": s, "was": snap.get(k) or "healthy", "at": iso(t_now)})
        for k, s in snap.items():
            # NFL reports reset each week, so a missing name means "no report yet", not "healthy": carry it forward quietly
            if k.startswith("nfl|"):
                if k not in cur: cur[k] = s
                continue
            if k not in cur and s in ("Out", "Doubtful"):
                sport, team, name = k.split("|", 2)
                news.append({"sport": sport, "team": team, "name": name, "status": "Off report", "was": s, "at": iso(t_now)})
    hist["injurySnap"] = cur; hist["injuryNews"] = news[-60:]

    # drop old pending picks that never got graded (e.g. postponed), keep graded ones
    for k in [k for k, p in picks.items() if p.get("result") is None and datetime.fromisoformat(p["kickoff"].replace("Z", "+00:00")) < t_now - timedelta(days=10)]:
        picks[k]["result"] = "void"
    hist["updated"] = iso(t_now)
    json.dump(hist, open(a.history, "w"), separators=(",", ":"))

    # ---------- 4. track record ----------
    def summary(sel):
        g = [p for p in sel if p.get("result") in ("win", "loss")]
        w = sum(p["result"] == "win" for p in g)
        return {"n": len(g), "w": w, "l": len(g) - w, "pct": round(w / len(g), 3) if g else None}
    nflp = [p for p in picks.values() if p["sport"] == "nfl"]
    buckets = []
    for lo, hi, label in ((.542, .56, "54–56%"), (.56, .58, "56–58%"), (.58, 1.01, "58%+")):
        sel = [p for p in nflp if lo <= p["chance"] < hi]; s = summary(sel)
        s["label"] = label; s["exp"] = round(sum(p["chance"] for p in sel if p.get("result") in ("win", "loss")) / s["n"], 3) if s["n"] else None
        buckets.append(s)
    nbap = [p for p in picks.values() if p["sport"] == "nba"]
    recent = sorted([p for p in picks.values() if p.get("result") in ("win", "loss", "push")], key=lambda p: p["kickoff"], reverse=True)[:40]
    track = {"nfl": {**summary(nflp), "buckets": buckets, "pending": sum(p.get("result") is None for p in nflp),
                     "byWeek": {str(w): summary([p for p in nflp if p["week"] == w]) for w in sorted({p["week"] for p in nflp})}},
             "nba": {"spread": summary([p for p in nbap if p["type"] == "spread"]), "total": summary([p for p in nbap if p["type"] == "total"]),
                     "pending": sum(p.get("result") is None for p in nbap)},
             "recent": recent, "since": min((p["logged"] for p in picks.values()), default=None)}

    # ---------- 5. Today page data ----------
    upcoming = [r for r in nfl_rows if datetime.fromisoformat(r["kickoff"]) > t_now]
    nfl_top = sorted([r for r in upcoming if r.get("chance") and r.get("status") not in ("Out", "Doubtful")], key=lambda r: -r["chance"])[:10]
    nba_day_games = []
    for g in nba_games:
        pH = nba_win(g); m = nba_model(g, nba)
        nba_day_games.append({"id": g["id"], "away": g["away"], "home": g["home"], "start": g["start"], "day": g["day"], "state": g.get("state"),
                              "pH": round(pH, 3) if pH is not None else None, "odds": g.get("odds"), "leans": nba_leans(g, nba) if g.get("state") == "pre" else [],
                              "model": {k: round(v, 1) for k, v in m.items()} if m else None, "scoreH": g.get("scoreH"), "scoreA": g.get("scoreA"), "detail": g.get("detail")})
    MIN = {"Points": 8, "Rebounds": 3, "Assists": 2, "3-Pointers": .9, "Pts+Reb+Ast": 14}
    nba_props = []
    for p in nba.get("players", []):
        if p.get("status") in ("Out",): continue
        for pr in p["props"]:
            base = pr.get("base") or pr.get("avg") or pr.get("prior")
            if not base or base < MIN.get(pr["stat"], 0): continue
            d = (nba.get("dvp", {}).get(p["opp"], {}) or {}).get(f"{p['pos']}|{pr['stat']}")
            k = {"Points": .3, "Rebounds": .35, "Assists": .4, "3-Pointers": .6, "Pts+Reb+Ast": .25}[pr["stat"]]
            sd = max(math.sqrt(.5 * pr["sd"] ** 2 + .5 * (k * max(pr["proj"], 1)) ** 2), 1)
            nba_props.append({"name": p["name"], "team": p["team"], "opp": p["opp"], "home": p["home"], "pos": p["pos"], "stat": pr["stat"], "proj": pr["proj"], "base": base,
                              "lift": round((pr["proj"] - base) / sd, 3), "rank": d["rank"] if d else None, "game": p["game"], "img": p.get("img", ""), "id": p["id"]})
    nba_top = sorted(nba_props, key=lambda x: -x["lift"])[:8]

    # recent results so saved bets can be graded in the browser
    res = {"nfl": {}, "nflGames": {}, "nba": {}, "nbaGames": {}}
    try:
        wk = nfl.get("week") or 1; s2 = st[st.week >= wk - 2]
        for r in s2.itertuples():
            for lab, col in NFL_COL.items():
                v = getattr(r, col)
                if pd.notna(v) and r.position in ("QB", "RB", "WR", "TE"):
                    res["nfl"].setdefault(f"{norm(r.player_display_name)}|{lab}", []).append([int(r.week), float(v)])
        for gid, r in final_nfl.items():
            if r.week >= wk - 2: res["nflGames"][gid] = [r.away_team, r.home_team, int(r.away_score), int(r.home_score)]
    except Exception as e:
        print("NFL results skipped:", e, file=sys.stderr)
    try:
        pb = load_csv(f"{SDV}/espn_nba_player_boxscores/player_box_2027.csv", a.local, "player_box_2027.csv")
        pb = pb[(pb.did_not_play != True) & pb.minutes.notna()]
        days = sorted(pb.game_date.astype(str).unique())[-4:]; pb = pb[pb.game_date.astype(str).isin(days)].copy()
        pb["pra"] = pb.points.fillna(0) + pb.rebounds.fillna(0) + pb.assists.fillna(0)
        for r in pb.itertuples():
            for lab, col in NBA_COL.items():
                v = getattr(r, col)
                if pd.notna(v): res["nba"].setdefault(f"{norm(r.athlete_display_name)}|{lab}", []).append([str(r.game_date), float(v), str(r.game_id)])
        if sch is not None:
            for r in sch[(sch.status_type_completed == True) & sch.game_date.astype(str).isin(days)].itertuples():
                res["nbaGames"][str(r.game_id)] = [r.away_abbreviation, r.home_abbreviation, int(r.away_score), int(r.home_score)]
    except Exception as e:
        print("NBA results skipped:", e, file=sys.stderr)

    nfl_inj = sorted([n for n in news if n["sport"] == "nfl"], key=lambda n: n["at"], reverse=True)[:12]
    nba_inj = sorted([n for n in news if n["sport"] == "nba"], key=lambda n: n["at"], reverse=True)[:12]
    nba_inj_now = []
    for t, lst in (nba.get("injuries") or {}).items():
        for x in lst:
            if (x.get("min") or 0) >= 24 and x["status"] in ("Out", "Day-To-Day", "Questionable", "Doubtful") and t in {g["home"] for g in nba_games} | {g["away"] for g in nba_games}:
                nba_inj_now.append({"team": t, "name": x["name"], "status": x["status"], "min": x.get("min"), "pts": x.get("pts"), "note": x.get("note", "")[:140]})
    nba_inj_now.sort(key=lambda x: -(x["pts"] or 0))
    nfl_games_out = []
    for g in nfl.get("games", []):
        pH = None
        if g.get("mlH") is not None and g.get("mlA") is not None:
            pa, ph = imp(g["mlA"]), imp(g["mlH"]); pH = ph / (pa + ph)
        nfl_games_out.append({"id": g["id"], "away": g["away"], "home": g["home"], "kickoff": iso(nfl_kickoff(g)), "spread": g.get("spread"), "total": g.get("total"), "pH": round(pH, 3) if pH else None})
    today = {"generated": iso(t_now), "nflWeek": nfl.get("week"), "nflLinesAt": fetched, "nflTop": nfl_top, "nflGames": nfl_games_out,
             "nbaGames": nba_day_games, "nbaTop": nba_top, "nbaInjuries": nba_inj_now[:10], "injuryNews": {"nfl": nfl_inj, "nba": nba_inj},
             "track": track, "results": res, "credits": (nfl.get("odds") or {}).get("remaining")}
    json.dump(today, open(a.out, "w"), separators=(",", ":"))
    print(f"today: {len(nfl_top)} NFL picks, {len(nba_day_games)} NBA games, {len(nba_top)} NBA props; track NFL {track['nfl']['w']}-{track['nfl']['l']} "
          f"({track['nfl']['pending']} pending), NBA ATS {track['nba']['spread']['w']}-{track['nba']['spread']['l']}, totals {track['nba']['total']['w']}-{track['nba']['total']['l']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--nfl", default="data.json"); ap.add_argument("--nba", default="nba.json")
    ap.add_argument("--history", default="history.json"); ap.add_argument("--out", default="today.json"); ap.add_argument("--local")
    run(ap.parse_args())
