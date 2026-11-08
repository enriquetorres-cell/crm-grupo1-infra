"""Demo del CRM con 2 tenants: login, Authorizer, reglas y el flujo de eventos completo.

  lead -> contactado -> calificado -> convertir
     ==> SNS crm-eventos -> SQS -> MS2 (cuenta + contacto) + MS4 (oportunidad) + MS5 (historial y tarea)
  y al final el aislamiento: el admin de Nexo NO ve el lead de Andina.

Funciona con o sin la ingesta hecha y se puede correr varias veces.
Solo necesita Python (sin librerías externas salvo boto3 para leer las URLs de CloudFormation).

Uso (en la EC2, desde crm-grupo1-infra):
  python3.12 datos/demo.py
"""
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

import crm_aws as aws

ESPERA_EVENTOS = 15  # segundos para que el evento viaje SNS -> SQS -> Lambda consumidora
DEMO = [t for t in aws.TENANTS if t['id'] in ('tenant_andina', 'tenant_nexo')]


def llamar(metodo, url, cuerpo=None, token=None):
    """Devuelve (status, json). Nunca lanza por 4xx: la demo también muestra los errores."""
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    pedido = urllib.request.Request(url, data=datos, method=metodo,
                                    headers={'Content-Type': 'application/json; charset=utf-8'})
    if token:
        pedido.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(pedido, timeout=30) as r:
            return r.status, json.loads(r.read() or b'null')
    except urllib.error.HTTPError as e:
        texto = e.read()
        try:
            return e.code, json.loads(texto)
        except ValueError:
            return e.code, {'error': texto.decode(errors='replace')}


def paso(texto):
    print(f'\n\033[36m>> {texto}\033[0m')


def mostrar(status, cuerpo, campos=None):
    if campos and isinstance(cuerpo, dict):
        cuerpo = {k: cuerpo.get(k) for k in campos}
    print(f'   HTTP {status}  {json.dumps(cuerpo, ensure_ascii=False, default=str)}')


def tabla(items, campos):
    if not items:
        print('   (sin resultados)')
    for item in items:
        print('   · ' + ' | '.join(f'{c}={item.get(c)}' for c in campos))


def buscar_actividad(url, token, ref, paginas=80):
    """Recorre el listado con el cursor `siguiente` hasta encontrar la actividad de la ficha.

    En DynamoDB `limit` cuenta los ítems leídos ANTES del filtro: con miles de actividades en el
    tenant, una página filtrada puede venir vacía aunque haya más resultados después.
    """
    siguiente = None
    for _ in range(paginas):
        consulta = {'estado': 'pendiente', 'limit': 100}
        if siguiente:
            consulta['siguiente'] = siguiente
        _, pagina = llamar('GET', f'{url}/ms5/actividades?{urllib.parse.urlencode(consulta)}', token=token)
        encontradas = [a for a in pagina.get('items', []) if a.get('ref') == ref]
        if encontradas:
            return encontradas
        siguiente = pagina.get('siguiente')
        if not siguiente:
            return []
    return []


def main():
    print(f'Demo CRM SaaS · stage {aws.STAGE}')
    url = {ms: aws.url_api(ms) for ms in
           ('ms1-usuarios', 'ms2-clientes', 'ms3-leads', 'ms4-oportunidades', 'ms5-actividades')}
    ms1, ms2, ms3, ms4, ms5 = url.values()

    paso('Health de los 5 microservicios')
    for ms, base in url.items():
        status, cuerpo = llamar('GET', f'{base}/{ms.split("-")[0]}/health')
        print(f'   {ms:<18} HTTP {status}  {cuerpo}')

    paso('Sin token la API responde 401 (Lambda Authorizer de MS1)')
    mostrar(*llamar('GET', f'{ms3}/ms3/leads'))

    sesion = {}
    for t in DEMO:
        dom = t['dominio']
        paso(f'Registrar {t["nombre"]} ({t["id"]}, plan {t["plan"]})')
        status, r = llamar('POST', f'{ms1}/ms1/tenants', {
            'tenant_id': t['id'], 'nombre': t['nombre'], 'plan': t['plan'],
            'admin': {'nombre': f'Admin {t["nombre"]}', 'email': f'admin@{dom}', 'password': t['clave']}})
        print('   ya existía (409)' if status == 409 else f'   HTTP {status} creado: {r.get("tenant", {}).get("tenant_id")}')

        status, admin = llamar('POST', f'{ms1}/ms1/login', {'tenant_id': t['id'], 'email': f'admin@{dom}',
                                                            'password': t['clave']})
        if status != 200:
            raise SystemExit(f'   No se pudo iniciar sesión como admin de {t["id"]}: {admin}')
        print(f'   login admin OK · rol {admin["usuario"]["rol"]} · plan {admin["plan"]}')

        status, r = llamar('POST', f'{ms1}/ms1/usuarios', {'nombre': f'Vendedor {t["nombre"]}', 'email': f'vendedor@{dom}',
                                                           'password': t['clave'], 'rol': 'vendedor'}, admin['token'])
        print(f'   vendedor creado: {r.get("email")}' if status == 201 else
              f'   vendedor: HTTP {status} {r.get("error")} (ya existía o el plan llegó a su límite)')
        sesion[t['id']] = admin['token']

    paso(f'Esperando {ESPERA_EVENTOS} s: usuario.creado viaja SNS -> SQS -> MS3 (el vendedor entra al reparto)')
    time.sleep(ESPERA_EVENTOS)

    andina = DEMO[0]
    status, vendedor = llamar('POST', f'{ms1}/ms1/login', {'tenant_id': andina['id'], 'email': f'vendedor@{andina["dominio"]}',
                                                           'password': andina['clave']})
    if status != 200:
        raise SystemExit(f'   No se pudo iniciar sesión como vendedor: {vendedor}')
    token_vendedor = vendedor['token']
    sufijo = random.randint(10_000, 99_999)

    paso('Admin de Andina crea un lead (se puntúa y se asigna solo)')
    status, lead = llamar('POST', f'{ms3}/ms3/leads', {
        'empresa': f'Constructora Lima {sufijo}', 'nombre': 'Lucía', 'apellido': 'Ramos',
        'cargo': 'Gerente de operaciones', 'email': f'lucia{sufijo}@constructoralima.pe',
        'telefono': f'9{sufijo:08d}', 'fuente': 'referido', 'empleados': 120, 'monto_estimado': 18000},
        sesion[andina['id']])
    mostrar(status, lead, ['lead_id', 'empresa', 'puntaje', 'temperatura', 'estado', 'propietario'])
    if status != 201:
        raise SystemExit('   No se pudo crear el lead')
    lead_id = lead['lead_id']

    paso('Regla: no se puede convertir un lead que no está calificado (409)')
    mostrar(*llamar('POST', f'{ms3}/ms3/leads/{lead_id}/convertir', token=token_vendedor))

    paso('El vendedor lo trabaja: nuevo -> contactado -> calificado -> convertir')
    for estado in ('contactado', 'calificado'):
        status, r = llamar('PATCH', f'{ms3}/ms3/leads/{lead_id}/estado', {'estado': estado}, token_vendedor)
        print(f'   -> {estado}: HTTP {status}' + ('' if status == 200 else f' {r}'))
    status, convertido = llamar('POST', f'{ms3}/ms3/leads/{lead_id}/convertir', token=token_vendedor)
    print(f'   -> convertir: HTTP {status} · estado {convertido.get("estado")}')

    paso(f'Esperando {ESPERA_EVENTOS} s: lead.convertido llega a MS2, MS4 y MS5')
    time.sleep(ESPERA_EVENTOS)

    paso('MS2: se creó la cuenta y su contacto principal')
    consulta = urllib.parse.urlencode({'q': f'Constructora Lima {sufijo}'})
    _, cuentas = llamar('GET', f'{ms2}/ms2/cuentas?{consulta}', token=token_vendedor)
    tabla(cuentas.get('items', []), ['id', 'razon_social', 'estado', 'segmento', 'propietario', 'lead_id'])

    paso('MS4: se abrió la oportunidad en prospección')
    _, oportunidades = llamar('GET', f'{ms4}/ms4/oportunidades?etapa=prospeccion&limit=5', token=token_vendedor)
    tabla([o for o in oportunidades.get('items', []) if o.get('lead_id') == lead_id] or oportunidades.get('items', [])[:1],
          ['id', 'titulo', 'etapa', 'monto', 'monto_ponderado', 'lead_id'])

    paso('MS5: historial del lead y tarea de primer contacto')
    _, historial = llamar('GET', f'{ms5}/ms5/historial/lead-{lead_id}', token=token_vendedor)
    tabla(historial.get('items', []), ['ocurrido_en', 'titulo'])
    tabla(buscar_actividad(ms5, token_vendedor, f'lead-{lead_id}'), ['titulo', 'vence_en', 'estado'])

    paso('Multi-tenancy: el admin de Nexo NO ve el lead de Andina (404)')
    mostrar(*llamar('GET', f'{ms3}/ms3/leads/{lead_id}', token=sesion['tenant_nexo']))
    _, lista = llamar('GET', f'{ms3}/ms3/leads', token=sesion['tenant_nexo'])
    ajenos = [l for l in lista.get('items', []) if l.get('tenant_id') != 'tenant_nexo']
    print(f'   primera página de leads de Nexo: {len(lista.get("items", []))} leads, '
          f'{len(ajenos)} de otro tenant')

    print('\n\033[32mDemo terminada.\033[0m')


if __name__ == '__main__':
    main()
