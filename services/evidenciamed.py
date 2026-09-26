"""
services/evidenciamed.py
Conexión server-to-server con EvidenciaMed (backend "An-lisis-paper-backend"),
sin modificar ese repo.

- consultar_paciente(): llama a POST /pacientes/chat (SSE) y junta la respuesta completa.
- resumir_para_voz(): con el modelo barato, convierte la respuesta en un texto corto
  para ser dicho por el avatar, y extrae zona y lado del problema para precargar
  el formulario de la orden.

Variables de entorno:
  EVIDENCIAMED_URL       URL base del backend de EvidenciaMed (sin "/" final)
  EVIDENCIAMED_API_KEY   la misma X-API-Key que usa su frontend
  ANTHROPIC_API_KEY
  MODELO_CLASIFICADOR    (opcional, por defecto claude-haiku-4-5)
"""

import json
import os
import re
from typing import List, Optional, Tuple

import anthropic
import httpx

EVIDENCIAMED_URL = os.environ.get("EVIDENCIAMED_URL", "").rstrip("/")
EVIDENCIAMED_API_KEY = os.environ.get("EVIDENCIAMED_API_KEY", "")

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODELO_CLASIFICADOR = os.environ.get("MODELO_CLASIFICADOR", "claude-haiku-4-5")

TIMEOUT_EVIDENCIAMED = httpx.Timeout(180.0, connect=60.0)

# Valores exactos que usa el formulario de ASISTENCIA-ICA
ZONAS = [
    "Rodilla", "Cadera", "Columna lumbar", "Columna cervical", "Columna dorsal",
    "Hombro", "Codo", "Mano", "Tobillo",
]
ZONAS_SIN_LADO = {"Columna lumbar", "Columna cervical", "Columna dorsal"}
LADOS = ["Derecha", "Izquierda"]


class EvidenciaMedError(Exception):
    pass


async def despertar() -> None:
    """Ping sin costo para despertar EvidenciaMed si está dormido en Render."""
    if not EVIDENCIAMED_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as c:
            await c.get(f"{EVIDENCIAMED_URL}/health")
    except Exception:
        pass


async def consultar_paciente(consulta: str) -> Tuple[str, List[dict]]:
    """Devuelve (texto_completo, papers_meta). Lanza EvidenciaMedError si no hay resultado."""
    if not EVIDENCIAMED_URL or not EVIDENCIAMED_API_KEY:
        raise EvidenciaMedError("EvidenciaMed no está configurado")

    texto: List[str] = []
    papers: List[dict] = []

    async with httpx.AsyncClient(timeout=TIMEOUT_EVIDENCIAMED) as c:
        async with c.stream(
            "POST",
            f"{EVIDENCIAMED_URL}/pacientes/chat",
            headers={"X-API-Key": EVIDENCIAMED_API_KEY, "Content-Type": "application/json"},
            json={"query": consulta},
        ) as res:
            if res.status_code == 404:
                raise EvidenciaMedError("sin_papers")
            if res.status_code != 200:
                raise EvidenciaMedError(f"HTTP {res.status_code}")

            async for linea in res.aiter_lines():
                if not linea.startswith("data:"):
                    continue
                dato = linea[5:].strip()
                if not dato or dato == "[DONE]":
                    continue
                try:
                    evento = json.loads(dato)
                except json.JSONDecodeError:
                    continue
                if evento.get("type") == "text":
                    texto.append(evento.get("text", ""))
                elif evento.get("type") == "papers_meta":
                    papers = evento.get("papers") or []

    completo = "".join(texto).strip()
    if not completo:
        raise EvidenciaMedError("respuesta vacía")
    return completo, papers


def _normalizar_zona(valor: Optional[str]) -> Optional[str]:
    if not isinstance(valor, str):
        return None
    v = valor.strip().lower()
    return next((z for z in ZONAS if z.lower() == v), None)


def _normalizar_lado(valor: Optional[str], zona: Optional[str]) -> Optional[str]:
    if zona in ZONAS_SIN_LADO or not isinstance(valor, str):
        return None
    v = valor.strip().lower()
    if v.startswith("der"):
        return "Derecha"
    if v.startswith("izq"):
        return "Izquierda"
    return None


def resumir_para_voz(consulta: str, texto_completo: Optional[str]) -> dict:
    """
    Devuelve {"voz": str, "zona": str|None, "lado": str|None}.
    Si texto_completo es None (EvidenciaMed no encontró nada), solo extrae zona y lado.
    """
    if texto_completo:
        tarea = (
            "1) Escribe en \"voz\" un resumen de la respuesta para ser DICHO en voz alta por una "
            "médica traumatóloga: entre 4 y 6 frases, lenguaje simple y cálido, español de Chile, "
            "sin listas, títulos, asteriscos ni formato. Solo informativo: no diagnostiques ni "
            "prescribas medicamentos. No inventes nada que no esté en la respuesta.\n"
        )
        contexto = f"RESPUESTA BASADA EN EVIDENCIA:\n{texto_completo}\n\n"
    else:
        tarea = "1) Deja \"voz\" como cadena vacía.\n"
        contexto = ""

    prompt = (
        f"CONSULTA DE LA PERSONA: {consulta}\n\n"
        f"{contexto}"
        "Tareas:\n"
        f"{tarea}"
        f"2) En \"zona\" indica la región del problema usando EXACTAMENTE uno de: "
        f"{json.dumps(ZONAS, ensure_ascii=False)}, o null si no se puede saber.\n"
        "3) En \"lado\" indica \"Derecha\" o \"Izquierda\" solo si la persona lo dijo; si no, null.\n\n"
        'Responde SOLO con JSON válido: {"voz": "...", "zona": "..."|null, "lado": "..."|null}'
    )

    mensaje = client.messages.create(
        model=MODELO_CLASIFICADOR,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    crudo = "".join(b.text for b in mensaje.content if getattr(b, "type", "") == "text").strip()
    crudo = crudo.replace("```json", "").replace("```", "").strip()
    coincidencia = re.search(r"\{.*\}", crudo, re.DOTALL)
    datos = json.loads(coincidencia.group(0) if coincidencia else crudo)

    zona = _normalizar_zona(datos.get("zona"))
    return {
        "voz": str(datos.get("voz") or "").strip(),
        "zona": zona,
        "lado": _normalizar_lado(datos.get("lado"), zona),
  }
  
