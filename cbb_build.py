"""Men's college basketball (Division I) for Overlay, free from ESPN: today's and tomorrow's games with DraftKings lines,
AP rankings, records, and Overlay's team ratings once there are enough games to mean anything. Writes cbb.json.

Usage: python cbb_build.py --out cbb.json [--local DIR]
"""
import argparse, collections, json, math, os, urllib.request
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
import numpy as np
from nba_build import parse_odds

PT = ZoneInfo("America/Los_Angeles")
SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball"
SEASON_START = date(2026, 11, 2)
MIN_GAMES = 6          # Overlay's ratings and leans switch on once most teams have this many D-I games
LAM = 3.0
LEAN_SPREAD, LEAN_TOTAL = 4, 6


def get_json(url, local, fname):
    if local:
        p = os.path.join(local, fname)
        return json.load(open(p)) if os.path.exists(p) else {}
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 overlay"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def no_vig(a, b):
    if a is None or b is None: return None
    imp = lambda o: (-o) / (-o + 100) if o < 0 else 100 / (o + 100)
    x, y = imp(a), imp(b); return round(x / (x + y), 3)


def team(comp):
    t = comp["team"]; rk = (comp.get("curatedRank") or {}).get("current")
    rec = next((r.get("summary") for r in comp.get("records", []) if r.get("type") in ("total", None)), "")
    return {"id": t["id"], "abbr": t.get("abbreviation"), "name": t.get("location") or t.get("displayName"), "nick": t.get("name"),
            "logo": t.get("logo") or f"https://a.espncdn.com/i/teamlogos/ncaa/500/{t['id']}.png", "rank": rk if rk and rk <= 25 else None, "rec": rec}


def build(local=None, cache_path="cbb_scores.json"):
    now = datetime.now(timezone.utc); today = now.astimezone(PT).date()
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {"dates": [], "games": {}}
    d = SEASON_START
    while d < today:
        ds = d.strftime("%Y%m%d")
        if ds not in cache["dates"] or d >= today - timedelta(days=2):
            try:
                sb = get_json(f"{SITE}/scoreboard?dates={ds}&groups=50&limit=500", local, f"cbb_sb_{ds}.json")
                done = True
                for e in sb.get("events", []):
                    c = e["competitions"][0]
                    if c["status"]["type"].get("state") != "post": done = False; continue
                    tm = {x["homeAway"]: x for x in c["competitors"]}
                    cache["games"][e["id"]] = {"date": d.isoformat(), "h": tm["home"]["team"]["id"], "a": tm["away"]["team"]["id"],
                                               "hs": int(tm["home"].get("score") or 0), "as": int(tm["away"].get("score") or 0), "neu": bool(c.get("neutralSite"))}
                if done and ds not in cache["dates"]: cache["dates"].append(ds)
            except Exception as e: print("skipped", ds, e)
        d += timedelta(days=1)
    json.dump(cache, open(cache_path, "w"), separators=(",", ":"))
    G = list(cache["games"].values())
    cnt = collections.Counter([g["h"] for g in G] + [g["a"] for g in G])
    d1 = {t for t, k in cnt.items() if k >= 2}
    fold = lambda t: t if t in d1 else "NONDI"
    G2 = [{**g, "h": fold(g["h"]), "a": fold(g["a"])} for g in G]
    typical = sorted(cnt[t] for t in d1)[len(d1) // 2] if d1 else 0
    ready = typical >= MIN_GAMES
    rating, off, dfn, mu, hfa = {}, {}, {}, 70.0, 3.0
    if G2:
        tl = sorted(d1 | {"NONDI"}); ix = {t: i for i, t in enumerate(tl)}; n = len(tl)
        X, y = [], []
        for g in G2:
            for tm_, op, p, home in ((g["h"], g["a"], g["hs"], 1), (g["a"], g["h"], g["as"], -1)):
                r = np.zeros(2 * n + 2); r[ix[tm_]] = 1; r[n + ix[op]] = 1; r[2 * n] = 1; r[2 * n + 1] = 0 if g["neu"] else home * .5
                X.append(r); y.append(p)
        X = np.array(X); y = np.array(y, float); P = np.eye(2 * n + 2) * LAM; P[2 * n, 2 * n] = 0; P[2 * n + 1, 2 * n + 1] = 0
        b = np.linalg.solve(X.T @ X + P, X.T @ y)
        off = {t: b[ix[t]] for t in tl}; dfn = {t: b[n + ix[t]] for t in tl}; mu = b[2 * n]; hfa = b[2 * n + 1]
        rating = {t: off[t] - dfn[t] for t in tl}
    order = sorted(d1, key=lambda t: -rating.get(t, 0)); rk = {t: i + 1 for i, t in enumerate(order)}

    slate, finals, teams = [], {}, {}
    for i in (-1, 0, 1):
        dd = today + timedelta(days=i)
        sb = get_json(f"{SITE}/scoreboard?dates={dd:%Y%m%d}&groups=50&limit=500", local, f"cbb_sb_{dd:%Y%m%d}.json")
        for e in sb.get("events", []):
            c = e["competitions"][0]; st = c["status"]["type"]; tm = {x["homeAway"]: x for x in c["competitors"]}
            H, A = team(tm["home"]), team(tm["away"])
            if st.get("state") == "post": finals[e["id"]] = [A["abbr"], H["abbr"], int(tm["away"].get("score") or 0), int(tm["home"].get("score") or 0)]
            if i < 0: continue
            o = parse_odds(c); neu = bool(c.get("neutralSite"))
            g = {"id": e["id"], "start": e["date"], "day": dd.isoformat(), "state": st.get("state"), "detail": st.get("shortDetail"), "neutral": neu,
                 "home": H, "away": A, "scoreH": int(tm["home"].get("score") or 0), "scoreA": int(tm["away"].get("score") or 0), "odds": o,
                 "pH": no_vig(o.get("mlH"), o.get("mlA")) if o else None, "tv": ", ".join(n for b_ in c.get("broadcasts", []) for n in b_.get("names", [])), "leans": []}
            h, a = fold(H["id"]), fold(A["id"])
            if rating:
                ph = mu + off.get(h, 0) + dfn.get(a, 0) + (0 if neu else hfa / 2); pa = mu + off.get(a, 0) + dfn.get(h, 0) - (0 if neu else hfa / 2)
                g["model"] = {"margin": round(float(ph - pa), 1), "total": round(float(ph + pa), 1)}
                if ready and o and h in d1 and a in d1:
                    if o.get("spreadH") is not None:
                        dlt = (ph - pa) + o["spreadH"]
                        if abs(dlt) >= LEAN_SPREAD:
                            sd = "H" if dlt > 0 else "A"
                            g["leans"].append({"type": "spread", "side": sd, "team": H["abbr"] if sd == "H" else A["abbr"], "line": o["spreadH"] if sd == "H" else -o["spreadH"], "by": round(abs(dlt), 1)})
                    if o.get("total") is not None:
                        dlt = (ph + pa) - o["total"]
                        if abs(dlt) >= LEAN_TOTAL: g["leans"].append({"type": "total", "side": "Over" if dlt > 0 else "Under", "line": o["total"], "by": round(abs(dlt), 1)})
            for T_, side in ((H, "h"), (A, "a")):
                t = fold(T_["id"])
                if t in d1: teams[T_["id"]] = {"abbr": T_["abbr"], "name": T_["name"], "gp": cnt[t], "rating": round(float(rating.get(t, 0)), 1), "ratingRank": rk.get(t),
                                               "ppg": round(float(np.mean([x["hs"] if x["h"] == t else x["as"] for x in G2 if t in (x["h"], x["a"])])), 1),
                                               "oppg": round(float(np.mean([x["as"] if x["h"] == t else x["hs"] for x in G2 if t in (x["h"], x["a"])])), 1), "of": len(d1)}
            slate.append(g)
    slate.sort(key=lambda g: (g["start"], min(g["home"]["rank"] or 99, g["away"]["rank"] or 99)))
    return {"generated": now.isoformat(timespec="minutes"), "games": slate, "teams": teams, "finals": finals,
            "ready": ready, "typicalGames": typical, "minGames": MIN_GAMES, "hfa": round(float(hfa), 1)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default="cbb.json"); ap.add_argument("--local")
    a = ap.parse_args()
    d = build(a.local, os.path.join(os.path.dirname(os.path.abspath(a.out)), "cbb_scores.json"))
    json.dump(d, open(a.out, "w"), separators=(",", ":"))
    print(f"CBB: {len(d['games'])} games, {sum(len(g['leans']) for g in d['games'])} leans, ratings {'on' if d['ready'] else 'warming up'} (typical team has {d['typicalGames']} games)")
