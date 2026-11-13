# CRM SaaS · Documentación del proyecto (CS2032 · Grupo 1)

Guía para entender **qué se construyó, cómo está configurado, cómo funciona cada microservicio y su lógica de negocio**, y cómo desplegarlo y demostrarlo.

> Estado: backend desplegado en AWS (Learner Lab, `us-east-1`, stage `dev`), **30 000 registros por microservicio** cargados y verificados, demo funcionando y frontend publicado en Amplify.

---

## Índice

1. [Qué pide la consigna y cómo se cumple](#1-qué-pide-la-consigna-y-cómo-se-cumple)
2. [Arquitectura](#2-arquitectura)
3. [Repositorios y estructura del código](#3-repositorios-y-estructura-del-código)
4. [Multi-tenancy: cómo se aíslan las empresas](#4-multi-tenancy-cómo-se-aíslan-las-empresas)
5. [Autenticación y Lambda Authorizer](#5-autenticación-y-lambda-authorizer)
6. [Arquitectura basada en eventos (SNS → SQS)](#6-arquitectura-basada-en-eventos-sns--sqs)
7. [Inventario de recursos AWS](#7-inventario-de-recursos-aws)
8. [Microservicios y lógica de negocio](#8-microservicios-y-lógica-de-negocio)
9. [Frontend (Amplify)](#9-frontend-amplify)
10. [Despliegue](#10-despliegue)
11. [Datos ficticios (ingesta y verificación)](#11-datos-ficticios-ingesta-y-verificación)
12. [Demo](#12-demo)
13. [Convenciones del código (para modificarlo)](#13-convenciones-del-código-para-modificarlo)
14. [Problemas encontrados y cómo se resolvieron](#14-problemas-encontrados-y-cómo-se-resolvieron)
15. [Simplificaciones del Learner Lab](#15-simplificaciones-del-learner-lab-decirlas-en-la-exposición)
16. [Pendientes](#16-pendientes)

---

## 1. Qué pide la consigna y cómo se cumple

| Requisito | Cómo se cumple |
| --- | --- |
| SaaS de tipo **CRM** (grupo 1) | Leads, cuentas, contactos, oportunidades, cotizaciones, agenda, usuarios y planes |
| **Multi-tenancy** | El tenant sale del token; DynamoDB lo usa como partition key; PostgreSQL usa **un schema por tenant** |
| **Serverless** | API Gateway (HTTP API) + Lambda (Python 3.12); nada corre en servidores propios |
| **Basada en eventos** | SNS `crm-eventos` → una cola SQS por consumidor (con DLQ) → Lambda consumidora |
| ≥ 5 microservicios **protegidos**, cada uno con **su propia BD** | 5 MS, cada uno con su HTTP API y protegido por el Lambda Authorizer de MS1 |
| SQL: **un único RDS**, Patrón 2 (*Single Database, Multiple Schemas*) | 1 RDS PostgreSQL; MS2 usa `clientes_db` y MS4 `ventas_db`; dentro, `tenant_andina`, `tenant_nexo`, `tenant_sol` |
| NoSQL: **DynamoDB** | MS1, MS3 y MS5 |
| E/R (SQL) o estructura JSON (NoSQL) por MS | `sql/esquema.sql` en MS2/MS4; estructuras JSON en el README de MS1, MS3 y MS5 |
| **30 000 registros** por MS en **3 tenants** | `datos/generar_datos.py` (10 000 por tenant) y `datos/verificar_datos.py` |
| Usar **S3, SNS y SQS** | S3: importación CSV (MS3) y cotizaciones HTML (MS4); SNS: eventos y emails; SQS: colas + DLQ |
| Despliegue automatizado con **Serverless Framework** | 7 `serverless.yml` + `desplegar.sh` despliega todo en orden (incluido el frontend) |
| **Frontend en Amplify** | React + Vite publicado con `desplegar-frontend.sh` |
| Demo con **2 tenants** | `datos/demo.py` (Andina y Nexo) |

---

## 2. Arquitectura

Diagrama completo (íconos oficiales de AWS): [`docs/arquitectura.drawio`](docs/arquitectura.drawio).

```
                         ┌───────────────────────────┐
  Navegador ───────────► │  AWS Amplify (React/Vite) │
                         └─────────────┬─────────────┘
                                       │ HTTPS + "Authorization: Bearer <token>"
        ┌──────────────┬───────────────┼───────────────┬──────────────┐
        ▼              ▼               ▼               ▼              ▼
   HTTP API MS1   HTTP API MS2    HTTP API MS3    HTTP API MS4   HTTP API MS5     (API Gateway)
        │              │               │               │              │
        └──────────────┴──── Lambda Authorizer (vive en MS1) ─────────┘
        │              │               │               │              │
   λ ms1 api      λ ms2 api       λ ms3 api       λ ms4 api      λ ms5 api
        │              │               │  │            │  │           │
   DynamoDB       RDS Postgres    DynamoDB  S3     RDS Postgres  S3  DynamoDB
   tenants        clientes_db     leads   import.  ventas_db    docs actividades
   usuarios       (schema x       carga   (CSV)    (schema x   (HTML) historial
   tokens          tenant)                          tenant)
        │              ▲               │  ▲            │  ▲           │  ▲
        └── publica ───┼───────────────┴──┼────────────┴──┼───────────┘  │
                       │     SNS crm-eventos (filtro por atributo "evento")
                       │               │  │               │              │
                  SQS crm-ms2     SQS crm-ms3        SQS crm-ms4    SQS crm-ms5   (+ DLQ c/u)
                       │               │                  │              │
                λ ms2 consumidor  λ ms3 consumidor  λ ms4 consumidor λ ms5 consumidor
                                                                         │
                                         SNS crm-notificaciones ◄────────┘──► email (gerente)
                         EventBridge (cada hora) ──► λ ms5 revisarVencidas
```

**Flujo típico (convertir un lead):**
1. El vendedor llama `POST /ms3/leads/{id}/convertir` con su token.
2. API Gateway llama al **Authorizer** (MS1), que valida el token y entrega `tenant_id`, `usuario_id`, `rol` y `plan`.
3. La Lambda de MS3 valida la regla (*solo leads calificados con puntaje ≥ 40*), guarda y **publica** `lead.convertido` en SNS.
4. SNS entrega el mensaje a las colas cuyo filtro lo acepta: **MS2, MS4 y MS5**.
5. Cada Lambda consumidora lo procesa: MS2 crea la cuenta y su contacto, MS4 abre una oportunidad y MS5 lo registra en el historial.

---

## 3. Repositorios y estructura del código

| Repo | Contenido |
| --- | --- |
| [crm-grupo1-infra](https://github.com/enriquetorres-cell/crm-grupo1-infra) | SNS, SQS, RDS, código común (`comun/`), scripts de despliegue, ingesta, verificación y demo |
| [crm-grupo1-ms1-usuarios](https://github.com/enriquetorres-cell/crm-grupo1-ms1-usuarios) | Tenants, login, usuarios, roles y Lambda Authorizer |
| [crm-grupo1-ms2-clientes](https://github.com/enriquetorres-cell/crm-grupo1-ms2-clientes) | Cuentas y contactos (PostgreSQL) |
| [crm-grupo1-ms3-leads](https://github.com/enriquetorres-cell/crm-grupo1-ms3-leads) | Leads, puntaje, asignación, importación CSV (DynamoDB + S3) |
| [crm-grupo1-ms4-oportunidades](https://github.com/enriquetorres-cell/crm-grupo1-ms4-oportunidades) | Pipeline, cotizaciones, pronóstico (PostgreSQL + S3) |
| [crm-grupo1-ms5-actividades](https://github.com/enriquetorres-cell/crm-grupo1-ms5-actividades) | Agenda, historial, notificaciones (DynamoDB + SNS) |
| [crm-grupo1-frontend](https://github.com/enriquetorres-cell/crm-grupo1-frontend) | React + Vite + Tailwind, publicado en Amplify |

Los repos se clonan **uno al lado del otro** (`./clonar.sh` lo hace solo):

```
carpeta/
├── crm-grupo1-infra/            compartido/  rds/  comun/  datos/  *.sh  *.ps1
├── crm-grupo1-ms1-usuarios/     serverless.yml  src/  tests/
├── crm-grupo1-ms2-clientes/     serverless.yml  src/  tests/  sql/esquema.sql
├── crm-grupo1-ms3-leads/
├── crm-grupo1-ms4-oportunidades/
├── crm-grupo1-ms5-actividades/
└── crm-grupo1-frontend/
```

**Cada microservicio tiene la misma forma:**

| Archivo | Para qué |
| --- | --- |
| `serverless.yml` | Lambdas, rutas de la API, tablas/buckets y permisos (infraestructura como código) |
| `src/handler.py` | Rutas **delgadas**: leer la petición → aplicar la regla → guardar → publicar el evento |
| `src/reglas.py` | ★ **Lógica de negocio pura**: sin base de datos ni HTTP; por eso se prueba sola |
| `src/comun.py` (+ `db.py` en MS2/MS4) | Copia del código común de `crm-grupo1-infra/comun/`. **No se edita aquí**: se edita allá y se corre `sincronizar-comun` |
| `tests/` | Tests de `reglas.py` con pytest (59 tests en total) |

---

## 4. Multi-tenancy: cómo se aíslan las empresas

**Regla de oro: el tenant SIEMPRE sale del token**, nunca del body ni de la URL. Así un usuario de Nexo no puede pedir datos de Andina aunque cambie la petición.

1. Al iniciar sesión, MS1 guarda el token junto con `tenant_id`, `usuario_id`, `rol` y `plan`.
2. En cada petición, el **Authorizer** valida el token y deja esos datos en `requestContext.authorizer.lambda`.
3. `comun.contexto(event)` los lee, y cada handler usa `ctx['tenant_id']`.

**En DynamoDB (MS1, MS3, MS5)**, `tenant_id` es la **partition key**: toda consulta es `Key('tenant_id').eq(ctx['tenant_id'])`, así que es imposible leer otra partición.

**En PostgreSQL (MS2, MS4)** se usa el **Patrón 2**: una base por MS y **un schema por tenant** (`tenant_andina`, `tenant_nexo`, ...).
- Al conectarse, `db.conexion(tenant_id)` ejecuta `SET search_path TO tenant_x`. Las consultas no llevan el nombre del schema.
- Antes de usar el `tenant_id` en SQL se valida con la regex `tenant_[a-z0-9]{3,30}` (`db.schema_valido`), lo que evita inyección SQL.
- **Alta automática:** `POST /ms1/tenants` publica `tenant.creado` y los consumidores de MS2 y MS4 crean el schema y sus tablas con su `sql/esquema.sql`. Nadie lo hace a mano.

**Otra empresa ⇒ 404, nunca 403.** Si un registro no existe *en tu tenant*, la respuesta es `404`. Así no se revela que existe en otro tenant.

**Visibilidad por rol** (`comun.puede_ver`): el **vendedor** solo ve lo suyo (campo `propietario`); **gerente** y **admin** ven todo el tenant.

---

## 5. Autenticación y Lambda Authorizer

| Aspecto | Detalle |
| --- | --- |
| Contraseñas | PBKDF2-SHA256, 100 000 iteraciones, sal aleatoria: se guarda `"<sal>$<hash>"`, nunca la clave |
| Login | `POST /ms1/login {tenant_id, email, password}` → token aleatorio de 32 bytes (`secrets.token_urlsafe`) |
| Duración | **8 horas** (`HORAS_SESION`). Tabla `tokens` con **TTL** en `expira`; el Authorizer también compara la fecha, porque el TTL de DynamoDB borra con retraso |
| Mensaje de error | Igual para "no existe" y "clave incorrecta" (`credenciales_invalidas`), para no revelar qué emails existen |
| Suspensión | Usuario o empresa con `activo = false` → no obtiene token (`403 cuenta_suspendida`) |
| Authorizer | Lambda tipo *request*, payload 2.0, respuestas simples. **API Gateway cachea el resultado 5 minutos** por token |
| Sin token | API Gateway responde `401 Unauthorized` |
| Token inválido o vencido | `403 {"message":"Forbidden"}` (el frontend lo detecta y cierra la sesión) |

El Authorizer vive en MS1. Los otros 4 MS lo referencian con `${cf:crm-ms1-usuarios-dev.AutorizadorArn}`, y cada uno crea un `AWS::Lambda::Permission` para que *su* API pueda invocarlo.

---

## 6. Arquitectura basada en eventos (SNS → SQS)

**Formato de todo evento** (lo arma `comun.publicar`):

```json
{
  "evento": "lead.convertido",
  "tenant_id": "tenant_andina",
  "id": "uuid-único-del-evento",
  "ocurrido_en": "2026-11-08T15:00:00Z",
  "datos": { "lead_id": "L1a2b3c4d5e6", "empresa": "...", "propietario": "..." }
}
```

Se publica con el atributo de mensaje `evento`. Cada suscripción SQS tiene una **FilterPolicy** sobre ese atributo, así que cada cola recibe **solo** los eventos que le importan.

| Evento | Publica | Lo consumen | Qué hace cada consumidor |
| --- | --- | --- | --- |
| `tenant.creado` | MS1 | MS2, MS4 | Crean el schema del tenant (y MS4 el catálogo de productos) |
| `usuario.creado` | MS1 | MS3, MS5 | MS3: si es vendedor, entra al reparto de leads. MS5: historial |
| `lead.asignado` | MS3 | MS5 | Crea la tarea "Primer contacto" con plazo según temperatura |
| `lead.convertido` | MS3 | MS2, MS4, MS5 | MS2: cuenta + contacto. MS4: oportunidad en prospección. MS5: historial |
| `oportunidad.etapa_cambiada` | MS4 | MS5 | Historial |
| `oportunidad.ganada` | MS4 | MS2, MS5 | MS2: la cuenta pasa a cliente y se recalcula el segmento. MS5: email al gerente si es > S/ 20 000 |
| `actividad.vencida` | MS5 (cada hora) | MS5 | Email de aviso |

**Confiabilidad:**
- **Una cola + una DLQ por consumidor.** Tras **3 intentos fallidos** (`maxReceiveCount`) el mensaje pasa a la DLQ, donde se guarda **14 días**.
- `VisibilityTimeout = 60 s`, que es 6 veces el timeout de la Lambda consumidora (10 s), como recomienda AWS.
- **`ReportBatchItemFailures`** (`comun.procesar_cola`): si en un lote de 10 falla 1, solo ese se reintenta.
- **Idempotencia:** SQS puede entregar un mensaje dos veces, así que cada consumidor ignora lo ya procesado:
  - MS2 y MS4: tabla `eventos_procesados (id UUID PK)`, insertada en la **misma transacción** que el cambio.
  - MS3: `put_item` condicional (`attribute_not_exists`).
  - MS5: el historial usa `orden = <fecha>#<id del evento>` con escritura condicional.
- MS4 publica **después del COMMIT**: nunca anuncia algo que no quedó guardado.
- La política de las colas solo permite escribir al tópico `crm-eventos` (`aws:SourceArn`).

**Emails:** el tópico `crm-notificaciones` tiene suscrito el correo indicado en `desplegar.sh --email ...`. AWS manda un correo *"Subscription Confirmation"* que hay que confirmar.

---

## 7. Inventario de recursos AWS

### 7 stacks de CloudFormation (todos por Serverless Framework)

| Stack | Recursos |
| --- | --- |
| `crm-compartido-dev` | SNS `crm-eventos-dev`, SNS `crm-notificaciones-dev`, SQS `crm-ms2/3/4/5-dev` + 4 DLQ, 4 suscripciones con filtro, suscripción email, política de colas |
| `crm-rds-dev` | RDS PostgreSQL `crm-dev` (db.t3.micro, 20 GB gp2, sin Multi-AZ, sin backups) + security group 5432. Dentro: `clientes_db` y `ventas_db` |
| `crm-ms1-usuarios-dev` | HTTP API, 2 Lambdas, 3 tablas DynamoDB |
| `crm-ms2-clientes-dev` | HTTP API, 2 Lambdas, permiso al Authorizer |
| `crm-ms3-leads-dev` | HTTP API, 3 Lambdas, 2 tablas DynamoDB, bucket S3 de importaciones |
| `crm-ms4-oportunidades-dev` | HTTP API, 2 Lambdas, bucket S3 de documentos |
| `crm-ms5-actividades-dev` | HTTP API, 3 Lambdas, 2 tablas DynamoDB (con 2 GSI), regla programada de EventBridge |

Más la app de **Amplify** `crm-grupo1-frontend` (rama `main`), que se crea con `desplegar-frontend.sh`.

### Las 12 Lambdas

Todas: **Python 3.12**, rol `LabRole`, fuera de la VPC. Por defecto **256 MB y 10 s**.

| Lambda | Disparador | Memoria / timeout | Qué hace |
| --- | --- | --- | --- |
| `crm-ms1-usuarios-dev-api` | HTTP API (7 rutas) | 256 MB / 10 s | Tenants, login, usuarios |
| `crm-ms1-usuarios-dev-autorizador` | Authorizer de las 5 APIs | **128 MB** / 10 s | Valida el token y entrega el contexto |
| `crm-ms2-clientes-dev-api` | HTTP API (6 rutas) | 256 MB / 10 s | Cuentas y contactos |
| `crm-ms2-clientes-dev-consumidor` | SQS `crm-ms2` (lotes de 10) | 256 MB / 10 s | `tenant.creado`, `lead.convertido`, `oportunidad.ganada` |
| `crm-ms3-leads-dev-api` | HTTP API (9 rutas) | 256 MB / 10 s | Leads e importaciones |
| `crm-ms3-leads-dev-importar` | S3 `entrada/*.csv` | **512 MB / 300 s** | Procesa el CSV subido |
| `crm-ms3-leads-dev-consumidor` | SQS `crm-ms3` | 256 MB / 10 s | `usuario.creado` |
| `crm-ms4-oportunidades-dev-api` | HTTP API (9 rutas) | 256 MB / 10 s | Oportunidades, cotizaciones, pronóstico |
| `crm-ms4-oportunidades-dev-consumidor` | SQS `crm-ms4` | 256 MB / 10 s | `tenant.creado`, `lead.convertido` |
| `crm-ms5-actividades-dev-api` | HTTP API (6 rutas) | 256 MB / 10 s | Agenda e historial |
| `crm-ms5-actividades-dev-consumidor` | SQS `crm-ms5` | 256 MB / 10 s | 6 tipos de evento |
| `crm-ms5-actividades-dev-revisarVencidas` | EventBridge `rate(1 hour)` | 256 MB / **60 s** | Marca actividades vencidas y publica `actividad.vencida` |

### Bases de datos y almacenamiento

| Recurso | MS | Clave / estructura |
| --- | --- | --- |
| DynamoDB `crm-ms1-dev-tenants` | MS1 | `tenant_id` |
| DynamoDB `crm-ms1-dev-usuarios` | MS1 | `tenant_id` + `email` |
| DynamoDB `crm-ms1-dev-tokens` | MS1 | `token` (TTL `expira`) |
| PostgreSQL `clientes_db` | MS2 | schema por tenant: `cuentas`, `contactos`, `eventos_procesados` |
| DynamoDB `crm-ms3-dev-leads` | MS3 | `tenant_id` + `sk` (single-table: `LEAD#`, `EMAIL#`, `TEL#`, `MES#`) |
| DynamoDB `crm-ms3-dev-carga` | MS3 | `tenant_id` + `vendedor` |
| S3 `crm-importaciones-<cuenta>-dev` | MS3 | `entrada/`, `resultados/`, `errores/` (privado, CORS para PUT) |
| PostgreSQL `ventas_db` | MS4 | schema por tenant: `productos`, `oportunidades`, `cotizaciones`, `cotizacion_items`, `eventos_procesados` |
| S3 `crm-documentos-<cuenta>-dev` | MS4 | `cotizaciones/<tenant>/<id>.html` (privado; se entrega con URL firmada) |
| DynamoDB `crm-ms5-dev-actividades` | MS5 | `tenant_id` + `actividad_id`; GSI `reuniones` y GSI `pendientes` |
| DynamoDB `crm-ms5-dev-historial` | MS5 | `ref_key` (`<tenant>#<ficha>`) + `orden` |

Todas las tablas DynamoDB están en modo **on-demand** (`PAY_PER_REQUEST`).

### URLs actuales (stage `dev`)

| Servicio | URL |
| --- | --- |
| Frontend | https://main.d2xbyfokesbhj7.amplifyapp.com |
| MS1 | https://2713kiu4u7.execute-api.us-east-1.amazonaws.com |
| MS2 | https://mnfkhqb5w0.execute-api.us-east-1.amazonaws.com |
| MS3 | https://6rmbwcownf.execute-api.us-east-1.amazonaws.com |
| MS4 | https://rwaagpkjxk.execute-api.us-east-1.amazonaws.com |
| MS5 | https://sqtdtdevyh.execute-api.us-east-1.amazonaws.com |

> Si se borra y se vuelve a desplegar, las URLs cambian. `desplegar.sh` las imprime al final y el frontend las vuelve a leer solo.

---

## 8. Microservicios y lógica de negocio

**Errores comunes a todos** (`{error, detalle}`):

| Código | Cuándo |
| --- | --- |
| `422` | Dato con formato inválido (`DatoInvalido`) |
| `409` | Dato válido, pero una regla de negocio lo impide (`ReglaNoCumplida`) |
| `404` | No existe **en tu tenant** (`NoEncontrado`) |
| `403` | Tu rol no alcanza (`SinPermiso`) |
| `500` | Error no controlado (queda en CloudWatch Logs) |

**Listados:**
- PostgreSQL usa `?page=&limit=` y devuelve `{total, page, limit, items}`.
- DynamoDB no puede saltar a la página N: usa `?limit=&siguiente=<cursor>` y devuelve `{limit, items, siguiente}`.

`limit` va de 1 a 100 (por defecto 20).

---

### MS1 · Tenants y Usuarios (DynamoDB)

| Método | Ruta | Acceso |
| --- | --- | --- |
| GET | `/ms1/health` | público |
| POST | `/ms1/tenants` | público: alta de empresa + primer admin |
| POST | `/ms1/login` | público |
| GET | `/ms1/me` | token |
| GET | `/ms1/usuarios` | admin, gerente |
| POST | `/ms1/usuarios` | admin |
| POST | `/ms1/usuarios/{email}/suspender` | admin |

**Reglas de negocio** (`reglas.py`):
- **Planes:** básico **5** usuarios / 1 000 leads al mes · pro **20** / 10 000 · enterprise **sin límite**. Crear un usuario por encima del límite → `409 limite_plan`.
- **Roles:** `admin`, `gerente`, `vendedor`.
- **`tenant_id`:** `tenant_` + 3 a 30 minúsculas o dígitos, porque también es el nombre del schema en PostgreSQL.
- **Email:** formato válido, se guarda en minúsculas y es único por empresa (`409 email_existente`).
- **Contraseña:** mínimo 8 caracteres, con letras **y** números.
- **Alta de empresa:** se valida el admin **antes** de crear el tenant, para no dejar empresas sin usuario.
- Un admin **no puede suspenderse a sí mismo**. Los tokens del suspendido siguen sirviendo hasta 5 minutos, por la caché del Authorizer.

---

### MS2 · Clientes (PostgreSQL `clientes_db`)

| Método | Ruta | Qué hace |
| --- | --- | --- |
| GET | `/ms2/cuentas?page=&limit=&estado=&segmento=&q=` | Listado paginado (`q` busca en razón social) |
| GET | `/ms2/cuentas/{id}` | Cuenta con sus contactos |
| POST | `/ms2/cuentas` | Crear cuenta |
| POST | `/ms2/cuentas/{id}/contactos` | Agregar contacto |
| PATCH | `/ms2/contactos/{id}` | `{"es_principal": true}` cambia el contacto principal |

**Modelo (por schema):** `cuentas (id, ruc UNIQUE, razon_social, dominio_email, industria, distrito, estado, segmento, monto_ganado_12m, propietario, lead_id, creado_en, ultima_actividad_en)` 1—N `contactos (id, cuenta_id FK, nombre, apellido, email, telefono, cargo, es_principal)`. Las referencias a otros MS (`propietario` → MS1, `lead_id` → MS3) son **lógicas, sin FK**, porque cada MS tiene su propia base.

**Reglas de negocio:**
- **RUC:** 11 dígitos, empieza con `10` (persona) o `20` (empresa), y el **dígito verificador se calcula con el módulo 11 de SUNAT** (factores 5,4,3,2,7,6,5,4,3,2). Es opcional.
- **Duplicados:** es la misma empresa si tiene **el mismo RUC o el mismo dominio corporativo** del email → `409 cuenta_existente`. Los dominios públicos (gmail, hotmail, outlook, yahoo, live, icloud) no cuentan.
- **Segmento** según lo ganado en 12 meses: **A ≥ S/ 50 000**, **B ≥ S/ 10 000**, **C** el resto.
- **Estados:** `prospecto` → `cliente` (al ganar una oportunidad) → `inactivo` (un cliente sin actividad en **90 días**; la regla existe y está probada, ver pendientes).
- **Contacto principal:** cada cuenta tiene exactamente uno. El primer contacto lo es siempre, y lo garantiza la base con un índice único parcial (`WHERE es_principal`). Cambiarlo desmarca al anterior en la misma transacción.
- El contacto necesita **email o teléfono**.

**Consumidor:**
- `lead.convertido` → crea la cuenta, o se vincula a la existente si es duplicada, y le agrega el contacto.
- `oportunidad.ganada` → la cuenta pasa a `cliente`, suma el monto y recalcula el segmento.

---

### MS3 · Leads (DynamoDB + S3)

| Método | Ruta | Qué hace |
| --- | --- | --- |
| GET | `/ms3/leads?limit=&siguiente=&estado=&temperatura=` | Listado con cursor |
| GET | `/ms3/leads/{id}` | Detalle |
| POST | `/ms3/leads` | Crea, puntúa y asigna |
| PATCH | `/ms3/leads/{id}/estado` | Cambia estado |
| POST | `/ms3/leads/{id}/convertir` | Convierte (publica `lead.convertido`) |
| DELETE | `/ms3/leads/{id}` | Elimina el lead, sus marcas `EMAIL#`/`TEL#` y descuenta la carga (una transacción). Un lead convertido no se elimina (`409`) |
| POST | `/ms3/importaciones` | URL firmada para subir un CSV |
| GET | `/ms3/importaciones/{id}` | Resultado de la importación |

**Puntaje (0 a 100):**

| Criterio | Puntos |
| --- | --- |
| Fuente | referido **30** · web **20** · evento **15** · llamada fría **5** |
| Tamaño (empleados) | ≥ 200: **25** · ≥ 50: **15** · ≥ 10: **8** · menos: **3** |
| Cargo decisor (gerente, director, jefe, CEO, dueño, fundador, presidente) | **+20** |
| Tiene email **y** teléfono | **+15** |
| Interacción en los últimos 7 días | **+10** |

**Temperatura:** **caliente ≥ 70**, **tibio ≥ 40**, **frío** el resto.

**Asignación automática:** al vendedor **activo con menos leads abiertos**; si hay empate, al que entró primero. La tabla `carga` lleva la cuenta, y se actualiza en la **misma transacción** que crea el lead.

**Estados:**
```
nuevo ──► contactado ──► calificado ──► convertido (solo con POST /convertir)
  │            │              │
  └────────────┴──────────────┴──► descartado (exige motivo)
```
- **Convertir** solo un lead **calificado con puntaje ≥ 40**.
- **Unicidad:** no puede haber dos leads con el mismo email o teléfono en un tenant. Se logra con ítems marca `EMAIL#...` y `TEL#...` escritos en la **misma transacción** (`TransactWriteItems`) con `attribute_not_exists`.
- **Límite mensual del plan:** un contador `MES#aaaa-mm` en la misma transacción (básico 1 000, pro 10 000).
- **Bloqueo optimista:** cambiar el estado exige que siga siendo el que leíste (`ConditionExpression`). Si otro usuario lo cambió, devuelve `409 estado_cambiado`.
- Validación: empresa obligatoria, email o teléfono, teléfono de 7 a 12 dígitos, RUC válido, empleados y monto ≥ 0. Devuelve **todos** los errores juntos.

**Importación CSV:**
1. El navegador pide `POST /ms3/importaciones` y recibe una **URL firmada de S3** (válida 15 minutos).
2. Sube el CSV **directo a S3** (sin pasar por Lambda) en `entrada/<tenant>/<plan>/<id>.csv`.
3. S3 dispara la Lambda `importar`, que crea cada fila con las mismas reglas y escribe `resultados/<tenant>/<id>.json`, más `errores/<tenant>/<id>.csv` si hubo rechazos.
4. El frontend consulta `GET /ms3/importaciones/{id}` hasta que diga `terminada`.

Columnas: `empresa, nombre, apellido, cargo, email, telefono, fuente, ruc, empleados, monto_estimado`.

**Consumidor:** `usuario.creado` con rol vendedor → lo agrega a `carga` con 0 leads abiertos.

---

### MS4 · Oportunidades y Cotizaciones (PostgreSQL `ventas_db` + S3)

| Método | Ruta | Qué hace |
| --- | --- | --- |
| GET | `/ms4/oportunidades?page=&limit=&etapa=` | Listado con monto ponderado |
| GET | `/ms4/oportunidades/{id}` | Detalle con cotizaciones (y el link a su documento) |
| POST | `/ms4/oportunidades` | Crear |
| PATCH | `/ms4/oportunidades/{id}/etapa` | Cambiar etapa |
| POST | `/ms4/cotizaciones` | Crear cotización (+ HTML en S3) |
| PATCH | `/ms4/cotizaciones/{id}/estado` | Cambiar estado |
| GET | `/ms4/pronostico` | gerente/admin |
| GET | `/ms4/productos` | Catálogo |

**Modelo (por schema):** `productos`, `oportunidades` 1—N `cotizaciones` 1—N `cotizacion_items` N—1 `productos`. El catálogo inicial tiene 5 productos (licencia básica S/ 1 200, profesional S/ 3 600, implementación S/ 5 000, capacitación por hora S/ 150, soporte premium S/ 2 400; precios sin IGV).

**Etapas y probabilidad:**

| Etapa | Probabilidad | Requisito para entrar |
| --- | --- | --- |
| prospección | 10 % | — |
| calificación | 25 % | — |
| propuesta | 50 % | **al menos una cotización** |
| negociación | 75 % | — |
| ganada | 100 % | **una cotización aceptada** (el monto pasa a ser el total de esa cotización) |
| perdida | 0 % | **motivo** |

- Se avanza **de una en una**; a *perdida* se puede saltar desde cualquier etapa abierta.
- **Retroceder** solo lo puede hacer el **gerente o el admin** (`403` para el vendedor).
- *Ganada* y *perdida* son **finales**.
- El cambio se hace con `SELECT ... FOR UPDATE`, que bloquea la fila durante la transacción.
- **Monto ponderado** = monto × probabilidad. El **pronóstico** suma lo ponderado de las oportunidades abiertas, por vendedor y por mes de cierre.

**Cotizaciones:**
- Cálculo: `subtotal` → menos `descuento` = `base imponible` → **+ IGV 18 %** = `total`, redondeado al céntimo.
- **Descuento máximo por rol:** vendedor **10 %**, gerente **25 %**, admin **100 %** (si se pasa, `403 descuento_excedido`).
- **Vigencia: 15 días.** Una cotización vencida no se puede aceptar.
- Estados: `borrador → enviada → aceptada | rechazada`.
- **Una sola aceptada por oportunidad**, garantizado por un índice único parcial en la base.
- No se cotiza una oportunidad cerrada.
- El documento se guarda como **HTML en S3** (el navegador lo imprime a PDF) y se entrega con una **URL firmada de 1 hora**.

**Consumidor:**
- `tenant.creado` → crea el schema y el catálogo.
- `lead.convertido` → abre una oportunidad en prospección con el monto estimado del lead (idempotente por `lead_id UNIQUE`).

---

### MS5 · Actividades y Notificaciones (DynamoDB + SNS)

| Método | Ruta | Qué hace |
| --- | --- | --- |
| GET | `/ms5/actividades?limit=&siguiente=&estado=` | Listado con cursor |
| POST | `/ms5/actividades` | Crear |
| PATCH | `/ms5/actividades/{id}/estado` | `completada` o `cancelada` |
| DELETE | `/ms5/actividades/{id}` | Elimina la actividad (no si está completada, `409`) |
| GET | `/ms5/historial/{ref}` | `ref` = `lead-<id>`, `cuenta-<id>` o `usuario-<email>` |

**Reglas de negocio:**
- **Tipos:** llamada, reunión, tarea, email, nota.
- **Reuniones:** necesitan inicio y fin, `fin > inicio`, duran **como máximo 8 horas** y **no pueden cruzarse** con otra reunión pendiente o vencida del mismo vendedor (`409 agenda_cruzada`). Que una termine justo cuando empieza la otra **no** cuenta como choque.
  - Para detectarlo se usa el GSI disperso `reuniones` (`gsi_vendedor = <tenant>#<vendedor>`, ordenado por `inicio`).
- **Plazo de primer contacto** (al recibir `lead.asignado`): caliente **24 h**, tibio **72 h**, frío **sin plazo**.
- **Vencidas:** cada hora, `revisarVencidas` consulta el GSI disperso `pendientes` (`gsi_estado = 'pendiente'`, ordenado por `vence_en`), las marca como `vencida` y publica `actividad.vencida`. El consumidor manda entonces el email. Al completar o cancelar una actividad se borra `gsi_estado`, así ya no puede vencer.
- Una actividad **completada o cancelada no cambia más**; una **vencida sí** se puede completar.
- Un **gerente o admin** puede agendar a otro vendedor; un vendedor solo a sí mismo.
- **Aviso al gerente:** email cuando se gana una oportunidad de **más de S/ 20 000**.

**Historial:** cada evento se guarda en la ficha de su lead, su cuenta o, si no tiene ninguna, en la del usuario. Se ordena por fecha, de más reciente a más antiguo.

---

## 9. Frontend (Amplify)

**Stack:** React 18 + Vite 6 + Tailwind 3 + React Router. Se eligió Vite 6 para que compile con el Node 18 de la EC2.

| Pantalla | Qué hace | APIs |
| --- | --- | --- |
| Login / Registro | Login por tenant (con botones de acceso rápido de la demo) y alta de empresa | MS1 |
| Panel | Cuentas, pipeline por etapa, pronóstico (gerente/admin), leads calientes y próximas actividades | MS2–MS5 |
| Leads | Filtros, alta, avanzar estado, descartar, convertir, historial e importación CSV | MS3, MS5 |
| Oportunidades | **Kanban** de 6 etapas con arrastrar y soltar; cotizaciones con IGV y su documento | MS4 |
| Cuentas | Listado paginado con búsqueda; detalle con contactos e historial | MS2, MS5 |
| Agenda | Actividades por estado; crear, completar y cancelar | MS5 |
| Usuarios | Crear (límite del plan) y suspender | MS1 |
| API (Swagger) · `/docs` | Swagger UI con la especificación OpenAPI de las 5 APIs; con sesión iniciada el token se carga solo y se puede probar cada endpoint | las 5 |

**Cómo está hecho:**
- `src/api.ts` es el único lugar que llama a las APIs: agrega `Authorization: Bearer`, convierte `{error, detalle}` en mensajes legibles y cierra la sesión si el token venció (401 o 403 del Authorizer).
- `src/sesion.tsx` guarda la sesión en `localStorage` y la descarta al vencer.
- Las URLs de las APIs llegan por variables de compilación (`VITE_MS1_URL` ... `VITE_MS5_URL`), que `desplegar-frontend.sh` lee de CloudFormation.
- El menú y los botones cambian según el rol, aunque **quien manda es el backend**: el frontend solo oculta lo que no corresponde.

**Despliegue a Amplify** (`desplegar-frontend.sh`, *despliegue manual* de Amplify Hosting, sin conectar GitHub):
1. Lee las 5 URLs de CloudFormation y escribe `.env.production`.
2. `npm ci` + `vite build`.
3. Empaqueta `dist/` en un zip.
4. Crea la app de Amplify la primera vez, con una **regla de reescritura de SPA** para que `/leads` o `/cuentas/12` sirvan `index.html`.
5. Sube el zip, inicia el despliegue y espera a `SUCCEED`.

---

## 10. Despliegue

Se despliega desde una **EC2** del Learner Lab. Así las credenciales son automáticas y no hay que copiar tokens que vencen.

### Crear la EC2 (una vez)
- **Amazon Linux 2023**, **t3.small** (con 1 GB, `npm install` se queda sin memoria), 20 GB, par de claves `vockey`.
- **Perfil de instancia IAM: `LabInstanceProfile`** ← imprescindible.
- Grupo de seguridad con SSH abierto, para conectarse por **EC2 Instance Connect** desde el navegador.

```bash
sudo dnf install -y git nodejs npm python3.12 python3.12-pip
aws sts get-caller-identity            # debe mostrar "LabRole"
git clone https://github.com/enriquetorres-cell/crm-grupo1-infra.git
cd crm-grupo1-infra && chmod +x *.sh && ./clonar.sh
# Clave del RDS: se genera aleatoria y se guarda en ~/.crm-env (solo legible por tu usuario)
echo "export CRM_DB_PASSWORD='$(openssl rand -hex 12)' PYTHON=python3.12 AWS_PAGER=\"\"" > ~/.crm-env && chmod 600 ~/.crm-env
source ~/.crm-env
python3.12 -m pip install --user -r requirements-dev.txt
./desplegar.sh --email correo-del-gerente@gmail.com
```

### Qué hace `desplegar.sh`
1. `sincronizar-comun.sh` copia `comun/comun.py` (y `db.py`) a cada MS.
2. Despliega **en orden** (cada stack lee los outputs del anterior con `${cf:...}`): `compartido` → `rds` (~10 min la primera vez) → crea `clientes_db` y `ventas_db` → `ms1` → `ms2` → `ms3` → `ms4` → `ms5` → `frontend`.
3. Imprime las URLs de las 5 APIs y la del frontend.

### Comandos del día a día

| Qué | Comando (en la EC2, dentro de `crm-grupo1-infra`) |
| --- | --- |
| Volver a entrar a la EC2 | `cd crm-grupo1-infra && source ~/.crm-env` |
| Traer los últimos cambios de los 7 repos | `git pull && ./clonar.sh` |
| Redesplegar un solo servicio (~1–2 min) | `./desplegar.sh --solo ms3-leads` (o `compartido`, `rds`, `msN-x`, `frontend`) |
| Desplegar todo | `./desplegar.sh --email correo@gmail.com` ⚠️ |
| Ver errores de una Lambda | `aws logs tail /aws/lambda/crm-ms3-leads-dev-api --since 30m --format short` |
| Estado de un stack | `aws cloudformation describe-stacks --stack-name crm-ms3-leads-dev --query "Stacks[0].StackStatus" --output text` |
| Borrar todo (ahorrar crédito) | `npx serverless remove --stage dev` en cada carpeta, en orden inverso: ms5 → ms1, luego `rds` y `compartido` |

> ⚠️ **Al desplegar todo, siempre con `--email`.** Sin ese parámetro, el stack `compartido` se actualiza sin correo y **borra la suscripción** a los avisos. Con `--solo` no pasa.

> El aviso amarillo `Invalid configuration ... provider.runtime ... python3.12` es normal: Serverless v3 tiene una lista vieja de runtimes. AWS sí soporta Python 3.12 y el despliegue funciona.

---

## 11. Datos ficticios (ingesta y verificación)

```bash
python3.12 datos/generar_datos.py              # carga (~2–4 min)
python3.12 datos/verificar_datos.py            # tabla de conteos por tenant (para la expo)
python3.12 datos/verificar_datos.py --muestra  # + un registro de ejemplo por MS
python3.12 datos/generar_datos.py --simular    # arma y valida todo sin tocar AWS
```

**3 tenants × 10 000 = 30 000 registros por microservicio:**

| MS | Por tenant |
| --- | --- |
| MS1 | 10 000 usuarios (1 admin, 50 gerentes, el resto vendedores) |
| MS2 | 4 000 cuentas + 6 000 contactos |
| MS3 | 10 000 leads (+ marcas EMAIL/TEL, contadores por mes y carga por vendedor) |
| MS4 | 4 000 oportunidades + 3 000 cotizaciones + 3 000 ítems |
| MS5 | 6 000 actividades + 4 000 entradas de historial |

**Cómo funciona:**
- Escribe **directo** en cada base: DynamoDB con `batch_writer` y PostgreSQL con `COPY`. Es una carga masiva: no pasa por las APIs, así que **no dispara eventos ni emails**.
- Cada registro se arma con las **reglas reales** de cada MS (importa los `reglas.py` de los repos hermanos), así que tiene RUC válidos, puntaje calculado, IGV correcto, un contacto principal por cuenta, etc.
- **Reproducible e idempotente:** usa una semilla fija; en DynamoDB las claves son deterministas (sobrescribe, no duplica); en PostgreSQL cada tenant se carga en una transacción y se salta si ya tiene los datos (`--reiniciar` lo vacía y recarga).
- Los leads son de los 12 meses anteriores, así el mes actual queda libre para el límite del plan.
- Las actividades pendientes vencen dentro de 45 a 180 días, así la tarea horaria no manda emails durante el semestre.

**Usuarios para probar:**

| Tenant | Plan | Admin | Vendedor | Contraseña |
| --- | --- | --- | --- | --- |
| `tenant_andina` | pro | `admin@andina.pe` | `vendedor@andina.pe` | `DemoAndina2026` |
| `tenant_nexo` | básico | `admin@nexo.pe` | `vendedor@nexo.pe` | `DemoNexo2026` |
| `tenant_sol` | enterprise | `admin@sollogistica.pe` | `vendedor@sollogistica.pe` | `DemoSol2026` |

Los 10 000 usuarios generados por tenant usan la contraseña `Usuario2026`.

---

## 12. Demo

**CRUD en DynamoDB para la exposición** (en la consola: DynamoDB → Tablas → `crm-ms3-dev-leads` → Explorar elementos, filtrando por `tenant_id`):
1. `POST /ms3/leads` → aparece el ítem `LEAD#...` (y sus marcas `EMAIL#`/`TEL#`).
2. `PATCH /ms3/leads/{id}/estado` → cambia `estado` y `actualizado_en`.
3. `DELETE /ms3/leads/{id}` → desaparecen el lead y sus marcas.

Se puede hacer desde la web (pantalla Leads) o desde Swagger (`/docs`).

```bash
python3.12 datos/demo.py
```

Recorre, contra las APIs reales:
1. Health de los 5 MS y `401` sin token (Authorizer).
2. Registro de Andina y Nexo (o "ya existía"), login del admin y alta del vendedor.
3. El admin crea un lead: se puntúa (80, caliente) y se asigna solo al vendedor.
4. Intento de convertirlo sin calificar → `409`.
5. El vendedor lo trabaja: contactado → calificado → convertir.
6. Tras ~15 s, el evento `lead.convertido` llegó: **cuenta + contacto en MS2**, **oportunidad en MS4**, **historial y tarea en MS5**.
7. **Aislamiento:** el admin de Nexo pide ese lead y recibe `404`.

En la web se puede mostrar lo mismo desde las pantallas Leads, Cuentas, Oportunidades y Agenda.

---

## 13. Convenciones del código (para modificarlo)

**Agregar un endpoint:**
1. La regla de negocio va en `reglas.py`: lanza `DatoInvalido` (422), `ReglaNoCumplida` (409), `NoEncontrado` (404) o `SinPermiso` (403). Escríbele un test en `tests/`.
2. La función de ruta va en `handler.py` con firma `def mi_ruta(event, ctx)`: usa `leer_cuerpo`, `parametro`, `ruta`, `exigir_rol` y `responder(status, cuerpo)`. Regístrala en el diccionario `RUTAS` (`'POST /ms3/algo': mi_ruta`).
3. Declara la ruta en `serverless.yml` con `authorizer: { name: tokenCrm }`.
4. Prueba con `python -m pytest -q` y despliega con `./desplegar.sh --solo msN-x`.

**Publicar un evento:** `publicar('nombre.evento', ctx['tenant_id'], {...datos})`. Para que una cola lo reciba, agrégalo a su `FilterPolicy` en `compartido/serverless.yml` y maneja el evento en el consumidor de ese MS. El consumidor tiene que ser **idempotente**.

**Cambiar el código común:** edita `crm-grupo1-infra/comun/comun.py` (o `db.py`), corre `./sincronizar-comun.sh` y haz commit **en cada repo** de MS que cambió.

**Tests:**

| MS | Tests |
| --- | --- |
| MS1 | 14 |
| MS2 | 15 |
| MS3 | 13 |
| MS4 | 9 |
| MS5 | 8 |
| **Total** | **59** |

Corren sin AWS: `pip install -r requirements-dev.txt && python -m pytest -q` en cada repo.

---

## 14. Problemas encontrados y cómo se resolvieron

| Problema | Causa | Solución |
| --- | --- | --- |
| `POST /ms3/leads` daba **500** (`Incorrect operand type ... operand type: M`) | MS3 tipaba los valores a mano (`{'N': '1'}`) y los pasaba al `meta.client` del *recurso* de boto3, que los vuelve a tipar: el número llegaba como un mapa | Usar `boto3.client('dynamodb')` (cliente de bajo nivel) para las transacciones |
| Listados filtrados de DynamoDB devolvían **páginas vacías** con 10 000 registros | DynamoDB aplica `Limit` **antes** del filtro | `comun.consultar_pagina` sigue leyendo (hasta 10 veces) hasta llenar la página y arma el cursor desde el último ítem devuelto |
| La cotización solo mostraba su documento al crearla | MS4 no devolvía la URL firmada al consultar | `GET /ms4/oportunidades/{id}` devuelve `documento_url` en cada cotización |
| El token vencido dejaba la web con errores | El Authorizer responde `403 Forbidden`, no `401` | El frontend trata ese 403 como sesión vencida |
| `git pull` fallaba en la EC2 (`local changes ... src/comun.py`) | `sincronizar-comun` había copiado una versión antes de que llegara el push | `clonar.sh` descarta las copias de `comun.py`/`db.py` antes del pull |
| `demo-avance.ps1` no corre en la EC2 | Es PowerShell | `datos/demo.py` (Python, corre en cualquier lado) |
| Crear las bases del RDS era un paso manual | — | `desplegar.sh` corre `rds/crear_bases.py` solo después del stack `rds` |

---

## 15. Simplificaciones del Learner Lab (decirlas en la exposición)

- **No se pueden crear roles IAM:** todo usa `LabRole`. En producción, cada Lambda tendría un rol con permisos mínimos.
- **Lambdas fuera de la VPC**, para llegar a SNS, SQS y DynamoDB sin NAT Gateway. Por eso el **RDS es público**, protegido por contraseña y SSL. En producción: Lambdas en la VPC, VPC endpoints y RDS privado.
- MS2 y MS4 usan el **usuario maestro** del RDS. En producción, cada MS tendría su propio usuario con permisos solo sobre su base.
- La clave del RDS va como variable de entorno. En producción iría en **Secrets Manager**.
- RDS sin Multi-AZ ni backups, para ahorrar crédito.
- La cotización se guarda como **HTML** en S3; el navegador la imprime a PDF.
- El segmento se calcula acumulando lo ganado; la ventana real de 12 meses la recalcularía un job.

---

## 16. Pendientes

- [x] Diagrama de arquitectura: [`docs/arquitectura.drawio`](docs/arquitectura.drawio) (abrir en app.diagrams.net → Archivo → Exportar como PNG).
- [ ] E/R de MS2 y MS4 y estructura JSON de MS1, MS3 y MS5 en formato de informe (la base ya está en los `esquema.sql` y los README).
- [ ] Informe (Word/PDF) con evidencias y links a los repos, y resumen en PPT.
- [ ] Colección de Postman (opcional).
- [ ] Tarea programada de MS2 que pase a `inactivo` a los clientes sin actividad en 90 días (la regla `debe_pasar_a_inactivo` ya existe y está probada).

**Fechas:** avance del 30 % hasta el **sábado 14-nov** · entrega final hasta el **domingo 22-nov-2026, 23:59** (Canvas) · exposición presencial y demo en la semana 16.
