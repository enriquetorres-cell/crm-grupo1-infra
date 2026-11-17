# Diseño de base de datos por microservicio

Generado desde el mismo modelo que los diagramas. E/R editables: [er-ms2-clientes.drawio](er-ms2-clientes.drawio) y [er-ms4-oportunidades.drawio](er-ms4-oportunidades.drawio) (abrir en app.diagrams.net).

## MS2 · Clientes — RDS PostgreSQL, base clientes_db (un schema por tenant)

**cuentas**

| Columna | Tipo | Clave | Nota |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK |  |
| `ruc` | CHAR(11) | UK | RUC SUNAT (módulo 11) |
| `razon_social` | VARCHAR(150) |  | NOT NULL |
| `dominio_email` | VARCHAR(100) |  | detecta duplicados |
| `industria` | VARCHAR(60) |  |  |
| `distrito` | VARCHAR(60) |  |  |
| `estado` | VARCHAR(10) |  | prospecto|cliente|inactivo |
| `segmento` | CHAR(1) |  | A|B|C |
| `monto_ganado_12m` | NUMERIC(12,2) |  |  |
| `propietario` | VARCHAR(120) |  | → MS1 (lógica) |
| `lead_id` | VARCHAR(40) |  | → MS3 (lógica) |
| `creado_en` | TIMESTAMPTZ |  |  |
| `ultima_actividad_en` | TIMESTAMPTZ |  |  |

**contactos**

| Columna | Tipo | Clave | Nota |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK |  |
| `cuenta_id` | BIGINT | FK | → cuentas.id, ON DELETE CASCADE |
| `nombre` | VARCHAR(80) |  | NOT NULL |
| `apellido` | VARCHAR(80) |  |  |
| `email` | VARCHAR(120) |  |  |
| `telefono` | VARCHAR(20) |  |  |
| `cargo` | VARCHAR(80) |  |  |
| `es_principal` | BOOLEAN |  | 1 por cuenta (índice único parcial) |
| `creado_en` | TIMESTAMPTZ |  |  |

**eventos_procesados**

| Columna | Tipo | Clave | Nota |
| --- | --- | --- | --- |
| `id` | UUID | PK | id del evento SNS/SQS |
| `procesado_en` | TIMESTAMPTZ |  |  |

Relaciones: `cuentas` 1─N `contactos` (por `cuenta_id`)

- Un schema por tenant (tenant_andina, tenant_nexo, tenant_sol): la conexión hace SET search_path TO tenant_x.
- ruc UNIQUE y dominio_email: una empresa no se registra dos veces (mismo RUC o mismo dominio corporativo).
- ux_contacto_principal: índice único parcial (cuenta_id) WHERE es_principal → exactamente un contacto principal por cuenta.
- propietario y lead_id son referencias LÓGICAS (sin FK) porque apuntan a la base de otro microservicio.
- eventos_procesados: idempotencia de los consumidores SQS (se inserta en la misma transacción que el cambio).
- Índices: ix_cuentas_dominio, ix_cuentas_propietario, ix_cuentas_lead, ix_contactos_cuenta.

## MS4 · Oportunidades — RDS PostgreSQL, base ventas_db (un schema por tenant)

**oportunidades**

| Columna | Tipo | Clave | Nota |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK |  |
| `titulo` | VARCHAR(150) |  | NOT NULL |
| `cuenta_id` | BIGINT |  | → MS2 (lógica) |
| `lead_id` | VARCHAR(40) | UK | → MS3 (lógica) |
| `propietario` | VARCHAR(120) |  | → MS1 (lógica) |
| `etapa` | VARCHAR(15) |  | 6 etapas (CHECK) |
| `monto` | NUMERIC(12,2) |  | ≥ 0 |
| `fecha_cierre_estimada` | DATE |  |  |
| `motivo_perdida` | VARCHAR(200) |  |  |
| `creado_en` | TIMESTAMPTZ |  |  |
| `actualizado_en` | TIMESTAMPTZ |  |  |
| `cerrado_en` | TIMESTAMPTZ |  |  |

**cotizaciones**

| Columna | Tipo | Clave | Nota |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK |  |
| `oportunidad_id` | BIGINT | FK | → oportunidades.id, CASCADE |
| `estado` | VARCHAR(10) |  | borrador|enviada|aceptada|rechazada |
| `descuento_pct` | NUMERIC(5,2) |  |  |
| `subtotal` | NUMERIC(12,2) |  |  |
| `descuento` | NUMERIC(12,2) |  |  |
| `base_imponible` | NUMERIC(12,2) |  |  |
| `igv` | NUMERIC(12,2) |  | 18 % |
| `total` | NUMERIC(12,2) |  |  |
| `vence_en` | DATE |  | 15 días |
| `documento_key` | VARCHAR(200) |  | HTML en S3 |
| `creado_por` | VARCHAR(120) |  |  |
| `creado_en` | TIMESTAMPTZ |  |  |

**cotizacion_items**

| Columna | Tipo | Clave | Nota |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK |  |
| `cotizacion_id` | BIGINT | FK | → cotizaciones.id, CASCADE |
| `producto_id` | BIGINT | FK | → productos.id |
| `cantidad` | INTEGER |  | > 0 |
| `precio_unitario` | NUMERIC(12,2) |  |  |

**productos**

| Columna | Tipo | Clave | Nota |
| --- | --- | --- | --- |
| `id` | BIGSERIAL | PK |  |
| `codigo` | VARCHAR(20) | UK |  |
| `nombre` | VARCHAR(120) |  | NOT NULL |
| `precio` | NUMERIC(12,2) |  | sin IGV, ≥ 0 |
| `activo` | BOOLEAN |  |  |

**eventos_procesados**

| Columna | Tipo | Clave | Nota |
| --- | --- | --- | --- |
| `id` | UUID | PK | id del evento SNS/SQS |
| `procesado_en` | TIMESTAMPTZ |  |  |

Relaciones: `oportunidades` 1─N `cotizaciones` (por `oportunidad_id`) · `cotizaciones` 1─N `cotizacion_items` (por `cotizacion_id`) · `productos` 1─N `cotizacion_items` (por `producto_id`)

- Un schema por tenant: cada empresa tiene sus propias tablas en ventas_db.
- oportunidades 1─N cotizaciones 1─N cotizacion_items N─1 productos.
- ux_cotizacion_aceptada: índice único parcial (oportunidad_id) WHERE estado = 'aceptada' → una sola cotización aceptada por oportunidad.
- lead_id UNIQUE: el consumidor de lead.convertido es idempotente (ON CONFLICT (lead_id) DO NOTHING).
- CHECK de etapa (prospeccion, calificacion, propuesta, negociacion, ganada, perdida), monto ≥ 0, precio ≥ 0, cantidad > 0.
- cuenta_id, lead_id y propietario son referencias LÓGICAS a MS2, MS3 y MS1. Índices: ix_oportunidades_etapa, ix_oportunidades_propietario.
- Catálogo inicial de 5 productos (licencias, implementación, capacitación, soporte), cargado por esquema.sql al crear el schema.

## MS1 · Usuarios — DynamoDB

Tenants (empresas), usuarios con su rol y los tokens de sesión que valida el Lambda Authorizer.

### `crm-ms1-<stage>-tenants`

- **Partition key:** tenant_id (S) · **Sort key:** — · **Índices:** — · **TTL:** —

| Atributo | Tipo | Descripción |
| --- | --- | --- |
| `tenant_id` | S | tenant_ + 3-30 minúsculas/dígitos; también es el nombre del schema SQL |
| `nombre` | S | razón social |
| `plan` | S | basico | pro | enterprise |
| `activo` | BOOL | tenant suspendido = false |
| `creado_en` | S | ISO-8601 UTC |

```json
{
  "tenant_id": "tenant_andina",
  "nombre": "Andina Seguros",
  "plan": "pro",
  "activo": true,
  "creado_en": "2026-11-08T15:00:00Z"
}
```

Patrones de acceso:

- Alta: put_item con attribute_not_exists(tenant_id)
- Login: get_item(tenant_id)

### `crm-ms1-<stage>-usuarios`

- **Partition key:** tenant_id (S) · **Sort key:** email (S) · **Índices:** — · **TTL:** —

| Atributo | Tipo | Descripción |
| --- | --- | --- |
| `tenant_id` | S | partition key → aislamiento por empresa |
| `email` | S | sort key, en minúsculas |
| `nombre` | S |  |
| `rol` | S | admin | gerente | vendedor |
| `password_hash` | S | PBKDF2-SHA256, 100 000 iteraciones: "<sal>$<hash>" |
| `activo` | BOOL | usuario suspendido = false |
| `creado_en` | S | ISO-8601 UTC |

```json
{
  "tenant_id": "tenant_andina",
  "email": "vendedor@andina.pe",
  "nombre": "Vendedor Andina Seguros",
  "rol": "vendedor",
  "password_hash": "9f2c…$7a41…",
  "activo": true,
  "creado_en": "2026-11-08T15:00:00Z"
}
```

Patrones de acceso:

- Login: get_item(tenant_id, email)
- Listar el equipo: query(tenant_id) paginado
- Límite del plan: query(tenant_id, Select=COUNT)

### `crm-ms1-<stage>-tokens`

- **Partition key:** token (S) · **Sort key:** — · **Índices:** — · **TTL:** expira (epoch)

| Atributo | Tipo | Descripción |
| --- | --- | --- |
| `token` | S | aleatorio, 32 bytes (secrets.token_urlsafe) |
| `tenant_id` | S | lo que el Authorizer entrega |
| `usuario_id` | S | email del usuario |
| `rol` | S |  |
| `plan` | S |  |
| `expira` | N | epoch, 8 h; atributo TTL (DynamoDB lo borra solo) |

```json
{
  "token": "Qm1x…Zp8",
  "tenant_id": "tenant_andina",
  "usuario_id": "vendedor@andina.pe",
  "rol": "vendedor",
  "plan": "pro",
  "expira": 1763190000
}
```

Patrones de acceso:

- Login: put_item
- Lambda Authorizer: get_item(token) en cada petición (cacheado 5 min por API Gateway)

## MS3 · Leads — DynamoDB

Diseño single-table: en una misma partición (el tenant) conviven el lead, sus marcas de unicidad y el contador mensual del plan. Una segunda tabla lleva la carga de cada vendedor para el reparto.

### `crm-ms3-<stage>-leads`

- **Partition key:** tenant_id (S) · **Sort key:** sk (S) · **Índices:** — · **TTL:** —

| Atributo | Tipo | Descripción |
| --- | --- | --- |
| `tenant_id` | S | partition key → aislamiento por empresa |
| `sk` | S | LEAD#<lead_id> · EMAIL#<email> · TEL#<telefono> · MES#<aaaa-mm> |
| `lead_id` | S | L + 12 hexadecimales |
| `empresa, nombre, apellido, cargo` | S |  |
| `email, telefono, ruc` | S | NULL | al menos email o teléfono |
| `fuente` | S | referido | web | evento | llamada_fria |
| `empleados, monto_estimado` | N |  |
| `puntaje` | N | 0 a 100 |
| `temperatura` | S | caliente ≥ 70 | tibio ≥ 40 | frio |
| `estado` | S | nuevo | contactado | calificado | convertido | descartado |
| `propietario` | S | email del vendedor asignado |
| `origen` | S | api | csv |
| `creado_en, actualizado_en, convertido_en` | S | ISO-8601 UTC |
| `motivo_descarte` | S | solo si estado = descartado |
| `total` | N | solo en ítems MES#: leads creados en el mes |

```json
{
  "tenant_id": "tenant_andina",
  "sk": "LEAD#L1a2b3c4d5e6",
  "lead_id": "L1a2b3c4d5e6",
  "empresa": "Constructora Lima",
  "nombre": "Lucía",
  "apellido": "Ramos",
  "cargo": "Gerente de operaciones",
  "email": "lucia@constructoralima.pe",
  "telefono": "987654321",
  "fuente": "referido",
  "ruc": null,
  "empleados": 120,
  "monto_estimado": 18000,
  "puntaje": 80,
  "temperatura": "caliente",
  "estado": "nuevo",
  "propietario": "vendedor@andina.pe",
  "origen": "api",
  "creado_en": "2026-11-08T15:00:00Z"
}
{
  "tenant_id": "tenant_andina",
  "sk": "EMAIL#lucia@constructoralima.pe",
  "lead_id": "L1a2b3c4d5e6"
}
{
  "tenant_id": "tenant_andina",
  "sk": "TEL#987654321",
  "lead_id": "L1a2b3c4d5e6"
}
{
  "tenant_id": "tenant_andina",
  "sk": "MES#2026-11",
  "total": 153
}
```

Patrones de acceso:

- Listar leads: query(tenant_id, begins_with(sk, "LEAD#")) + filtro por estado/temperatura/propietario
- Detalle: get_item(tenant_id, "LEAD#<id>")
- Crear: TransactWriteItems = lead + EMAIL# + TEL# (attribute_not_exists) + MES# (límite del plan) + carga
- Eliminar: TransactWriteItems = borra LEAD#, EMAIL#, TEL# y descuenta la carga

### `crm-ms3-<stage>-carga`

- **Partition key:** tenant_id (S) · **Sort key:** vendedor (S) · **Índices:** — · **TTL:** —

| Atributo | Tipo | Descripción |
| --- | --- | --- |
| `tenant_id` | S |  |
| `vendedor` | S | email |
| `abiertos` | N | leads abiertos asignados |
| `orden` | S | fecha de alta (desempate) |
| `activo` | BOOL |  |

```json
{
  "tenant_id": "tenant_andina",
  "vendedor": "vendedor@andina.pe",
  "abiertos": 4,
  "orden": "2026-11-08T15:00:00Z",
  "activo": true
}
```

Patrones de acceso:

- Asignación: query(tenant_id) → el vendedor activo con menos abiertos
- Alta de vendedor: consumidor de usuario.creado (put_item condicional)

## MS5 · Actividades — DynamoDB

Agenda de cada vendedor con dos índices secundarios dispersos (sparse) y el historial de cada ficha (lead, cuenta o usuario).

### `crm-ms5-<stage>-actividades`

- **Partition key:** tenant_id (S) · **Sort key:** actividad_id (S) · **Índices:** GSI reuniones (gsi_vendedor, inicio) · GSI pendientes (gsi_estado, vence_en) — ambos dispersos, proyección ALL · **TTL:** —

| Atributo | Tipo | Descripción |
| --- | --- | --- |
| `tenant_id` | S | partition key |
| `actividad_id` | S | A + 12 hexadecimales |
| `tipo` | S | llamada | reunion | tarea | email | nota |
| `titulo, notas` | S |  |
| `vendedor` | S | email del responsable |
| `estado` | S | pendiente | completada | cancelada | vencida |
| `ref` | S | lead-<id> | cuenta-<id> |
| `inicio, fin` | S | solo reuniones (máx. 8 h) |
| `vence_en` | S | ISO-8601; en reuniones = fin |
| `creado_por, creado_en, cerrado_en` | S |  |
| `gsi_vendedor` | S | "<tenant>#<vendedor>", solo en reuniones → detecta choques |
| `gsi_estado` | S | "pendiente" solo mientras está pendiente → encuentra las vencidas |

```json
{
  "tenant_id": "tenant_andina",
  "actividad_id": "A9f8e7d6c5b4a",
  "tipo": "reunion",
  "titulo": "Demo con Constructora Lima",
  "vendedor": "vendedor@andina.pe",
  "estado": "pendiente",
  "ref": "lead-L1a2b3c4d5e6",
  "inicio": "2026-11-20T15:00:00Z",
  "fin": "2026-11-20T16:00:00Z",
  "vence_en": "2026-11-20T16:00:00Z",
  "creado_por": "vendedor@andina.pe",
  "creado_en": "2026-11-08T15:00:00Z",
  "gsi_vendedor": "tenant_andina#vendedor@andina.pe",
  "gsi_estado": "pendiente"
}
```

Patrones de acceso:

- Listar: query(tenant_id) + filtro por estado/vendedor
- Choques de agenda: query GSI reuniones (gsi_vendedor, inicio < fin nueva) + filtro fin > inicio nueva
- Tarea horaria: query GSI pendientes (gsi_estado = pendiente, vence_en < ahora)
- Completar/cancelar: update_item condicional + REMOVE gsi_estado · Eliminar: delete_item condicional

### `crm-ms5-<stage>-historial`

- **Partition key:** ref_key (S) · **Sort key:** orden (S) · **Índices:** — · **TTL:** —

| Atributo | Tipo | Descripción |
| --- | --- | --- |
| `ref_key` | S | "<tenant>#<ficha>": la ficha es lead-<id>, cuenta-<id> o usuario-<email> |
| `orden` | S | "<ocurrido_en>#<evento_id>": orden cronológico e idempotencia |
| `evento` | S | tipo de evento recibido por SQS |
| `titulo` | S | texto para la línea de tiempo |
| `ocurrido_en` | S | ISO-8601 UTC |
| `datos` | M | datos del evento |

```json
{
  "ref_key": "tenant_andina#lead-L1a2b3c4d5e6",
  "orden": "2026-11-08T15:05:00Z#5b1e…",
  "evento": "lead.convertido",
  "titulo": "Lead convertido en cliente",
  "ocurrido_en": "2026-11-08T15:05:00Z",
  "datos": {
    "lead_id": "L1a2b3c4d5e6",
    "empresa": "Constructora Lima"
  }
}
```

Patrones de acceso:

- Historial de una ficha: query(ref_key) descendente (ScanIndexForward=false)
- Registrar evento: put_item con attribute_not_exists(orden) → idempotente
