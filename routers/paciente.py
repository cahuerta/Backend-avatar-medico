"""
routers/paciente.py
Avatar informativo para personas: conversa por voz y responde con la mejor
evidencia científica disponible (EvidenciaMed). Solo informativo: no emite
órdenes ni diagnósticos (eso vive en el avatar clínico dentro de ICA).

  POST /paciente/consultar  {consulta}
       -> {tipo, voz, texto, papers}

  tipo: respondida | sin_evidencia | error_evidencia | no_entendida
"""

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from services import evidenciamed

router = APIRouter(prefix="/paciente")

FRASE_SIN_EVIDENCIA = "No encontré estudios científicos específicos sobre lo que me contaste. ¿Puedes contarme un poco más?"
FRASE_ERROR_EVIDENCIA = "Tuve un problema para revisar la evidencia científica en este momento. Intenta de nuevo en unos minutos."
FRASE_NO_ENTENDI = "No alcancé a entenderte bien. ¿Puedes contarme un poco más qué te pasa?"
CIERRE = "Recuerda que esta información es educativa y no reemplaza la consulta médica."

MIN_PALABRAS = 3


class ConsultaIn(BaseModel):
    consulta: str = Field(..., min_length=1, max_length=3000)


@router.post("/consultar")
async def consultar(body: ConsultaIn):
    consulta = body.consulta.strip()
    if len(consulta.split()) < MIN_PALABRAS:
        return {"tipo": "no_entendida", "voz": FRASE_NO_ENTENDI, "texto": "", "papers": []}

    # 1. Evidencia
    try:
        texto, papers = await evidenciamed.consultar_paciente(consulta)
    except evidenciamed.EvidenciaMedError as e:
        motivo = str(e)
        print(f"[paciente] EvidenciaMed sin resultado: {motivo}")
        if motivo == "sin_papers":
            return {"tipo": "sin_evidencia", "voz": FRASE_SIN_EVIDENCIA, "texto": "", "papers": []}
        return {"tipo": "error_evidencia", "voz": FRASE_ERROR_EVIDENCIA, "texto": "", "papers": []}
    except Exception as e:  # noqa: BLE001 — timeouts, red, etc.
        print(f"[paciente] EvidenciaMed error de conexión: {e!r}")
        return {"tipo": "error_evidencia", "voz": FRASE_ERROR_EVIDENCIA, "texto": "", "papers": []}

    # 2. Resumen para voz
    try:
        resumen = await run_in_threadpool(evidenciamed.resumir_para_voz, consulta, texto)
        voz = resumen.get("voz") or ""
    except Exception as e:  # noqa: BLE001
        print(f"[paciente] error resumiendo para voz: {e!r}")
        voz = ""

    if not voz:
        voz = "Revisé la evidencia científica sobre lo que me contaste. Puedes leer el detalle en la pantalla."

    return {"tipo": "respondida", "voz": f"{voz} {CIERRE}", "texto": texto, "papers": papers}
  
