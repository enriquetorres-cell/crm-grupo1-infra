# CRM SaaS · CS2032 Cloud Computing · Grupo 1

CRM **multi-tenant, serverless y basado en eventos** en AWS.
5 microservicios con API Gateway + Lambda, cada uno con su propia base de datos, comunicados por SNS → SQS.

Este repo tiene la **infraestructura compartida** (SNS, SQS, RDS), el **código común** y los **scripts** que despliegan todo en orden.

## Repositorios

| Repo | Qué es | BD |
| --- | --- | --- |
| [crm-grupo1-infra](https://github.com/enriquetorres-cell/crm-grupo1-infra) | Tópicos SNS, colas SQS + DLQ, el RDS, `comun/` y los scripts | — |
| [crm-grupo1-ms1-usuarios](https://github.com/enriquetorres-cell/crm-grupo1-ms1-usuarios) | Tenants, login, usuarios, roles y el **Lambda Authorizer** | DynamoDB |
| [crm-grupo1-ms2-clientes](https://github.com/enriquetorres-cell/crm-grupo1-ms2-clientes) | Cuentas y contactos | RDS `clientes_db` |
| [crm-grupo1-ms3-leads](https://github.com/enriquetorres-cell/crm-grupo1-ms3-leads) | Leads, puntaje, asignación, importación CSV por **S3** | DynamoDB |
| [crm-grupo1-ms4-oportunidades](https://github.com/enriquetorres-cell/crm-grupo1-ms4-oportunidades) | Pipeline, cotizaciones (HTML en **S3**), pronóstico | RDS `ventas_db` |
| [crm-grupo1-ms5-actividades](https://github.com/enriquetorres-cell/crm-grupo1-ms5-actividades) | Agenda, historial del cliente, avisos por email | DynamoDB |

## Este repo

```
crm-grupo1-infra/
├── compartido/serverless.yml   # SNS crm-eventos y crm-notificaciones + una cola SQS (y DLQ) por consumidor
├── rds/serverless.yml          # el único RDS PostgreSQL (Patrón 2: una base, un schema por tenant)
├── rds/crear_bases.py          # crea clientes_db y ventas_db (desplegar lo corre solo)
├── comun/comun.py, db.py       # código común: ORIGINAL (las copias viven en cada MS)
├── clonar.sh / .ps1            # clona los 5 repos de MS al lado de este
├── sincronizar-comun.sh / .ps1 # copia comun/ a ../crm-grupo1-msN/src
├── desplegar.sh / .ps1         # despliega los 7 stacks en orden
└── demo-avance.ps1             # demo: 2 tenants, flujo de eventos y aislamiento
```

## Cómo funciona

```
Frontend (Amplify) ── Bearer token ──► API Gateway (1 HTTP API por MS) ──► Authorizer (MS1) ──► Lambda del MS
                                                                                                │
                      tenant_id, usuario_id, rol, plan  ◄── requestContext.authorizer.lambda ───┘
Lambda ── publica ──► SNS crm-eventos ── filtro por atributo "evento" ──► SQS crm-msN ──► Lambda consumidora
```

- **Tenant:** sale SOLO del token. En DynamoDB es la partition key; en PostgreSQL es el nombre del schema
  (`SET search_path TO tenant_x`, validado con regex antes de usarlo).
- **Alta de un tenant:** `POST /ms1/tenants` publica `tenant.creado` y MS2/MS4 crean su schema solos.
- **Idempotencia:** cada consumidor ignora un evento que ya procesó (SQS puede entregarlo dos veces).
- **Errores:** `{error, detalle}` · 422 dato inválido · 409 regla de negocio · 404 no existe en el tenant · 403 rol.
- **Listados:** PostgreSQL usa `?page=&limit=` → `{total, page, limit, items}`. DynamoDB usa `?limit=&siguiente=<cursor>` → `{limit, items, siguiente}`.

### Eventos

| Evento | Publica | Consumen |
| --- | --- | --- |
| `tenant.creado` | MS1 | MS2, MS4 (crean el schema) |
| `usuario.creado` | MS1 | MS3 (el vendedor entra al reparto), MS5 |
| `lead.asignado` | MS3 | MS5 (tarea de primer contacto con plazo) |
| `lead.convertido` | MS3 | MS2 (cuenta + contacto), MS4 (oportunidad), MS5 |
| `oportunidad.etapa_cambiada` | MS4 | MS5 |
| `oportunidad.ganada` | MS4 | MS2 (cliente + segmento), MS5 (email al gerente si > S/ 20 000) |
| `actividad.vencida` | MS5 (cada hora) | MS5 (email) |

## Desplegar desde una EC2 (recomendado)

1. EC2 **Amazon Linux 2023**, tipo **t3.small**, con el perfil de instancia **`LabInstanceProfile`**
   (así toma credenciales sola y no hay que copiar tokens).
2. Instalar las herramientas:
   ```bash
   sudo dnf install -y git nodejs npm python3.12 python3.12-pip
   node -v && python3.12 --version   # Node 18+ y Python 3.12
   ```
3. Clonar y desplegar:
   ```bash
   git clone https://github.com/enriquetorres-cell/crm-grupo1-infra.git
   cd crm-grupo1-infra
   chmod +x *.sh
   ./clonar.sh
   export CRM_DB_PASSWORD='UnaClaveSegura2026'
   export PYTHON=python3.12
   ./desplegar.sh --email tu-correo@gmail.com   # el RDS tarda ~10 min la primera vez
   ```
4. Confirmar el email de SNS que llega al correo.

Un solo servicio: `./desplegar.sh --solo ms3-leads`.

## Desplegar desde Windows

Node 18+, Python 3.12 y AWS CLI con credenciales vigentes en `~/.aws/credentials`:

```powershell
.\clonar.ps1
$env:CRM_DB_PASSWORD = 'UnaClaveSegura2026'
.\desplegar.ps1 -Email tu-correo@gmail.com
.\demo-avance.ps1
```

## Borrar todo (para ahorrar crédito)

`npx serverless remove --stage dev` en cada carpeta, en orden inverso: ms5 → ms1, luego `rds` y `compartido`.
El RDS es lo que más cuesta.

## Simplificaciones del Learner Lab (decirlas en la exposición)

- No se pueden crear roles IAM: todo usa `LabRole`.
- Las Lambdas van fuera de la VPC (para llegar a SNS/SQS/DynamoDB sin NAT), así que el RDS es público,
  protegido por contraseña y SSL. En producción: Lambdas en VPC + VPC endpoints + RDS privado.
- MS2 y MS4 usan el usuario maestro del RDS; en producción cada MS tendría su propio usuario.
- La cotización se guarda como HTML en S3 (el navegador la imprime a PDF).

## Pendiente

- [ ] Generador de datos: 30 000 registros por MS (10 000 por tenant, 3 tenants).
- [ ] Frontend en Amplify (repo aparte).
- [ ] Diagrama de arquitectura, E/R de MS2 y MS4, estructura JSON de MS1, MS3 y MS5.
- [ ] Colección de Postman.
