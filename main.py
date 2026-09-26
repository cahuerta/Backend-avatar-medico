"""
Backend — Avatar Hipokratia
main.py — app FastAPI del avatar de voz.

- Avatar de clase: lee (solo lectura) los materiales del curso de traumatología
  desde Supabase, sin tocar el backend de traumatología.
- Avatar para personas: conversa con EvidenciaMed y propone órdenes de examen
  con ASISTENCIA-ICA, ambos server-to-server y sin tocar esos repos.

Arranque en Render:
  uvicorn main:app --host 0.0.0.0 --port $PORT
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import avatar, paciente

app = FastAPI(title="Avatar Hipokratia API")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.vercel\.app|https://([a-z0-9-]+\.)*hipokratia\.health|https://([a-z0-9-]+\.)*icarticular\.cl|http://localhost:\d+",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(avatar.router)
app.include_router(paciente.router)


@app.get("/")
def raiz():
    return {"servicio": "avatar-hipokratia", "ok": True}
  
