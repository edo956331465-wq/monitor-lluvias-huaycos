"""Descarga precios y procedencias del módulo mayorista de SISAP.

La fuente es un formulario HTML legado. Se guarda la última respuesta válida en
data/cache/sisap_latest.json para que una caída temporal de MIDAGRI no rompa el sitio.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from common import CACHE_DIR, USER_AGENT, banner

URL = "http://sistemas.midagri.gob.pe/sisap/portal2/mayorista/resumenes/filtrar"
CACHE = CACHE_DIR / "sisap_latest.json"

MARKETS = {
    "15011501": "Gran Mercado Mayorista de Lima",
    "15011503": "Mercado Modelo de Frutas",
    "15013704": "Mercado de Productores de Santa Anita",
}

PRODUCTS = {
    "0611": "Limón", "0212": "Cebolla", "0202": "Ají fresco",
    "0215": "Culantro", "0626": "Palta", "0615": "Mango",
    "0614": "Mandarina", "0622": "Naranja", "0628": "Piña",
    "0627": "Papaya", "0629": "Plátano", "0228": "Tomate",
    "0104": "Papa", "0101": "Camote", "0401": "Arroz",
}


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self.row: list[dict] | None = None
        self.cell: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            a = dict(attrs)
            self.cell = {"text": "", "rowspan": int(a.get("rowspan", "1"))}
        elif tag == "br" and self.cell is not None:
            self.cell["text"] += " "

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.cell["text"] = re.sub(r"\s+", " ", self.cell["text"]).strip()
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def expand_rows(raw_rows: list[list[dict]]) -> list[list[str]]:
    expanded: list[list[str]] = []
    spans: dict[int, list] = {}
    for raw in raw_rows:
        row: list[str] = []
        source = iter(raw)
        col = 0
        while col < 7:
            if col in spans:
                remaining, value = spans[col]
                row.append(value)
                if remaining <= 1:
                    del spans[col]
                else:
                    spans[col][0] -= 1
            else:
                try:
                    cell = next(source)
                except StopIteration:
                    row.append("")
                    col += 1
                    continue
                value = cell["text"]
                row.append(value)
                if cell["rowspan"] > 1:
                    spans[col] = [cell["rowspan"] - 1, value]
            col += 1
        expanded.append(row)
    return expanded


def fetch_market(code: str, day: str) -> list[dict]:
    params: list[tuple[str, str]] = [
        ("mercado", code), ("fecha", day), ("desde", day), ("hasta", day),
        ("periodicidad", "dia"),
    ]
    params += [("productos[]", product) for product in PRODUCTS]
    params += [("variables[]", variable) for variable in
               ("precio_min", "precio_prom", "precio_max", "volumen")]
    params.append(("__ajax_carga_final", "consulta"))
    request = urllib.request.Request(
        URL,
        data=urllib.parse.urlencode(params).encode(),
        headers={
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "http://sistemas.midagri.gob.pe/sisap/portal2/mayorista/",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", "replace")
    if "mensajeDeError" in body and "<table" not in body:
        raise RuntimeError(re.sub(r"<[^>]+>", " ", html.unescape(body)).strip())

    parser = TableParser()
    parser.feed(body)
    rows = expand_rows(parser.rows)
    result = []
    for row in rows:
        if not row or row[0].lower() == "producto":
            continue
        try:
            values = [float(row[i]) if row[i] not in ("", "-") else None for i in range(2, 6)]
        except ValueError:
            continue
        result.append({
            "market_code": code,
            "market": MARKETS[code],
            "product": row[0],
            "variety": row[1],
            "price_min": values[0],
            "price_avg": values[1],
            "price_max": values[2],
            "volume_t": values[3],
            "origin_province": row[6] or "Procedencia desconocida",
        })
    return result


def main() -> int:
    banner("Descargando precios y procedencias SISAP")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo("America/Lima"))
    records: list[dict] = []
    errors: list[str] = []
    market_dates: dict[str, str] = {}
    for code, name in MARKETS.items():
        last_error = "sin resultados"
        for offset in range(8):
            day = (now.date() - timedelta(days=offset)).strftime("%d/%m/%Y")
            try:
                batch = fetch_market(code, day)
                if batch:
                    for record in batch:
                        record["date"] = day
                    records.extend(batch)
                    market_dates[name] = day
                    print(f"  {name}: {len(batch)} filas ({day})")
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                # Solo retrocedemos de fecha cuando la fuente respondió que ese
                # día no tuvo datos. Un timeout se corta de inmediato y usa caché.
                if "No existen resultados" not in last_error:
                    break
        else:
            errors.append(f"{name}: {last_error}")
            print(f"  {name}: FALLO ({last_error})")
        time.sleep(0.8)

    if records:
        payload = {
            "source": "SISAP · MIDAGRI",
            "source_url": "http://sistemas.midagri.gob.pe/sisap/portal2/mayorista/",
            "date": max(market_dates.values(), default=now.strftime("%d/%m/%Y")),
            "market_dates": market_dates,
            "fetched_at": now.isoformat(timespec="minutes"),
            "records": records,
            "errors": errors,
        }
        CACHE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  -> {CACHE} ({len(records)} filas)")
        return 0
    if CACHE.exists():
        print("  SISAP no respondió; se conserva la última descarga válida.")
        return 0
    print("  No hay datos ni respaldo SISAP disponible.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
