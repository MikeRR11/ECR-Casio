# SR-S820 — Bitácora de emparejamientos Bluetooth

Registro de cada vínculo BLE nuevo entre la PC y la caja Casio SR-S820.
Anota una entrada cada vez que se re-empareja (el vínculo se pierde si a la
caja se le quitan las pilas de respaldo o se resetea). El procedimiento y las
trampas están en `docs/protocol/live_findings.md` (sección Pairing).

Herramienta: `app/pair_device.py` (WinRT `DeviceInformationCustomPairing`,
tipo de emparejamiento `PROVIDE_PIN` = la caja muestra un PIN de 6 dígitos y
la PC debe entregarlo).

| Fecha | Dirección BLE | Nombre GATT | Resultado | Notas |
|---|---|---|---|---|
| 2026-07-18 | `08:00:74:57:30:EF` | `CASIO REGISTER` | ✅ ÉXITO (status 0) | Re-emparejamiento tras desincronización de hora. Requirió 2 intentos fallidos antes: status 15 (proceso `pair_device.py` viejo colgado bloqueando) y status 19 (PIN entregado demasiado tarde, venció la ventana de la caja). Éxito al enviar el PIN apenas apareció ("cronometrado"). PIN de esa ceremonia: 036101 (efímero, no reutilizable). |

## Plantilla para nuevas entradas

```
| AAAA-MM-DD | <dir BLE> | <nombre GATT> | ✅/❌ (status N) | <intentos, causa si fallo, gotchas> |
```

## Códigos de resultado observados (WinRT `DevicePairingResultStatus`)

- **0 = Paired** → éxito, el vínculo quedó guardado.
- **15 = OperationAlreadyInProgress** → hay OTRO `pair_device.py` todavía vivo
  reteniendo una ceremonia de emparejamiento. Matar todos los procesos
  `pair_device.py` antes de reintentar (`Get-CimInstance Win32_Process` →
  `Stop-Process`). Correr siempre con `python -u` para ver el progreso; con
  buffer no se nota que el proceso sigue vivo esperando el PIN.
- **19 = Failed (genérico)** → casi siempre **timing**: la caja muestra el PIN
  solo unos segundos y el envío del PIN por chat llegó tarde. Solución: el
  operador manda los 6 dígitos APENAS aparecen, sin esperar a que se le
  pregunte; la PC los escribe al instante.
