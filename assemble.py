"""Build the self-contained pages: Today (index.html), NFL (nfl.html), NBA (nba.html).
Usage: python assemble.py [today|nfl|nba|all] [out_path]"""
import os, re, sys
here = os.path.dirname(os.path.abspath(__file__))
rd = lambda f: open(os.path.join(here, f)).read()
PAGES = {"today": ("today.part", "today.json", "index.html", "Overlay"),
         "nfl": ("body.part", "data.json", "nfl.html", "Overlay NFL"),
         "nba": ("nba.part", "nba.json", "nba.html", "Overlay NBA")}
HEAD = ('<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        '<meta name="theme-color" content="#0e141b">\n<link rel="manifest" href="manifest.webmanifest">\n'
        '<link rel="icon" href="icon-192.png" type="image/png">\n<link rel="apple-touch-icon" href="apple-touch-icon.png">\n'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n<meta name="mobile-web-app-capable" content="yes">\n'
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n<meta name="apple-mobile-web-app-title" content="Overlay">\n'
        '<style>body{margin:0;font-size:14px}img{max-width:100%}[hidden]{display:none!important}</style>\n')
FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">\n')
SW = '<script>if("serviceWorker" in navigator&&location.protocol==="https:")navigator.serviceWorker.register("sw.js").catch(()=>{});</script>\n'

def build(name, out=None):
    part, data_f, default_out, title = PAGES[name]
    data = rd(data_f).replace("</", "<\\/")
    shared = "" if name == "nfl" else "\n".join(re.findall(r"<style>.*?</style>", rd("body.part"), re.S))
    page = HEAD + f"<title>{title}</title>\n" + FONTS + rd("style.part") + shared + rd(part).replace("__DATA__", data) + SW
    out = out or os.path.join(here, default_out)
    open(out, "w").write(page)
    print("wrote", out, len(page))

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    for n in (PAGES if which == "all" else [which]):
        if os.path.exists(os.path.join(here, PAGES[n][1])): build(n, sys.argv[2] if len(sys.argv) > 2 and which != "all" else None)
        else: print("skip", n, "(no", PAGES[n][1] + ")")
