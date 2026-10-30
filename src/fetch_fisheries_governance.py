"""Pipeline medallón de vedas PRODUCE y evidencia biológica IMARPE."""
from __future__ import annotations

import gzip
import html
import json
import re
import unicodedata
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from common import DATA_DIR, USER_AGENT, banner

BASE = "https://www.gob.pe"
SOURCES = {
    "produce_vedas": "https://www.gob.pe/institucion/produce/colecciones/1918-vedas",
    "imarpe_pelagicos": "https://www.gob.pe/institucion/imarpe/colecciones/52287-informes-que-dan-origen-a-r-m-pelagicos",
    "imarpe_demersales": "https://www.gob.pe/institucion/imarpe/colecciones/84268-informes-que-dan-origen-a-r-m-demersales",
    "imarpe_gti": "https://www.gob.pe/institucion/imarpe/colecciones/45774-grupo-de-trabajo-institucional-el-nino-g-t-i",
}
BRONZE = DATA_DIR / "bronze" / "fisheries_governance"
SILVER = DATA_DIR / "silver" / "fisheries_governance.json"
GOLD = DATA_DIR / "gold" / "fisheries_governance.json"

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "setiembre": 9, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
SPECIES = {
    "BONITO": ["bonito", "sarda chiliensis"],
    "POTA": ["pota", "calamar gigante", "dosidicus gigas"],
    "PERICO": ["perico", "dorado", "coryphaena hippurus"],
    "ANCHOVETA": ["anchoveta", "engraulis ringens"],
    "MERLUZA": ["merluza", "merluccius gayi"],
    "JUREL": ["jurel", "trachurus murphyi"],
    "CABALLA": ["caballa", "scomber japonicus"],
    "PEJERREY": ["pejerrey", "odontesthes regia"],
    "CHORO": ["choro", "aulacomya atra"],
    "CARACOL": ["caracol", "thaisella chocolata"],
    "PULPO": ["pulpo", "octopus mimus"],
    "TIBURON MARTILLO": ["tiburón martillo", "tiburon martillo", "sphyrna zygaena"],
    "CAMARON DE RIO": ["camarón de río", "camaron de rio", "macrobrachium caementarius"],
    "CONCHA DE ABANICO": ["concha de abanico", "argopecten purpuratus"],
    "ERIZO": ["erizo", "loxechinus albus"],
    "ANGUILA": ["anguila", "ophichthus remiger"],
}

# Reglas recurrentes verificables del calendario oficial de PRODUCE. Se mantienen
# separadas de resoluciones coyunturales para no extrapolar una suspensión temporal.
CALENDAR_RULES = [
    {
        "species_key": "PERICO", "species": "Perico o dorado", "start_month": 5, "start_day": 1,
        "end_month": 9, "end_day": 30, "scope": "Nacional", "resolution": "R.M. N.° 245-2014-PRODUCE",
        "source_url": "https://pescayconsumoresponsable.produce.gob.pe/vedas.html",
    },
    {
        "species_key": "CONCHA PATA DE BURRO", "species": "Concha pata de burro", "start_month": 2,
        "start_day": 1, "end_month": 3, "end_day": 31, "scope": "Nacional",
        "resolution": "R.M. N.° 107-2022-PRODUCE",
        "source_url": "https://pescayconsumoresponsable.produce.gob.pe/vedas.html",
    },
    {
        "species_key": "PAICHE", "species": "Paiche", "start_month": 10, "start_day": 1,
        "end_month": 2, "end_day": 28, "scope": "Cuerpos de agua públicos, excepto Putumayo",
        "resolution": "R.M. N.° 215-2001-PE",
        "source_url": "https://pescayconsumoresponsable.produce.gob.pe/vedas.html",
    },
]

CARD = re.compile(
    r'<div class="border-b border-gray-700 pb-2 font-medium mb-3">\s*(.*?)\s*</div>'
    r'<a class="leading-6 font-bold" href="([^"]+)">(.*?)</a>(.*?)'
    r'<div class="mt-3 leading-5 text-sm">', re.I | re.S,
)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value or "") if not unicodedata.combining(c)).casefold()


def parse_date(value: str) -> date | None:
    match = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", fold(value))
    if not match or match.group(2) not in MONTHS:
        return None
    return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1)))


def species_matches(text: str) -> list[str]:
    normalized = fold(text)
    return [key for key, aliases in SPECIES.items() if any(fold(alias) in normalized for alias in aliases)]


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT or "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=90) as response:
        raw = response.read()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1252", "replace")


def parse_cards(source: str, source_url: str, body: str) -> list[dict]:
    records = []
    for published, href, title, summary in CARD.findall(body):
        day = parse_date(clean(published)); title = clean(title); summary = clean(summary)
        text = f"{title} {summary}"
        records.append({
            "source": source, "published_at": day.isoformat() if day else None,
            "title": title, "summary": summary, "url": urllib.request.urljoin(BASE, href),
            "source_index": source_url, "species_keys": species_matches(text),
            "measure_type": "veda_o_suspension" if re.search(r"veda|suspend|concluir.*temporada", fold(text)) else "documento_cientifico",
            "extraction_method": "official_index_html", "confidence": "alta" if day and title else "media",
        })
    return records


def meta_description(body: str) -> str:
    tag = next((value for value in re.findall(r"<meta\b[^>]*>", body, re.I | re.S)
                if re.search(r'name=["\']description["\']', value, re.I)), "")
    match = re.search(r'content=["\'](.*?)["\']', tag, re.I | re.S)
    return clean(match.group(1)) if match else ""


def explicit_period(text: str) -> tuple[date, date] | None:
    """Extrae periodos expresos; no infiere vigencias desde la fecha de publicación."""
    normalized = fold(text)
    same_month = re.search(
        r"(?:del|desde el)\s+(\d{1,2})\s+(?:al|hasta el)\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})",
        normalized,
    )
    if same_month and same_month.group(3) in MONTHS:
        month, year = MONTHS[same_month.group(3)], int(same_month.group(4))
        return date(year, month, int(same_month.group(1))), date(year, month, int(same_month.group(2)))
    full = re.search(
        r"(?:del|desde el)\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4}).{0,80}?"
        r"(?:al|hasta el)\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})",
        normalized,
    )
    if full and full.group(2) in MONTHS and full.group(5) in MONTHS:
        return (
            date(int(full.group(3)), MONTHS[full.group(2)], int(full.group(1))),
            date(int(full.group(6)), MONTHS[full.group(5)], int(full.group(4))),
        )
    return None


def enrich_regulations(regulations: list[dict], failures: dict) -> None:
    details = BRONZE / "resolution_details"
    details.mkdir(parents=True, exist_ok=True)
    for item in regulations:
        if item["measure_type"] != "veda_o_suspension" or not item["species_keys"]:
            continue
        slug = item["url"].rstrip("/").rsplit("/", 1)[-1]
        path = details / f"{slug}.html.gz"
        try:
            body = fetch(item["url"])
            with gzip.open(path, "wt", encoding="utf-8") as stream:
                stream.write(body)
        except Exception as exc:  # noqa: BLE001
            failures[f"detail:{slug}"] = str(exc)
            if not path.exists():
                continue
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                body = stream.read()
        detail = meta_description(body)
        if detail:
            item["official_description"] = detail
        period = explicit_period(f"{item['title']} {item['summary']} {detail}")
        if period:
            item["effective_period"] = {"start_date": period[0].isoformat(), "end_date": period[1].isoformat()}


def calendar_window(rule: dict, today: date) -> tuple[date, date, str, int]:
    start = date(today.year, rule["start_month"], rule["start_day"])
    end_year = today.year + (1 if (rule["end_month"], rule["end_day"]) < (rule["start_month"], rule["start_day"]) else 0)
    end = date(end_year, rule["end_month"], rule["end_day"])
    if today > end:
        start = date(today.year + 1, rule["start_month"], rule["start_day"])
        end_year = start.year + (1 if (rule["end_month"], rule["end_day"]) < (rule["start_month"], rule["start_day"]) else 0)
        end = date(end_year, rule["end_month"], rule["end_day"])
    elif today < start and end_year > today.year:
        previous_start = date(today.year - 1, rule["start_month"], rule["start_day"])
        previous_end = date(today.year, rule["end_month"], rule["end_day"])
        if previous_start <= today <= previous_end:
            start, end = previous_start, previous_end
    active = start <= today <= end
    status = "En veda" if active else "Próxima" if 0 <= (start - today).days <= 30 else "Fuera de veda calendarizada"
    days = (end - today).days if active else (start - today).days
    return start, end, status, days


def main() -> int:
    banner("Actualizando gobierno pesquero PRODUCE / IMARPE")
    now = datetime.now(ZoneInfo("America/Lima")); fetched = {}; failures = {}
    BRONZE.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        try:
            body = fetch(url); fetched[name] = body
            with gzip.open(BRONZE / f"{name}.html.gz", "wt", encoding="utf-8") as stream:
                stream.write(body)
        except Exception as exc:  # noqa: BLE001
            failures[name] = str(exc)
            path = BRONZE / f"{name}.html.gz"
            if path.exists():
                with gzip.open(path, "rt", encoding="utf-8") as stream:
                    fetched[name] = stream.read()

    manifest = {
        "layer": "bronze", "fetched_at": now.isoformat(timespec="seconds"), "sources": SOURCES,
        "successful": sorted(fetched), "failures": failures,
    }
    (BRONZE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if "produce_vedas" not in fetched:
        print("  PRODUCE no disponible y no existe Bronze reutilizable")
        return 0 if GOLD.exists() else 1

    regulations = parse_cards("PRODUCE", SOURCES["produce_vedas"], fetched["produce_vedas"])
    enrich_regulations(regulations, failures)
    science = []
    for name in ("imarpe_pelagicos", "imarpe_demersales", "imarpe_gti"):
        if name in fetched:
            science.extend(parse_cards("IMARPE", SOURCES[name], fetched[name]))
    silver = {
        "contract_version": 1, "layer": "silver", "generated_at": now.isoformat(timespec="seconds"),
        "regulations": regulations, "science_documents": science, "calendar_rules": CALENDAR_RULES,
        "quality": {"regulation_records": len(regulations), "science_records": len(science),
                    "dated_records": sum(bool(x["published_at"]) for x in regulations + science),
                    "species_linked_records": sum(bool(x["species_keys"]) for x in regulations + science)},
        "lineage": manifest,
    }
    SILVER.parent.mkdir(parents=True, exist_ok=True)
    SILVER.write_text(json.dumps(silver, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    today = now.date(); keys = set(SPECIES) | {rule["species_key"] for rule in CALENDAR_RULES}
    species_rows = []
    for key in sorted(keys):
        rules = []
        for rule in CALENDAR_RULES:
            if rule["species_key"] != key:
                continue
            start, end, status, days = calendar_window(rule, today)
            rules.append({**rule, "start_date": start.isoformat(), "end_date": end.isoformat(),
                          "status": status, "days_to_change": days, "confidence": "alta",
                          "extraction_method": "curated_official_calendar"})
        related_regulations = sorted(
            (item for item in regulations if key in item["species_keys"]),
            key=lambda item: item["published_at"] or "", reverse=True,
        )[:5]
        related_science = sorted(
            (item for item in science if key in item["species_keys"]),
            key=lambda item: item["published_at"] or "", reverse=True,
        )[:5]
        for item in related_regulations:
            period = item.get("effective_period")
            if not period:
                continue
            start, end = date.fromisoformat(period["start_date"]), date.fromisoformat(period["end_date"])
            status = "En veda" if start <= today <= end else "Próxima" if today < start and (start - today).days <= 30 else "Finalizada"
            rules.append({
                "species_key": key, "species": key.title(), "start_date": start.isoformat(),
                "end_date": end.isoformat(), "scope": "Según texto de la resolución",
                "resolution": item["title"], "source_url": item["url"], "status": status,
                "days_to_change": (end - today).days if status == "En veda" else (start - today).days,
                "confidence": "alta", "extraction_method": "official_resolution_explicit_period",
            })
        active = next((rule for rule in rules if rule["status"] == "En veda"), None)
        upcoming = next((rule for rule in rules if rule["status"] == "Próxima"), None)
        regulatory_status = "En veda" if active else "Próxima veda" if upcoming else "Sin veda recurrente activa"
        species_rows.append({
            "species_key": key, "regulatory_status": regulatory_status,
            "calendar": rules, "recent_regulations": related_regulations,
            "science_evidence": related_science,
            "science_status": "Evidencia IMARPE disponible" if related_science else "Sin informe específico indexado",
        })
    gold = {
        "contract_version": 1, "layer": "gold", "generated_at": now.isoformat(timespec="seconds"),
        "as_of_date": today.isoformat(), "species": species_rows,
        "recent_regulations": sorted(regulations, key=lambda x: x["published_at"] or "", reverse=True)[:12],
        "recent_science": sorted(science, key=lambda x: x["published_at"] or "", reverse=True)[:12],
        "quality": silver["quality"], "lineage": {"silver": str(SILVER.relative_to(DATA_DIR.parent)), "sources": SOURCES},
        "disclaimer": "La norma legal y la evidencia científica se muestran separadas. Un informe IMARPE disponible no implica por sí solo disponibilidad favorable.",
    }
    GOLD.write_text(json.dumps(gold, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  -> Bronze {len(fetched)}/{len(SOURCES)} fuentes")
    print(f"  -> Silver {len(regulations)} normas, {len(science)} documentos IMARPE")
    print(f"  -> Gold {len(species_rows)} especies reguladas/monitoreadas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
