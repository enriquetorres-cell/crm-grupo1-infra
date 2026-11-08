"""Ingesta de datos ficticios: 10 000 registros por tenant en cada microservicio.

3 tenants (Andina, Nexo, Sol) x 10 000 = 30 000 registros por microservicio:

  MS1 usuarios          10 000 usuarios                                  (DynamoDB)
  MS2 clientes           4 000 cuentas + 6 000 contactos                 (PostgreSQL, schema por tenant)
  MS3 leads             10 000 leads (+ marcas de unicidad y contadores) (DynamoDB)
  MS4 oportunidades      4 000 oportunidades + 3 000 cotizaciones
                         + 3 000 ítems de cotización                     (PostgreSQL, schema por tenant)
  MS5 actividades        6 000 actividades + 4 000 entradas de historial (DynamoDB)

Escribe DIRECTO en cada base (DynamoDB con batch_writer, PostgreSQL con COPY), como una
carga masiva: no pasa por las APIs ni dispara eventos (así no llegan miles de emails).
Cada registro se arma con las reglas de negocio REALES del microservicio
(validar_lead, puntaje, segmento, calcular_cotizacion, ...), cargadas de ../crm-grupo1-msN.

Es reproducible (semilla fija) e idempotente:
  - DynamoDB usa claves deterministas: volver a correrlo sobrescribe, no duplica.
  - PostgreSQL carga cada tenant en UNA transacción y se salta el tenant si ya tiene los datos.

Uso (en la EC2, desde crm-grupo1-infra):
  source ~/.crm-env
  python3.12 datos/generar_datos.py                  # los 5 MS y los 3 tenants
  python3.12 datos/generar_datos.py --ms ms3 ms5     # solo algunos MS
  python3.12 datos/generar_datos.py --simular        # arma y valida todo SIN tocar AWS
  python3.12 datos/generar_datos.py --reiniciar      # PostgreSQL: vacía las tablas del tenant antes
"""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import io
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

import crm_aws as aws

POR_TENANT = 10_000
UTC = dt.timezone.utc
AHORA = dt.datetime.now(UTC).replace(microsecond=0)
INICIO_MES = AHORA.replace(day=1, hour=0, minute=0, second=0)
HACE_UN_ANO = AHORA - dt.timedelta(days=365)

NOMBRES = ['Lucia', 'Mateo', 'Valeria', 'Diego', 'Camila', 'Sebastian', 'Daniela', 'Nicolas', 'Sofia',
           'Gabriel', 'Mariana', 'Andres', 'Paula', 'Jorge', 'Fernanda', 'Luis', 'Carla', 'Ricardo',
           'Ana', 'Miguel', 'Rosa', 'Carlos', 'Elena', 'Javier', 'Patricia', 'Renzo', 'Milagros', 'Cesar']
APELLIDOS = ['Ramos', 'Quispe', 'Flores', 'Mendoza', 'Torres', 'Rojas', 'Vargas', 'Castillo', 'Chavez',
             'Huaman', 'Gutierrez', 'Salazar', 'Paredes', 'Espinoza', 'Cardenas', 'Vasquez', 'Herrera',
             'Soto', 'Aguilar', 'Medina', 'Navarro', 'Rivera', 'Campos', 'Ortiz', 'Silva', 'Romero']
CARGOS = ['Gerente general', 'Gerente comercial', 'Director de operaciones', 'Jefe de compras',
          'Jefe de TI', 'Analista de compras', 'Asistente administrativo', 'Coordinador de logistica',
          'CEO', 'Fundador', 'Supervisor de ventas', 'Contador', 'Practicante']
INDUSTRIAS = ['Retail', 'Construccion', 'Mineria', 'Seguros', 'Educacion', 'Salud', 'Logistica',
              'Manufactura', 'Agroindustria', 'Tecnologia', 'Banca', 'Turismo']
DISTRITOS = ['Miraflores', 'San Isidro', 'Surco', 'La Molina', 'San Borja', 'Lince', 'Jesus Maria',
             'Ate', 'Los Olivos', 'Callao', 'Chorrillos', 'Barranco', 'Magdalena', 'Brena']
PREFIJOS = ['Grupo', 'Corporacion', 'Inversiones', 'Comercial', 'Servicios', 'Distribuidora',
            'Constructora', 'Consultora', 'Industrias', 'Transportes', 'Importadora', 'Agricola']
NUCLEOS = ['Andes', 'Pacifico', 'Inca', 'Lima', 'Sol', 'Condor', 'Amazonas', 'Titicaca', 'Huascaran',
           'Misti', 'Paracas', 'Nazca', 'Chavin', 'Moche', 'Rimac', 'Chimu', 'Wari', 'Qhapaq', 'Vilcanota']
TIPOS_EMPRESA = ['S.A.C.', 'S.A.', 'E.I.R.L.', 'S.R.L.']
MOTIVOS_DESCARTE = ['Sin presupuesto', 'Eligio a la competencia', 'No responde', 'No es decisor', 'Fuera de zona']
MOTIVOS_PERDIDA = ['Precio alto', 'Eligio a la competencia', 'Proyecto cancelado', 'Sin presupuesto este ano']
SERVICIOS = ['Licencias CRM', 'Implementacion', 'Renovacion anual', 'Ampliacion de usuarios', 'Soporte premium']
TITULO_ACTIVIDAD = {'llamada': 'Llamada a', 'reunion': 'Reunion con', 'tarea': 'Enviar propuesta a',
                    'email': 'Correo de seguimiento a', 'nota': 'Nota sobre'}

_lock = threading.Lock()


def avisar(texto):
    with _lock:
        print(texto, flush=True)


# ---------- Ayudas ----------

def rng_de(tenant, ms):
    return random.Random(f'{tenant["id"]}|{ms}')


def id_det(prefijo, *partes):
    """Id con el mismo formato que la API (prefijo + 12 hex) pero determinista."""
    return prefijo + hashlib.md5('|'.join(map(str, partes)).encode()).hexdigest()[:12]


def fecha_entre(rng, desde, hasta):
    return desde + dt.timedelta(seconds=rng.randint(0, max(0, int((hasta - desde).total_seconds()))))


def empresa(rng):
    nucleo = rng.choice(NUCLEOS)
    return f'{rng.choice(PREFIJOS)} {nucleo} {rng.choice(TIPOS_EMPRESA)}', nucleo.lower()


def ruc_valido(reglas_ms2, tenant_idx, n):
    """RUC de empresa (20...) único por tenant y con dígito verificador de SUNAT correcto."""
    primeros = f'20{tenant_idx * 10_000_000 + 1_000_000 + n:08d}'
    return primeros + str(reglas_ms2.digito_verificador_ruc(primeros))


_equipos = {}


def equipo(t):
    """Usuarios del tenant (los usan MS1, y como propietarios MS2-MS5). 10 000 en total."""
    if t['id'] not in _equipos:
        rng, dom = rng_de(t, 'equipo'), t['dominio']
        personas = []
        for i in range(1, POR_TENANT - 1):  # + admin + vendedor de la demo = 10 000
            nombre, apellido = rng.choice(NOMBRES), rng.choice(APELLIDOS)
            personas.append({'email': f'{nombre}.{apellido}{i:05d}@{dom}'.lower(), 'nombre': f'{nombre} {apellido}',
                             'rol': 'gerente' if i <= 50 else 'vendedor'})
        vendedores = [p['email'] for p in personas if p['rol'] == 'vendedor']
        _equipos[t['id']] = {'admin': f'admin@{dom}', 'vendedor_demo': f'vendedor@{dom}',
                             'personas': personas, 'reparto': vendedores[:25]}
    return _equipos[t['id']]


def escribir_dynamo(tabla_ddb, items):
    with tabla_ddb.batch_writer() as lote:
        for item in items:
            lote.put_item(Item=item)


def poner_si_no_existe(tabla_ddb, item, clave):
    from botocore.exceptions import ClientError
    try:
        tabla_ddb.put_item(Item=item, ConditionExpression=f'attribute_not_exists({clave})')
        return True
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False


# ---------- PostgreSQL ----------

def _texto(valor):
    if isinstance(valor, bool):
        return 't' if valor else 'f'
    if isinstance(valor, dt.datetime):
        return aws.iso(valor)
    return str(valor)


def copiar(con, tabla_pg, columnas, filas):
    """COPY ... FROM STDIN (CSV): la forma rápida de cargar miles de filas en PostgreSQL."""
    buffer = io.StringIO()
    escritor = csv.writer(buffer, lineterminator='\n')
    for fila in filas:
        escritor.writerow(['' if v is None else _texto(v) for v in fila])  # vacío sin comillas = NULL
    buffer.seek(0)
    con.run(f'COPY {tabla_pg} ({", ".join(columnas)}) FROM STDIN WITH (FORMAT csv)', stream=buffer)


def crear_schema(con, tenant_id, repo):
    """Lo mismo que hace el MS al recibir tenant.creado (idempotente)."""
    nombre = aws.schema(tenant_id)
    sql = aws.ruta_esquema(repo).read_text(encoding='utf-8')
    sin_comentarios = '\n'.join(l for l in sql.splitlines() if not l.strip().startswith('--'))
    con.run('BEGIN')
    try:
        con.run(f'CREATE SCHEMA IF NOT EXISTS {nombre}')
        con.run(f'SET LOCAL search_path TO {nombre}')
        for sentencia in (s.strip() for s in sin_comentarios.split(';')):
            if sentencia:
                con.run(sentencia)
        con.run('COMMIT')
    except Exception:
        con.run('ROLLBACK')
        raise


def cargar_pg(base, repo, t, tablas, contar_sql, reiniciar, cargar):
    """Carga un tenant en UNA transacción. `cargar(con)` hace los COPY."""
    con = aws.conectar_pg(base)
    try:
        crear_schema(con, t['id'], repo)
        con.run('BEGIN')
        try:
            con.run(f'SET LOCAL search_path TO {aws.schema(t["id"])}')
            if reiniciar:
                con.run(f'TRUNCATE {", ".join(tablas)} RESTART IDENTITY CASCADE')
            existentes = con.run(contar_sql)[0][0]
            if existentes >= POR_TENANT:
                con.run('ROLLBACK')
                return f'ya tenía {existentes} registros (se salta; usa --reiniciar para recargar)'
            cargar(con)
            con.run('COMMIT')
            return None
        except Exception:
            con.run('ROLLBACK')
            raise
    finally:
        con.close()


def base_id(con, tabla_pg):
    return con.run(f'SELECT COALESCE(max(id), 0) FROM {tabla_pg}')[0][0]


def ajustar_secuencia(con, tabla_pg):
    con.run(f"SELECT setval(pg_get_serial_sequence('{tabla_pg}', 'id'), (SELECT COALESCE(max(id), 1) FROM {tabla_pg}))")


# ---------- MS1 · Usuarios ----------

_hash_comun = None


def ms1(t, simular, reiniciar):
    global _hash_comun
    eq, rng = equipo(t), rng_de(t, 'ms1')
    if _hash_comun is None:
        _hash_comun = aws.hash_password('Usuario2026')  # todos los usuarios generados comparten clave
    hash_demo = aws.hash_password(t['clave'])
    usuarios = [
        {'tenant_id': t['id'], 'email': eq['admin'], 'nombre': f'Admin {t["nombre"]}', 'rol': 'admin',
         'password_hash': hash_demo, 'activo': True, 'creado_en': aws.iso(HACE_UN_ANO)},
        {'tenant_id': t['id'], 'email': eq['vendedor_demo'], 'nombre': f'Vendedor {t["nombre"]}', 'rol': 'vendedor',
         'password_hash': hash_demo, 'activo': True, 'creado_en': aws.iso(HACE_UN_ANO)},
    ]
    for p in eq['personas']:
        usuarios.append({'tenant_id': t['id'], 'email': p['email'], 'nombre': p['nombre'], 'rol': p['rol'],
                         'password_hash': _hash_comun, 'activo': rng.random() > 0.03,
                         'creado_en': aws.iso(fecha_entre(rng, HACE_UN_ANO, AHORA))})
    if not simular:
        s = aws.sesion()
        poner_si_no_existe(aws.tabla('ms1', 'tenants', s), {
            'tenant_id': t['id'], 'nombre': t['nombre'], 'plan': t['plan'], 'activo': True,
            'creado_en': aws.iso(HACE_UN_ANO)}, 'tenant_id')
        escribir_dynamo(aws.tabla('ms1', 'usuarios', s), usuarios)
    return {'usuarios': len(usuarios)}


# ---------- MS3 · Leads ----------

def ms3(t, simular, reiniciar):
    r3, r2 = aws.cargar_reglas('ms3-leads'), aws.cargar_reglas('ms2-clientes')
    eq, rng, tid = equipo(t), rng_de(t, 'ms3'), t['id']
    idx = [x['id'] for x in aws.TENANTS].index(tid) + 1
    leads, marcas = [], []
    por_mes, abiertos = collections.Counter(), collections.Counter()
    for n in range(POR_TENANT):
        nombre, apellido = rng.choice(NOMBRES), rng.choice(APELLIDOS)
        razon, nucleo = empresa(rng)
        # Los leads son de los 12 meses ANTERIORES: el mes actual queda libre para el límite del plan.
        creado = fecha_entre(rng, HACE_UN_ANO, INICIO_MES - dt.timedelta(seconds=1))
        email = f'{nombre}.{apellido}@{nucleo}{n}.com.pe'.lower() if rng.random() < 0.9 else None
        telefono = f'9{idx}{n:07d}' if (rng.random() < 0.8 or not email) else None
        lead = r3.validar_lead({
            'empresa': razon, 'nombre': nombre, 'apellido': apellido, 'cargo': rng.choice(CARGOS),
            'email': email, 'telefono': telefono,
            'fuente': rng.choices(list(r3.PUNTOS_FUENTE), [20, 45, 20, 15])[0],
            'ruc': ruc_valido(r2, idx, n) if rng.random() < 0.3 else None,
            'empleados': rng.choice([3, 8, 15, 30, 60, 120, 250, 500]) + rng.randint(0, 9),
            'monto_estimado': Decimal(rng.randrange(1000, 80000, 500)),
            'ultima_interaccion': aws.iso(creado - dt.timedelta(days=rng.randint(0, 15))) if rng.random() < 0.5 else None,
        })
        puntos = r3.puntaje(lead, creado)
        estado = rng.choices(r3.ESTADOS, [30, 25, 15, 15, 15])[0]
        if estado == 'convertido' and puntos < r3.PUNTAJE_MINIMO_CONVERSION:
            estado = 'descartado'
        propietario = rng.choice(eq['reparto'])
        lead_id = id_det('L', tid, n)
        item = {'tenant_id': tid, 'sk': f'LEAD#{lead_id}', 'lead_id': lead_id, **lead,
                'puntaje': puntos, 'temperatura': r3.temperatura(puntos), 'estado': estado,
                'propietario': propietario, 'origen': 'csv', 'creado_en': aws.iso(creado)}
        if estado != 'nuevo':
            item['actualizado_en'] = aws.iso(min(AHORA, creado + dt.timedelta(days=rng.randint(1, 30))))
        if estado == 'convertido':
            item['convertido_en'] = item['actualizado_en']
        if estado == 'descartado':
            item['motivo_descarte'] = rng.choice(MOTIVOS_DESCARTE)
        leads.append(item)
        for prefijo, valor in (('EMAIL', lead['email']), ('TEL', lead['telefono'])):
            if valor:
                marcas.append({'tenant_id': tid, 'sk': f'{prefijo}#{valor}', 'lead_id': lead_id})
        por_mes[creado.strftime('%Y-%m')] += 1
        if estado in r3.ABIERTOS:
            abiertos[propietario] += 1

    contadores = [{'tenant_id': tid, 'sk': f'MES#{mes}', 'total': total} for mes, total in sorted(por_mes.items())]
    cargas = [{'tenant_id': tid, 'vendedor': v, 'abiertos': abiertos[v], 'activo': True,
               'orden': aws.iso(HACE_UN_ANO + dt.timedelta(minutes=k))} for k, v in enumerate(eq['reparto'])]
    if not simular:
        s = aws.sesion()
        escribir_dynamo(aws.tabla('ms3', 'leads', s), leads + marcas + contadores)
        tabla_carga = aws.tabla('ms3', 'carga', s)
        escribir_dynamo(tabla_carga, cargas)
        # El vendedor de la demo entra primero en el reparto (sin leads), así el lead de la demo es suyo.
        poner_si_no_existe(tabla_carga, {'tenant_id': tid, 'vendedor': eq['vendedor_demo'], 'abiertos': 0,
                                         'orden': '2000-01-01T00:00:00Z', 'activo': True}, 'vendedor')
    return {'leads': len(leads), 'marcas EMAIL/TEL': len(marcas), 'contadores MES': len(contadores),
            'vendedores en reparto': len(cargas)}


# ---------- MS5 · Actividades ----------

def ms5(t, simular, reiniciar):
    r5 = aws.cargar_reglas('ms5-actividades')
    eq, rng, tid = equipo(t), rng_de(t, 'ms5'), t['id']
    actividades = []
    for n in range(6_000):
        vendedor = rng.choice(eq['reparto'])
        tipo = rng.choices(r5.TIPOS, [30, 25, 25, 15, 5])[0]
        # Las pendientes vencen dentro de 45 a 180 días: la tarea horaria no las marca
        # vencidas (ni manda emails) durante el semestre.
        if rng.random() < 0.2:
            inicio, estado = fecha_entre(rng, AHORA + dt.timedelta(days=45), AHORA + dt.timedelta(days=180)), 'pendiente'
        else:
            inicio = fecha_entre(rng, HACE_UN_ANO, AHORA - dt.timedelta(days=2))
            estado = rng.choices(['completada', 'cancelada', 'vencida'], [65, 15, 20])[0]
        inicio = inicio.replace(minute=0, second=0)
        razon, _ = empresa(rng)
        datos = {'tipo': tipo, 'titulo': f'{TITULO_ACTIVIDAD[tipo]} {razon}',
                 'ref': f'lead-{id_det("L", tid, rng.randrange(POR_TENANT))}'}
        if tipo == 'reunion':
            datos.update(inicio=aws.iso(inicio), fin=aws.iso(inicio + dt.timedelta(hours=1)))
        else:
            datos['vence_en'] = aws.iso(inicio)
        validada = r5.validar_actividad(datos)
        item = {'tenant_id': tid, 'actividad_id': id_det('A', tid, n), 'vendedor': vendedor, 'estado': estado,
                'creado_por': vendedor, 'creado_en': aws.iso(inicio - dt.timedelta(days=rng.randint(1, 10))),
                **{k: aws.iso(v) if isinstance(v, dt.datetime) else v for k, v in validada.items() if v is not None}}
        if tipo == 'reunion':
            item['gsi_vendedor'] = f'{tid}#{vendedor}'
        if estado == 'pendiente':
            item['gsi_estado'] = 'pendiente'
        elif estado in ('completada', 'cancelada'):
            item['cerrado_en'] = item['vence_en']
        actividades.append(item)

    eventos = [e for e in r5.TITULOS_HISTORIAL if e != 'usuario.creado']
    etapas = ['prospeccion', 'calificacion', 'propuesta', 'negociacion', 'ganada']
    historial = []
    for n in range(4_000):
        lead_id = id_det('L', tid, rng.randrange(POR_TENANT))
        evento = rng.choice(eventos)
        datos = {'lead_id': lead_id}
        if evento == 'oportunidad.etapa_cambiada':
            i = rng.randrange(len(etapas) - 1)
            datos.update(desde=etapas[i], hacia=etapas[i + 1])
        ocurrido = aws.iso(fecha_entre(rng, HACE_UN_ANO, AHORA))
        historial.append({'ref_key': f'{tid}#lead-{lead_id}',
                          'orden': f'{ocurrido}#{uuid.uuid5(uuid.NAMESPACE_URL, f"{tid}|historial|{n}")}',
                          'evento': evento, 'titulo': r5.titulo_historial(evento, datos),
                          'ocurrido_en': ocurrido, 'datos': datos})
    if not simular:
        s = aws.sesion()
        escribir_dynamo(aws.tabla('ms5', 'actividades', s), actividades)
        escribir_dynamo(aws.tabla('ms5', 'historial', s), historial)
    return {'actividades': len(actividades), 'historial': len(historial)}


# ---------- MS2 · Clientes ----------

def ms2(t, simular, reiniciar):
    r2 = aws.cargar_reglas('ms2-clientes')
    eq, rng, tid = equipo(t), rng_de(t, 'ms2'), t['id']
    idx = [x['id'] for x in aws.TENANTS].index(tid) + 1
    cuentas = []
    for i in range(4_000):
        razon, nucleo = empresa(rng)
        estado = rng.choices(['prospecto', 'cliente', 'inactivo'], [45, 40, 15])[0]
        monto = Decimal('0') if estado == 'prospecto' else Decimal(rng.randrange(2000, 150000, 250))
        if estado == 'cliente':
            ultima = fecha_entre(rng, AHORA - dt.timedelta(days=80), AHORA)
        elif estado == 'inactivo':
            ultima = fecha_entre(rng, HACE_UN_ANO, AHORA - dt.timedelta(days=r2.DIAS_PARA_INACTIVO + 5))
        else:
            ultima = None
        creado = fecha_entre(rng, HACE_UN_ANO - dt.timedelta(days=180), ultima or AHORA)
        datos = r2.validar_cuenta({'razon_social': razon, 'ruc': ruc_valido(r2, idx, i) if rng.random() < 0.7 else None,
                                   'email': f'ventas@{nucleo}{i}.com.pe', 'industria': rng.choice(INDUSTRIAS),
                                   'distrito': rng.choice(DISTRITOS)})
        cuentas.append({**datos, 'estado': estado, 'segmento': r2.segmento(monto), 'monto_ganado_12m': monto,
                        'propietario': rng.choice(eq['reparto']), 'creado_en': creado, 'ultima_actividad_en': ultima})
    contactos = []
    for i in range(6_000):
        cuenta = i if i < 4_000 else i - 4_000  # cada cuenta tiene 1 principal; 2 000 tienen un 2.º contacto
        nombre, apellido = rng.choice(NOMBRES), rng.choice(APELLIDOS)
        datos = r2.validar_contacto({'nombre': nombre, 'apellido': apellido,
                                     'email': f'{nombre}.{apellido}{i}@{cuentas[cuenta]["dominio_email"]}'.lower(),
                                     'telefono': f'9{idx}{i:07d}' if rng.random() < 0.7 else None,
                                     'cargo': rng.choice(CARGOS)})
        contactos.append({**datos, 'cuenta': cuenta, 'es_principal': i < 4_000,
                          'creado_en': cuentas[cuenta]['creado_en'] + dt.timedelta(days=rng.randint(0, 30))})
    resumen = {'cuentas': len(cuentas), 'contactos': len(contactos)}
    if simular:
        return resumen

    def cargar(con):
        base_c, base_k = base_id(con, 'cuentas'), base_id(con, 'contactos')
        copiar(con, 'cuentas',
               ['id', 'ruc', 'razon_social', 'dominio_email', 'industria', 'distrito', 'estado', 'segmento',
                'monto_ganado_12m', 'propietario', 'creado_en', 'ultima_actividad_en'],
               ([base_c + 1 + i, c['ruc'], c['razon_social'], c['dominio_email'], c['industria'], c['distrito'],
                 c['estado'], c['segmento'], c['monto_ganado_12m'], c['propietario'], c['creado_en'],
                 c['ultima_actividad_en']] for i, c in enumerate(cuentas)))
        copiar(con, 'contactos',
               ['id', 'cuenta_id', 'nombre', 'apellido', 'email', 'telefono', 'cargo', 'es_principal', 'creado_en'],
               ([base_k + 1 + i, base_c + 1 + k['cuenta'], k['nombre'], k['apellido'], k['email'], k['telefono'],
                 k['cargo'], k['es_principal'], k['creado_en']] for i, k in enumerate(contactos)))
        ajustar_secuencia(con, 'cuentas')
        ajustar_secuencia(con, 'contactos')

    aviso = cargar_pg('clientes_db', 'ms2-clientes', t, ['contactos', 'cuentas'],
                      'SELECT (SELECT count(*) FROM cuentas) + (SELECT count(*) FROM contactos)', reiniciar, cargar)
    return aviso or resumen


# ---------- MS4 · Oportunidades ----------

def ms4(t, simular, reiniciar):
    r4 = aws.cargar_reglas('ms4-oportunidades')
    eq, rng, tid = equipo(t), rng_de(t, 'ms4'), t['id']
    # Mismo catálogo que siembra sql/esquema.sql (ids 1-5 en un schema nuevo).
    catalogo = [(1, Decimal('1200.00')), (2, Decimal('3600.00')), (3, Decimal('5000.00')),
                (4, Decimal('150.00')), (5, Decimal('2400.00'))]
    oportunidades, cotizaciones, items = [], [], []
    for i in range(4_000):
        razon, _ = empresa(rng)
        creado = fecha_entre(rng, HACE_UN_ANO, AHORA - dt.timedelta(days=5))
        con_cotizacion = i < 3_000
        if con_cotizacion:
            etapa = rng.choices(['propuesta', 'negociacion', 'ganada', 'perdida'], [30, 25, 25, 20])[0]
        else:
            etapa = rng.choices(['prospeccion', 'calificacion'], [60, 40])[0]
        cerrada = etapa in r4.FINALES
        cerrado_en = fecha_entre(rng, creado, AHORA) if cerrada else None
        op = {'titulo': f'{rng.choice(SERVICIOS)} para {razon}'[:150], 'cuenta_id': rng.randint(1, 4_000),
              'propietario': rng.choice(eq['reparto']), 'etapa': etapa,
              'monto': Decimal(rng.randrange(3000, 90000, 500)),
              'fecha_cierre_estimada': (cerrado_en or fecha_entre(rng, AHORA, AHORA + dt.timedelta(days=180))).date(),
              'motivo_perdida': rng.choice(MOTIVOS_PERDIDA) if etapa == 'perdida' else None,
              'creado_en': creado, 'actualizado_en': cerrado_en or fecha_entre(rng, creado, AHORA),
              'cerrado_en': cerrado_en}
        if con_cotizacion:
            producto, precio = rng.choice(catalogo)
            cantidad = rng.randint(1, 12)
            calculo = r4.calcular_cotizacion([(cantidad, precio)], rng.choice([0, 5, 10, 15, 20, 25]), 'gerente')
            estado = {'ganada': 'aceptada', 'perdida': 'rechazada', 'negociacion': 'enviada'}.get(
                etapa, rng.choice(['borrador', 'enviada']))
            fecha_cot = fecha_entre(rng, creado, op['actualizado_en'])
            cotizaciones.append({**calculo, 'op': i, 'estado': estado, 'creado_en': fecha_cot,
                                 'vence_en': r4.vencimiento_cotizacion(fecha_cot.date()), 'creado_por': op['propietario']})
            items.append({'cot': len(cotizaciones) - 1, 'producto_id': producto, 'cantidad': cantidad, 'precio': precio})
            op['monto'] = calculo['total']  # el monto de la oportunidad es el de su cotización
        r4.validar_etapa(etapa)
        oportunidades.append(op)
    resumen = {'oportunidades': len(oportunidades), 'cotizaciones': len(cotizaciones), 'items': len(items)}
    if simular:
        return resumen

    def cargar(con):
        base_o, base_c, base_i = base_id(con, 'oportunidades'), base_id(con, 'cotizaciones'), base_id(con, 'cotizacion_items')
        copiar(con, 'oportunidades',
               ['id', 'titulo', 'cuenta_id', 'propietario', 'etapa', 'monto', 'fecha_cierre_estimada',
                'motivo_perdida', 'creado_en', 'actualizado_en', 'cerrado_en'],
               ([base_o + 1 + i, o['titulo'], o['cuenta_id'], o['propietario'], o['etapa'], o['monto'],
                 o['fecha_cierre_estimada'], o['motivo_perdida'], o['creado_en'], o['actualizado_en'], o['cerrado_en']]
                for i, o in enumerate(oportunidades)))
        copiar(con, 'cotizaciones',
               ['id', 'oportunidad_id', 'estado', 'descuento_pct', 'subtotal', 'descuento', 'base_imponible',
                'igv', 'total', 'vence_en', 'creado_por', 'creado_en'],
               ([base_c + 1 + i, base_o + 1 + c['op'], c['estado'], c['descuento_pct'], c['subtotal'], c['descuento'],
                 c['base_imponible'], c['igv'], c['total'], c['vence_en'], c['creado_por'], c['creado_en']]
                for i, c in enumerate(cotizaciones)))
        copiar(con, 'cotizacion_items', ['id', 'cotizacion_id', 'producto_id', 'cantidad', 'precio_unitario'],
               ([base_i + 1 + i, base_c + 1 + it['cot'], it['producto_id'], it['cantidad'], it['precio']]
                for i, it in enumerate(items)))
        for tabla_pg in ('oportunidades', 'cotizaciones', 'cotizacion_items'):
            ajustar_secuencia(con, tabla_pg)

    aviso = cargar_pg('ventas_db', 'ms4-oportunidades', t, ['cotizacion_items', 'cotizaciones', 'oportunidades'],
                      'SELECT (SELECT count(*) FROM oportunidades) + (SELECT count(*) FROM cotizaciones)'
                      ' + (SELECT count(*) FROM cotizacion_items)', reiniciar, cargar)
    return aviso or resumen


GENERADORES = {'ms1': ms1, 'ms2': ms2, 'ms3': ms3, 'ms4': ms4, 'ms5': ms5}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ms', nargs='+', choices=list(GENERADORES), default=list(GENERADORES))
    parser.add_argument('--simular', action='store_true', help='arma y valida los datos sin tocar AWS')
    parser.add_argument('--reiniciar', action='store_true', help='PostgreSQL: vacía las tablas del tenant antes')
    args = parser.parse_args()

    modo = 'SIMULACIÓN (no escribe en AWS)' if args.simular else f'stage {aws.STAGE}, región {aws.REGION}'
    print(f'Ingesta CRM · {POR_TENANT:_} registros por tenant · {modo}\n'.replace('_', ' '))
    # Se importan aquí, en un solo hilo: importar el mismo módulo desde varios hilos a la vez no es seguro.
    for repo in ('ms2-clientes', 'ms3-leads', 'ms4-oportunidades', 'ms5-actividades'):
        aws.cargar_reglas(repo)
    for t in aws.TENANTS:
        equipo(t)
    inicio, errores = time.time(), 0
    trabajos = [(ms, t) for ms in args.ms for t in aws.TENANTS]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futuros = {pool.submit(GENERADORES[ms], t, args.simular, args.reiniciar): (ms, t) for ms, t in trabajos}
        for futuro in as_completed(futuros):
            ms, t = futuros[futuro]
            try:
                resultado = futuro.result()
                detalle = resultado if isinstance(resultado, str) else ', '.join(f'{k} {v}' for k, v in resultado.items())
                avisar(f'  ✔ {ms} · {t["id"]:<14} {detalle}')
            except Exception as e:  # noqa: BLE001 - se informa y se sigue con el resto
                errores += 1
                avisar(f'  ✘ {ms} · {t["id"]:<14} ERROR: {type(e).__name__}: {e}')
    print(f'\nListo en {time.time() - inicio:.0f} s' + (f' con {errores} errores' if errores else ''))
    if not args.simular:
        print('Comprueba los conteos con: python3.12 datos/verificar_datos.py')
    raise SystemExit(1 if errores else 0)


if __name__ == '__main__':
    main()
