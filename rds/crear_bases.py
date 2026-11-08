"""Crea las 2 bases del RDS (una por MS SQL). Se corre UNA vez tras desplegar infra/rds.

Los schemas por tenant NO se crean aquí: MS2 y MS4 los crean solos al recibir
el evento `tenant.creado` (cuando una empresa se registra en MS1).

Uso (PowerShell):
    pip install pg8000
    $env:CRM_DB_PASSWORD = '...'
    python infra/rds/crear_bases.py --host <DbHost del output de crm-rds>
"""
import argparse
import os
import ssl

import pg8000.native

BASES = ('clientes_db', 'ventas_db')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', required=True)
    parser.add_argument('--usuario', default='crm_admin')
    args = parser.parse_args()

    contexto_ssl = ssl.create_default_context()
    contexto_ssl.check_hostname = False
    contexto_ssl.verify_mode = ssl.CERT_NONE

    con = pg8000.native.Connection(
        user=args.usuario,
        password=os.environ['CRM_DB_PASSWORD'],
        host=args.host,
        database='postgres',
        ssl_context=contexto_ssl,
    )
    existentes = {fila[0] for fila in con.run('SELECT datname FROM pg_database')}
    for base in BASES:
        if base in existentes:
            print(f'{base}: ya existe')
        else:
            con.run(f'CREATE DATABASE {base}')
            print(f'{base}: creada')
    con.close()


if __name__ == '__main__':
    main()
