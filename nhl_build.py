"""NHL data for Overlay, all free from ESPN: tonight's games with DraftKings lines, skater/goalie game logs
(from cached box scores), projections, and DraftKings player prop lines. Writes nhl.json; caches box scores in nhl_box.json.

Usage: python nhl_build.py --out nhl.json [--local DIR]
"""
import argparse, collections, json, math, os, urllib.request
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
from nba_build import parse_odds, odds_num, line_num

PT = ZoneInfo("America/Los_Angeles")
SITE = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"
CORE = "https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl"
SEASON_START = date(2026, 10, 1)
PROP_TYPES = {"Total Shots on Goal": "Shots on Goal", "Total Points": "Points", "Total Assists": "Assists",
              "Total Goals": "Goals", "Total Saves": "Saves", "Total Blocked Shots": "Blocked Shots"}
SKATER_STATS = ["Shots on Goal", "Points", "Assists", "Goals", "Blocked Shots"]


def get_json(url, local, fname):
    if local:
        p = os.path.join(local, fname)
        return json.load(open(p)) if os.path.exists(p) else {}
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 overlay"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def toi(x):
    try: m, s = str(x).split(":"); return int(m) + int(s) / 60
    except Exception: return 0.0


def num(x):
    try: return float(str(x).replace(",", ""))
    except ValueError: return 0.0


def box_from_summary(sm):
    """{athlete_id: {...}} for one finished game."""
    out = {}
    for team in (sm.get("boxscore") or {}).get("players", []):
        abbr = (team.get("team") or {}).get("abbreviation")
        for grp in team.get("statistics", []):
            lab = grp.get("labels") or []; gname = grp.get("name")
            for a in grp.get("athletes", []):
                ath = a.get("athlete") or {}; st = a.get("stats") or []
                if not ath.get("id") or not st: continue
                v = dict(zip(lab, st)); pos = ((ath.get("position") or {}).get("abbreviation")) or ("G" if gname == "goalies" else "")
                if gname == "goalies":
                    if toi(v.get("TOI")) < 1: continue
                    rec = {"Saves": num(v.get("SV")), "SA": num(v.get("SA")), "GA": num(v.get("GA")), "toi": round(toi(v.get("TOI")), 1)}
                else:
                    g, a_ = num(v.get("G")), num(v.get("A"))
                    rec = {"Goals": g, "Assists": a_, "Points": g + a_, "Shots on Goal": num(v.get("S")), "Blocked Shots": num(v.get("BS")),
                           "toi": round(toi(v.get("TOI")), 1), "pptoi": round(toi(v.get("PPTOI")), 1)}
                out[ath["id"]] = {"name": ath.get("displayName"), "team": abbr, "pos": pos, "img": (ath.get("headshot") or {}).get("href", ""), "s": rec}
    return out


def fetch_props(gid, local):
    items, page = [], 1
    while page <= 4:
        d = get_json(f"{CORE}/events/{gid}/competitions/{gid}/odds/100/propBets?lang=en&region=us&limit=1000&page={page}", local, f"nhl_props_{gid}_{page}.json")
        items += d.get("items", [])
        if page >= (d.get("pageCount") or 1): break
        page += 1
    pairs = {}
    for it in items:
        stat = PROP_TYPES.get((it.get("type") or {}).get("name"))
        if not stat or "athlete" not in it: continue
        aid = it["athlete"]["$ref"].split("/athletes/")[1].split("?")[0]
        o = it.get("odds") or {}
        ln = line_num((o.get("total") or {}).get("value")); price = odds_num((o.get("american") or {}).get("value"))
        if ln is None or price is None: continue
        pairs.setdefault((aid, stat, ln), []).append(price)
    out = {}
    imp = lambda x: (-x) / (-x + 100) if x < 0 else 100 / (x + 100)
    for (aid, stat, ln), v in pairs.items():
        if len(v) != 2: continue
        ov, un = v; p_over = imp(ov) / (imp(ov) + imp(un))
        cur = out.setdefault(aid, {}).get(stat)
        if cur is None or abs(p_over - .5) < abs(cur["pOver"] - .5):
            out[aid][stat] = {"line": ln, "overOdds": ov, "underOdds": un, "pOver": round(p_over, 4)}
    return out


def pois_over(lam, line):
    lam = max(lam, 0.01); c, term = 0.0, math.exp(-lam)
    for i in range(int(math.floor(line)) + 1):
        if i: term *= lam / i
        c += term
    return 1 - c


def build(local=None, cache_path="nhl_box.json"):
    now = datetime.now(timezone.utc); today = now.astimezone(PT).date()
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {"dates": [], "games": {}}
    # ---- every finished game this season, cached ----
    d = SEASON_START
    while d < today:
        ds = d.strftime("%Y%m%d")
        if ds not in cache["dates"] or d >= today - timedelta(days=2):
            try:
                sb = get_json(f"{SITE}/scoreboard?dates={ds}", local, f"nhl_sb_{ds}.json")
                done = True
                for e in sb.get("events", []):
                    c = e["competitions"][0]
                    if (c.get("status") or {}).get("type", {}).get("state") != "post": done = False; continue
                    if e["id"] in cache["games"]: continue
                    tm = {x["homeAway"]: x for x in c["competitors"]}
                    sm = get_json(f"{SITE}/summary?event={e['id']}", local, f"nhl_summary_{e['id']}.json")
                    cache["games"][e["id"]] = {"date": d.isoformat(), "home": tm["home"]["team"]["abbreviation"], "away": tm["away"]["team"]["abbreviation"],
                                               "hs": int(tm["home"].get("score") or 0), "as": int(tm["away"].get("score") or 0), "box": box_from_summary(sm)}
                if done and ds not in cache["dates"]: cache["dates"].append(ds)
            except Exception as e:
                print("skipped", ds, e)
        d += timedelta(days=1)
    json.dump(cache, open(cache_path, "w"), separators=(",", ":"))
    games = sorted(cache["games"].items(), key=lambda kv: kv[1]["date"])

    # ---- team numbers: goals and shots for/against per game ----
    T = collections.defaultdict(lambda: {"gf": [], "ga": [], "sf": [], "sa": [], "res": []})
    for gid, g in games:
        sog = collections.defaultdict(float)
        for p in g["box"].values():
            if p["pos"] != "G": sog[p["team"]] += p["s"].get("Shots on Goal", 0)
        for me, op, gf, ga in ((g["home"], g["away"], g["hs"], g["as"]), (g["away"], g["home"], g["as"], g["hs"])):
            t = T[me]; t["gf"].append(gf); t["ga"].append(ga); t["sf"].append(sog[me]); t["sa"].append(sog[op])
            t["res"].append({"date": g["date"], "opp": op, "home": me == g["home"], "gf": gf, "ga": ga})
    mean = lambda x: sum(x) / len(x) if x else 0
    lg = {k: mean([mean(t[k]) for t in T.values() if t[k]]) for k in ("gf", "sf")}
    teams = {}
    for k, t in T.items():
        teams[k] = {"gp": len(t["gf"]), "gf": round(mean(t["gf"]), 2), "ga": round(mean(t["ga"]), 2), "sf": round(mean(t["sf"]), 1), "sa": round(mean(t["sa"]), 1),
                    "last": t["res"][-6:]}
    for key, better in (("gf", -1), ("ga", 1), ("sf", -1), ("sa", 1)):
        order = sorted(teams, key=lambda x: better * teams[x][key])
        for i, x in enumerate(order): teams[x][key + "Rank"] = i + 1

    # ---- tonight + tomorrow, finals for grading ----
    slate, finals, box_recent = [], {}, {}
    for i in (-1, 0, 1):
        dd = today + timedelta(days=i)
        sb = get_json(f"{SITE}/scoreboard?dates={dd:%Y%m%d}", local, f"nhl_sb_{dd:%Y%m%d}.json")
        for e in sb.get("events", []):
            c = e["competitions"][0]; st = c["status"]["type"]; tm = {x["homeAway"]: x for x in c["competitors"]}
            H, A = tm["home"]["team"], tm["away"]["team"]
            if st.get("state") == "post":
                finals[e["id"]] = [A["abbreviation"], H["abbreviation"], int(tm["away"].get("score") or 0), int(tm["home"].get("score") or 0)]
                if e["id"] in cache["games"]: box_recent[e["id"]] = {p["name"]: p["s"] for p in cache["games"][e["id"]]["box"].values()}
                else:
                    try: box_recent[e["id"]] = {p["name"]: p["s"] for p in box_from_summary(get_json(f"{SITE}/summary?event={e['id']}", local, f"nhl_summary_{e['id']}.json")).values()}
                    except Exception: pass
            if i < 0: continue
            rec = lambda x: next((r.get("summary") for r in x.get("records", []) if r.get("type") in ("total", None)), "")
            o = parse_odds(c)
            pH = None
            if o and o.get("mlH") is not None and o.get("mlA") is not None:
                imp = lambda x: (-x) / (-x + 100) if x < 0 else 100 / (x + 100)
                pH = round(imp(o["mlH"]) / (imp(o["mlH"]) + imp(o["mlA"])), 3)
            slate.append({"id": e["id"], "start": e["date"], "day": dd.isoformat(), "state": st.get("state"), "detail": st.get("shortDetail"),
                          "home": H["abbreviation"], "away": A["abbreviation"], "homeName": H.get("displayName"), "awayName": A.get("displayName"),
                          "recH": rec(tm["home"]), "recA": rec(tm["away"]), "scoreH": int(tm["home"].get("score") or 0), "scoreA": int(tm["away"].get("score") or 0),
                          "odds": o, "pH": pH, "tv": ", ".join(n for b in c.get("broadcasts", []) for n in b.get("names", []))})
    slate.sort(key=lambda g: g["start"])

    # ---- players: logs, projections, DraftKings lines ----
    logs = collections.defaultdict(list)
    for gid, g in games:
        for aid, p in g["box"].items():
            logs[aid].append({"date": g["date"], "opp": g["away"] if p["team"] == g["home"] else g["home"], "home": p["team"] == g["home"], **p})
    book = {}
    for g in slate:
        if g["state"] != "pre": continue
        try: book[g["id"]] = fetch_props(g["id"], local)
        except Exception as e: print("props skipped", g["id"], e)
    players = []
    team_games = {k: [r["date"] for r in t["res"]] for k, t in T.items()}
    for g in slate:
        if g["state"] != "pre": continue
        for side in ("home", "away"):
            team = g[side]; opp = g["away"] if side == "home" else g["home"]
            recent_dates = set(team_games.get(team, [])[-2:])
            for aid, L in logs.items():
                if L[-1]["team"] != team or not (recent_dates & {x["date"] for x in L[-3:]}): continue
                bk = (book.get(g["id"]) or {}).get(aid, {})
                is_g = L[-1]["pos"] == "G"
                if is_g and "Saves" not in bk: continue  # only the goalie DraftKings expects to start
                last5 = L[-5:]; toi_avg = mean([x["s"]["toi"] for x in last5])
                if not is_g and toi_avg < 13 and not bk: continue
                w = [0.85 ** (len(L) - 1 - i) for i in range(len(L))]
                props = []
                for stat in (["Saves"] if is_g else SKATER_STATS):
                    vals = [x["s"].get(stat, 0) for x in L]
                    rate = sum(v * wi for v, wi in zip(vals, w)) / sum(w)
                    if is_g:
                        fac = 1 + 0.5 * ((teams.get(opp, {}).get("sf", lg["sf"]) / lg["sf"]) - 1) if lg["sf"] else 1
                        proj = rate * fac
                    else:
                        tw = [x["s"]["toi"] for x in L]; per60 = sum(v * wi for v, wi in zip(vals, w)) / max(sum(t * wi for t, wi in zip(tw, w)), 1) * 60
                        if stat == "Shots on Goal" or stat == "Blocked Shots":
                            base = teams.get(opp, {}).get("sa" if stat == "Shots on Goal" else "sf", lg["sf"])
                            fac = 1 + 0.5 * (base / lg["sf"] - 1) if lg["sf"] else 1
                        else:
                            fac = 1 + 0.5 * ((teams.get(opp, {}).get("ga", lg["gf"]) / lg["gf"]) - 1) if lg["gf"] else 1
                        proj = per60 * toi_avg / 60 * fac
                    pr = {"stat": stat, "avg": round(mean(vals), 2), "base": round(rate, 2), "proj": round(proj, 2), "fac": round(fac, 2),
                          "log": [{"d": x["date"][5:], "opp": x["opp"], "home": x["home"], "v": x["s"].get(stat, 0)} for x in L[-10:]]}
                    if stat in bk:
                        pr.update(bk[stat])
                        pr["ours"] = round(pois_over(proj, pr["line"]), 4)
                    if stat in ("Goals", "Blocked Shots") and stat not in bk: continue
                    props.append(pr)
                if not props: continue
                players.append({"id": aid, "name": L[-1]["name"], "team": team, "opp": opp, "home": side == "home", "pos": L[-1]["pos"], "img": L[-1]["img"],
                                "game": g["id"], "start": g["start"], "day": g["day"], "toi": round(toi_avg, 1), "gp": len(L), "props": props})
    players.sort(key=lambda p: (p["start"], p["team"], -p["toi"]))
    return {"generated": now.isoformat(timespec="minutes"), "games": slate, "teams": teams, "league": {k: round(v, 2) for k, v in lg.items()},
            "players": players, "finals": finals, "box": box_recent, "propsBook": "DraftKings"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default="nhl.json"); ap.add_argument("--local")
    a = ap.parse_args()
    d = build(a.local, os.path.join(os.path.dirname(os.path.abspath(a.out)), "nhl_box.json"))
    json.dump(d, open(a.out, "w"), separators=(",", ":"))
    n = sum(1 for p in d["players"] for pr in p["props"] if pr.get("line") is not None)
    print(f"NHL: {len(d['games'])} games, {len(d['players'])} players, {n} DraftKings lines, {len(d['teams'])} teams")
