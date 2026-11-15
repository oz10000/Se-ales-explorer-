# D.A.P.S. Ω — Fase 1: explorador de certificación Streamlit

Explorador de solo lectura. Inventaría la estructura visible de cada aplicación Streamlit
para preparar el futuro daemon de recolección (Fase 2).

**No descarga archivos, no pulsa botones de generación, no interpreta señales, no modifica nada.**

## Salidas

- `reporte.txt` — inventario legible por URL + resumen estadístico.
- `reporte.json` — misma información estructurada.
- `log.txt` — inicio/fin, errores y duraciones.
- `salida/<NN_slug>/captura.png` y `pagina.html` — por aplicación.

## Instalación local

```bash
pip install -r requirements.txt
playwright install chromium
python explorer.py
