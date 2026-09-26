"""
services/ica.py
Conexión server-to-server con ASISTENCIA-ICA-BACKEND (módulo IA), sin modificar ese repo.

Mismo flujo que el módulo IA del frontend ASISTENCIA-ICA:
  1. POST /api/preview-informe   -> diagnóstico presuntivo, explicación y exámenes
  2. POST /api/guardar-datos-ia  -> correo y checklist de resonancia
  3. GET  /api/pdf-ia-orden/{id} -> orden PDF firmada (ese backend también la envía por correo)

El registro en ficha clínica lo decide ASISTENCIA-ICA: solo registra si origen == "reserva".
El avatar no envía origen, así que no se registra.

Variable de entorno:
  ASISTENCIA_ICA_URL   URL base de ASISTENCIA-ICA-BACKEND (sin "/" final)
"""

import os
import re
import uuid
from typing import List, Optional

import httpx

ASISTENCIA_ICA_URL = os.environ.get("ASISTENCIA_ICA_URL", "").rstrip("/")

TIMEOUT_ICA = httpx.Timeout(120.0, connect=60.0)


class IcaError(Exception):
    pass


# ---------------- RUT (misma lógica que el frontend de ICA) ----------------
def limpiar_rut(valor: str) -> str:
    return re.sub(r"[^0-9kK]", "", str(valor or "")).upper()


def calcular_dv(cuerpo: str) -> str:
    suma, multiplicador = 0, 2
    for digito in reversed(cuerpo):
        suma += int(digito) * multiplicador
        multiplicador = 2 if multiplicador == 7 else multiplicador + 1
    resto = 11 - (suma % 11)
    if resto == 11:
        return "0"
    if resto == 10:
        return "K"
    return str(resto)


def rut_valido(valor: str) -> bool:
    s = limpiar_rut(valor)
    if len(s) < 2:
        return False
    cuerpo, dv = s[:-1], s[-1]
    return bool(re.fullmatch(r"\d{1,8}", cuerpo)) and calcular_dv(cuerpo) == dv


def formatear_rut(valor: str) -> str:
    s = limpiar_rut(valor)
    cuerpo, dv = s[:-1], s[-1]
    cuerpo_fmt = f"{int(cuerpo):,}".replace(",", ".")
    return f"{cuerpo_fmt}-{dv}"


# ---------------- Utilidades ----------------
def nuevo_id_pago() -> str:
    return f"avatar-{uuid.uuid4().hex}"


def contiene_rm(texto: str) -> bool:
    """Misma regla que ASISTENCIA-ICA (_contiene_rm)."""
    s = str(texto or "")
    return "resonancia" in s.lower() or bool(re.search(r"\brm\b", s, re.I))


def _seccion(texto: str, titulo: str, siguientes: List[str]) -> List[str]:
    """Extrae las viñetas de una sección del formato fijo de preview-informe."""
    fin = "|".join(re.escape(t) for t in siguientes)
    patron = rf"{re.escape(titulo)}\s*:?\s*([\s\S]*?)(?:\n\s*(?:{fin})\s*:|$)"
    m = re.search(patron, texto, re.I)
    if not m:
        return []
    lineas = []
    for linea in m.group(1).splitlines():
        limpia = re.sub(r"^[\s•\-\*·]+", "", linea).strip()
        if limpia:
            lineas.append(limpia)
    return lineas


async def despertar() -> None:
    """Ping sin costo para despertar ASISTENCIA-ICA si está dormido en Render."""
    if not ASISTENCIA_ICA_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as c:
            await c.get(f"{ASISTENCIA_ICA_URL}/health")
    except Exception:
        pass


# ---------------- Llamadas a ASISTENCIA-ICA ----------------
async def preview_informe(id_pago: str, consulta: str, datos: dict) -> dict:
    """
    datos: {nombre, rut, edad, genero, dolor, lado}
    Devuelve {diagnosticos, explicacion, examenes, informe, requiere_rm}.
    """
    if not ASISTENCIA_ICA_URL:
        raise IcaError("ASISTENCIA_ICA_URL no está configurada")

    cuerpo = {
        "idPago": id_pago,
        "consulta": consulta,
        "nombre": datos.get("nombre"),
        "rut": datos.get("rut"),
        "edad": datos.get("edad"),
        "genero": datos.get("genero"),
        "dolor": datos.get("dolor"),
        "lado": datos.get("lado"),
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_ICA) as c:
        res = await c.post(f"{ASISTENCIA_ICA_URL}/api/preview-informe", json=cuerpo)
    if res.status_code != 200:
        raise IcaError(f"preview-informe HTTP {res.status_code}")
    j = res.json()
    if not j.get("ok"):
        raise IcaError(j.get("error") or "preview-informe sin resultado")

    informe = j.get("respuesta") or ""
    diagnosticos = _seccion(informe, "Diagnóstico presuntivo", ["Explicación breve", "Examenes sugeridos", "Exámenes sugeridos", "Indicaciones"])
    explicacion = " ".join(_seccion(informe, "Explicación breve", ["Examenes sugeridos", "Exámenes sugeridos", "Indicaciones"]))
    examenes = j.get("examenes") or _seccion(informe, "Examenes sugeridos", ["Indicaciones"])
    examenes = [str(e).strip() for e in examenes if str(e).strip()]

    return {
        "diagnosticos": diagnosticos,
        "explicacion": explicacion,
        "examenes": examenes,
        "informe": informe,
        "requiere_rm": contiene_rm("\n".join(examenes)),
    }


async def guardar_datos(id_pago: str, email: Optional[str] = None,
                        checklist_rm: Optional[dict] = None, resumen_rm: Optional[str] = None) -> None:
    if not ASISTENCIA_ICA_URL:
        raise IcaError("ASISTENCIA_ICA_URL no está configurada")
    cuerpo: dict = {"idPago": id_pago}
    if email:
        cuerpo["datosPaciente"] = {"email": email}
    if checklist_rm:
        cuerpo["resonanciaChecklist"] = checklist_rm
    if resumen_rm:
        cuerpo["resonanciaResumenTexto"] = resumen_rm
    async with httpx.AsyncClient(timeout=TIMEOUT_ICA) as c:
        res = await c.post(f"{ASISTENCIA_ICA_URL}/api/guardar-datos-ia", json=cuerpo)
    if res.status_code != 200:
        raise IcaError(f"guardar-datos-ia HTTP {res.status_code}")


async def pdf_orden(id_pago: str) -> bytes:
    """Orden firmada. Ojo: cada llamada hace que ASISTENCIA-ICA reenvíe el correo."""
    if not ASISTENCIA_ICA_URL:
        raise IcaError("ASISTENCIA_ICA_URL no está configurada")
    async with httpx.AsyncClient(timeout=TIMEOUT_ICA) as c:
        res = await c.get(f"{ASISTENCIA_ICA_URL}/api/pdf-ia-orden/{id_pago}")
    if res.status_code != 200:
        raise IcaError(f"pdf-ia-orden HTTP {res.status_code}")
    return res.content


def texto_voz_propuesta(diagnosticos: List[str], examenes: List[str], requiere_rm: bool, con_correo: bool) -> str:
    """Texto hablado de la propuesta, armado sin llamar a Claude."""
    partes = []
    if diagnosticos:
        dx = diagnosticos[0].rstrip(".")
        if len(diagnosticos) > 1:
            dx += f", o también podría tratarse de {diagnosticos[1].rstrip('.')}"
        partes.append(f"Con lo que me contaste, el diagnóstico presuntivo es {dx}.")
    if examenes:
        if len(examenes) == 1:
            partes.append(f"Te propongo el siguiente examen: {examenes[0].rstrip('.')}.")
        else:
            lista = ", ".join(e.rstrip(".") for e in examenes[:-1]) + f" y {examenes[-1].rstrip('.')}"
            partes.append(f"Te propongo estos exámenes: {lista}.")
    if requiere_rm:
        partes.append("Como incluye una resonancia, antes de emitir la orden necesito que respondas unas preguntas de seguridad en la pantalla.")
    else:
        partes.append("Ya puedes descargar tu orden en la pantalla.")
        if con_correo:
            partes.append("También te la enviaremos por correo.")
    partes.append("Recuerda que esto no reemplaza la evaluación presencial con un especialista.")
    return " ".join(partes)
  
