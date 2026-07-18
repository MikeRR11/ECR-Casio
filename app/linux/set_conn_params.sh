#!/usr/bin/env bash
# Set the BlueZ default LE connection parameters so the NEXT connection uses a
# long link-supervision timeout -- the whole point of moving to Linux. On
# Windows the longest preset only reached ~20s (register drops mid-print);
# here we push the supervision timeout to the BLE maximum (32s), which should
# outlast the SR-S820's ~16-20s print window so it can then stream the report.
#
# Units (BlueZ debugfs, all in 1.25ms for intervals, 10ms for timeout):
#   conn_min_interval / conn_max_interval : 1.25ms units
#   supervision_timeout                   : 10ms units  (0x0C80 = 3200 = 32s)
#
# Run BEFORE connecting:  sudo ./set_conn_params.sh   (defaults to hci0)
set -euo pipefail

HCI="${1:-hci0}"
DBG="/sys/kernel/debug/bluetooth/${HCI}"

if [[ $EUID -ne 0 ]]; then
    echo "Run as root:  sudo $0 ${HCI}" >&2
    exit 1
fi
if [[ ! -d "$DBG" ]]; then
    echo "No debugfs for ${HCI} at ${DBG}." >&2
    echo "Mount debugfs first:  sudo mount -t debugfs none /sys/kernel/debug" >&2
    exit 1
fi

# Slower interval (like Windows PowerOptimized: 72-144 * 1.25ms = 90-180ms) +
# max supervision timeout. Writing each is best-effort; report what took.
set_param() {
    local name="$1" val="$2"
    if [[ -w "${DBG}/${name}" ]]; then
        echo "$val" > "${DBG}/${name}" && echo "  ${name} <- ${val}" \
            || echo "  ${name}: write failed (kernel may reject value)"
    else
        echo "  ${name}: not present/writable on this kernel"
    fi
}

echo "Setting LE default connection params on ${HCI}:"
set_param conn_min_interval 72     # 90 ms
set_param conn_max_interval 144    # 180 ms
set_param conn_latency 0
set_param supervision_timeout 3200 # 32 s (BLE max)

echo
echo "Current values:"
for p in conn_min_interval conn_max_interval conn_latency supervision_timeout; do
    [[ -r "${DBG}/${p}" ]] && printf "  %-22s %s\n" "$p" "$(cat "${DBG}/${p}")"
done
echo
echo "Done. Now connect/run the capture (these apply to the NEXT connection)."
