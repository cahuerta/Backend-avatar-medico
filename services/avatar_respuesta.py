"""
services/avatar_respuesta.py
Responde preguntas de alumnos de traumatología para el avatar de voz.

Flujo (para no gastar de más):
1. Filtro sin costo: frases muy cortas se descartan sin llamar a Claude.
2. Clasificación (modelo barato, sin materiales): ¿es de traumatología? ¿de qué región?
   - No pertinente        -> frase fija, sin segunda llamada.
   - Región no ubicable   -> pide la región, sin segunda llamada.
3. Respuesta (modelo completo) usando SOLO el material .docx de esa región.
   Texto pensado para ser dicho en voz alta: corto, sin listas ni formato.

Variables de entorno:
  ANTHROPIC_API_KEY
  MODELO_CLASIFICADOR  (opcional, por defecto claude-haiku-4-5)
  MODELO_RESPUESTA     (opcional, por defecto claude-sonnet-4-6)
"""

import json
import os
import re
import unicodedata
from typing import List, Optional

import anthropic

from services.materiales import listar_regiones, texto_region

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODELO_CLASIFICADOR = os.environ.get("MODELO_CLASIFICADOR", "claude-haiku-4-5")
MODELO_RESPUESTA = os.environ.get("MODELO_RESPUESTA", "claude-sonnet-4-6")

MIN_PALABRAS = 3
MAX_CARACTERES_PREGUNTA = 600

FRASE_NO_ENTENDI = "No alcancé a entender la pregunta. ¿Puedes repetirla un poco más completa?"
FRASE_NO_PERTINENTE = "Solo puedo responder preguntas de traumatología del curso."
FRASE_SIN_REGION = "¿De qué región anatómica es tu pregunta? Por ejemplo, cadera, rodilla u hombro."
FRASE_SIN_MATERIAL = "Todavía no hay material cargado para esa región, así que no puedo responderte con el contenido del curso."
FRASE_ERROR = "Tuve un problema para buscar la respuesta. Intenta de nuevo en un momento."


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFD", texto)
    sin_tildes = "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", sin_tildes.lower()).strip()


def _texto_de(mensaje) -> str:
    return "".join(b.text for b in mensaje.content if getattr(b, "type", "") == "text").strip()


def _resultado(respuesta: str, tipo: str, region: Optional[str] = None, fuentes: Optional[List[str]] = None) -> dict:
    return {"respuesta": respuesta, "tipo": tipo, "region": region, "fuentes": fuentes or []}


def clasificar(pregunta: str, regiones: List[str]) -> dict:
    """Devuelve {"pertinente": bool, "region": str | None} usando el modelo barato."""
    prompt = (
        "Clasifica la pregunta de un alumno de 4to año de medicina en un curso de traumatología.\n\n"
        f"PREGUNTA: {pregunta}\n\n"
        f"REGIONES DISPONIBLES (usa exactamente uno de estos textos): {json.dumps(regiones, ensure_ascii=False)}\n\n"
        "Reglas:\n"
        "- pertinente = true solo si la pregunta es sobre traumatología u ortopedia "
        "(lesiones, fracturas, patología musculoesquelética, examen físico, imagenología, tratamiento).\n"
        "- region = la región disponible a la que corresponde la pregunta, o null si no se puede ubicar "
        "en ninguna o si no es pertinente.\n\n"
        'Responde SOLO con JSON válido, sin texto adicional: {"pertinente": true|false, "region": "texto"|null}'
    )
    mensaje = client.messages.create(
        model=MODELO_CLASIFICADOR,
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    texto = _texto_de(mensaje).replace("```json", "").replace("```", "").strip()
    coincidencia = re.search(r"\{.*\}", texto, re.DOTALL)
    datos = json.loads(coincidencia.group(0) if coincidencia else texto)

    pertinente = bool(datos.get("pertinente"))
    region_propuesta = datos.get("region")
    region = None
    if pertinente and isinstance(region_propuesta, str):
        # Aceptar solo regiones que existen (comparación sin tildes ni mayúsculas)
        objetivo = _normalizar(region_propuesta)
        region = next((r for r in regiones if _normalizar(r) == objetivo), None)
    return {"pertinente": pertinente, "region": region}


def responder_con_material(pregunta: str, region: str, contexto: str) -> str:
    prompt = (
        "Eres una docente de traumatología que responde EN VOZ ALTA, en vivo, la pregunta de un "
        "alumno de 4to año de medicina. Tu respuesta será leída por una voz sintética.\n\n"
        f"REGIÓN: {region}\n"
        f"PREGUNTA DEL ALUMNO: {pregunta}\n\n"
        "MATERIAL DEL CURSO (documentos subidos por los docentes, cada uno bajo '### Título'):\n"
        f"{contexto}\n\n"
        "Instrucciones:\n"
        "- Responde SOLO con lo que está en el material del curso. No agregues información externa.\n"
        "- Si el material no permite responder, di exactamente: "
        "\"Eso no está en el material del curso. Te sugiero consultarlo con tu docente.\"\n"
        "- Entre 3 y 5 frases, claras y directas, en español de Chile.\n"
        "- Sin listas, viñetas, títulos, asteriscos, emojis ni formato: solo texto corrido para ser hablado.\n"
        "- Escribe las siglas y números como se dicen en voz alta cuando ayude a entender.\n"
        "- No menciones los títulos de los documentos."
    )
    mensaje = client.messages.create(
        model=MODELO_RESPUESTA,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return _texto_de(mensaje)


def procesar_pregunta(pregunta: str) -> dict:
    pregunta = (pregunta or "").strip()[:MAX_CARACTERES_PREGUNTA]

    # 1. Filtro sin costo
    if len(pregunta.split()) < MIN_PALABRAS:
        return _resultado(FRASE_NO_ENTENDI, "no_entendida")

    try:
        regiones = listar_regiones()
        if not regiones:
            return _resultado(FRASE_SIN_MATERIAL, "sin_material")

        # 2. Clasificación barata
        clase = clasificar(pregunta, regiones)
        if not clase["pertinente"]:
            return _resultado(FRASE_NO_PERTINENTE, "no_pertinente")
        if not clase["region"]:
            return _resultado(FRASE_SIN_REGION, "sin_region")

        region = clase["region"]
        contexto, fuentes = texto_region(region)
        if not contexto.strip():
            return _resultado(FRASE_SIN_MATERIAL, "sin_material", region)

        # 3. Respuesta con el material de la región
        respuesta = responder_con_material(pregunta, region, contexto)
        if not respuesta:
            return _resultado(FRASE_ERROR, "error", region)
        return _resultado(respuesta, "respondida", region, fuentes)

    except Exception as e:  # noqa: BLE001 — el avatar siempre debe tener algo que decir
        print(f"[avatar] error procesando pregunta: {e!r}")
        return _resultado(FRASE_ERROR, "error")
