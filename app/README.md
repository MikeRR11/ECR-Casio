# Casio ECR — App de reportes (SR-S820)

Lee los totales de venta de la caja Casio SR-S820 por Bluetooth y los
muestra en un dashboard web con persistencia local en el PC.

## Requisitos (ya instalados en este PC)
- Python 3.13
- Paquetes: `pip install -r requirements.txt` (bleak, fastapi, uvicorn)
- La caja emparejada por Bluetooth con Windows (ver `pair_device.py` si hay
  que reemparejar).

## Arrancar el dashboard
```
cd "D:\MIKE_GIT\ECR Casio\app"
python -m casio_ecr.web.server
```
Luego abre http://127.0.0.1:8770 en el navegador.

- **Leer caja**: toma una lectura de los totales actuales y la guarda.
- Gráficos por día y por mes, historial de lecturas.
- Ajustes: símbolo de moneda, decimales (0 para estilo COP), dirección BLE.

Los datos se guardan en `data/ecr.db` (SQLite) y persisten entre sesiones,
incluso después de un cierre Z en la caja.

## Estado del proyecto
- ✅ Reportes de totales por Bluetooth (funciona)
- ⏳ Configuración (mensajes/PLUs/logos) por Bluetooth — bloqueada, ver
  `../docs/protocol/live_findings.md`
- ⏳ Reportes históricos X/Z completos — futuro, requiere tarjeta SD

Ver `../docs/protocol/` para la especificación del protocolo y los
hallazgos de pruebas en hardware real.
