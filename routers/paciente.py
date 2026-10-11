"""
routers/paciente.py
Sophia, la investigadora (asistentes de voz de ICA): responde con la mejor
evidencia científica disponible (EvidenciaMed). Solo informativo: no emite
órdenes ni hace diagnósticos nuevos.

  POST /paciente/consultar  {consulta, caso?}
       -> {tipo, voz, texto, papers}

  consulta: la pregunta de la persona. Puede venir vacía cuando viene "caso":
            entonces se pregunta qué dice la evidencia sobre su diagnóstico
            presuntivo y su tratamiento.
  caso:     lo que Ipo ya sabe del paciente al pasárselo a Sophia: zona, lado,
            edad, sexo, relato, respuestas de la consulta, diagnóstico presuntivo,
            fundamento y examen propuesto. Nunca nombre ni RUT. Con caso,
            EvidenciaMed busca sobre el diagnóstico presuntivo y la respuesta
            hablada une la evidencia con lo que contó el paciente.
            Sin caso (Sophia directa): igual que antes, solo la pregunta.

  tipo: respondida | sin_evidencia | error_evidencia | no_entendida

Compatible con el frontend anterior del avatar (que manda solo {consulta}).
"""

from typing import List, Optional

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from services import evidenciamed

router = APIRouter(prefix="/paciente")

FRASE_SIN_EVIDENCIA = "No encontré estudios científicos específicos sobre lo que me preguntaste. ¿Puedes contarme un poco más?"
FRASE_SIN_EVIDENCIA_CASO = (
    "No encontré estudios científicos específicos sobre tu diagnóstico presuntivo. "
    "Puedes preguntarme algo más concreto, por ejemplo sobre un tratamiento."
)
FRASE_ERROR_EVIDENCIA = "Tuve un problema para revisar la evidencia científica en este momento. Intenta de nuevo en unos minutos."
FRASE_NO_ENTENDI = "No alcancé a entenderte bien. ¿Puedes contarme un poco más qué quieres saber?"
FRASE_SIN_RESUMEN = "Revisé la evidencia científica sobre lo que me preguntaste. Puedes leer el detalle en la pantalla."
CIERRE = "Recuerda que esta información es educativa y no reemplaza la consulta médica."

# Con caso y sin pregunta (recién llega desde Ipo)
PREGUNTA_POR_DEFECTO = "¿Qué dice la evidencia científica sobre mi diagnóstico y su tratamiento?"

MIN_PALABRAS = 3


class CasoIn(BaseModel):
    # Los textos largos se recortan en services/evidenciamed.py (no se rechazan)
    zona: Optional[str] = None
    lado: Optional[str] = None
    edad: Optional[int] = Field(None, ge=0, le=120)
    sexo: Optional[str] = None
    relato: Optional[str] = None
    respuestas: List[str] = Field(default_factory=list)  # "Resumen de la pregunta: respuesta"
    prediagnostico: Optional[str] = None
    fundamento: Optional[str] = None
    examen: Optional[str] = None


class ConsultaIn(BaseModel):
    consulta: str = Field("", max_length=3000)
    caso: Optional[CasoIn] = None


def _respuesta(tipo: str, voz: str, texto: str = "", papers: Optional[list] = None) -> dict:
    return {"tipo": tipo, "voz": voz, "texto": texto, "papers": papers or []}


@router.post("/consultar")
async def consultar(body: ConsultaIn):
    consulta = body.consulta.strip()
    caso = evidenciamed.limpiar_caso(body.caso.model_dump()) if body.caso else None
    con_caso = evidenciamed.caso_util(caso)

    if not con_caso and len(consulta.split()) < MIN_PALABRAS:
        return _respuesta("no_entendida", FRASE_NO_ENTENDI)
    if con_caso and not consulta:
        consulta = PREGUNTA_POR_DEFECTO

    # 1. Evidencia (con caso, la búsqueda va sobre el diagnóstico presuntivo)
    busqueda = evidenciamed.armar_busqueda(consulta, caso) if con_caso else consulta
    try:
        texto, papers = await evidenciamed.consultar_paciente(busqueda)
    except evidenciamed.EvidenciaMedError as e:
        motivo = str(e)
        print(f"[paciente] EvidenciaMed sin resultado: {motivo}")
        if motivo == "sin_papers":
            return _respuesta("sin_evidencia", FRASE_SIN_EVIDENCIA_CASO if con_caso else FRASE_SIN_EVIDENCIA)
        return _respuesta("error_evidencia", FRASE_ERROR_EVIDENCIA)
    except Exception as e:  # noqa: BLE001 — timeouts, red, etc.
        print(f"[paciente] EvidenciaMed error de conexión: {e!r}")
        return _respuesta("error_evidencia", FRASE_ERROR_EVIDENCIA)

    # 2. Resumen para voz (con caso: une la evidencia con lo que contó el paciente)
    try:
        resumen = await run_in_threadpool(
            evidenciamed.resumir_para_voz, consulta, texto, caso if con_caso else None,
        )
        voz = resumen.get("voz") or ""
    except Exception as e:  # noqa: BLE001
        print(f"[paciente] error resumiendo para voz: {e!r}")
        voz = ""

    if not voz:
        voz = FRASE_SIN_RESUMEN

    return _respuesta("respondida", f"{voz} {CIERRE}", texto, papers)
