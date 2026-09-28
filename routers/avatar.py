"""
routers/avatar.py
Endpoints públicos del avatar.

  GET  /ping              -> despierta este servidor y EvidenciaMed, y precarga
                             materiales del curso (avatar de clase).
  POST /avatar/preguntar  -> {pregunta} -> {respuesta, tipo, region, fuentes}   (avatar de clase)
"""

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field

from services import evidenciamed
from services.avatar_respuesta import procesar_pregunta
from services.materiales import cache_vigente, precargar_todo

router = APIRouter()


class PreguntaIn(BaseModel):
    pregunta: str = Field(..., min_length=1, max_length=2000)


@router.get("/ping")
def ping(tareas: BackgroundTasks):
    """Responde al instante; lo demás corre después de responder."""
    precargando = not cache_vigente()
    if precargando:
        tareas.add_task(precargar_todo)
    tareas.add_task(evidenciamed.despertar)
    return {"ok": True, "precargando": precargando}


@router.post("/avatar/preguntar")
def preguntar(body: PreguntaIn):
    return procesar_pregunta(body.pregunta)
  
