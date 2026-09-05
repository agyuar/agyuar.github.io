"""Reproducción forense: extractor CSS genérico (el viejo) vs HTML real.
Demuestra por qué 'primera <a> con texto entre 20-200 chars' muere en
IEEE Spectrum, ArXiv y GitHub Releases."""
import re, json, subprocess
from bs4 import BeautifulSoup
from urllib.parse import urljoin

SOURCES = json.load(open("/home/agent/.openclaw/workspace/intelligence/sources.json"))["sources"]

# --- EL VIEJO: un solo selector genérico para todas las fuentes ---
def old_generic_extract(soup, base_url):
    out = []
    for a in soup.find_all('a'):
        text = a.get_text(strip=True)
        href = a.get('href')
        if text and href and 20 < len(text) < 200:
            out.append(f"- [{text}]({urljoin(base_url, href)})")
    return out[:8]

# --- La variante CSS-selector que también era "genérica" (tipo HN) ---
def old_css_attempt(css, soup, base_url):
    out = []
    for el in soup.select(css):
        a = el if el.name == 'a' else el.find('a')
        if a and a.get('href'):
            t = a.get_text(strip=True)
            if t:
                out.append(f"- [{t}]({urljoin(base_url, a['href'])})")
    return out[:8]

for s in SOURCES:
    name, url = s['name'], s['url']
    if name == 'Hacker News':
        continue
    html = subprocess.run(['curl','-sL','--max-time','15','-A','Mozilla/5.0',url],
                          capture_output=True, text=True).stdout
    soup = BeautifulSoup(html, 'html.parser')

    print(f"\n########## {name} ({url}) ##########")
    print("---- VIEJO: primer <a> genérico (lo que salía en el report antes) ----")
    for line in old_generic_extract(soup, url)[:8]:
        print("   ", line[:130])
    if name == 'IEEE Spectrum Robotics':
        print("---- VIEJO: intento CSS '.card-title' (había una clase y NO un <article>) ----")
        for line in old_css_attempt('.card-title', soup, url)[:8] or ["   (0 resultados — clase no existe / renombrada)"]:
            print("   ", line[:130])
    if name == 'ArXiv AI':
        print("---- VIEJO: intento CSS 'h1, h2, .list-title' (el HTML usa <dt>/<dd>, no títulos) ----")
        for line in old_css_attempt('.list-title', soup, url)[:8] or ["   (0 resultados — el título está en <dd>, no en una clase .list-title simple)"]:
            print("   ", line[:130])
    if name == 'OpenClaw Releases':
        print("---- VIEJO: intento CSS 'h2 a' (títulos de release dentro de h3/div, no h2) ----")
        for line in old_css_attempt('h2 a', soup, url)[:8] or ["   (0 resultados)"]:
            print("   ", line[:130])
