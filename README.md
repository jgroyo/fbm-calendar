# Calendarios FBM → iCal (multi-equipo)

> 🌐 **Web generada por este repo: <https://jgroyo.github.io/fbm-calendar/>**
> Entra, elige tu equipo y suscríbete a su calendario.

Genera un archivo `.ics` por equipo a partir de la web de la
[Federación de Baloncesto de Madrid](https://www.fbm.es), y los publica en
**GitHub Pages**. Cada familia se suscribe al calendario de su equipo desde
Google Calendar o Apple Calendar, y **se actualiza solo**: si un partido cambia
de día, hora o pabellón, el evento se corrige automáticamente (sin duplicados,
gracias a identificadores de evento estables).

Todo corre en **GitHub Actions**: no necesitas ningún servidor ni dejar tu
ordenador encendido.

## Fuente de los datos

Los partidos se obtienen automáticamente de la página del club en la
Federación de Baloncesto de Madrid, concretamente de su sección **Calendario**:

- Patrocinio San José C.B.:
  <https://www.fbm.es/resultados-club-6341/patrocinio-san-jose-cb>

La URL se compone a partir del ID y el slug del club definidos en `equipos.yaml`
(`https://www.fbm.es/resultados-club-<id>/<slug>`). Esa única página contiene el
calendario completo de **todos** los equipos del club, por eso una sola descarga
genera todos los calendarios.

## Cómo funciona

1. Una acción programada (cada hora) raspa la página de cada club en la FBM.
2. `generar.py` lee la sección **CALENDARIO** de la web (la fuente completa y
   fiable: toda la liga del equipo, con aplazados y partidos entre semana),
   filtra los partidos de los equipos definidos en `equipos.yaml` y escribe un
   `.ics` por equipo + un `index.html` con los enlaces.
3. El resultado se publica en GitHub Pages: `https://<usuario>.github.io/<repo>/`.

## Archivos

| Archivo                           | Qué es                                            |
|-----------------------------------|---------------------------------------------------|
| `equipos.yaml`                    | Configuración: clubes y equipos a seguir.         |
| `generar.py`                      | Script que descarga, filtra y genera los `.ics`.  |
| `requirements.txt`                | Dependencias de Python.                           |
| `.github/workflows/actualizar.yml`| La acción de GitHub que actualiza y publica.      |
| `public/`                         | Salida generada (no se versiona; la crea la acción). |

## Añadir un equipo

Edita `equipos.yaml` y añade un bloque bajo `equipos`:

```yaml
  - id: infantil-masc-1          # nombre del fichero: infantil-masc-1.ics
    nombre: "Infantil Masculino 1º año"
    club: patrocinio-san-jose    # debe existir en la sección "clubes"
    categoria: "infantil masc. 1"  # prefijo de la categoría en la web FBM
    equipo_contiene: ""          # "VERDE"/"NEGRO"... si hay varios; vacío si no
```

¿Un club nuevo? Añádelo bajo `clubes` con su ID (sale de la URL de su página en
la FBM: `.../resultados-club-<ID>/<slug>`):

```yaml
clubes:
  otro-club:
    id: 1234
    slug: otro-club-cb
    nombre: Otro Club C.B.
    nombre_fbm: "OTRO CLUB"   # cómo aparece el club en la web FBM (para
                              # quedarse solo con SUS partidos del grupo)
```

Al hacer `git push`, la acción se ejecuta sola y actualiza los calendarios.

## Puesta en marcha en GitHub (una sola vez)

1. **Crea el repositorio** en GitHub (público, p.ej. `fbm-calendar`) y sube
   estos archivos. Desde esta carpeta:

   ```bash
   git init
   git add .
   git commit -m "Calendarios FBM multi-equipo"
   git branch -M main
   git remote add origin https://github.com/<usuario>/fbm-calendar.git
   git push -u origin main
   ```

2. **Activa GitHub Pages con origen "GitHub Actions":**
   repo → *Settings* → *Pages* → en *Build and deployment* → *Source* elige
   **GitHub Actions**.

3. **Lanza la primera ejecución:** repo → pestaña *Actions* →
   *Actualizar calendarios FBM* → *Run workflow*. (Luego se repetirá sola cada
   hora.)

4. Abre `https://<usuario>.github.io/fbm-calendar/` — verás la página con un
   botón de suscripción por equipo.

## Suscribirse a un calendario

Desde la página de índice, para el equipo que quieras:

- **iPhone / iPad / Mac:** pulsa **Suscribirse** (enlace `webcal://`) y confirma.
  - A mano en Mac: *Calendario → Archivo → Nueva suscripción*, pega la URL.
  - A mano en iPhone: *Ajustes → Calendario → Cuentas → Añadir cuenta → Otra →
    Añadir calendario suscrito*.
- **Google Calendar (desde ordenador):** copia la **URL https** y ve a
  *Otros calendarios (+) → Suscribirse a un calendario → Desde una URL*.

> Frecuencia de refresco: los calendarios suscritos no se actualizan al
> instante. Apple suele refrescar cada pocas horas; Google puede tardar entre
> 8 y 24 h. La acción mantiene el `.ics` siempre al día en origen.

## Probar en local (opcional)

```bash
pip install -r requirements.txt
python3 generar.py --debug --base-url "https://<usuario>.github.io/fbm-calendar"
# genera public/*.ics y public/index.html
```

## Notas

- La acción corre cada hora (`cron: "0 * * * *"` en UTC). Ajusta esa línea
  en el workflow si quieres otra frecuencia.
- Si la FBM está caída durante una ejecución, la acción simplemente no publica
  nada nuevo; la versión anterior en Pages se conserva hasta la siguiente.
- Las jornadas de **DESCANSA** no generan evento.
- Si aún no hay hora publicada para un partido, se crea como evento de **día
  completo** y se actualizará a hora concreta cuando la FBM la publique.
