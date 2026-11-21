#!/usr/bin/env python3
"""Lector de la Gaceta Oficial (gacetaoficial.gob.ve) para GazettePro.

Consulta el servicio diario del portal oficial, lee la ficha de cada Gaceta
(tipo, fecha, páginas, estado, sumarios y PDF) y escribe data/gacetas.json,
que la franja de últimas Gacetas lee desde abaxgrid.com.

Uso:
  python scripts/gaceta_feed.py                # últimos 30 días
  DAYS=90 python scripts/gaceta_feed.py        # otro rango
  python scripts/gaceta_feed.py --year 2026    # recalcula faltantes del año

Solo usa la biblioteca estándar de Python.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

BASE = "http://www.gacetaoficial.gob.ve"
UA = "GazettePro/1.0 (+https://abaxgrid.com/gazettepro/; lector de la Gaceta Oficial)"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "gacetas.json")
KEEP = 400          # Gacetas que se conservan en el archivo
PAUSE = 1.0         # segundos entre consultas, para no cargar el portal


def get(path, tries=3):
    url = path if path.startswith("http") else BASE + path
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/html"})
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read().decode("utf-8", "replace")
            time.sleep(PAUSE)
            return body
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"No se pudo leer {url}: {last}")


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


class Tables(HTMLParser):
    """Recoge las filas de cada <table>, el id de la tabla y el enlace al PDF."""

    def __init__(self):
        super().__init__()
        self.tables, self.stack, self.cell, self.row, self.pdf = [], [], None, None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self.stack.append({"id": a.get("id", ""), "rows": []})
        elif tag == "tr" and self.stack:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "object" and "/storage/" in (a.get("data") or ""):
            self.pdf = self.pdf or a["data"].split("#")[0]
        elif tag == "a" and "/storage/" in (a.get("href") or "") and a["href"].lower().endswith(".pdf"):
            self.pdf = self.pdf or a["href"]

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(clean("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None and self.stack:
            if self.row:
                self.stack[-1]["rows"].append(self.row)
            self.row = None
        elif tag == "table" and self.stack:
            self.tables.append(self.stack.pop())

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def to_int(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None


def iso(fecha):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", fecha or "")
    return f"{m[3]}-{m[2]}-{m[1]}" if m else (fecha or "")


def parse_detail(page, numero):
    p = Tables()
    p.feed(page)
    g = {"numero": numero, "url": f"{BASE}/gacetas/{numero}"}
    for t in p.tables:
        head = [c.lower() for c in t["rows"][0]] if t["rows"] else []
        body = t["rows"][1:]
        if "número de gaceta" in head and body:
            r = dict(zip(head, body[0]))
            g["tipo"] = r.get("tipo de gaceta", "").upper()
            g["titulo"] = r.get("título", "")
            g["fecha"] = iso(r.get("fecha de publicación", ""))
            g["paginas"] = [to_int(r.get("página inicial")), to_int(r.get("página final"))]
            g["estado"] = r.get("estado de registro", "")
        elif t["id"] == "sumarios-table" or "órgano" in head:
            g["sumarios"] = [{"organo": c[0], "ente": c[1], "titulo": c[2],
                              "paginas": [to_int(c[3]), to_int(c[4])]}
                             for c in body if len(c) >= 5]
    if p.pdf:
        g["pdf"] = p.pdf if p.pdf.startswith("http") else BASE + p.pdf
    g.setdefault("sumarios", [])
    return g


def day_list(d):
    raw = get(f"/gacetas/dia?year={d.year}&month={d.month}&day={d.day}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Respuesta no JSON para {d}: {raw[:120]!r}")


def calendar_numbers(page):
    """Números por fecha a partir del calendario anual (atributo title de cada día)."""
    out = []
    for y, m, d, t in re.findall(r'data-year="(\d+)"\s*data-month="(\d+)"\s*data-day="(\d+)"[^>]*?title="([^"]*)"', page, re.S):
        if ":" in t:
            for n in re.findall(r"\d+", t.split(":", 1)[1]):
                out.append((f"{y}-{int(m):02d}-{int(d):02d}", int(n)))
    return out


def gaps(nums):
    """Separa la serie ordinaria y la extraordinaria y lista los números ausentes.
    Los valores fuera de la serie (errores de captura) se informan aparte."""
    nums = sorted(set(nums))
    series = {"ordinaria": [n for n in nums if n >= 40000], "extraordinaria": [n for n in nums if n < 40000]}
    res = {}
    for k, ns in series.items():
        if not ns:
            continue
        med = ns[len(ns) // 2]
        core = [n for n in ns if abs(n - med) <= 400]
        odd = [n for n in ns if n not in core]
        have = set(core)
        res[k] = {"desde": core[0], "hasta": core[-1], "en_portal": len(core),
                  "faltantes": [n for n in range(core[0], core[-1] + 1) if n not in have],
                  "fuera_de_serie": odd}
    return res


def main():
    args = sys.argv[1:]
    try:
        feed = json.load(open(OUT, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        feed = {"gacetas": [], "faltantes": {}}
    known = {g["numero"]: g for g in feed.get("gacetas", [])}

    if "--year" in args:
        year = int(args[args.index("--year") + 1])
        nums = [n for _, n in calendar_numbers(get(f"/?year={year}"))]
        feed.setdefault("faltantes", {})[str(year)] = gaps(nums)

    days = int(os.environ.get("DAYS", "30"))
    today = dt.date.today()
    seen = 0
    for i in range(days):
        d = today - dt.timedelta(days=i)
        for item in day_list(d):
            n = to_int(str(item.get("numero_gaceta")))
            if not n:
                continue
            seen += 1
            estado = (item.get("status") or {}).get("nombre", "")
            old = known.get(n)
            if old and old.get("estado") == estado and old.get("sumarios"):
                continue
            g = parse_detail(get(f"/gacetas/{n}"), n)
            g.setdefault("fecha", item.get("fecha_gaceta", ""))
            g["tipo"] = g.get("tipo") or (item.get("categoria") or "").upper()
            g["estado"] = estado or g.get("estado", "")
            g["advertencia"] = item.get("estado_registro_id") == 2
            g["visto"] = old.get("visto") if old else dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            known[n] = g

    feed["gacetas"] = sorted(known.values(), key=lambda g: (g.get("fecha", ""), g["numero"]), reverse=True)[:KEEP]
    feed["fuente"] = BASE
    feed["actualizado"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=1)
    print(f"{seen} Gacetas en {days} días; {len(feed['gacetas'])} en el archivo.")


if __name__ == "__main__":
    main()
