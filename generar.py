#!/usr/bin/env python3
"""
Genera un archivo .ics por equipo a partir de la configuración en equipos.yaml,
raspando la web de la FBM. Además crea un index.html con los enlaces de
suscripción, pensado para publicarse en GitHub Pages.

Cada evento usa un UID estable (equipo + enfrentamiento), de modo que cuando un
partido cambia de fecha, hora o pabellón, el evento se ACTUALIZA en el
calendario suscrito en lugar de duplicarse.

Uso:
    python3 generar.py                     # usa equipos.yaml y escribe en ./public
    python3 generar.py --config otro.yaml --salida ./dist
    python3 generar.py --base-url https://usuario.github.io/repo
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests
import yaml
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (fbm-calendar)"
TIMEOUT = 30
FBM_URL = "https://www.fbm.es/resultados-club-{id}/{slug}"


# --------------------------------------------------------------------------- #
# MODELO
# --------------------------------------------------------------------------- #

@dataclass
class Partido:
    categoria: str
    local: str
    visitante: str
    inicio: datetime | None
    tiene_hora: bool
    pabellon: str
    direccion: str

    def uid(self, equipo_id: str) -> str:
        base = f"{equipo_id}|{self.categoria}|{self.local}|{self.visitante}"
        h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]
        return f"fbm-{h}@fbm-calendar"

    def titulo(self, etiqueta: str) -> str:
        return f"🏀 {self.local} vs {self.visitante} ({etiqueta})"

    def ubicacion(self) -> str:
        return ", ".join(p for p in (self.pabellon, self.direccion) if p)


@dataclass
class Equipo:
    id: str
    nombre: str
    club: str
    categoria: str
    equipo_contiene: str = ""
    partidos: list[Partido] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# UTILIDADES
# --------------------------------------------------------------------------- #

def _normaliza(texto: str) -> str:
    texto = texto.lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u")):
        texto = texto.replace(a, b)
    return texto


def _celda_lineas(celda) -> list[str]:
    for br in celda.find_all("br"):
        br.replace_with("\n")
    return [l.strip() for l in celda.get_text("\n").split("\n") if l.strip()]


def _parse_fecha(texto: str) -> tuple[datetime | None, bool]:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", texto.strip())
    if not m:
        return None, False
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if m.group(4) is not None:
        return datetime(y, mo, d, int(m.group(4)), int(m.group(5))), True
    return datetime(y, mo, d), False


# --------------------------------------------------------------------------- #
# SCRAPING
# --------------------------------------------------------------------------- #

def descargar_club(club: dict, sesion: requests.Session) -> str:
    url = FBM_URL.format(id=club["id"], slug=club["slug"])
    r = sesion.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def extraer_partidos(html: str) -> list[Partido]:
    soup = BeautifulSoup(html, "html.parser")
    cont = soup.find("div", id="proximosPartidos")
    if cont is None:
        raise RuntimeError("No se encontró 'proximosPartidos' (¿cambió la web de la FBM?).")
    tabla = cont.find("table")
    if tabla is None:
        raise RuntimeError("No se encontró la tabla de próximos partidos.")

    partidos: list[Partido] = []
    for fila in tabla.find_all("tr"):
        celdas = fila.find_all("td")
        if len(celdas) != 4:
            continue
        cat = " ".join(_celda_lineas(celdas[0]))
        enc = _celda_lineas(celdas[1])
        campo = _celda_lineas(celdas[3])
        inicio, tiene_hora = _parse_fecha(celdas[2].get_text(" ", strip=True))
        partidos.append(
            Partido(
                categoria=cat,
                local=enc[0] if enc else "",
                visitante=enc[1] if len(enc) > 1 else "",
                inicio=inicio,
                tiene_hora=tiene_hora,
                pabellon=campo[0] if campo else "",
                direccion=" ".join(campo[1:]) if len(campo) > 1 else "",
            )
        )
    return partidos


def filtrar(partidos: list[Partido], equipo: Equipo) -> list[Partido]:
    pref = _normaliza(equipo.categoria)
    quiere = _normaliza(equipo.equipo_contiene) if equipo.equipo_contiene else ""
    sel = []
    for p in partidos:
        if not _normaliza(p.categoria).startswith(pref):
            continue
        if "descansa" in _normaliza(p.local) or "descansa" in _normaliza(p.visitante):
            continue
        if quiere and quiere not in _normaliza(f"{p.local} {p.visitante}"):
            continue
        sel.append(p)
    return sel


# --------------------------------------------------------------------------- #
# ICS
# --------------------------------------------------------------------------- #

def _escapar(t: str) -> str:
    return t.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _plegar(linea: str) -> str:
    out, raw = [], linea.encode("utf-8")
    while len(raw) > 75:
        corte = 75
        while corte > 0 and (raw[corte] & 0xC0) == 0x80:
            corte -= 1
        out.append(raw[:corte].decode("utf-8"))
        raw = b" " + raw[corte:]
    out.append(raw.decode("utf-8"))
    return "\r\n".join(out)


def _dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def _vtimezone(tzid: str) -> list[str]:
    # Definición CET/CEST (válida para Europe/Madrid).
    return [
        "BEGIN:VTIMEZONE", f"TZID:{tzid}",
        "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "TZNAME:CEST",
        "DTSTART:19700329T020000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
        "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "TZNAME:CET",
        "DTSTART:19701025T030000", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD",
        "END:VTIMEZONE",
    ]


def generar_ics(equipo: Equipo, ajustes: dict) -> str:
    tzid = ajustes.get("tzid", "Europe/Madrid")
    dur = int(ajustes.get("duracion_minutos", 90))
    aviso = int(ajustes.get("aviso_horas_antes", 2))
    ahora = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lineas = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//fbm-calendar//ES", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escapar(equipo.nombre)}",
        f"X-WR-TIMEZONE:{tzid}",
        *_vtimezone(tzid),
    ]
    for p in equipo.partidos:
        if p.inicio is None:
            continue
        lineas += ["BEGIN:VEVENT", f"UID:{p.uid(equipo.id)}", f"DTSTAMP:{ahora}"]
        if p.tiene_hora:
            fin = p.inicio + timedelta(minutes=dur)
            lineas += [f"DTSTART;TZID={tzid}:{_dt(p.inicio)}", f"DTEND;TZID={tzid}:{_dt(fin)}"]
        else:
            fin = p.inicio + timedelta(days=1)
            lineas += [f"DTSTART;VALUE=DATE:{p.inicio.strftime('%Y%m%d')}",
                       f"DTEND;VALUE=DATE:{fin.strftime('%Y%m%d')}"]
        lineas.append(f"SUMMARY:{_escapar(p.titulo(equipo.nombre))}")
        if p.ubicacion():
            lineas.append(f"LOCATION:{_escapar(p.ubicacion())}")
        desc = f"Categoría: {p.categoria}\nLocal: {p.local}\nVisitante: {p.visitante}"
        if not p.tiene_hora:
            desc += "\n(Hora aún no publicada por la FBM)"
        lineas.append(f"DESCRIPTION:{_escapar(desc)}")
        if p.tiene_hora and aviso > 0:
            lineas += ["BEGIN:VALARM", "ACTION:DISPLAY",
                       "DESCRIPTION:Partido de baloncesto",
                       f"TRIGGER:-PT{aviso}H", "END:VALARM"]
        lineas.append("END:VEVENT")
    lineas.append("END:VCALENDAR")
    return "\r\n".join(_plegar(l) for l in lineas) + "\r\n"


# --------------------------------------------------------------------------- #
# INDEX HTML
# --------------------------------------------------------------------------- #

def generar_index(equipos: list[Equipo], ajustes: dict, base_url: str) -> str:
    titulo = ajustes.get("titulo_sitio", "Calendarios de baloncesto FBM")
    base = base_url.rstrip("/")
    filas = []
    for eq in equipos:
        n_con_fecha = sum(1 for p in eq.partidos if p.inicio is not None)
        if base:
            https_url = f"{base}/{eq.id}.ics"
            webcal_url = re.sub(r"^https?://", "webcal://", https_url)
            botones = (
                f'<a class="btn" href="{html_mod.escape(webcal_url)}">📅 Suscribirse</a>'
                f'<a class="btn ghost" href="{html_mod.escape(https_url)}">Copiar URL (https)</a>'
            )
            url_txt = f'<code>{html_mod.escape(https_url)}</code>'
        else:
            botones = f'<a class="btn" href="{eq.id}.ics">Descargar .ics</a>'
            url_txt = f'<code>{eq.id}.ics</code>'
        filas.append(f"""
        <div class="card">
          <h2>{html_mod.escape(eq.nombre)}</h2>
          <p class="meta">{n_con_fecha} partido(s) programado(s)</p>
          <div class="acciones">{botones}</div>
          <p class="url">{url_txt}</p>
        </div>""")

    actualizado = datetime.now().strftime("%d/%m/%Y %H:%M")
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_mod.escape(titulo)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 760px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
  h1 {{ font-size: 1.6rem; }}
  .sub {{ color: #666; margin-top: -0.5rem; }}
  .card {{ border: 1px solid #ddd; border-radius: 12px; padding: 1rem 1.25rem;
          margin: 1rem 0; box-shadow: 0 1px 3px rgba(0,0,0,.05); }}
  .card h2 {{ margin: 0 0 .25rem; font-size: 1.15rem; }}
  .meta {{ color: #888; margin: 0 0 .75rem; font-size: .9rem; }}
  .acciones {{ display: flex; gap: .5rem; flex-wrap: wrap; }}
  .btn {{ display: inline-block; background: #c4242f; color: #fff;
         text-decoration: none; padding: .5rem .9rem; border-radius: 8px;
         font-size: .95rem; }}
  .btn.ghost {{ background: transparent; color: #c4242f; border: 1px solid #c4242f; }}
  .url {{ font-size: .8rem; color: #888; word-break: break-all; margin: .6rem 0 0; }}
  footer {{ margin-top: 2rem; color: #999; font-size: .85rem; }}
  .ayuda {{ background: #f6f6f6; border-radius: 10px; padding: 1rem; font-size: .9rem; }}
</style>
</head>
<body>
  <h1>🏀 {html_mod.escape(titulo)}</h1>
  <p class="sub">Partidos de la Federación de Baloncesto de Madrid, siempre al día.</p>

  {''.join(filas)}

  <div class="ayuda">
    <strong>¿Cómo suscribirse?</strong>
    <ul>
      <li><strong>iPhone / iPad / Mac:</strong> pulsa “Suscribirse” (enlace <code>webcal://</code>) y confirma.</li>
      <li><strong>Google Calendar:</strong> copia la URL <code>https</code> y ve a
          <em>Otros calendarios → Suscribirse a un calendario → Desde una URL</em>.</li>
    </ul>
    El calendario se actualiza solo: si un partido cambia de día u hora, tu
    calendario lo refleja automáticamente (puede tardar unas horas en Google).
  </div>

  <footer>Actualizado por última vez: {actualizado}. Generado automáticamente desde fbm.es.</footer>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #

def cargar_config(ruta: str) -> dict:
    with open(ruta, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera .ics por equipo desde equipos.yaml.")
    parser.add_argument("--config", default="equipos.yaml")
    parser.add_argument("--salida", default="public", help="Carpeta de salida (se publica en Pages).")
    parser.add_argument(
        "--base-url", default=os.environ.get("BASE_URL", ""),
        help="URL pública base (p.ej. https://usuario.github.io/repo). "
             "Si se omite, el index enlaza a ficheros locales.",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    try:
        cfg = cargar_config(args.config)
    except FileNotFoundError:
        print(f"ERROR: no se encontró la config '{args.config}'.", file=sys.stderr)
        return 1

    clubes = cfg.get("clubes", {})
    ajustes = cfg.get("ajustes", {})
    equipos = [
        Equipo(
            id=e["id"], nombre=e["nombre"], club=e["club"],
            categoria=e["categoria"], equipo_contiene=e.get("equipo_contiene", "") or "",
        )
        for e in cfg.get("equipos", [])
    ]
    if not equipos:
        print("ERROR: no hay equipos en la configuración.", file=sys.stderr)
        return 1

    sesion = requests.Session()
    sesion.headers.update({"User-Agent": USER_AGENT})

    # Descargar cada club una sola vez (caché).
    cache_html: dict[str, list[Partido]] = {}
    errores = 0
    for clave_club in {e.club for e in equipos}:
        club = clubes.get(clave_club)
        if club is None:
            print(f"ERROR: club '{clave_club}' no definido en la config.", file=sys.stderr)
            errores += 1
            continue
        try:
            html = descargar_club(club, sesion)
            cache_html[clave_club] = extraer_partidos(html)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR descargando club '{clave_club}': {exc}", file=sys.stderr)
            errores += 1

    os.makedirs(args.salida, exist_ok=True)
    generados = 0
    for eq in equipos:
        todos = cache_html.get(eq.club)
        if todos is None:
            continue
        eq.partidos = filtrar(todos, eq)
        ics = generar_ics(eq, ajustes)
        ruta = os.path.join(args.salida, f"{eq.id}.ics")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(ics)
        generados += 1
        con_fecha = sum(1 for p in eq.partidos if p.inicio is not None)
        print(f"OK {eq.id}: {len(eq.partidos)} partido(s), {con_fecha} con fecha -> {ruta}")
        if args.debug:
            for p in eq.partidos:
                cuando = p.inicio.strftime("%d/%m/%Y %H:%M") if p.inicio else "sin fecha"
                print(f"    - {p.local} vs {p.visitante} | {cuando} | {p.pabellon}")

    # index.html (solo con los equipos que se pudieron generar).
    equipos_ok = [e for e in equipos if e.club in cache_html]
    index = generar_index(equipos_ok, ajustes, args.base_url)
    with open(os.path.join(args.salida, "index.html"), "w", encoding="utf-8") as f:
        f.write(index)
    print(f"OK index.html -> {os.path.join(args.salida, 'index.html')}")

    if errores:
        print(f"Terminado con {errores} error(es).", file=sys.stderr)
        # Si no se generó NADA, fallar para que el cron/Action conserve lo previo.
        return 1 if generados == 0 else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
