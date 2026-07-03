#!/usr/bin/env python3
"""
iCal-Alarmos-Webservice

Ruft bei jedem HTTP-Request live den Feuernetz-ICS-Kalender ab, filtert die
Termine auf ein konfigurierbares Zeitfenster und liefert sie als gerenderte
HTML-Seite aus. Es findet kein Caching und kein Datei-Upload (SFTP) mehr
statt - der Client ruft die Seite direkt über diesen Webservice ab.

Start (Entwicklung):
    uvicorn app:app --host 0.0.0.0 --port 8081 --reload

Start (Produktion):
    siehe calendar-webservice.service
"""

import json
import logging
import os
import time as systime
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import requests
import yaml
from dateutil import tz
from typing import Optional

from fastapi import FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from icalendar import Calendar
from jinja2 import Environment, FileSystemLoader, select_autoescape

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", BASE_DIR / "config.yaml"))

GERMAN_MONTHS = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April", 5: "Mai", 6: "Juni",
    7: "Juli", 8: "August", 9: "September", 10: "Oktober", 11: "November", 12: "Dezember",
}


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Erlaubt, das ICS-URL-Secret ohne Anfassen der YAML zu überschreiben
    # (z.B. per systemd EnvironmentFile oder Secrets-Manager)
    env_url = os.environ.get("ICAL_URL")
    if env_url:
        cfg["ical"]["url"] = env_url

    return cfg


config = load_config()

logging.basicConfig(
    level=getattr(logging, config.get("logging", {}).get("level", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ical-alarmos")

app = FastAPI(title="iCal Alarmos Webservice")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.get("service", {}).get("cors_allow_origins", ["*"]),
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

jinja_env = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=select_autoescape(["html", "j2"]),  # verhindert HTML-Injection aus Termin-Texten
)
template = jinja_env.get_template("calendar.html.j2")


def get_cache_path() -> Optional[Path]:
    cache_cfg = config.get("cache", {})
    if not cache_cfg.get("enabled", True):
        return None
    raw_path = Path(cache_cfg.get("file_path", "cache/last_calendar.json"))
    return raw_path if raw_path.is_absolute() else BASE_DIR / raw_path


def save_cache(ics_text: str) -> None:
    """Speichert den zuletzt erfolgreich abgerufenen ICS-Text atomar (tmp + rename)."""
    cache_path = get_cache_path()
    if cache_path is None:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": datetime.now(tz.tzutc()).isoformat(),
            "ics_text": ics_text,
        }
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        tmp_path.replace(cache_path)
    except OSError:
        logger.exception("Konnte Kalender-Cache nicht schreiben (%s)", cache_path)


def load_cache() -> Optional[tuple[str, datetime]]:
    """Lädt den zuletzt gecachten ICS-Text, falls vorhanden."""
    cache_path = get_cache_path()
    if cache_path is None or not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        return payload["ics_text"], fetched_at
    except (OSError, ValueError, KeyError):
        logger.exception("Konnte Kalender-Cache nicht lesen (%s)", cache_path)
        return None


def fetch_ics_text() -> str:
    """Ruft den ICS-Feed ab, mit Timeout und konfigurierbaren Retries."""
    ical_cfg = config["ical"]
    url = ical_cfg["url"]
    timeout = ical_cfg.get("timeout_seconds", 10)
    retries = ical_cfg.get("fetch_retries", 2)
    delay = ical_cfg.get("retry_delay_seconds", 2)

    last_error = None
    for attempt in range(1, retries + 2):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            return r.text
        except requests.RequestException as exc:
            last_error = exc
            logger.warning(
                "ICS-Abruf fehlgeschlagen (Versuch %s/%s): %s", attempt, retries + 1, exc
            )
            if attempt <= retries:
                systime.sleep(delay)

    raise last_error


def get_ics_text() -> tuple[str, bool, Optional[datetime]]:
    """
    Liefert (ics_text, is_stale, as_of).

    Gelingt der Live-Abruf, wird das Ergebnis zusätzlich gecacht und
    (ics_text, False, None) zurückgegeben. Schlägt er fehl, wird versucht,
    auf den zuletzt gecachten Stand auszuweichen: (ics_text, True, fetched_at).
    Ist auch kein Cache vorhanden, wird der ursprüngliche Fehler weitergereicht.
    """
    try:
        ics_text = fetch_ics_text()
        save_cache(ics_text)
        return ics_text, False, None
    except requests.RequestException as exc:
        cached = load_cache()
        if cached is None:
            raise
        ics_text, fetched_at = cached
        logger.warning(
            "ICS-Quelle nicht erreichbar (%s) - liefere gecachten Stand vom %s aus",
            exc, fetched_at.isoformat(),
        )
        return ics_text, True, fetched_at


def parse_events(ics_text: str, weeks_ahead: int) -> list[dict]:
    """Parst das ICS und filtert auf den Zeitraum [heute 00:00, heute + weeks_ahead]."""
    cal = Calendar.from_ical(ics_text)
    local_tz = tz.tzlocal()
    now = datetime.now(local_tz)
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(weeks=weeks_ahead)

    events = []
    for component in cal.walk("VEVENT"):
        start = component.decoded("DTSTART")
        end = component.decoded("DTEND")

        if not isinstance(start, datetime):
            # Ganztägige Events
            start = datetime.combine(start, dtime.min, tzinfo=local_tz)
            end = datetime.combine(end, dtime.min, tzinfo=local_tz)
        else:
            start = start.astimezone(local_tz)
            end = end.astimezone(local_tz)

        if not (window_start <= start <= window_end):
            continue

        events.append({
            "summary": str(component.get("SUMMARY") or ""),
            "location": str(component.get("LOCATION") or ""),
            "day": start.strftime("%d"),
            "month": GERMAN_MONTHS[start.month],
            "start_dt": start,
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
        })

    events.sort(key=lambda e: e["start_dt"])
    return events

def limit_events_by_height(events: list[dict], height_px: Optional[int]) -> list[dict]:
    """
    Begrenzt die Termine so, dass nur vollständige Termin-Boxen in ein
    gegebenes Höhen-Budget (in Pixeln) passen. Weitere, in der Zukunft
    liegende Termine werden abgeschnitten statt teilweise angezeigt.

    Die Boxhöhe/der Abstand müssen zum tatsächlichen CSS (static/style.css)
    passen - siehe layout.event_height_px / layout.gap_px in config.yaml.
    """
    if height_px is None:
        return events

    layout_cfg = config.get("layout", {})
    event_height = layout_cfg.get("event_height_px", 76)
    gap = layout_cfg.get("gap_px", 8)

    if height_px < event_height:
        return []

    # n Boxen + (n-1) Lücken <= height_px  =>  n <= (height_px + gap) / (event_height + gap)
    max_events = (height_px + gap) // (event_height + gap)
    return events[: max(max_events, 0)]

def render_unavailable(reason: str) -> Response:
    """Rendert eine Hinweisseite statt eines rohen 502, wenn weder Live-Daten
    noch ein Cache-Stand verfügbar sind (z.B. direkt nach Ersteinrichtung ohne
    Netzverbindung, oder wenn der ICS-Feed nicht geparst werden kann)."""
    html = template.render(events=[], is_stale=False, as_of=None, is_unavailable=True)
    logger.error("Keine Kalenderdaten ausgeliefert: %s", reason)
    response = Response(content=html, media_type="text/html; charset=utf-8", status_code=200)
    response.headers["X-Calendar-Cache"] = "unavailable"
    return response


@app.get("/calendar.html", response_class=Response)
def get_calendar(
    height_px: Optional[int] = Query(
        default=None,
        ge=0,
        description="Verfügbare Höhe in Pixeln. Wenn gesetzt, werden nur so viele "
        "vollständige Termin-Boxen ausgegeben, wie hineinpassen; der Rest wird abgeschnitten.",
    )
):
    try:
        ics_text, is_stale, as_of = get_ics_text()
    except requests.RequestException as exc:
        return render_unavailable(f"ICS-Quelle nicht erreichbar und kein Cache vorhanden: {exc}")

    try:
        events = parse_events(ics_text, config["ical"].get("weeks_ahead", 4))
    except Exception as exc:
        return render_unavailable(f"Fehler beim Parsen des ICS-Feeds: {exc}")

    events = limit_events_by_height(events, height_px)

    local_as_of = as_of.astimezone(tz.tzlocal()) if as_of else None
    html = template.render(events=events, is_stale=is_stale, as_of=local_as_of, is_unavailable=False)
    logger.info(
        "Kalender ausgeliefert (%s Termine, height_px=%s, stale=%s)",
        len(events), height_px, is_stale,
    )
    response = Response(content=html, media_type="text/html; charset=utf-8")
    response.headers["X-Calendar-Cache"] = "stale" if is_stale else "live"
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=config.get("service", {}).get("host", "0.0.0.0"),
        port=config.get("service", {}).get("port", 8081),
    )
