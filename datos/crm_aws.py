"""Utilidades que comparten demo.py, generar_datos.py y verificar_datos.py.

Encuentra los recursos desplegados (stacks, tablas, RDS) a partir del stage, y carga
las reglas de negocio REALES de cada microservicio (../crm-grupo1-msN/src/reglas.py)
para que los datos ficticios cumplan las mismas validaciones que la API.
"""
import datetime
import hashlib
import importlib
import importlib.util
import os
import re
import secrets
import ssl
import sys
from pathlib import Path

INFRA = Path(__file__).resolve().parent.parent
RAIZ = INFRA.parent
STAGE = os.environ.get('STAGE', 'dev')
REGION = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-east-1'

# Los 3 tenants de los datos. La demo usa Andina y Nexo (los mismos usuarios y claves).
TENANTS = [
    {'id': 'tenant_andina', 'nombre': 'Andina Seguros', 'plan': 'pro',
     'dominio': 'andina.pe', 'clave': 'DemoAndina2026'},
    {'id': 'tenant_nexo', 'nombre': 'Nexo Retail', 'plan': 'basico',
     'dominio': 'nexo.pe', 'clave': 'DemoNexo2026'},
    {'id': 'tenant_sol', 'nombre': 'Sol Logistica', 'plan': 'enterprise',
     'dominio': 'sollogistica.pe', 'clave': 'DemoSol2026'},
]
_PATRON_SCHEMA = re.compile(r'tenant_[a-z0-9]{3,30}')


def iso(fecha):
    return fecha.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def schema(tenant_id):
    """El tenant_id es el nombre del schema: se valida antes de meterlo en SQL."""
    if not _PATRON_SCHEMA.fullmatch(tenant_id):
        raise ValueError(f'tenant_id inválido: {tenant_id}')
    return tenant_id


# ---------- AWS ----------

def sesion():
    """Una sesión por hilo: los clientes de boto3 no se comparten entre hilos."""
    try:
        import boto3
    except ImportError:
        raise SystemExit('Falta boto3: python3.12 -m pip install --user -r requirements-dev.txt')
    return boto3.session.Session(region_name=REGION)


def salida(stack, clave):
    from botocore.exceptions import ClientError
    try:
        stacks = sesion().client('cloudformation').describe_stacks(StackName=stack)['Stacks']
    except ClientError:
        raise SystemExit(f'No encuentro el stack {stack}. ¿Ya desplegaste?')
    for o in stacks[0].get('Outputs', []):
        if o['OutputKey'] == clave:
            return o['OutputValue']
    raise SystemExit(f'El stack {stack} no tiene el output {clave}')


def url_api(ms):
    """ms = 'ms1-usuarios', ... -> https://xxxx.execute-api.us-east-1.amazonaws.com"""
    return salida(f'crm-{ms}-{STAGE}', 'HttpApiUrl').rstrip('/')


def tabla(ms, nombre, sesion_boto=None):
    """tabla('ms1', 'usuarios') -> crm-ms1-dev-usuarios"""
    return (sesion_boto or sesion()).resource('dynamodb').Table(f'crm-{ms}-{STAGE}-{nombre}')


def conectar_pg(base):
    try:
        import pg8000.native
    except ImportError:
        raise SystemExit('Falta pg8000: python3.12 -m pip install --user -r requirements-dev.txt')
    clave = os.environ.get('CRM_DB_PASSWORD')
    if not clave:
        raise SystemExit('Define CRM_DB_PASSWORD (en la EC2: source ~/.crm-env)')
    contexto_ssl = ssl.create_default_context()
    contexto_ssl.check_hostname = False
    contexto_ssl.verify_mode = ssl.CERT_NONE
    return pg8000.native.Connection(
        user='crm_admin', password=clave, host=salida(f'crm-rds-{STAGE}', 'DbHost'),
        database=base, ssl_context=contexto_ssl, timeout=60,
    )


# ---------- Reglas de negocio de cada MS ----------

def cargar_reglas(repo):
    """Importa src/reglas.py de ../crm-grupo1-<repo> (con su comun.py) como un paquete aparte."""
    nombre = 'reglas_' + repo.replace('-', '_')
    if f'{nombre}.reglas' in sys.modules:
        return sys.modules[f'{nombre}.reglas']
    src = RAIZ / f'crm-grupo1-{repo}' / 'src'
    if not (src / 'reglas.py').is_file():
        raise SystemExit(f'Falta {src}: corre ./clonar.sh desde crm-grupo1-infra')
    spec = importlib.util.spec_from_file_location(
        nombre, src / '__init__.py', submodule_search_locations=[str(src)])
    paquete = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = paquete
    spec.loader.exec_module(paquete)
    return importlib.import_module(f'{nombre}.reglas')


def ruta_esquema(repo):
    return RAIZ / f'crm-grupo1-{repo}' / 'sql' / 'esquema.sql'


def hash_password(password, sal=None):
    """Mismo formato que MS1: <sal hex>$<pbkdf2-sha256 100 000 iteraciones>."""
    sal = sal or secrets.token_hex(16)
    clave = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(sal), 100_000).hex()
    return f'{sal}${clave}'
