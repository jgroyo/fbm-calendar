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
        return f"🏀 {etiqueta} · {self.local} vs {self.visitante}"

    def ubicacion(self) -> str:
        return ", ".join(p for p in (self.pabellon, self.direccion) if p)


@dataclass
class Equipo:
    id: str
    nombre: str
    club: str
    categoria: str
    equipo_contiene: str = ""
    club_nombre_fbm: str = ""
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
        hh, mm = int(m.group(4)), int(m.group(5))
        # La FBM usa 00:00 como marcador de "hora aún sin asignar": lo tratamos
        # como evento de día completo hasta que publiquen la hora real.
        if hh == 0 and mm == 0:
            return datetime(y, mo, d), False
        return datetime(y, mo, d, hh, mm), True
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
    """Extrae TODOS los partidos de la sección CALENDARIO.

    El CALENDARIO es la fuente completa y fiable: contiene toda la liga del
    equipo (todas las jornadas, con fechas y horas reales), incluidos los
    partidos aplazados y los entre semana. Cada bloque de calendario lleva su
    categoría en un <h4> inmediatamente anterior, que usamos para filtrar luego
    por equipo. La vista 'Próximos partidos' NO se usa porque es parcial y
    enmascara los aplazados como 'DESCANSA'.
    """
    soup = BeautifulSoup(html, "html.parser")
    cal = soup.find("div", id="calendarios")
    if cal is None:
        raise RuntimeError(
            "No se encontró la sección 'calendarios' (¿cambió la web de la FBM?)."
        )

    bloques = cal.find_all("div", id=re.compile(r"^capa_calendario_\d+$"))
    if not bloques:
        raise RuntimeError("No se encontraron bloques de calendario por competición.")

    partidos: list[Partido] = []
    for bloque in bloques:
        h4 = bloque.find_previous("h4")
        categoria = h4.get_text(" ", strip=True) if h4 else ""

        for tr in bloque.find_all("tr"):
            celdas = tr.find_all("td")
            if len(celdas) != 6:
                continue  # cabecera u otra fila (no es un partido)
            local = celdas[0].get_text(" ", strip=True)
            visitante = celdas[3].get_text(" ", strip=True)
            fecha_lineas = _celda_lineas(celdas[4])
            campo_lineas = _celda_lineas(celdas[5])
            fecha_txt = " ".join(fecha_lineas)
            inicio, tiene_hora = _parse_fecha(fecha_txt)

            partidos.append(
                Partido(
                    categoria=categoria,
                    local=local,
                    visitante=visitante,
                    inicio=inicio,
                    tiene_hora=tiene_hora,
                    pabellon=campo_lineas[0] if campo_lineas else "",
                    direccion=" ".join(campo_lineas[1:]) if len(campo_lineas) > 1 else "",
                )
            )

    # Salvaguarda: completar la hora desde 'Próximos partidos' cuando el
    # CALENDARIO aún no la tenga para un partido inminente. La fuente sigue
    # siendo el calendario; próximos solo aporta la hora que falte.
    _completar_horas_desde_proximos(soup, partidos)
    return partidos


def _horas_proximos(soup: BeautifulSoup) -> dict:
    """Mapa {(dd/mm/aaaa, local_norm, visitante_norm): (hh, mm)} con las horas
    reales publicadas en la sección 'Próximos partidos'."""
    horas: dict = {}
    cont = soup.find("div", id="proximosPartidos")
    if cont is None:
        return horas
    tabla = cont.find("table")
    if tabla is None:
        return horas
    for tr in tabla.find_all("tr"):
        celdas = tr.find_all("td")
        if len(celdas) != 4:
            continue
        enc = _celda_lineas(celdas[1])
        if len(enc) < 2:
            continue
        inicio, tiene_hora = _parse_fecha(celdas[2].get_text(" ", strip=True))
        if inicio is None or not tiene_hora:
            continue
        clave = (inicio.strftime("%d/%m/%Y"), _normaliza(enc[0]), _normaliza(enc[1]))
        horas[clave] = (inicio.hour, inicio.minute)
    return horas


def _completar_horas_desde_proximos(soup: BeautifulSoup, partidos: list[Partido]) -> None:
    horas = _horas_proximos(soup)
    if not horas:
        return
    for p in partidos:
        if p.inicio is None or p.tiene_hora:
            continue
        clave = (p.inicio.strftime("%d/%m/%Y"), _normaliza(p.local), _normaliza(p.visitante))
        hm = horas.get(clave)
        if hm:
            p.inicio = p.inicio.replace(hour=hm[0], minute=hm[1])
            p.tiene_hora = True


def filtrar(partidos: list[Partido], equipo: Equipo) -> list[Partido]:
    """Selecciona los partidos de un equipo concreto desde el CALENDARIO.

    - Filtra por prefijo de categoría.
    - Exige que el equipo (p.ej. 'PATROCINIO SAN JOSE [NEGRO]') juegue, y
      descarta las filas de DESCANSA.
    - Si 'equipo_contiene' está definido (VERDE/NEGRO...), exige ese color.
    - Deduplica por fecha+rival, prefiriendo la fila con hora (un aplazado
      reprogramado puede convivir con su antigua ranura de descanso).
    """
    pref = _normaliza(equipo.categoria)
    quiere = _normaliza(equipo.equipo_contiene) if equipo.equipo_contiene else ""
    club = _normaliza(equipo.club_nombre_fbm) if equipo.club_nombre_fbm else ""

    candidatos: list[Partido] = []
    for p in partidos:
        if not _normaliza(p.categoria).startswith(pref):
            continue
        if "descansa" in _normaliza(p.local) or "descansa" in _normaliza(p.visitante):
            continue

        local_norm = _normaliza(p.local)
        visitante_norm = _normaliza(p.visitante)
        # Identificar en qué lado juega el club.
        if club:
            if club in local_norm:
                lado_club = local_norm
            elif club in visitante_norm:
                lado_club = visitante_norm
            else:
                continue  # el club no juega este partido
        else:
            lado_club = f"{local_norm} {visitante_norm}"

        # El color/equipo concreto (VERDE/NEGRO...) debe estar en el LADO del
        # club, no en el rival (evita colar un rival que lleve ese color).
        if quiere and quiere not in lado_club:
            continue
        candidatos.append(p)

    # Deduplicar: misma fecha + mismo par de equipos -> preferir con hora.
    mejor: dict[tuple, Partido] = {}
    for p in candidatos:
        dia = p.inicio.strftime("%Y%m%d") if p.inicio else "sinfecha"
        clave = (dia, _normaliza(p.local), _normaliza(p.visitante))
        actual = mejor.get(clave)
        if actual is None or (p.tiene_hora and not actual.tiene_hora):
            mejor[clave] = p

    seleccion = list(mejor.values())
    seleccion.sort(key=lambda p: (p.inicio or datetime.max))
    return seleccion


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


def _temporada(partidos: list[Partido]) -> str:
    """Devuelve la temporada en formato 'AA-AA' (p.ej. '26-27') a partir de las
    fechas de los partidos. La temporada se considera que empieza en verano: un
    partido de sep-dic pertenece a 'año/año+1' y uno de ene-jun a 'año-1/año'."""
    fechas = [p.inicio for p in partidos if p.inicio is not None]
    if not fechas:
        return ""
    ref = min(fechas)
    inicio = ref.year if ref.month >= 7 else ref.year - 1
    return f"{inicio % 100:02d}-{(inicio + 1) % 100:02d}"


def generar_ics(equipo: Equipo, ajustes: dict) -> str:
    tzid = ajustes.get("tzid", "Europe/Madrid")
    dur = int(ajustes.get("duracion_minutos", 90))
    aviso = int(ajustes.get("aviso_horas_antes", 2))
    ahora = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    temporada = _temporada(equipo.partidos)
    nombre_cal = f"{equipo.nombre} {temporada}".strip() if temporada else equipo.nombre

    lineas = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//fbm-calendar//ES", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escapar(nombre_cal)}",
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
            # Google no autorrellena de forma fiable la URL en el diálogo de
            # "añadir desde URL", así que el botón copia la URL al portapapeles
            # y luego abre Google: el usuario solo tiene que pegar.
            google_url = "https://calendar.google.com/calendar/u/0/r/settings/addbyurl"
            botones = (
                f'<button class="btn" type="button" data-url="{html_mod.escape(https_url)}" '
                f'data-google="{html_mod.escape(google_url)}" onclick="añadirGoogle(this)">Añadir a Google Calendar</button>'
                f'<a class="btn apple" href="{html_mod.escape(webcal_url)}">Añadir a Apple / iPhone</a>'
                f'<button class="btn ghost" type="button" data-url="{html_mod.escape(https_url)}" onclick="copiarUrl(this)">Copiar URL</button>'
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
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 760px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5;
         background: #ffffff; color: #1a1a1a; }}
  h1 {{ font-size: 1.6rem; }}
  .sub {{ color: #555; margin-top: -0.5rem; }}
  .card {{ border: 1px solid #e0e0e0; border-radius: 12px; padding: 1rem 1.25rem;
          margin: 1rem 0; box-shadow: 0 1px 3px rgba(0,0,0,.06); background: #fff; }}
  .card h2 {{ margin: 0 0 .25rem; font-size: 1.15rem; color: #1a1a1a; }}
  .meta {{ color: #666; margin: 0 0 .75rem; font-size: .9rem; }}
  .acciones {{ display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }}
  .btn {{ display: inline-block; background: #c4242f; color: #fff;
         text-decoration: none; padding: .5rem .9rem; border-radius: 8px;
         font-size: .95rem; border: none; cursor: pointer; font-family: inherit; }}
  .btn.apple {{ background: #0a84ff; }}
  .btn.ghost {{ background: #fff; color: #c4242f; border: 1px solid #c4242f; }}
  .btn.ghost.ok {{ color: #1a7f37; border-color: #1a7f37; }}
  .url {{ font-size: .8rem; color: #666; word-break: break-all; margin: .6rem 0 0; }}
  .url code {{ background: #f2f2f2; padding: .15rem .35rem; border-radius: 4px; }}
  footer {{ margin-top: 2rem; color: #777; font-size: .85rem; }}
  .ayuda {{ background: #f4f6f8; border: 1px solid #e0e4e8; border-radius: 10px;
           padding: 1rem 1.25rem; font-size: .92rem; color: #1a1a1a; }}
  .ayuda strong {{ color: #111; }}
  .ayuda code {{ background: #e7ebef; padding: .1rem .3rem; border-radius: 4px; }}
</style>
</head>
<body>
  <h1>🏀 {html_mod.escape(titulo)}</h1>
  <p class="sub">Partidos de la Federación de Baloncesto de Madrid, siempre al día.</p>

  {''.join(filas)}

  <div class="ayuda">
    <strong>¿Cómo suscribirse?</strong>
    <ul>
      <li><strong>Google Calendar</strong> (ordenador): pulsa «Añadir a Google
          Calendar». Se copia la URL automáticamente y se abre Google; solo tienes
          que <em>pegar</em> (Ctrl/Cmd+V) en el campo «URL del calendario» y pulsar
          «Añadir calendario».</li>
      <li><strong>iPhone / iPad / Mac:</strong> pulsa «Añadir a Apple / iPhone»
          (enlace <code>webcal://</code>) y confirma.</li>
      <li><strong>Android:</strong> suscríbete primero en Google Calendar desde un
          ordenador; el calendario aparecerá luego en la app del móvil.</li>
    </ul>
    <strong>Importante:</strong> usa «suscribirse por URL», no «importar». Así el
    calendario <em>se actualiza solo</em>: si un partido cambia de día, hora o
    pabellón, tu calendario lo refleja automáticamente (Google puede tardar unas
    horas en refrescar).
  </div>

  <footer>Actualizado por última vez: {actualizado}. Generado automáticamente desde fbm.es.</footer>

  <script>
  function copiarUrl(btn) {{
    const url = btn.getAttribute('data-url');
    const previo = btn.textContent;
    const hecho = () => {{
      btn.textContent = '¡URL copiada!';
      btn.classList.add('ok');
      setTimeout(() => {{ btn.textContent = previo; btn.classList.remove('ok'); }}, 2000);
    }};
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(url).then(hecho).catch(() => prompt('Copia la URL:', url));
    }} else {{
      prompt('Copia la URL:', url);
    }}
  }}

  function añadirGoogle(btn) {{
    const url = btn.getAttribute('data-url');
    const google = btn.getAttribute('data-google');
    const abrir = () => window.open(google, '_blank', 'noopener');
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(url).then(() => {{
        alert('URL copiada. Se abrirá Google Calendar: pega la URL (Ctrl/Cmd+V) en «URL del calendario» y pulsa «Añadir calendario».');
        abrir();
      }}).catch(() => {{ prompt('Copia esta URL y pégala en Google:', url); abrir(); }});
    }} else {{
      prompt('Copia esta URL y pégala en Google:', url);
      abrir();
    }}
  }}
  </script>
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
            club_nombre_fbm=(clubes.get(e["club"], {}) or {}).get("nombre_fbm", "") or "",
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
