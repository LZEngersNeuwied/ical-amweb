# iCal-AMWeb-Webservice

Ruft bei jedem Request live den Feuernetz-ICS-Kalender ab und liefert ihn als HTML-Seite aus. Der Client fragt den Webservice direkt ab.

Ist die Kalender-Quelle gerade nicht erreichbar (Timeout, Netzwerkfehler, HTTP-Fehler), liefert der Dienst automatisch den zuletzt erfolgreich abgerufenen Kalenderstand aus dem Cache aus, statt einen Fehler zu zeigen. Sobald ein neuer Abruf wieder gelingt, wird der Cache automatisch aktualisiert. Auf der Seite erscheint dann ein dezenter Hinweis mit Zeitstempel des angezeigten Standes; per Response-Header `X-Calendar-Cache: live|stale` lässt sich das auch maschinell auswerten. Liegt weder ein Live-Abruf noch ein Cache-Stand vor (z. B. direkt nach der Ersteinrichtung ohne Netzverbindung) oder kann der ICS-Feed nicht verarbeitet werden, liefert der Dienst weiterhin `200 OK` mit einer Hinweisseite ("Aktuell können keine Kalenderdaten angezeigt werden."), erkennbar am Header `X-Calendar-Cache: unavailable`.

## Setup (lokal / zum Testen)

```bash
cd ical-amweb
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

uvicorn app:app --host 0.0.0.0 --port 8081 --reload
```

Aufruf im Browser: `http://<server>:8081/calendar.html`

### Auf verfügbare Höhe begrenzen

Optional kann per Query-Parameter `height_px` eine verfügbare Höhe in Pixeln
angegeben werden. Es werden dann nur so viele **vollständige** Termin-Boxen
ausgegeben, wie hineinpassen — alle weiteren (zeitlich späteren) Termine
werden abgeschnitten, nicht abgeschnitten dargestellt:

```
http://<server>:8081/?height_px=600
```

Die Berechnung basiert auf `layout.event_height_px` und `layout.gap_px` in
`config.yaml`, die zur festen Boxhöhe in `static/style.css` passen müssen.
Wird `height_px` nicht angegeben, werden alle Termine im Zeitfenster
(`ical.weeks_ahead`) angezeigt.

## Konfiguration

Alle Einstellungen liegen in `config.yaml`:

- `ical.url` — die ICS-Feed-URL von FeuerNetz
- `ical.weeks_ahead` — Zeitfenster in Wochen ab heute (aktuell: 8)
- `ical.timeout_seconds` / `fetch_retries` / `retry_delay_seconds` — Verhalten bei Abruf-Fehlern
- `service.port` — Port des Webservice (Standard 8081)
- `service.cors_allow_origins` — erlaubte Origins für CORS
- `cache.enabled` — Cache-Fallback aktivieren/deaktivieren (Standard: an)
- `cache.file_path` — Pfad zur Cache-Datei (Standard: `cache/last_calendar.json`, relativ zum Projektverzeichnis)

Die ICS-URL (enthält ein personenbezogenes Token) kann alternativ per
Umgebungsvariable überschrieben werden, ohne `config.yaml` anzufassen:

```bash
export ICAL_URL="https://einheit.feuernetz.de/..."
```

## Produktivbetrieb (systemd)

1. Projekt nach `/opt/ical-amweb` kopieren
2. Virtualenv dort anlegen (`python3 -m venv venv && venv/bin/pip install -r requirements.txt`)
3. Systembenutzer anlegen
   
   ````bash
   sudo useradd \
    --system \
    --home /opt/ical-amweb \
    --shell /usr/sbin/nologin \
    ical
    ````
4. Berechtigungen setzen

   ````bash
   sudo chown -R ical:ical /opt/ical-amweb && \
   sudo find /opt/ical-amweb -type d -exec chmod 755 {} \; && \
   sudo find /opt/ical-amweb -type f -exec chmod 644 {} \; && \
   sudo chmod 755 /opt/ical-amweb/venv/bin/python && \
   sudo chown -R ical:ical /opt/ical-amweb/venv
   ````
5. `calendar-webservice.service` nach `/etc/systemd/system/` kopieren
6. Danach:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now calendar-webservice
sudo systemctl status calendar-webservice
```

Logs: `journalctl -u calendar-webservice -f`

### Einrichtung im AMWeb

Wenn der Kalender lokal bereitgestellt wird und nicht durch einen Reverse-Proxy über https aufgerufen wird, muss im Browser die Anzeige von gemischten Inhalten (https und http auf einer Seite) ativiert werden.

Für Firefox:

1. Geben Sie about:config in die Firefox-Adressleiste ein und drücken Sie Enter.
2. Bestätigen Sie den Warnhinweis („Risiko akzeptieren und fortfahren“).
3. Suchen Sie in der oberen Suchleiste nach folgendem Eintrag: security.mixed_content.block_active_content.
4. Klicken Sie doppelt auf den Eintrag (oder nutzen Sie das Pfeil-Symbol rechts), um den Wert von true auf false zu ändern.

**Hinweis: Dies reduziert die allgemeine Sicherheit Ihres Browsers beim Surfen im Internet.**