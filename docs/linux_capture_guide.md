# Capturar el reporte X/Z por Bluetooth desde Linux (live-USB de Ubuntu)

**Por qué Linux:** en Windows el enlace BLE se cae a los ~16-20 s mientras la
caja imprime el reporte, antes de que lo transmita (ver
`docs/protocol/live_findings.md`). Es un *timeout de supervisión del enlace*
que Windows no deja subir más allá de ~20 s. Linux/BlueZ **sí** permite fijarlo
al máximo BLE de **32 s**, que debería aguantar la ventana de impresión.

Todo el código Python (`app/`) es igual — `bleak` usa BlueZ automáticamente en
Linux. Lo único distinto es: (1) fijar el timeout, y (2) el emparejamiento se
hace con `bluetoothctl` en vez del helper de WinRT.

---

## 0. Preparar el live-USB (una sola vez, en cualquier PC)

1. Descarga **Ubuntu Desktop LTS** (ubuntu.com/download) — la ISO.
2. Con **Rufus** (o balenaEtcher) graba la ISO en un USB de ≥8 GB.
3. Arranca este PC desde el USB (F12/F9/Esc según la marca → elegir el USB) y
   elige **"Try Ubuntu"** (NO "Install") — así no toca tu Windows.

Copia la carpeta `app/` del proyecto a una carpeta en el escritorio de Ubuntu
(desde otro USB, o clonando el repo). No hace falta el resto del repo.

---

## 1. Preparar Bluetooth y dependencias (en la sesión de Ubuntu)

```bash
sudo mount -t debugfs none /sys/kernel/debug 2>/dev/null || true   # normalmente ya está
sudo apt update
sudo apt install -y python3-pip bluez
pip3 install --break-system-packages bleak     # o: python3 -m venv .venv && source .venv/bin/activate && pip install bleak

# Enciende el adaptador
sudo rfkill unblock bluetooth
bluetoothctl power on
```

Confirma que ves la caja (debe estar en REG, Bluetooth activado):

```bash
bluetoothctl scan on
# ...espera a ver una línea con  08:00:74:57:30:EF  EY240139106940
bluetoothctl scan off
```

---

## 2. Emparejar la caja en Linux (equivalente al truco del PIN)

La caja muestra un PIN de 6 dígitos y nosotros lo entregamos (igual que en
Windows, pero aquí con `bluetoothctl`). En la caja, ve a
**PGM → [Bluetooth] → System Setting ON → [Pairing with mobile]** hasta que
muestre su código de dispositivo. Luego en Ubuntu:

```bash
bluetoothctl
# dentro del prompt:
agent KeyboardOnly
default-agent
pair 08:00:74:57:30:EF
# La caja mostrará un PIN de 6 dígitos -> cuando bluetoothctl pida
# "Enter passkey" / "Enter PIN code", escríbelo RÁPIDO (la caja lo vence en
# pocos segundos, igual que en Windows).
trust 08:00:74:57:30:EF
quit
```

Si falla por tiempo, reintenta `pair` y ten el PIN listo para teclearlo al
instante. (`trust` evita que pida el PIN otra vez.)

---

## 3. Fijar el supervision timeout a 32 s  ← la pieza clave

```bash
cd <carpeta>/app
sudo ./linux/set_conn_params.sh          # pone supervision_timeout=3200 (32s)
```

Debe imprimir `supervision_timeout <- 3200`. Si dice que no existe el archivo,
tu kernel usa otra ruta — dímelo y lo ajusto (alternativa: `hcitool lecup`
sobre el handle activo).

---

## 4. Sincronizar hora y capturar

```bash
python3 sync_time.py 08:00:74:57:30:EF      # opcional: pone la hora
python3 pull_report.py X                      # captura el reporte X (solo lectura)
```

Qué esperar (y qué vigilar en el log):
- Disparo `00` → ETX → la caja imprime el reporte en papel (~15-20 s).
- **La diferencia:** con el timeout en 32 s el enlace **no debe caerse** durante
  la impresión (antes se moría a los ~16-20 s).
- Al terminar de imprimir, la caja abre su transferencia (`register opened its
  data transfer (STX)`) y empieza a llegar el archivo (`XZ receive: +N bytes`).
- Se guarda en `app/data/captures/xz_X_*.bin` y se parsea automáticamente
  mostrando el detalle por departamento/PLU/hora/etc.

Para el **cierre Z del día** (resetea totales, imprime y transmite el Z):
```bash
python3 pull_report.py Z --z
```
(Asegúrate primero de que **"Z data → mobile" = YES** en
PGM → [Bluetooth] → Functions.)

Re-parsear un `.bin` capturado, sin BLE:
```bash
python3 pull_report.py --parse data/captures/xz_X_XXXX.bin
```

---

## 5. Si funciona → solución permanente sin "quita y pon"

El live-USB es la prueba. Una vez confirmado, el objetivo "inalámbrico, una
sola tanda, sin tocar nada" se logra con un **Raspberry Pi** (cualquiera con
BLE, incluso una Pi Zero 2 W):
- Se queda encendida y emparejada con la caja.
- Corre `pull_report.py` / el dashboard a demanda o programado (cron).
- Publica los reportes al dashboard por red — los ves desde el PC/celular.
- Cero cables, cero SD, cero quita-y-pon.

Los mismos 4 pasos de arriba aplican en la Pi (Raspberry Pi OS ya trae BlueZ).

---

## Diagnóstico si el enlace AÚN se cae con 32 s

Sería señal de que la caja cierra activamente su módulo tras imprimir (no solo
un timeout). En ese caso, capturar el log completo de HCI ayuda:
```bash
sudo btmon > hci.log &     # deja corriendo durante la captura
python3 pull_report.py X
# Ctrl-C al btmon; mándame hci.log
```
Ese log muestra a nivel de radio quién cierra y por qué (motivo del
`Disconnect Complete`), y con eso decidimos el siguiente paso.
