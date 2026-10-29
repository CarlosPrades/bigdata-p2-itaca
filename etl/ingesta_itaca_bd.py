import os
import json
import logging
import hashlib
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile
import psycopg2
import psycopg2.extras
from psycopg2 import sql
from dotenv import load_dotenv

# Las rutas parten del proyecto, no de la carpeta desde la que se ejecute Python.
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data" / "raw"
load_dotenv(BASE_DIR / ".env")

# Registramos el progreso y los errores, pero nunca contraseñas ni filas del alumnado.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("ingesta_itaca_bd")

TABLAS = ["alumnos", "contenidos", "calificaciones", "cursos"]

def conectar():
    """Valida la configuración antes de intentar conectar con PostgreSQL."""
    variables = [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    ]
    faltantes = [variable for variable in variables if not os.getenv(variable)]
    if faltantes:
        # Solo mostramos nombres de variables, nunca sus valores.
        raise ValueError(f"Faltan variables de configuración: {', '.join(faltantes)}")

    try:
        puerto = int(os.environ["POSTGRES_PORT"])
    except ValueError:
        raise ValueError("POSTGRES_PORT debe ser un número entero") from None
    if not 1 <= puerto <= 65535:
        raise ValueError("POSTGRES_PORT debe estar entre 1 y 65535")

    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=puerto,
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=10,  # Evita esperar indefinidamente si el servidor no responde.
    )

def anonimizar_nia(nia: str | None) -> str | None:
    """Genera un seudónimo SHA-256 estable, conservando los valores nulos."""
    if nia is None:
        return None
    return hashlib.sha256(str(nia).encode("utf-8")).hexdigest()[:10]

def triaje_alumnos(fila: dict) -> dict:
    """Conserva los campos solicitados de alumnos y sustituye el NIA por su hash."""
    campos = [
        "anyo",
        "nia",
        "sexo",
        "nacionalidad",
        "provincia",
        "municipio",
        "ensenanza",
        "curso",
        "grupo",
        "turno",
        "linea",
        "modalidad",
        "repite",
        "estado_matricula",
        "tipo_matricula",
        "fecha_matricula",
        "fecha_ingreso_centro",
    ]
    alumno = {campo: fila[campo] for campo in campos}
    alumno["nia"] = anonimizar_nia(alumno["nia"])
    return alumno

def triaje_calificaciones(fila: dict) -> dict:
    """Selecciona los campos de calificaciones y protege la referencia al alumno."""
    campos = [
        "anyo",
        "evaluacion",
        "alumno",
        "ensenanza",
        "curso",
        "contenido",
        "bloque_contenido",
        "nota_numerica",
        "tipo_nota",
    ]
    calificacion = {campo: fila[campo] for campo in campos}
    # El mismo identificador genera el mismo hash que en la tabla alumnos.
    calificacion["alumno"] = anonimizar_nia(calificacion["alumno"])
    return calificacion

def triaje_contenidos(fila: dict) -> dict:
    """Conserva únicamente los campos solicitados de contenidos."""
    campos = ["anyo", "codigo", "nombre_cas", "nombre_val", "ensenanza", "curso"]
    return {campo: fila[campo] for campo in campos}

def triaje_cursos(fila: dict) -> dict:
    """Conserva únicamente los campos solicitados de cursos."""
    campos = [
        "anyo",
        "codigo",
        "nombre_cas",
        "nombre_val",
        "abreviatura",
        "ensenanza",
        "padre",
    ]
    return {campo: fila[campo] for campo in campos}

def anonimizar_datos(datos: list[dict], tabla: str) -> list[dict]:
    """Aplica el triaje de cada tabla y el hash del NIA sin modificar el origen."""
    funciones_triaje = {
        "alumnos": triaje_alumnos,
        "calificaciones": triaje_calificaciones,
        "contenidos": triaje_contenidos,
        "cursos": triaje_cursos,
    }
    funcion_triaje = funciones_triaje[tabla]
    return [funcion_triaje(fila) for fila in datos]

def extraer_tabla(conexion, tabla: str) -> list[dict]:
    """Consulta una tabla permitida y cierra el cursor incluso si falla el SELECT."""
    if tabla not in TABLAS:
        raise ValueError("La tabla solicitada no forma parte de esta ingesta")

    with conexion.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Los nombres de esquema y tabla son identificadores, no valores SQL.
        # Identifier los entrecomilla correctamente, sin interpolarlos con f-strings.
        consulta = sql.SQL("SELECT * FROM {}.{}").format(
            sql.Identifier("public"), sql.Identifier(tabla)
        )
        cur.execute(consulta)
        datos = [dict(fila) for fila in cur.fetchall()]

    if not datos:
        logger.warning("La tabla %s está vacía; se exportará una lista vacía", tabla)

    return anonimizar_datos(datos, tabla)

def guardar(datos: list[dict], nombre: str) -> None:
    """Publica el JSON únicamente cuando la escritura ha terminado correctamente."""
    if nombre not in TABLAS:
        raise ValueError("El nombre de salida no corresponde a una tabla permitida")

    fecha = date.today().isoformat()
    ruta = DATA_DIR / f"{nombre}_{fecha}.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Escribimos primero en un archivo temporal de la misma carpeta. Si falla la
    # escritura, el JSON de una ejecución anterior no queda truncado ni incompleto.
    temporal = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=DATA_DIR,
            prefix=f".{nombre}_", suffix=".tmp", delete=False,
        ) as f:
            temporal = Path(f.name)
            json.dump(datos, f, ensure_ascii=False, indent=2, default=str)

        # Sustituye la salida del mismo día solo cuando el JSON ya está completo.
        temporal.replace(ruta)
    finally:
        # También retiramos el temporal si la escritura o la sustitución fallan.
        if temporal is not None:
            temporal.unlink(missing_ok=True)

    logger.info("Guardados %d registros en %s", len(datos), ruta)

def main() -> int:
    """Coordina la ingesta y devuelve 0 si termina bien o 1 si ocurre un error."""
    conexion = None
    try:
        conexion = conectar()
        # Protección adicional: esta sesión no debe escribir en PostgreSQL.
        # No sustituye la recomendación de utilizar un usuario con permisos SELECT.
        conexion.set_session(readonly=True)
        logger.info("Conexión con PostgreSQL establecida en modo de solo lectura")

        for tabla in TABLAS:
            logger.info("Extrayendo la tabla %s", tabla)
            datos = extraer_tabla(conexion, tabla)
            guardar(datos, tabla)

    except psycopg2.Error:
        logger.exception("Error de PostgreSQL: revisa la conexión, las tablas y los permisos")
        return 1
    except OSError:
        logger.exception("Error de archivos: revisa los permisos y el espacio disponible")
        return 1
    except (ValueError, KeyError, TypeError):
        logger.exception("Error de configuración, campos esperados o formato de los datos")
        return 1
    except Exception:
        # Un error inesperado tampoco debe dejar la conexión abierta ni anunciar éxito.
        logger.exception("Error inesperado durante la ingesta")
        return 1
    finally:
        # Se ejecuta tanto al terminar correctamente como al producirse un error.
        if conexion is not None:
            conexion.close()
            logger.info("Conexión con PostgreSQL cerrada")

    logger.info("Ingesta completada correctamente")
    return 0

if __name__ == "__main__":
    # El código de salida permite detectar los fallos desde la terminal o un planificador.
    raise SystemExit(main())
