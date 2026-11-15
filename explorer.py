"""
explorer.py — Fase 1 del proyecto D.A.P.S. Ω.
Explorador de certificación de aplicaciones Streamlit. Solo lectura.
No descarga, no interpreta señales, no pulsa botones de generación.
Salidas: reporte.txt, reporte.json, log.txt, salida/<NN_slug>/{captura.png,pagina.html}
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


# ---------------- Entorno ----------------
def _es_android_no_soportado() -> bool:
    return sys.platform == "android" or "pydroid" in sys.executable.lower()


if _es_android_no_soportado():
    print(
        "ENTORNO NO COMPATIBLE: PyDroid/Android no soporta Playwright de forma nativa.\n"
        "Use GitHub Actions (Ubuntu) o un equipo Linux/macOS/Windows."
    )
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Falta Playwright. pip install -r requirements.txt && playwright install chromium")
    sys.exit(1)


# ---------------- Configuración ----------------
CONFIG_DEFECTO = {
    "timeout_ms": 60_000,
    "espera_estabilidad_ms": 2_000,
    "max_despertar_s": 45,
    "max_html_bytes": 10 * 1024 * 1024,
    "max_captura_alto_px": 6_000,
    "headless": True,
    "max_peticiones": 200,
    "max_errores_js": 20,
    "max_url_len": 250,
}


def cargar_config() -> dict:
    cfg = dict(CONFIG_DEFECTO)
    p = Path("config.json")
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"Aviso: config.json inválido ({e}). Se usan valores por defecto.")
    if os.environ.get("HEADLESS") == "0":
        cfg["headless"] = False
    return cfg


# ---------------- Utilidades ----------------
def url_valida(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def slugify(url: str) -> str:
    s = re.sub(r"^https?://", "", url)
    return re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_")[:70] or "app"


def carpeta_para(url: str, indice: int) -> Path:
    d = Path("salida") / f"{indice:02d}_{slugify(url)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def contar(page, selector: str) -> int:
    try:
        return page.locator(selector).count()
    except Exception:
        return 0


def contar_dataframes(page) -> int:
    for sel in ("[data-testid='stDataFrame']", ".stDataFrame", "[role='grid']"):
        n = contar(page, sel)
        if n > 0:
            return n
    return 0


# ---------------- Detección de "dormida" ----------------
PATRONES_BOTON_DESPERTAR = ("back up", "wake it", "reactivate")
PATRONES_TEXTO_DORMIDA = ("gone to sleep", "has gone to sleep")


def detectar_dormida(page) -> bool:
    try:
        for pat in PATRONES_BOTON_DESPERTAR:
            if page.locator(f"button:has-text('{pat}')").count() > 0:
                return True
        try:
            cuerpo = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            cuerpo = ""
        bajo = cuerpo.lower()
        return any(pat in bajo for pat in PATRONES_TEXTO_DORMIDA)
    except Exception:
        return False


def despertar_si_dormida(page, max_s: int) -> tuple:
    """Devuelve (dormida, despertada_ok)."""
    if not detectar_dormida(page):
        return False, False
    clicked = False
    for pat in PATRONES_BOTON_DESPERTAR:
        try:
            btn = page.locator(f"button:has-text('{pat}')")
            if btn.count() > 0:
                btn.first.click(timeout=10_000)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        return True, False
    try:
        page.wait_for_function(
            "() => !((document.body?.innerText || '').toLowerCase().includes('gone to sleep'))",
            timeout=max_s * 1000,
        )
        return True, True
    except Exception:
        return True, False


# ---------------- Espera de estabilidad del DOM ----------------
def esperar_dom_estable(page, timeout_ms: int, quiet_ms: int, min_wait_ms: int = 2_000) -> None:
    try:
        page.wait_for_load_state("load", timeout=15_000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(min_wait_ms)
    except Exception:
        return
    t_inicio = time.time()
    ultimo = -1
    estable_desde = None
    while (time.time() - t_inicio) * 1000 < timeout_ms:
        try:
            n = page.evaluate("() => document.querySelectorAll('*').length")
        except Exception:
            return
        if n == ultimo and n > 0:
            if estable_desde is None:
                estable_desde = time.time()
            elif (time.time() - estable_desde) * 1000 >= quiet_ms:
                return
        else:
            estable_desde = None
            ultimo = n
        try:
            page.wait_for_timeout(300)
        except Exception:
            return


# ---------------- Listeners ----------------
def attach_listeners(page, cfg: dict):
    peticiones, errores_js = [], []

    def on_response(resp):
        if len(peticiones) >= cfg["max_peticiones"]:
            return
        try:
            url = resp.url
            if url.startswith(("data:", "blob:")):
                return
            peticiones.append({
                "metodo": resp.request.method,
                "url": url[: cfg["max_url_len"]],
                "status": resp.status,
                "ok": bool(resp.ok),
                "tipo": (resp.headers.get("content-type") or "")[:80],
            })
        except Exception:
            pass

    def on_console(msg):
        if len(errores_js) >= cfg["max_errores_js"]:
            return
        try:
            if msg.type in ("error", "warning"):
                errores_js.append({"tipo": msg.type, "texto": msg.text[:200]})
        except Exception:
            pass

    page.on("response", on_response)
    page.on("console", on_console)
    return peticiones, errores_js


# ---------------- Extracción ----------------
SELECTOR_CONTROLES = (
    "button, input, select, textarea, "
    "[role='button'], [role='checkbox'], [role='combobox'], "
    "[role='radio'], [role='slider']"
)


def detectar_login(page) -> bool:
    return contar(page, "input[type='password']") > 0


def textos_botones(page):
    try:
        return page.evaluate(
            "() => Array.from(document.querySelectorAll('button'))"
            ".map(b => (b.innerText || '').trim()).filter(t => t.length > 0)"
        )
    except Exception:
        return []


def tablas_html(page):
    try:
        return page.evaluate(
            "() => Array.from(document.querySelectorAll('table')).map(t => ({"
            "  columnas: Array.from(t.querySelectorAll('th')).map(th => (th.innerText || '').trim())"
            "}))"
        )
    except Exception:
        return []


def controles(page):
    try:
        return page.evaluate(
            """(sel) => {
                const c = {};
                document.querySelectorAll(sel).forEach(el => {
                    const r = el.getAttribute('role');
                    const k = el.tagName.toLowerCase() + (r ? '['+r+']' : '');
                    c[k] = (c[k] || 0) + 1;
                });
                return c;
            }""",
            SELECTOR_CONTROLES,
        )
    except Exception:
        return {}


def detectar_descargas(page, max_url_len: int):
    hallados = []
    for sel in ("[data-testid='stDownloadButton']", ".stDownloadButton"):
        try:
            for el in page.locator(sel).all():
                try:
                    txt = (el.inner_text() or "").strip()
                    hallados.append({"nombre": txt or "(sin texto)", "tipo": "stDownloadButton"})
                except Exception:
                    pass
        except Exception:
            pass
    try:
        for a in page.locator("a[download]").all():
            try:
                hallados.append({
                    "nombre": a.get_attribute("download") or "(sin nombre)",
                    "href": (a.get_attribute("href") or "")[:max_url_len],
                    "tipo": "a[download]",
                })
            except Exception:
                pass
    except Exception:
        pass
    try:
        for b in page.locator(
            "button:has-text('Descargar'), button:has-text('Download'), "
            "button:has-text('Exportar'), button:has-text('Export')"
        ).all():
            try:
                txt = (b.inner_text() or "").strip()
                if txt:
                    hallados.append({"nombre": txt, "tipo": "boton-keyword"})
            except Exception:
                pass
    except Exception:
        pass
    vistos, unicos = set(), []
    for d in hallados:
        k = (d["nombre"], d["tipo"])
        if k not in vistos:
            vistos.add(k)
            unicos.append(d)
    return unicos


# ---------------- Guardado con límites ----------------
def guardar_html(page, carpeta: Path, cfg: dict, info: dict) -> None:
    try:
        html = page.content()
    except Exception:
        try:
            html = page.evaluate("() => document.body ? document.body.innerHTML : ''")
            info["observaciones"].append("page.content() falló; guardado solo body.innerHTML.")
        except Exception as e2:
            info["observaciones"].append(f"HTML no guardado: {e2}")
            return
    data = html.encode("utf-8", errors="replace")
    if len(data) > cfg["max_html_bytes"]:
        recorte = data[: cfg["max_html_bytes"]].decode("utf-8", errors="replace")
        recorte += "\n<!-- HTML TRUNCADO POR LÍMITE DE TAMAÑO -->"
        info["observaciones"].append(
            f"HTML truncado a {cfg['max_html_bytes']} bytes (original {len(data)})."
        )
        data = recorte.encode("utf-8")
    try:
        (carpeta / "pagina.html").write_bytes(data)
    except Exception as e:
        info["observaciones"].append(f"HTML no escrito: {e}")


def guardar_captura(page, carpeta: Path, cfg: dict, info: dict) -> None:
    try:
        alto = page.evaluate(
            "() => Math.max(document.documentElement.scrollHeight, document.body.scrollHeight, 0)"
        ) or 0
    except Exception:
        alto = 0
    completo = alto <= cfg["max_captura_alto_px"]
    if not completo:
        info["observaciones"].append(
            f"Captura limitada a viewport (página {alto}px > {cfg['max_captura_alto_px']}px)."
        )
    try:
        page.screenshot(
            path=str(carpeta / "captura.png"),
            full_page=completo,
            timeout=30_000,
            animations="disabled",
        )
    except Exception as e:
        info["observaciones"].append(f"Captura no guardada: {e}")


# ---------------- Exploración ----------------
def ficha_vacia(url: str, error: str = "") -> dict:
    return {
        "url": url,
        "estado": f"ERROR: {error}" if error else "OK",
        "titulo_pagina": "",
        "url_final": "",
        "tiempo_carga": 0.0,
        "dormida": False,
        "despertada_ok": False,
        "requiere_login": False,
        "botones": [],
        "tablas": [],
        "controles": {},
        "enlaces": 0,
        "formularios": 0,
        "descargas": [],
        "dataframes": 0,
        "peticiones": [],
        "errores_js": [],
        "observaciones": [],
    }


def explorar(page, url: str, indice: int, cfg: dict) -> dict:
    info = ficha_vacia(url)
    peticiones, errores_js = attach_listeners(page, cfg)
    t0 = time.time()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=cfg["timeout_ms"])
        esperar_dom_estable(page, cfg["timeout_ms"], cfg["espera_estabilidad_ms"])
        dormida, despertada_ok = despertar_si_dormida(page, cfg["max_despertar_s"])
        info["dormida"] = dormida
        info["despertada_ok"] = despertada_ok
        if dormida:
            info["observaciones"].append(
                f"App dormida; despertar {'OK' if despertada_ok else 'falló'}."
            )
            if despertada_ok:
                esperar_dom_estable(page, cfg["timeout_ms"], cfg["espera_estabilidad_ms"])
        info["tiempo_carga"] = round(time.time() - t0, 2)
        info["url_final"] = (page.url or "")[:500]
        try:
            info["titulo_pagina"] = (page.title() or "")[:200]
        except Exception:
            pass
        info["requiere_login"] = detectar_login(page)
        info["botones"] = textos_botones(page)
        info["tablas"] = tablas_html(page)
        info["controles"] = controles(page)
        info["enlaces"] = contar(page, "a")
        info["formularios"] = contar(page, "form")
        info["dataframes"] = contar_dataframes(page)
        info["descargas"] = detectar_descargas(page, cfg["max_url_len"])
    except Exception as e:
        info["estado"] = f"ERROR: {e}"
    finally:
        carpeta = carpeta_para(url, indice)
        guardar_html(page, carpeta, cfg, info)
        guardar_captura(page, carpeta, cfg, info)
        # Copias defensivas: los listeners siguen vivos hasta page.close().
        info["peticiones"] = list(peticiones)
        info["errores_js"] = list(errores_js)
        if len(peticiones) >= cfg["max_peticiones"]:
            info["observaciones"].append(
                f"Peticiones HTTP truncadas a {cfg['max_peticiones']} (límite alcanzado)."
            )
        if len(errores_js) >= cfg["max_errores_js"]:
            info["observaciones"].append(
                f"Errores JS truncados a {cfg['max_errores_js']} (límite alcanzado)."
            )
    return info


# ---------------- Reporte ----------------
def escribir_ficha_txt(f, info: dict) -> None:
    f.write("=" * 50 + "\n")
    f.write(f"URL: {info['url']}\n")
    f.write(f"Estado: {info['estado']}\n")
    f.write(f"Título: {info['titulo_pagina'] or '(no detectado)'}\n")
    f.write(f"URL final: {info['url_final'] or '(no detectada)'}\n")
    f.write(f"Tiempo de carga: {info['tiempo_carga']} s\n")
    f.write(f"Dormida: {'sí' if info['dormida'] else 'no'}\n")
    if info["dormida"]:
        f.write(f"Despertada OK: {'sí' if info['despertada_ok'] else 'no'}\n")
    f.write(f"Requiere login: {'sí' if info['requiere_login'] else 'no'}\n")
    f.write(f"Botones encontrados: {len(info['botones'])}\n")
    for b in info["botones"]:
        f.write(f"  - {b[:150]}\n")
    f.write(f"Tablas <table>: {len(info['tablas'])}\n")
    for i, t in enumerate(info["tablas"], 1):
        f.write(f"  Tabla {i} columnas: {t['columnas']}\n")
    f.write(f"Dataframes Streamlit (stDataFrame): {info['dataframes']}\n")
    f.write(f"Controles interactivos: {info['controles']}\n")
    f.write(f"Enlaces: {info['enlaces']}\n")
    f.write(f"Formularios: {info['formularios']}\n")
    f.write(f"Descargas / archivos detectados: {len(info['descargas'])}\n")
    for d in info["descargas"]:
        f.write(f"  - {d}\n")
    f.write(f"Peticiones HTTP observadas: {len(info['peticiones'])}\n")
    for r in info["peticiones"][:30]:
        ok = "ok" if r.get("ok") else "fail"
        f.write(f"  - {r['metodo']} {r['status']} {ok} {r['tipo']} {r['url']}\n")
    if len(info["peticiones"]) > 30:
        f.write(f"  ... ({len(info['peticiones']) - 30} más)\n")
    f.write(f"Errores de consola JS: {len(info['errores_js'])}\n")
    for e in info["errores_js"]:
        f.write(f"  - [{e['tipo']}] {e['texto']}\n")
    if info["observaciones"]:
        f.write("Observaciones:\n")
        for o in info["observaciones"]:
            f.write(f"  - {o}\n")
    f.write("\n")


def calcular_estadisticas(registros: list, resumen: dict, duracion_total: float) -> dict:
    con_tiempo = [r for r in registros if r.get("tiempo_carga", 0) > 0]
    rapida = min(con_tiempo, key=lambda r: r["tiempo_carga"]) if con_tiempo else None
    lenta = max(con_tiempo, key=lambda r: r["tiempo_carga"]) if con_tiempo else None
    total = resumen["total"] or 1
    return {
        "duracion_total_s": round(duracion_total, 2),
        "duracion_promedio_s": round(duracion_total / total, 2),
        "app_mas_rapida": rapida["url"] if rapida else None,
        "app_mas_lenta": lenta["url"] if lenta else None,
        "pct_exito": round(100.0 * resumen["abiertas"] / total, 2),
        "pct_error": round(100.0 * resumen["errores"] / total, 2),
    }


def log(msg: str, log_file) -> None:
    linea = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(linea)
    try:
        log_file.write(linea + "\n")
        log_file.flush()
    except Exception:
        pass


# ---------------- Principal ----------------
def main() -> None:
    cfg = cargar_config()
    urls_file = Path("urls.txt")
    if not urls_file.exists():
        print("ERROR: no existe urls.txt.")
        sys.exit(1)
    urls = list(dict.fromkeys(
        l.strip()
        for l in urls_file.read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.startswith("#")
    ))
    if not urls:
        print("No hay URLs para explorar.")
        sys.exit(1)

    Path("salida").mkdir(exist_ok=True)
    t_inicio = time.time()
    registros = []
    resumen = {"total": len(urls), "abiertas": 0, "con_botones": 0,
               "con_tablas": 0, "con_descargas": 0, "con_login": 0, "errores": 0}

    with Path("log.txt").open("w", encoding="utf-8") as lf, \
         Path("reporte.txt").open("w", encoding="utf-8") as rtxt, \
         sync_playwright() as p:

        log(f"INICIO — {len(urls)} URLs", lf)
        try:
            browser = p.chromium.launch(headless=cfg["headless"])
        except Exception as e:
            log(f"ERROR FATAL al iniciar Chromium: {e}", lf)
            sys.exit(1)

        try:
            for i, url in enumerate(urls, 1):
                log(f"[{i}/{len(urls)}] {url}", lf)
                if not url_valida(url):
                    info = ficha_vacia(url, "URL INVÁLIDA")
                    resumen["errores"] += 1
                    log("  URL INVÁLIDA", lf)
                    registros.append(info)
                    continue

                page = None
                try:
                    page = browser.new_page()
                    info = explorar(page, url, i, cfg)
                    if info["estado"] == "OK":
                        resumen["abiertas"] += 1
                    else:
                        resumen["errores"] += 1
                    if info["botones"]:
                        resumen["con_botones"] += 1
                    if info["tablas"] or info["dataframes"]:
                        resumen["con_tablas"] += 1
                    if info["descargas"]:
                        resumen["con_descargas"] += 1
                    if info["requiere_login"]:
                        resumen["con_login"] += 1
                    log(f"  {info['estado']} — {info['tiempo_carga']}s", lf)
                except Exception as e:
                    info = ficha_vacia(url, str(e))
                    resumen["errores"] += 1
                    log(f"  ERROR: {e}", lf)
                finally:
                    if page is not None:
                        try:
                            page.close()
                        except Exception:
                            pass
                    registros.append(info)
        finally:
            try:
                browser.close()
            except Exception:
                pass

        duracion = time.time() - t_inicio
        stats = calcular_estadisticas(registros, resumen, duracion)

        rtxt.write("REPORTE DE EXPLORACIÓN DE APLICACIONES STREAMLIT\n")
        rtxt.write(f"Fecha UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}\n")
        rtxt.write(f"Total de URLs: {resumen['total']}\n\n")
        for info in registros:
            escribir_ficha_txt(rtxt, info)
        rtxt.write("=" * 50 + "\nRESUMEN FINAL\n")
        rtxt.write(f"Streamlit analizados: {resumen['total']}\n")
        rtxt.write(f"Pudieron abrirse: {resumen['abiertas']}\n")
        rtxt.write(f"Con botones: {resumen['con_botones']}\n")
        rtxt.write(f"Con tablas (HTML o stDataFrame): {resumen['con_tablas']}\n")
        rtxt.write(f"Con botones de descarga: {resumen['con_descargas']}\n")
        rtxt.write(f"Requieren login: {resumen['con_login']}\n")
        rtxt.write(f"Con errores: {resumen['errores']}\n")
        rtxt.write(f"Duración total: {stats['duracion_total_s']} s\n")
        rtxt.write(f"Duración promedio: {stats['duracion_promedio_s']} s\n")
        rtxt.write(f"Aplicación más rápida: {stats['app_mas_rapida'] or 'n/a'}\n")
        rtxt.write(f"Aplicación más lenta: {stats['app_mas_lenta'] or 'n/a'}\n")
        rtxt.write(f"Porcentaje de éxito: {stats['pct_exito']}%\n")
        rtxt.write(f"Porcentaje de error: {stats['pct_error']}%\n")

        Path("reporte.json").write_text(
            json.dumps(
                {
                    "fecha_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "config": cfg,
                    "resumen": {**resumen, **stats},
                    "aplicaciones": registros,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        log(f"FIN — duración {stats['duracion_total_s']}s", lf)

    print("\nReporte: reporte.txt\nJSON: reporte.json\nLog: log.txt\nArtefactos: salida/")


if __name__ == "__main__":
    main()
