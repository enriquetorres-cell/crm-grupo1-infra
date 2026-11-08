# Demo del avance (30 %): 2 tenants, login, Authorizer, y el flujo de eventos completo
#   lead -> contactado -> calificado -> convertir  ==>  SNS -> SQS -> MS2 (cuenta) + MS4 (oportunidad) + MS5 (historial)
# Al final prueba el aislamiento: Nexo no ve los leads de Andina.
#
# Requisitos: los 7 stacks desplegados y crear_bases.py ya ejecutado.
# Uso: .\demo-avance.ps1            (se puede correr varias veces)
param([string]$Stage = 'dev')
$ErrorActionPreference = 'Stop'

# Credenciales de prueba de la demo (solo para este proyecto).
$tenants = @(
    @{ id = 'tenant_andina'; nombre = 'Andina Seguros'; plan = 'pro';    admin = 'admin@andina.pe'; vendedor = 'vendedor@andina.pe'; clave = 'DemoAndina2026' },
    @{ id = 'tenant_nexo';   nombre = 'Nexo Retail';    plan = 'basico'; admin = 'admin@nexo.pe';   vendedor = 'vendedor@nexo.pe';   clave = 'DemoNexo2026' }
)

function Url($servicio) {
    $u = aws cloudformation describe-stacks --stack-name "crm-$servicio-$Stage" --query "Stacks[0].Outputs[?OutputKey=='HttpApiUrl'].OutputValue" --output text
    if (-not $u -or $u -eq 'None') { throw "No encuentro el stack crm-$servicio-$Stage. ¿Ya desplegaste?" }
    return $u.TrimEnd('/')
}

function Llamar($metodo, $url, $cuerpo = $null, $token = $null) {
    $cabeceras = @{}
    if ($token) { $cabeceras['Authorization'] = "Bearer $token" }
    $parametros = @{ Method = $metodo; Uri = $url; Headers = $cabeceras; ContentType = 'application/json; charset=utf-8' }
    if ($cuerpo) { $parametros['Body'] = [Text.Encoding]::UTF8.GetBytes(($cuerpo | ConvertTo-Json -Depth 5)) }
    try {
        return Invoke-RestMethod @parametros
    } catch {
        $status = $_.Exception.Response.StatusCode.value__
        return [pscustomobject]@{ status = $status; error = $_.ErrorDetails.Message }
    }
}

function Paso($texto) { Write-Host "`n>> $texto" -ForegroundColor Cyan }

$ms1 = Url 'ms1-usuarios'; $ms2 = Url 'ms2-clientes'; $ms3 = Url 'ms3-leads'; $ms4 = Url 'ms4-oportunidades'; $ms5 = Url 'ms5-actividades'

Paso 'Health de los 5 microservicios'
foreach ($par in @(@('ms1', $ms1), @('ms2', $ms2), @('ms3', $ms3), @('ms4', $ms4), @('ms5', $ms5))) {
    Llamar GET "$($par[1])/$($par[0])/health" | Format-Table -AutoSize | Out-String | Write-Host
}

Paso 'Sin token la API responde 401 (Authorizer)'
Llamar GET "$ms3/ms3/leads" | Format-List | Out-String | Write-Host

$sesion = @{}
foreach ($t in $tenants) {
    Paso "Registrar $($t.nombre) ($($t.id), plan $($t.plan))"
    $r = Llamar POST "$ms1/ms1/tenants" @{ tenant_id = $t.id; nombre = $t.nombre; plan = $t.plan
        admin = @{ nombre = "Admin $($t.nombre)"; email = $t.admin; password = $t.clave } }
    if ($r.status -eq 409) { Write-Host '   ya existía' } else { Write-Host "   creado: $($r.tenant.tenant_id)" }

    $admin = Llamar POST "$ms1/ms1/login" @{ tenant_id = $t.id; email = $t.admin; password = $t.clave }
    Write-Host "   login admin OK, rol $($admin.usuario.rol), plan $($admin.plan)"

    $r = Llamar POST "$ms1/ms1/usuarios" @{ nombre = "Vendedor $($t.nombre)"; email = $t.vendedor; password = $t.clave; rol = 'vendedor' } $admin.token
    if ($r.status -eq 409) { Write-Host '   vendedor ya existía' } else { Write-Host "   vendedor creado: $($r.email)" }
    $sesion[$t.id] = @{ admin = $admin.token }
}

Paso 'Esperando 15 s: usuario.creado viaja por SNS -> SQS -> MS3 (el vendedor entra al reparto)'
Start-Sleep -Seconds 15

$andina = $tenants[0]
$vendedor = Llamar POST "$ms1/ms1/login" @{ tenant_id = $andina.id; email = $andina.vendedor; password = $andina.clave }
$sufijo = Get-Random -Maximum 99999

Paso 'Admin de Andina crea un lead (se puntúa y se asigna solo)'
$lead = Llamar POST "$ms3/ms3/leads" @{ empresa = "Constructora Lima $sufijo"; nombre = 'Lucía'; apellido = 'Ramos'
    cargo = 'Gerente de operaciones'; email = "lucia$sufijo@constructoralima.pe"; telefono = "9$('{0:D8}' -f $sufijo)"
    fuente = 'referido'; empleados = 120; monto_estimado = 18000 } $sesion[$andina.id].admin
$lead | Select-Object lead_id, empresa, puntaje, temperatura, estado, propietario | Format-List | Out-String | Write-Host

Paso 'Regla: no se puede convertir un lead que no está calificado (409)'
Llamar POST "$ms3/ms3/leads/$($lead.lead_id)/convertir" $null $vendedor.token | Format-List | Out-String | Write-Host

Paso 'El vendedor lo trabaja: nuevo -> contactado -> calificado -> convertir'
Llamar PATCH "$ms3/ms3/leads/$($lead.lead_id)/estado" @{ estado = 'contactado' } $vendedor.token | Out-Null
Llamar PATCH "$ms3/ms3/leads/$($lead.lead_id)/estado" @{ estado = 'calificado' } $vendedor.token | Out-Null
$convertido = Llamar POST "$ms3/ms3/leads/$($lead.lead_id)/convertir" $null $vendedor.token
Write-Host "   estado: $($convertido.estado)"

Paso 'Esperando 15 s: lead.convertido llega a MS2, MS4 y MS5'
Start-Sleep -Seconds 15

Paso 'MS2: se creó la cuenta y su contacto principal'
(Llamar GET "$ms2/ms2/cuentas?q=Constructora%20Lima%20$sufijo" $null $vendedor.token).items |
    Select-Object id, razon_social, estado, segmento, propietario, lead_id | Format-Table -AutoSize | Out-String | Write-Host

Paso 'MS4: se abrió la oportunidad en prospección'
(Llamar GET "$ms4/ms4/oportunidades?etapa=prospeccion&limit=5" $null $vendedor.token).items |
    Select-Object id, titulo, etapa, monto, monto_ponderado, lead_id | Format-Table -AutoSize | Out-String | Write-Host

Paso 'MS5: historial del lead y tarea de primer contacto'
(Llamar GET "$ms5/ms5/historial/lead-$($lead.lead_id)" $null $vendedor.token).items |
    Select-Object ocurrido_en, titulo | Format-Table -AutoSize | Out-String | Write-Host
(Llamar GET "$ms5/ms5/actividades?estado=pendiente" $null $vendedor.token).items |
    Select-Object titulo, vence_en, estado | Format-Table -AutoSize | Out-String | Write-Host

Paso 'Multi-tenancy: el admin de Nexo NO ve el lead de Andina (404)'
Llamar GET "$ms3/ms3/leads/$($lead.lead_id)" $null $sesion['tenant_nexo'].admin | Format-List | Out-String | Write-Host
Write-Host "   leads visibles para Nexo: $((Llamar GET "$ms3/ms3/leads" $null $sesion['tenant_nexo'].admin).items.Count)"

Write-Host "`nDemo terminada." -ForegroundColor Green
