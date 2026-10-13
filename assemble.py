"""Combine the page template with data.json into one self-contained index.html."""
import os, sys
here = os.path.dirname(os.path.abspath(__file__))
data = open(os.path.join(here, "data.json")).read().replace("</", "<\\/")
page = ((('<!doctype html>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
         '<style>body{margin:0;font-size:14px}img{max-width:100%}[hidden]{display:none!important}</style>\n')
        if sys.argv[1:] and sys.argv[1].endswith("index.html") else "") + "<title>Overlay NFL</title>\n"
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">\n'
        + open(os.path.join(here, "style.part")).read()
        + open(os.path.join(here, "body.part")).read().replace("__DATA__", data))
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "index.html")
open(out, "w").write(page)
print("wrote", out, len(page))
