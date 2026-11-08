"""Acceso a PostgreSQL con un schema por tenant (Patrón 2). Lo usan MS2 y MS4.

Se COPIA en ms2-clientes/src y ms4-oportunidades/src con `sincronizar-comun.ps1`.
"""
import os
import re
import ssl
from contextlib import contextmanager
from pathlib import Path

import pg8000.native

from .comun import DatoInvalido, log

_PATRON_SCHEMA = re.compile(r'tenant_[a-z0-9]{3,30}')
_conexion = None


def _conectar():
    contexto_ssl = ssl.create_default_context()
    # RDS PostgreSQL exige SSL. En el lab no se verifica el certificado;
    # en producción se empaqueta el bundle de certificados de RDS.
    contexto_ssl.check_hostname = False
    contexto_ssl.verify_mode = ssl.CERT_NONE
    return pg8000.native.Connection(
        user=os.environ['DB_USUARIO'],
        password=os.environ['DB_CLAVE'],
        host=os.environ['DB_HOST'],
        port=int(os.environ.get('DB_PUERTO', '5432')),
        database=os.environ['DB_NOMBRE'],
        ssl_context=contexto_ssl,
        timeout=5,
    )


def schema_valido(tenant_id):
    """El tenant_id se usa como nombre de schema: se valida SIEMPRE antes de meterlo en SQL."""
    if not isinstance(tenant_id, str) or not _PATRON_SCHEMA.fullmatch(tenant_id):
        raise DatoInvalido('tenant_invalido', tenant_id)
    return tenant_id


def conexion(tenant_id):
    """Conexión reutilizada entre invocaciones, apuntando al schema del tenant."""
    global _conexion
    schema = schema_valido(tenant_id)
    for intento in (1, 2):
        try:
            if _conexion is None:
                _conexion = _conectar()
            _conexion.run(f'SET search_path TO {schema}')
            return _conexion
        except (pg8000.exceptions.InterfaceError, OSError):
            log.warning('Conexión a PostgreSQL perdida, reintentando')
            _conexion = None
            if intento == 2:
                raise


def filas(con, sql, **params):
    resultado = con.run(sql, **params)
    nombres = [c['name'] for c in (con.columns or [])]
    return [dict(zip(nombres, fila)) for fila in resultado]


def fila(con, sql, **params):
    resultado = filas(con, sql, **params)
    return resultado[0] if resultado else None


@contextmanager
def transaccion(con):
    con.run('BEGIN')
    try:
        yield con
        con.run('COMMIT')
    except Exception:
        con.run('ROLLBACK')
        raise


def ya_procesado(con, evento_id):
    """Idempotencia de los consumidores SQS. Llamar DENTRO de la transacción del evento."""
    insertado = fila(
        con,
        'INSERT INTO eventos_procesados (id) VALUES (CAST(:id AS uuid)) ON CONFLICT DO NOTHING RETURNING id',
        id=evento_id,
    )
    return insertado is None


def crear_schema(tenant_id):
    """Al recibir tenant.creado: crea el schema y sus tablas con sql/esquema.sql (idempotente)."""
    schema = schema_valido(tenant_id)
    con = conexion(schema)
    sql = (Path(__file__).resolve().parent.parent / 'sql' / 'esquema.sql').read_text(encoding='utf-8')
    # Se quitan las líneas de comentario y se separa por ';' (el archivo no usa ';' en comentarios).
    sin_comentarios = '\n'.join(l for l in sql.splitlines() if not l.strip().startswith('--'))
    with transaccion(con):
        con.run(f'CREATE SCHEMA IF NOT EXISTS {schema}')
        con.run(f'SET LOCAL search_path TO {schema}')
        for sentencia in (s.strip() for s in sin_comentarios.split(';')):
            if sentencia:
                con.run(sentencia)
    log.info('Schema %s listo', schema)
