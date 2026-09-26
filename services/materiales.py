"""
services/materiales.py
Lectura SOLO LECTURA de los materiales del curso de traumatología, directo
desde Supabase (misma base que usa el backend de traumatología, sin tocarlo).

- Tabla `materiales` (id, titulo, storage_path, region, ...)
- Bucket de Storage `materiales` con los .docx

Caché en memoria:
- Lista de regiones con material .docx.
- Texto completo por región (todos sus .docx concatenados, con su título).
Ambas expiran tras CACHE_TTL_SEG. `precargar_todo()` llena la caché en segundo
plano (se llama desde /ping al abrir la página del avatar).

Variables de entorno:
  SUPABASE_URL
  SUPABASE_SERVICE_KEY
"""

import io
import os
import threading
import time
from typing import Dict, List, Tuple

import docx
from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

CACHE_TTL_SEG = 15 * 60
MAX_CARACTERES_REGION = 150_000  # tope de contexto por región

_lock_global = threading.Lock()
_locks_region: Dict[str, threading.Lock] = {}

_regiones_cache: Dict[str, object] = {"datos": None, "ts": 0.0}
_texto_cache: Dict[str, Tuple[str, List[str], float]] = {}  # region -> (texto, fuentes, ts)

_precarga_en_curso = threading.Event()


def _vigente(ts: float) -> bool:
    return (time.time() - ts) < CACHE_TTL_SEG


def _lock_de(region: str) -> threading.Lock:
    with _lock_global:
        if region not in _locks_region:
            _locks_region[region] = threading.Lock()
        return _locks_region[region]


def _filas_docx() -> List[dict]:
    filas = sb.table("materiales").select("id, titulo, storage_path, region").execute().data or []
    return [f for f in filas if (f.get("storage_path") or "").lower().endswith(".docx") and f.get("region")]


def _extraer_texto_docx(contenido: bytes) -> str:
    documento = docx.Document(io.BytesIO(contenido))
    return "\n".join(p.text for p in documento.paragraphs if p.text.strip())


def listar_regiones() -> List[str]:
    """Regiones que tienen al menos un material .docx."""
    if _regiones_cache["datos"] is not None and _vigente(_regiones_cache["ts"]):
        return list(_regiones_cache["datos"])
    regiones = sorted({f["region"] for f in _filas_docx()})
    _regiones_cache["datos"] = regiones
    _regiones_cache["ts"] = time.time()
    return list(regiones)


def texto_region(region: str) -> Tuple[str, List[str]]:
    """Devuelve (texto, fuentes) de todos los .docx de la región. Usa caché."""
    guardado = _texto_cache.get(region)
    if guardado and _vigente(guardado[2]):
        return guardado[0], list(guardado[1])

    with _lock_de(region):
        # Otro hilo pudo haberla cargado mientras se esperaba el lock
        guardado = _texto_cache.get(region)
        if guardado and _vigente(guardado[2]):
            return guardado[0], list(guardado[1])

        filas = [f for f in _filas_docx() if f["region"] == region]
        bloques: List[str] = []
        fuentes: List[str] = []
        for f in filas:
            try:
                contenido = sb.storage.from_("materiales").download(f["storage_path"])
                texto = _extraer_texto_docx(contenido)
            except Exception:
                continue  # material ilegible: se omite sin romper el flujo
            if texto.strip():
                bloques.append(f"### {f['titulo']}\n{texto}")
                fuentes.append(f["titulo"])

        texto_total = "\n\n".join(bloques)[:MAX_CARACTERES_REGION]
        _texto_cache[region] = (texto_total, fuentes, time.time())
        return texto_total, list(fuentes)


def precargar_todo() -> None:
    """Llena la caché de regiones y textos. Si ya hay una precarga corriendo, no hace nada."""
    if _precarga_en_curso.is_set():
        return
    _precarga_en_curso.set()
    try:
        try:
            regiones = listar_regiones()
        except Exception as e:  # noqa: BLE001 — la precarga nunca debe romper el ping
            print(f"[materiales] precarga sin regiones: {e!r}")
            return
        for region in regiones:
            try:
                texto_region(region)
            except Exception:
                continue
    finally:
        _precarga_en_curso.clear()


def cache_vigente() -> bool:
    """True si la lista de regiones y todos sus textos están en caché vigente."""
    if _regiones_cache["datos"] is None or not _vigente(_regiones_cache["ts"]):
        return False
    for region in _regiones_cache["datos"]:
        guardado = _texto_cache.get(region)
        if not guardado or not _vigente(guardado[2]):
            return False
    return True
  
