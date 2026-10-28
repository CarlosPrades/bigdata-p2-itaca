import os
import json
import logging
import hashlib
from datetime import date
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ingesta_itaca_bd")

TABLAS = ["alumnos", "contenidos", "calificaciones", "cursos"]

def conectar():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST"), port=os.getenv("POSTGRES_PORT"),
        dbname=os.getenv("POSTGRES_DB"), user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )

def anonimizar_nia(nia: str | None) -> str | None:
    """Genera un seudónimo SHA-256 estable, conservando los valores nulos."""
    if nia is None:
        return None
    return hashlib.sha256(str(nia).encode("utf-8")).hexdigest()

def extraer_tabla(conexion, tabla: str) -> list[dict]:
    with conexion.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT * FROM {tabla}")
        datos = [dict(fila) for fila in cur.fetchall()]

    # El mismo NIA debe tener el mismo hash en ambas tablas para mantener los cruces.
    campo_alumno = {"alumnos": "nia", "calificaciones": "alumno"}.get(tabla)
    if campo_alumno is not None:
        for fila in datos:
            fila[campo_alumno] = anonimizar_nia(fila[campo_alumno])

    return datos

def guardar(datos: list[dict], nombre: str) -> None:
    fecha = date.today().isoformat()
    ruta = f"data/raw/{nombre}_{fecha}.json"
    os.makedirs("data/raw", exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2, default=str)
    logger.info("Guardados %d registros en %s", len(datos), ruta)

if __name__ == "__main__":
    conexion = conectar()
    for tabla in TABLAS:
        guardar(extraer_tabla(conexion, tabla), tabla)
    conexion.close()
