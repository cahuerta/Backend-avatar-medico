"""
routers/paciente.py
Avatar para personas: conversa por voz, informa con EvidenciaMed y propone
una orden de examen firmada usando ASISTENCIA-ICA.

  POST /paciente/consultar        {consulta}
       -> {voz, texto, papers, zona, lado, tipo}
  POST /paciente/proponer-examen  {consulta, nombre, rut, edad, genero, dolor, lado, email}
       -> {idPago, diagnosticos, explicacion, examenes, requiereRM, voz}
  POST /paciente/resonancia       {idPago, checklist, resumen}
       -> {ok, voz}
  GET  /paciente/orden/{idPago}   -> PDF de la orden firmada

Gratis por ahora (sin pago).
"""

import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

from services import evidenciamed, ica

router = APIRouter(prefix="/paciente")

OFRECER_EXAMEN = "Si quieres, puedo proponerte un examen. Toca el botón Proponer examen en la pantalla."
FRASE_SIN_EVIDENCIA = "No encontré estudios científicos específicos sobre lo que me contaste."
FRASE_ERROR_EVIDENCIA = "Tuve un problema para revisar la evidencia científica en este momento."
FRASE_NO_ENTENDI = "No alcancé a entenderte bien. ¿Puedes contarme un poco más qué te pasa?"

MIN_PALABRAS = 3
ID_PAGO_VALIDO = re.compile(r"^avatar-[0-9a-f]{32}$")


class ConsultaIn(BaseModel):
    consulta: str = Field(..., min_length=1, max_length=3000)


class PropuestaIn(BaseModel):
    consulta: str = Field(..., min_length=1, max_length=5000)
    nombre: str = Field(..., min_length=2, max_length=120)
    rut: str = Field(..., min_length=2, max_length=15)
    edad: int = Field(..., ge=0, le=120)
    genero: str = Field(..., max_length=20)
    dolor: str = Field(..., max_length=40)
    lado: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=200)


class ResonanciaIn(BaseModel):
    idPago: str
    checklist: dict
    resumen: str = Field("", max_length=3000)


def _validar_id_pago(id_pago: str) -> None:
    if not ID_PAGO_VALIDO.match(id_pago or ""):
        raise HTTPException(status_code=400, detail="idPago inválido")


@router.post("/consultar")
async def consultar(body: ConsultaIn):
    consulta = body.consulta.strip()
    if len(consulta.split()) < MIN_PALABRAS:
        return {"tipo": "no_entendida", "voz": FRASE_NO_ENTENDI, "texto": "", "papers": [], "zona": None, "lado": None}

    texto, papers, tipo = None, [], "respondida"
    try:
        texto, papers = await evidenciamed.consultar_paciente(consulta)
    except evidenciamed.EvidenciaMedError as e:
        tipo = "sin_evidencia" if str(e) == "sin_papers" else "error_evidencia"
    except Exception as e:  # noqa: BLE001
        print(f"[paciente] error EvidenciaMed: {e!r}")
        tipo = "error_evidencia"

    try:
        resumen = await run_in_threadpool(evidenciamed.resumir_para_voz, consulta, texto)
    except Exception as e:  # noqa: BLE001
        print(f"[paciente] error resumen: {e!r}")
        resumen = {"voz": "", "zona": None, "lado": None}

    if tipo == "respondida" and resumen["voz"]:
        voz = resumen["voz"]
    elif tipo == "sin_evidencia":
        voz = FRASE_SIN_EVIDENCIA
    elif tipo == "error_evidencia":
        voz = FRASE_ERROR_EVIDENCIA
    else:
        voz = FRASE_ERROR_EVIDENCIA
        tipo = "error_evidencia"

    return {
        "tipo": tipo,
        "voz": f"{voz} {OFRECER_EXAMEN}",
        "texto": texto or "",
        "papers": papers,
        "zona": resumen["zona"],
        "lado": resumen["lado"],
    }


@router.post("/proponer-examen")
async def proponer_examen(body: PropuestaIn):
    if not ica.rut_valido(body.rut):
        raise HTTPException(status_code=422, detail="RUT inválido")
    if body.dolor not in evidenciamed.ZONAS:
        raise HTTPException(status_code=422, detail="Zona inválida")
    lado = None if body.dolor in evidenciamed.ZONAS_SIN_LADO else body.lado
    if lado not in (None, "", *evidenciamed.LADOS):
        raise HTTPException(status_code=422, detail="Lado inválido")
    email = (body.email or "").strip() or None
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Correo inválido")

    id_pago = ica.nuevo_id_pago()
    datos = {
        "nombre": body.nombre.strip(),
        "rut": ica.formatear_rut(body.rut),
        "edad": body.edad,
        "genero": body.genero,
        "dolor": body.dolor,
        "lado": lado or "",
    }

    try:
        propuesta = await ica.preview_informe(id_pago, body.consulta.strip(), datos)
        if email:
            await ica.guardar_datos(id_pago, email=email)
    except ica.IcaError as e:
        print(f"[paciente] error ICA: {e!r}")
        raise HTTPException(status_code=502, detail="No se pudo generar la propuesta de examen")

    if not propuesta["examenes"]:
        raise HTTPException(status_code=502, detail="La propuesta no incluyó exámenes")

    return {
        "idPago": id_pago,
        "diagnosticos": propuesta["diagnosticos"],
        "explicacion": propuesta["explicacion"],
        "examenes": propuesta["examenes"],
        "requiereRM": propuesta["requiere_rm"],
        "voz": ica.texto_voz_propuesta(
            propuesta["diagnosticos"], propuesta["examenes"], propuesta["requiere_rm"], bool(email)
        ),
    }


@router.post("/resonancia")
async def guardar_resonancia(body: ResonanciaIn):
    _validar_id_pago(body.idPago)
    try:
        await ica.guardar_datos(body.idPago, checklist_rm=body.checklist, resumen_rm=body.resumen)
    except ica.IcaError as e:
        print(f"[paciente] error ICA resonancia: {e!r}")
        raise HTTPException(status_code=502, detail="No se pudo guardar el checklist")
    return {"ok": True, "voz": "Gracias. Ya puedes descargar tu orden en la pantalla."}


@router.get("/orden/{id_pago}")
async def orden(id_pago: str):
    _validar_id_pago(id_pago)
    try:
        pdf = await ica.pdf_orden(id_pago)
    except ica.IcaError as e:
        print(f"[paciente] error ICA pdf: {e!r}")
        raise HTTPException(status_code=502, detail="No se pudo generar la orden")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="orden_examen.pdf"'},
                           )
  
