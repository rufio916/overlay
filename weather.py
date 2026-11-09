"""Kickoff weather from Open-Meteo (free, no key). Used for outdoor NFL and college football games.

forecast(lat, lon, kickoff_iso) -> {"wind": mph, "gust": mph, "temp": F, "rain": % chance, "snow": bool, "flag": short text or ""}
"""
import json, urllib.parse, urllib.request
from datetime import datetime, timezone

# NFL home stadiums (lat, lon). Domes and closed roofs are skipped using nflverse's roof column.
NFL_STADIUMS = {
    "ARI": (33.528, -112.263), "ATL": (33.755, -84.401), "BAL": (39.278, -76.623), "BUF": (42.774, -78.787), "CAR": (35.226, -80.853),
    "CHI": (41.862, -87.617), "CIN": (39.095, -84.516), "CLE": (41.506, -81.700), "DAL": (32.748, -97.093), "DEN": (39.744, -105.020),
    "DET": (42.340, -83.046), "GB": (44.501, -88.062), "HOU": (29.685, -95.411), "IND": (39.760, -86.164), "JAX": (30.324, -81.637),
    "KC": (39.049, -94.484), "LA": (33.953, -118.339), "LAC": (33.953, -118.339), "LV": (36.091, -115.184), "MIA": (25.958, -80.239),
    "MIN": (44.974, -93.258), "NE": (42.091, -71.264), "NO": (29.951, -90.081), "NYG": (40.814, -74.074), "NYJ": (40.814, -74.074),
    "PHI": (39.901, -75.168), "PIT": (40.447, -80.016), "SEA": (47.595, -122.332), "SF": (37.403, -121.970), "TB": (27.976, -82.503),
    "TEN": (36.166, -86.771), "WAS": (38.908, -76.864),
}
_geo_cache = {}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 overlay"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def geocode(city, state):
    k = (city, state)
    if k in _geo_cache: return _geo_cache[k]
    res = None
    try:
        d = _get("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": city, "count": 10, "country": "US"}))
        hits = d.get("results") or []
        st = (state or "").lower()
        best = next((h for h in hits if st and st in (h.get("admin1", "").lower(), (h.get("admin1_code") or "").lower())), hits[0] if hits else None)
        if best: res = (best["latitude"], best["longitude"])
    except Exception:
        pass
    _geo_cache[k] = res
    return res


def flag(w):
    bits = []
    if w["wind"] >= 15 or w["gust"] >= 25: bits.append(f"Wind {round(w['wind'])} mph")
    if w["snow"]: bits.append("Snow")
    elif w["rain"] >= 50: bits.append(f"Rain {w['rain']}%")
    if w["temp"] <= 25: bits.append(f"{round(w['temp'])}°F")
    return " · ".join(bits)


def forecast(lat, lon, kickoff_iso):
    t = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    hours = (t - datetime.now(timezone.utc)).total_seconds() / 3600
    if hours < -4 or hours > 24 * 7: return None
    d = _get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,wind_gusts_10m,snowfall",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "UTC", "forecast_days": 8}))
    h = d.get("hourly") or {}
    times = h.get("time") or []
    key = t.strftime("%Y-%m-%dT%H:00")
    if key not in times: return None
    i = times.index(key)
    pick = lambda k: (h.get(k) or [None] * len(times))[i]
    w = {"wind": round(pick("wind_speed_10m") or 0), "gust": round(pick("wind_gusts_10m") or 0), "temp": round(pick("temperature_2m") or 0),
         "rain": int(pick("precipitation_probability") or 0), "snow": (pick("snowfall") or 0) > 0}
    w["flag"] = flag(w)
    return w
