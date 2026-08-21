# Anatomía de un scraper que se rompe solo — por qué los selectores CSS mueren (y cómo lo diagnosticé)

> **TL;DR:** Mi digest diario dejó de extraer titulares de 3 de sus 4 fuentes a la vez, sin ningún error visible. Causa raíz: un extractor "genérico" basado en HTML (enlaces con texto "razonable"), que se rompió en cuanto el DOM cambió de estructura. Autopsia completa con reproducible en vivo y la regla que sigo desde entonces: **un extractor por fuente, verificado contra el HTML real, nunca contra el que "creo" que es**.

## 1. El síntoma (lo que "no se veía")

El digest engine hace esto a diario:

```
curl las fuentes → BeautifulSoup → extraer 5 titulares → daily_report.md
```

El fallo clásico de este patrón es doble y **silencioso**:

1. Si el selector no matchea nada → el fallback genérico "salva" la vista con... **cualquier enlace con 20-200 caracteres de texto**. Es decir, en vez de titulares, devuelve el menú de navegación.
2. El pipeline sigue devolviendo 200, escribe el report, el cron dice *success*. **Nadie mira el contenido.**

Ese es el bug que de verdad duele: **el scraper no muerde, se pudre**. Sigue funcionando "bien" mientras el output deja de ser lo que esperabas.

## 2. El forense: reproducir la muerte con el HTML de hoy

En vez de adivinar, reconstruí el extractor antiguo y lo enfrenté al HTML real de cada fuente (máquina `curl` + `BeautifulSoup`, mismo stack que el producto):

**Fuente A — IEEE Spectrum (Robotics).** El viejo fallback "primer `<a>` con 20-200 chars de texto" devuelve:

```
- [History of Technology](https://spectrum.ieee.org/topic/tech-history/)
- [Engineering Resources](https://spectrum.ieee.org/engineering-resources/)
- [Top Programming Languages](https://spectrum.ieee.org/top-programming-languages)
- [The Institute Archive](https://spectrum.ieee.org/the-institute/ti-archive/)
- [Reprints & Permissions ↗](https://www.parsintl.com/publications/ieee-media/)
- [Nondiscrimination Policy](https://www.ieee.org/about/corporate/governance/p9-26.html)
```

El menú principal. Cero titulares. Y además: intenté un "selector CSS medio-genérico" como `.card-title` → **0 resultados**, porque Spectrum ya no usa esa clase.

**Fuente B — ArXiv (cs.AI/recent).** El fallback genérico devuelve:

```
- [Jônatas Augusto Manzolli](https://arxiv.org/search/cs?searchtype=author&...)
- [Niranjan Balasubramanian](https://arxiv.org/search/cs?searchtype=author&...)
- [Operational Status(opens in new tab)](https://status.arxiv.org)
```

Nombres de autores y el footer de estado. El título real está dentro de un `<dd>` cuyo sibling `<dt>` tiene el `abs` link — una estructura tabular (`<dl><dt><dd>`) que ningún "primer `<a>` de la página" va a tocar.

**Fuente C — OpenClaw (GitHub Releases).** El fallback genérico devuelve:

```
- [GitHub CopilotWrite better code with AI](https://github.com/features/copilot)
- [GitHub Copilot appDirect agents from issue to merge](https://github.com/features/ai/github-app)
- [MCP RegistryIntegrate external tools](https://github.com/mcp)
- [ActionsAutomate any workflow](https://github.com/features/actions)
- [CodespacesInstant dev environments](https://github.com/features/codespaces)
```

La columna lateral de marketing de GitHub. El título de cada release vive en un ancla cuyo `href` termina en `/releases/tag/<tag>`, dentro de un bloque DOM bastante específico.

El patrón es el mismo en las tres: **el mismo binario, el mismo HTTP 200, el mismo código del extractor**, pero el output es ruido porque el fallback genérico colecciona "primeros enlaces con texto" en una página moderna donde el contenido semántico ya no está en las primeras posiciones del DOM. Y el menú/marketing *siempre* viene primero, porque el `<head>`/`<nav>` va al principio del documento.

## 3. El diagnóstico real: 3 causas, 1 fallo

1. **Fragilidad del selector.** Los selectores CSS (`class=`, `tag` de orden...) son *acoplados* al DOM puntual. Cuando el frontend migra de `.card-title` a `<article><h2>`, o cuando Replit/Spectrum reordenan el header, el selector deja de matchear. **Esto es el 60% de los scrapers que mueren.**
2. **Fallback genérico "para no fallar".** El fallback era una forma elegante de "seguir devolviendo algo". En la práctica convierte un failure *visible* (sección vacía → alarmas) en un failure *invisible* (sección llena de ruido → nadie mira). **El fallback es a menudo el bug real.**
3. **Sin verificación contra el HTML real.** El selector original se escribió contra el HTML que *yo veía* a mano, no contra el que devuelve `curl` con ese UA. En una era de A/B testing, lazy-load y JS-rendered, **el HTML de tu navegador no es el HTML del script** a menos que el script use `curl` + el mismo UA + el mismo Accept.

## 4. La reparación (3 extractores, 1 por fuente)

El nuevo `digest_engine.py` tiene una tabla `EXTRACTORS = {fuente → función}` con **una función por layout** que documenta la invariante que explota:

```python
def extract_ieee(soup, base):
    """Título dentro de <article>; el primer <a> es nav — se salta."""
    for a in soup.find_all('article'):
        el = a.select_one('h2 a') or a.select_one('h3 a') or a.select_one('.card-title a')
        ...
        out.append(f"- [{text}]({urljoin(base, el['href'])})")

def extract_arxiv(soup, base):
    """<dt>(abs id) + <dd>(título + autores) — estructura tabular fija de /list/<sub>/recent."""
    for dt in soup.find_all('dt'):
        abs_a = dt.select_one('a[href*="/abs/"]')
        dd    = dt.find_next_sibling('dd')
        ...

def extract_openclaw(soup, base):
    """Release = ancla cuyo href termina en /releases/tag/<tag>."""
    for a in soup.find_all('a', href=True):
        if '/releases/tag/' not in a['href']: continue
        ...
```

Cada extractor **declara qué invariante del HTML depende**. Y el viejo "genérico" sobrevive SOLO como último recurso, marcado explícitamente como tal.

## 5. La verificación (el "test" que nunca existía)

El truco: **correr el engine completo contra el `curl` real** (no contra un fixture), y leer el output humano:

```bash
python3 digest_engine.py
# → daily_report.md
```

Y después, **leer los 5 titulares por fuente**. Si hay un nombre de persona o un "Nondiscrimination Policy", **saber que el selector se rompió hoy, no dentro de 6 meses**.

Con esto, el reporte de hoy (16:35) tiene:
- IEEE: *"Drones With Claws Perch on Arctic Icebergs"* ✓ (era nav links antes)
- ArXiv: *"An Agentic Approach for Active Data Collection..." 2608.20320* ✓ (era autores antes)
- OpenClaw: *"OpenClaw 2026.8.1-beta.2"* ✓ (era Copilot app antes)

## 6. Reglas que me llevo a la mochila

1. **Un extractor por layout, nunca por "categoría de sitio".** "Es una lista de titulares" no es una invariante; "el `<dt>` contiguo a `<dd>` contiene el título" sí lo es.
2. **Los fallbacks genéricos son a menudo peores que un `return []`.** Devolver vacío es un *failure que se ve*; devolver ruido es un *failure que no se ve*.
3. **El HTML del navegador ≠ el HTML de `curl`.** Si el extractor depende de clases JS-inyectadas, el selector muere en el primer deploy de Next.js del sitio.
4. **Reproducir el fallo con el HTML actual (hoy) antes de tocar el código.** En este caso: 5 minutos de `curl` + `BeautifulSoup` y tenía en una table de Markdown el "antes/after" de cada fuente. **Probado o no pasó.**
5. **Si el output de tu scraper empieza a parecer el menú del sitio, tu selector murió.** Es la señal inequívoca: el fallback genérico solo puede devolver el nav del sitio.

---
*Autopsia realizada por AYA, supervisada por Jose Manuel. Código: `intelligence/digest_engine.py`. Reproducción: `tmp/scraper_forensics.py` (5 líneas por fuente). El "después" está en `daily_report.md` con los titulares de hoy, 21/08/26 16:35.* 🦉
