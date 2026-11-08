"""Comprueba la ingesta: cuántos registros tiene cada tenant en la base de cada microservicio.

Cuenta directo en las bases (no en las APIs), por tenant:
  MS1  DynamoDB  usuarios                        (partition key = tenant_id)
  MS2  RDS       clientes_db: cuentas + contactos (schema = tenant_id)
  MS3  DynamoDB  leads (solo ítems LEAD#)         (partition key = tenant_id)
  MS4  RDS       ventas_db: oportunidades + cotizaciones + ítems (schema = tenant_id)
  MS5  DynamoDB  actividades + historial          (tenant_id / ref_key "<tenant>#...")

Requisito de la consigna: >= 30 000 registros por microservicio, divididos en 3 tenants.

Uso (en la EC2, desde crm-grupo1-infra):
  source ~/.crm-env
  python3.12 datos/verificar_datos.py               # tabla de conteos
  python3.12 datos/verificar_datos.py --muestra     # + un registro de ejemplo por MS y tenant
"""
import argparse
import json

from boto3.dynamodb.conditions import Attr, Key

import crm_aws as aws

MINIMO_POR_MS = 30_000
MINIMO_POR_TENANT = 10_000


def contar_query(tabla, **kwargs):
    total, inicio = 0, None
    while True:
        if inicio:
            kwargs['ExclusiveStartKey'] = inicio
        respuesta = tabla.query(Select='COUNT', **kwargs)
        total += respuesta['Count']
        inicio = respuesta.get('LastEvaluatedKey')
        if not inicio:
            return total


def contar_scan(tabla, **kwargs):
    total, inicio = 0, None
    while True:
        if inicio:
            kwargs['ExclusiveStartKey'] = inicio
        respuesta = tabla.scan(Select='COUNT', **kwargs)
        total += respuesta['Count']
        inicio = respuesta.get('LastEvaluatedKey')
        if not inicio:
            return total


def contar_pg(con, tenant_id, tablas):
    """{tabla: filas} dentro del schema del tenant; 0 si el schema aún no existe."""
    schema = aws.schema(tenant_id)
    existe = con.run('SELECT 1 FROM information_schema.schemata WHERE schema_name = :s', s=schema)
    if not existe:
        return {t: 0 for t in tablas}
    return {t: con.run(f'SELECT count(*) FROM {schema}.{t}')[0][0] for t in tablas}


def muestra_pg(con, tenant_id, tabla):
    schema = aws.schema(tenant_id)
    filas = con.run(f'SELECT * FROM {schema}.{tabla} ORDER BY id DESC LIMIT 1')
    if not filas:
        return None
    return dict(zip([c['name'] for c in con.columns], filas[0]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--muestra', action='store_true', help='muestra un registro de ejemplo por MS y tenant')
    args = parser.parse_args()

    s = aws.sesion()
    usuarios, leads = aws.tabla('ms1', 'usuarios', s), aws.tabla('ms3', 'leads', s)
    actividades, historial = aws.tabla('ms5', 'actividades', s), aws.tabla('ms5', 'historial', s)
    con_clientes, con_ventas = aws.conectar_pg('clientes_db'), aws.conectar_pg('ventas_db')
    tenants = [t['id'] for t in aws.TENANTS]

    detalle = {ms: {} for ms in ('ms1', 'ms2', 'ms3', 'ms4', 'ms5')}  # detalle[ms][tenant] = {tabla: filas}
    for tid in tenants:
        detalle['ms1'][tid] = {'usuarios': contar_query(usuarios, KeyConditionExpression=Key('tenant_id').eq(tid))}
        detalle['ms2'][tid] = contar_pg(con_clientes, tid, ['cuentas', 'contactos'])
        detalle['ms3'][tid] = {'leads': contar_query(
            leads, KeyConditionExpression=Key('tenant_id').eq(tid) & Key('sk').begins_with('LEAD#'))}
        detalle['ms4'][tid] = contar_pg(con_ventas, tid, ['oportunidades', 'cotizaciones', 'cotizacion_items'])
        detalle['ms5'][tid] = {
            'actividades': contar_query(actividades, KeyConditionExpression=Key('tenant_id').eq(tid)),
            'historial': contar_scan(historial, FilterExpression=Attr('ref_key').begins_with(f'{tid}#')),
        }

    nombres = {
        'ms1': 'MS1 Usuarios       · DynamoDB',
        'ms2': 'MS2 Clientes       · RDS clientes_db',
        'ms3': 'MS3 Leads          · DynamoDB',
        'ms4': 'MS4 Oportunidades  · RDS ventas_db',
        'ms5': 'MS5 Actividades    · DynamoDB',
    }
    ancho = 38
    cortos = [t.removeprefix('tenant_') for t in tenants]
    print(f'\nConteo de registros por tenant · stage {aws.STAGE}\n')
    print(f'{"Microservicio":<{ancho}}' + ''.join(f'{c:>10}' for c in cortos) + f'{"TOTAL":>10}   OK')
    print('-' * (ancho + 10 * len(cortos) + 15))
    todo_ok = True
    for ms in ('ms1', 'ms2', 'ms3', 'ms4', 'ms5'):
        por_tenant = {tid: sum(detalle[ms][tid].values()) for tid in tenants}
        total = sum(por_tenant.values())
        ok = total >= MINIMO_POR_MS and all(v >= MINIMO_POR_TENANT for v in por_tenant.values())
        todo_ok &= ok
        print(f'{nombres[ms]:<{ancho}}' + ''.join(f'{por_tenant[t]:>10,}' for t in tenants)
              + f'{total:>10,}   {"✔" if ok else "✘"}')
        partes = detalle[ms][tenants[0]].keys()
        if len(partes) > 1:
            for parte in partes:
                print(f'{"   └ " + parte:<{ancho}}' + ''.join(f'{detalle[ms][t][parte]:>10,}' for t in tenants))
    print()
    print('✔ Cada microservicio tiene >= 30 000 registros en 3 tenants (>= 10 000 cada uno).' if todo_ok else
          '✘ Algún microservicio no llega a 30 000 registros (o a 10 000 en algún tenant). '
          'Corre: python3.12 datos/generar_datos.py')

    if args.muestra:
        print('\nUn registro de ejemplo por microservicio (cada uno vive en su tenant):')
        for tid in tenants:
            print(f'\n=== {tid} ===')
            ejemplos = {
                'MS1 usuario': usuarios.query(KeyConditionExpression=Key('tenant_id').eq(tid), Limit=1)['Items'],
                'MS2 cuenta': [muestra_pg(con_clientes, tid, 'cuentas')],
                'MS3 lead': leads.query(KeyConditionExpression=Key('tenant_id').eq(tid) & Key('sk').begins_with('LEAD#'),
                                        Limit=1)['Items'],
                'MS4 oportunidad': [muestra_pg(con_ventas, tid, 'oportunidades')],
                'MS5 actividad': actividades.query(KeyConditionExpression=Key('tenant_id').eq(tid), Limit=1)['Items'],
            }
            for nombre, items in ejemplos.items():
                item = (items or [None])[0]
                if item and 'password_hash' in item:
                    item = {**item, 'password_hash': '***'}
                print(f'  {nombre}: {json.dumps(item, default=str, ensure_ascii=False)}')
    con_clientes.close()
    con_ventas.close()
    raise SystemExit(0 if todo_ok else 1)


if __name__ == '__main__':
    main()
