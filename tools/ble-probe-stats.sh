#!/bin/bash
# Summarise the BLE link-quality probe log: per (adapter, lock),
# success rate, latency distribution, retry incidence. Pass
# --since=<spec> to limit the window.
#
# Usage:
#   ble-probe-stats.sh                       # whole log
#   ble-probe-stats.sh --since='1 day ago'   # last 24h

set -u

LOG_FILE="${LOG_FILE:-/home/pi/ble-probe/probe.log}"
SINCE=""
for arg in "$@"; do
    case "$arg" in
        --since=*) SINCE="${arg#--since=}" ;;
        --log=*)   LOG_FILE="${arg#--log=}" ;;
        *) echo "Unknown arg: $arg" >&2; exit 64 ;;
    esac
done

if [ ! -s "$LOG_FILE" ]; then
    echo "No data in $LOG_FILE" >&2
    exit 1
fi

cutoff_epoch=0
if [ -n "$SINCE" ]; then
    cutoff_epoch=$(date -d "$SINCE" +%s 2>/dev/null) || {
        echo "Bad --since spec: $SINCE" >&2; exit 64
    }
fi

awk -F, -v cutoff="$cutoff_epoch" '
function pct(arr, n, p,    idx) {
    idx = int(n * p / 100); if (idx < 1) idx = 1; if (idx > n) idx = n
    return arr[idx]
}
{
    iso = $1; gsub(/[-T:]/, " ", iso); sub(/\+.*/, "", iso); sub(/Z/, "", iso)
    epoch = mktime(iso)
    if (epoch < cutoff) next

    antenna = $2; adapter = $3; lock = $4; rc = $5 + 0; dur = $6 + 0
    key = antenna "|" adapter "|" lock
    total[key]++
    if (rc == 0) {
        ok[key]++
        durs[key, ++n[key]] = dur
    } else {
        fail[key, rc]++
    }
}
END {
    PROCINFO["sorted_in"] = "@ind_str_asc"
    for (key in total) {
        split(key, kp, "|"); antenna = kp[1]; adapter = kp[2]; lock = kp[3]
        printf "\n=== antenna=%s  adapter=%s  lock=%s ===\n", antenna, adapter, lock
        printf "  samples       : %d\n", total[key]
        printf "  ok            : %d (%.1f%%)\n", ok[key]+0, (ok[key]+0)*100.0/total[key]
        for (fk in fail) {
            split(fk, fp, SUBSEP)
            if (fp[1] == key) printf "  rc=%-10s: %d\n", fp[2], fail[fk]
        }
        if (n[key] > 0) {
            cnt = n[key]
            for (i = 1; i <= cnt; i++) sorted[i] = durs[key, i]
            for (i = 2; i <= cnt; i++) {
                v = sorted[i]; j = i - 1
                while (j > 0 && sorted[j] > v) { sorted[j+1] = sorted[j]; j-- }
                sorted[j+1] = v
            }
            printf "  duration ms   : min=%d  p10=%d  median=%d  p90=%d  max=%d\n",
                   sorted[1], pct(sorted, cnt, 10), pct(sorted, cnt, 50),
                   pct(sorted, cnt, 90), sorted[cnt]
            delete sorted
        }
    }
}' "$LOG_FILE"

echo
echo "Per-sample debug logs: ${DEBUG_DIR:-/home/pi/ble-probe/debug}/"
echo "Sample stderr: ls -lt ${DEBUG_DIR:-/home/pi/ble-probe/debug}/ | head"
