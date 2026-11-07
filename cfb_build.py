"""College football (FBS) data for Overlay, all free from ESPN: this week's games with DraftKings lines,
rankings, records, team ratings and Overlay's rough numbers. Writes cfb.json.

Usage: python cfb_build.py --out cfb.json [--local DIR]   (DIR holds saved sb_w*.json / sb_current.json for offline work)

Backtest (weeks 4-9 of 2026, model fit only on earlier weeks, vs DraftKings closing lines):
the score-based model below was about a coin flip against the spread (e.g. 6+ point gaps went 85-82)
and its average miss was ~2 points worse than the line. So leans are labelled as context and tracked, not trusted.
"""
import argparse, collections, json, math, os, sys, urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import numpy as np
from nba_build import parse_odds

PT = ZoneInfo("America/Los_Angeles")
SITE = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
CONF = {"1": "ACC", "4": "Big 12", "5": "Big Ten", "8": "SEC", "9": "Pac-12", "12": "C-USA", "15": "MAC",
        "17": "Mountain West", "18": "Independent", "37": "Sun Belt", "151": "American"}
LAM, CAP = 5.0, 28           # ridge strength and blowout cap for the margin model
LEAN_SPREAD, LEAN_TOTAL = 6, 6  # gaps (points) that count as a lean


def get_json(url, local, fname):
    if local:
        p = os.path.join(local, fname)
        return json.load(open(p)) if os.path.exists(p) else {}
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 overlay"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def norm_cdf(x): return .5 * (1 + math.erf(x / math.sqrt(2)))


def no_vig(a, b):
    imp = lambda o: (-o) / (-o + 100) if o < 0 else 100 / (o + 100)
    if a is None or b is None: return None
    x, y = imp(a), imp(b); return x / (x + y)


def team_info(comp):
    t = comp["team"]
    rec = {r.get("type"): r.get("summary") for r in comp.get("records", [])}
    return {"id": t["id"], "abbr": t.get("abbreviation"), "name": t.get("location") or t.get("displayName"), "nick": t.get("name"),
            "logo": t.get("logo") or f"https://a.espncdn.com/i/teamlogos/ncaa/500/{t['id']}.png", "color": t.get("color"),
            "conf": CONF.get(str(t.get("conferenceId")), ""), "rank": (comp.get("curatedRank") or {}).get("current") if (comp.get("curatedRank") or {}).get("current", 99) <= 25 else None,
            "rec": rec.get("total"), "confRec": rec.get("vsconf")}


def fit_margin(G, fbs):
    teams = sorted(fbs | {"FCS"}); ix = {t: i for i, t in enumerate(teams)}; n = len(teams)
    X = np.zeros((len(G), n + 1)); y = np.zeros(len(G))
    for k, g in enumerate(G):
        X[k, ix[g["h"]]] = 1; X[k, ix[g["a"]]] = -1; X[k, n] = 0 if g["neu"] else 1
        y[k] = max(-CAP, min(CAP, g["hs"] - g["as"]))
    P = np.eye(n + 1) * LAM; P[n, n] = 0
    b = np.linalg.solve(X.T @ X + P, X.T @ y)
    return {t: b[ix[t]] for t in teams}, b[n]


def fit_points(G, fbs):
    teams = sorted(fbs | {"FCS"}); ix = {t: i for i, t in enumerate(teams)}; n = len(teams)
    X, y = [], []
    for g in G:
        for tm, op, p in ((g["h"], g["a"], g["hs"]), (g["a"], g["h"], g["as"])):
            r = np.zeros(2 * n + 1); r[ix[tm]] = 1; r[n + ix[op]] = 1; r[2 * n] = 1; X.append(r); y.append(min(p, 56))
    X = np.array(X); y = np.array(y); P = np.eye(2 * n + 1) * LAM; P[2 * n, 2 * n] = 0
    b = np.linalg.solve(X.T @ X + P, X.T @ y)
    return {t: b[ix[t]] for t in teams}, {t: b[n + ix[t]] for t in teams}, b[2 * n]


def build(local=None):
    cur = get_json(f"{SITE}/scoreboard?groups=80&limit=400", local, "sb_current.json")
    week = (cur.get("week") or {}).get("number") or 1
    season_type = (cur.get("season") or {}).get("type", 2)
    events = []
    for w in range(1, week + 1):
        sb = cur if w == week else get_json(f"{SITE}/scoreboard?groups=80&week={w}&seasontype=2&limit=400", local, f"sb_w{w}.json")
        for e in sb.get("events", []): events.append((w, e))

    # ---- finished games and team table ----
    games, teams = [], {}
    for w, e in events:
        c = e["competitions"][0]; tm = {x["homeAway"]: x for x in c["competitors"]}
        for side in ("home", "away"):
            info = team_info(tm[side]); old = teams.get(info["id"])
            if old is None or w >= old.get("_w", 0): teams[info["id"]] = {**info, "_w": w}
        if c["status"]["type"].get("state") != "post": continue
        games.append({"id": e["id"], "w": w, "date": e["date"], "h": tm["home"]["team"]["id"], "a": tm["away"]["team"]["id"],
                      "hs": int(tm["home"].get("score") or 0), "as": int(tm["away"].get("score") or 0), "neu": bool(c.get("neutralSite"))})
    cnt = collections.Counter([g["h"] for g in games] + [g["a"] for g in games])
    fbs = {t for t, k in cnt.items() if k >= 4}
    fold = lambda t: t if t in fbs else "FCS"
    G = [{**g, "h": fold(g["h"]), "a": fold(g["a"])} for g in games]
    rating, hfa = fit_margin(G, fbs)
    off, dfn, mu = fit_points(G, fbs)

    # per-team season numbers
    tstats = {t: {"pf": [], "pa": [], "games": []} for t in fbs}
    for g in sorted(games, key=lambda g: g["date"]):
        for me, op, ps, pa, home in ((g["h"], g["a"], g["hs"], g["as"], True), (g["a"], g["h"], g["as"], g["hs"], False)):
            if me not in fbs: continue
            s = tstats[me]; s["pf"].append(ps); s["pa"].append(pa)
            o = teams.get(op, {})
            s["games"].append({"w": g["w"], "opp": o.get("abbr", "FCS"), "oppRank": None, "home": home if not g["neu"] else None,
                               "pf": ps, "pa": pa, "res": "W" if ps > pa else "L"})
    order = sorted(fbs, key=lambda t: -rating[t]); rank_of = {t: i + 1 for i, t in enumerate(order)}
    oorder = sorted(fbs, key=lambda t: -off[t]); orank = {t: i + 1 for i, t in enumerate(oorder)}
    dorder = sorted(fbs, key=lambda t: dfn[t]); drank = {t: i + 1 for i, t in enumerate(dorder)}
    sos = {}
    for t in fbs:
        opp = [g["a"] if g["h"] == t else g["h"] for g in G if t in (g["h"], g["a"])]
        sos[t] = float(np.mean([rating[fold(o)] for o in opp])) if opp else 0
    sorder = sorted(fbs, key=lambda t: -sos[t]); srank = {t: i + 1 for i, t in enumerate(sorder)}
    team_out = {}
    for t in fbs:
        s = tstats[t]; n = len(s["pf"]) or 1; info = teams.get(t, {})
        team_out[t] = {k: v for k, v in info.items() if not k.startswith("_")}
        team_out[t].update({"rating": round(float(rating[t]), 1), "ratingRank": rank_of[t], "of": len(fbs),
                            "ppg": round(sum(s["pf"]) / n, 1), "oppg": round(sum(s["pa"]) / n, 1),
                            "offRank": orank[t], "defRank": drank[t], "sosRank": srank[t], "last": s["games"][-6:]})

    # ---- this week's games ----
    slate = []
    for e in cur.get("events", []):
        c = e["competitions"][0]; tm = {x["homeAway"]: x for x in c["competitors"]}
        H, A = team_info(tm["home"]), team_info(tm["away"]); st = c["status"]["type"]
        h, a = fold(H["id"]), fold(A["id"]); neu = bool(c.get("neutralSite"))
        pm = rating[h] - rating[a] + (0 if neu else hfa)
        ph = mu + off[h] + dfn[a] + (0 if neu else hfa / 2); pa = mu + off[a] + dfn[h] - (0 if neu else hfa / 2)
        o = parse_odds(c)
        g = {"id": e["id"], "start": e["date"], "day": datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(PT).strftime("%Y-%m-%d"),
             "state": st.get("state"), "detail": st.get("shortDetail"), "neutral": neu, "conf": bool(c.get("conferenceCompetition")),
             "tv": ", ".join(n for b in c.get("broadcasts", []) for n in b.get("names", [])),
             "home": H, "away": A, "scoreH": int(tm["home"].get("score") or 0) if st.get("state") != "pre" else None,
             "scoreA": int(tm["away"].get("score") or 0) if st.get("state") != "pre" else None, "odds": o,
             "model": {"margin": round(float(pm), 1), "total": round(float(ph + pa), 1), "pH": round(norm_cdf(pm / 15.5), 3)}}
        leans = []
        if o:
            g["pH"] = no_vig(o.get("mlH"), o.get("mlA"))
            if g["pH"] is None and o.get("spreadH") is not None: g["pH"] = round(norm_cdf(-o["spreadH"] / 15.5), 3)
            if o.get("spreadH") is not None and h in fbs and a in fbs:
                d = pm + o["spreadH"]  # >0: Overlay likes the home side more than the line does
                if abs(d) >= LEAN_SPREAD:
                    side = "H" if d > 0 else "A"
                    leans.append({"type": "spread", "side": side, "team": H["abbr"] if side == "H" else A["abbr"],
                                  "line": o["spreadH"] if side == "H" else -o["spreadH"], "by": round(abs(d), 1)})
            if o.get("total") is not None and h in fbs and a in fbs:
                d = (ph + pa) - o["total"]
                if abs(d) >= LEAN_TOTAL:
                    leans.append({"type": "total", "side": "Over" if d > 0 else "Under", "line": o["total"], "by": round(abs(d), 1)})
        g["leans"] = leans
        slate.append(g)
    slate.sort(key=lambda g: (g["start"], -(min(g["home"]["rank"] or 99, g["away"]["rank"] or 99))))
    finals = {g["id"]: [teams[g["a"]]["abbr"], teams[g["h"]]["abbr"], g["as"], g["hs"]] for g in games if g["w"] >= week - 1}
    return {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "week": week, "seasonType": season_type,
            "hfa": round(float(hfa), 1), "games": slate, "teams": team_out, "finals": finals,
            "note": "Overlay's numbers come from this season's scores only. In a backtest of weeks 4-9 they were about a coin flip against DraftKings, so treat leans as context."}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default="cfb.json"); ap.add_argument("--local")
    a = ap.parse_args()
    d = build(a.local)
    json.dump(d, open(a.out, "w"), separators=(",", ":"))
    print(f"CFB week {d['week']}: {len(d['games'])} games, {sum(len(g['leans']) for g in d['games'])} leans, {len(d['teams'])} FBS teams, HFA {d['hfa']}")
