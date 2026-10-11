"""
services/evidenciamed.py
Conexión server-to-server con EvidenciaMed (backend "An-lisis-paper-backend"),
sin modificar ese repo.

- consultar_paciente(): llama a POST /pacientes/chat (SSE) y junta la respuesta completa.
- limpiar_caso() / caso_util() / armar_busqueda(): el caso que Ipo le pasa a Sophia
  (zona, lado, edad, sexo, relato, respuestas, diagnóstico presuntivo, fundamento y
  examen). Con caso, la búsqueda va sobre el diagnóstico presuntivo.
- resumir_para_voz(): con el modelo barato, convierte la respuesta en un texto corto
  para ser dicho por Sophia. Con caso, une la evidencia con lo que contó el paciente.
  También extrae zona y lado (lo usaba el frontend anterior del avatar).

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

# Topes del caso (lo que sobra se recorta, no se rechaza)
MAX_TEXTO_CORTO = 120
MAX_DIAGNOSTICO = 300
MAX_FUNDAMENTO = 1500
MAX_RELATO = 3000
MAX_RESPUESTAS = 40
MAX_RESPUESTA = 400
MAX_BUSQUEDA = 900


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


# ---------------- caso que Ipo le pasa a Sophia ----------------
def _texto(valor, maximo: int) -> str:
    if not isinstance(valor, str):
        return ""
    return re.sub(r"\s+", " ", valor).strip()[:maximo]


def limpiar_caso(caso: Optional[dict]) -> Optional[dict]:
    """Deja solo lo esperado, recortado. Nunca trae nombre ni RUT (no se piden)."""
    if not isinstance(caso, dict):
        return None
    edad = caso.get("edad")
    respuestas = [
        _texto(r, MAX_RESPUESTA) for r in (caso.get("respuestas") or [])[:MAX_RESPUESTAS]
    ]
    return {
        "zona": _texto(caso.get("zona"), MAX_TEXTO_CORTO),
        "lado": _texto(caso.get("lado"), MAX_TEXTO_CORTO),
        "edad": edad if isinstance(edad, int) and 0 < edad <= 120 else None,
        "sexo": _texto(caso.get("sexo"), MAX_TEXTO_CORTO),
        "relato": _texto(caso.get("relato"), MAX_RELATO),
        "respuestas": [r for r in respuestas if r],
        "prediagnostico": _texto(caso.get("prediagnostico"), MAX_DIAGNOSTICO),
        "fundamento": _texto(caso.get("fundamento"), MAX_FUNDAMENTO),
        "examen": _texto(caso.get("examen"), MAX_TEXTO_CORTO * 2),
    }


def caso_util(caso: Optional[dict]) -> bool:
    """Hay caso si al menos trae el diagnóstico presuntivo o la zona."""
    return bool(caso and (caso.get("prediagnostico") or caso.get("zona")))


def _paciente_en_texto(caso: dict) -> str:
    sexo = (caso.get("sexo") or "").lower()
    quien = "mujer" if sexo.startswith("fem") else "hombre" if sexo.startswith("mas") else "persona"
    edad = caso.get("edad")
    return f"{quien} de {edad} años" if edad else quien


def _zona_en_texto(caso: dict) -> str:
    zona = (caso.get("zona") or "").lower()
    lado = (caso.get("lado") or "").lower()
    return f"{zona} {lado}".strip()


def armar_busqueda(consulta: str, caso: dict) -> str:
    """Lo que se le pregunta a EvidenciaMed: la pregunta + diagnóstico presuntivo y contexto breve."""
    partes = [consulta.strip()]
    if caso.get("prediagnostico"):
        partes.append(f"Diagnóstico presuntivo: {caso['prediagnostico']}.")
    zona = _zona_en_texto(caso)
    if zona:
        partes.append(f"Dolor en {zona}.")
    partes.append(f"Paciente: {_paciente_en_texto(caso)}.")
    return " ".join(p for p in partes if p)[:MAX_BUSQUEDA]


def describir_caso(caso: dict) -> str:
    """El caso en texto, para que el resumen lo relacione con la evidencia."""
    lineas = [f"Paciente: {_paciente_en_texto(caso)}."]
    zona = _zona_en_texto(caso)
    if zona:
        lineas.append(f"Zona del dolor: {zona}.")
    if caso.get("relato"):
        lineas.append(f"Lo que contó: {caso['relato']}")
    if caso.get("respuestas"):
        lineas.append("Respuestas de la consulta:")
        lineas.extend(f"- {r}" for r in caso["respuestas"])
    if caso.get("prediagnostico"):
        lineas.append(f"Diagnóstico presuntivo (de Ipo): {caso['prediagnostico']}")
    if caso.get("fundamento"):
        lineas.append(f"Fundamento de Ipo: {caso['fundamento']}")
    if caso.get("examen"):
        lineas.append(f"Examen propuesto por Ipo: {caso['examen']}")
    return "\n".join(lineas)


# ---------------- resumen para voz ----------------
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


def resumir_para_voz(consulta: str, texto_completo: Optional[str], caso: Optional[dict] = None) -> dict:
    """
    Devuelve {"voz": str, "zona": str|None, "lado": str|None}.
    Si texto_completo es None (EvidenciaMed no encontró nada), solo extrae zona y lado.
    Con caso: la voz une la evidencia con lo que contó el paciente.
    """
    contexto_caso = f"CASO DEL PACIENTE (lo que recogió Ipo):\n{describir_caso(caso)}\n\n" if caso else ""

    if texto_completo and caso:
        tarea = (
            "1) Escribe en \"voz\" lo que dirá en voz alta Sophia, investigadora científica del Instituto de "
            "Cirugía Articular, a esta persona: entre 5 y 7 frases, en segunda persona (tú), lenguaje simple "
            "y cálido, español de Chile, sin listas, títulos, asteriscos ni formato. Explica qué dice la "
            "evidencia sobre su diagnóstico presuntivo y relaciónalo con su caso (su edad y lo que contó). "
            "Si la evidencia lo respalda, explica por qué tiene sentido el examen propuesto. No cambies el "
            "diagnóstico presuntivo ni hagas uno nuevo: si la evidencia menciona otras posibilidades, di que "
            "el especialista lo evaluará. No recetes medicamentos ni dosis. No inventes nada que no esté en la "
            "respuesta basada en evidencia o en el caso.\n"
        )
        contexto = f"{contexto_caso}RESPUESTA BASADA EN EVIDENCIA:\n{texto_completo}\n\n"
    elif texto_completo:
        tarea = (
            "1) Escribe en \"voz\" un resumen de la respuesta para ser DICHO en voz alta por Sophia, "
            "investigadora científica: entre 4 y 6 frases, lenguaje simple y cálido, español de Chile, "
            "sin listas, títulos, asteriscos ni formato. Solo informativo: no diagnostiques ni "
            "prescribas medicamentos. No inventes nada que no esté en la respuesta.\n"
        )
        contexto = f"RESPUESTA BASADA EN EVIDENCIA:\n{texto_completo}\n\n"
    else:
        tarea = "1) Deja \"voz\" como cadena vacía.\n"
        contexto = contexto_caso

    prompt = (
        f"PREGUNTA DE LA PERSONA: {consulta}\n\n"
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
        max_tokens=900,
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
