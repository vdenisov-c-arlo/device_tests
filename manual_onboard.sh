#!/bin/sh
# Manual onboarding script for Lory DUT.
# Prerequisites: ISP is up, WiFi connected via "itool connect".
# Run on ISP console (or via ssh root@<device-ip>).

set -e

echo "=== Manual Onboard: checking prerequisites ==="

# Verify network is up
if ! ip addr show wlan0 2>/dev/null | grep -q "inet "; then
    echo "ERROR: wlan0 has no IP. Run 'itool connect' first."
    exit 1
fi

# Verify time is sane (TLS needs valid time)
YEAR=$(date +%Y)
if [ "$YEAR" -lt 2025 ]; then
    echo "WARNING: system time looks wrong ($YEAR). Attempting NTP sync..."
    ntpd -q -p pool.ntp.org 2>/dev/null || sntp -s pool.ntp.org 2>/dev/null || true
    YEAR=$(date +%Y)
    if [ "$YEAR" -lt 2025 ]; then
        echo "ERROR: time sync failed. TLS will not work."
        exit 1
    fi
fi
echo "Time OK: $(date)"

# Check if xagent is running
if ! pidof xagent > /dev/null 2>&1; then
    echo "xagent not running, starting it..."

    # Find config file
    CONF=""
    for f in /etc/xagent.conf /etc/xagent.conf.initial /tmp/xagent.conf; do
        [ -f "$f" ] && CONF="$f" && break
    done
    if [ -z "$CONF" ]; then
        echo "ERROR: no xagent config file found"
        exit 1
    fi

    # Get device serial
    SERIAL=$(arloutil info 2>/dev/null | grep -i serial | awk '{print $NF}')
    MODEL=$(arloutil info 2>/dev/null | grep -i model | awk '{print $NF}')
    if [ -z "$SERIAL" ] || [ -z "$MODEL" ]; then
        echo "ERROR: cannot determine serial/model from arloutil info"
        exit 1
    fi

    echo "Starting xagent: model=$MODEL serial=$SERIAL config=$CONF"
    xagent --log_debug --service_id 1002 --model_id "$MODEL" --hardware_id "$SERIAL" --config_file "$CONF" &
    sleep 3

    if ! pidof xagent > /dev/null 2>&1; then
        echo "ERROR: xagent failed to start"
        exit 1
    fi
fi
echo "xagent PID: $(pidof xagent)"

# Force connection (proceed without being claimed)
echo "=== Setting force_connection ==="
kvcmd write x_force_connection 1
kvcmd commit

# Resume connections (preregister -> register -> advisor -> MQTT)
echo "=== Resuming cloud connections ==="
xagent_control -c resume_connections
sleep 5

# Check registration result
XID=$(kvcmd read-s x_agent_id 2>/dev/null || true)
CLAIM=$(kvcmd read-s x_agent_claim_code 2>/dev/null || true)
echo "x_agent_id: ${XID:-<not set>}"
echo "x_agent_claim_code: ${CLAIM:-<not set>}"

if [ -z "$XID" ]; then
    echo "WARNING: registration may not have completed yet. Check logs."
fi

# Send discovery (makes device visible for claiming in the app)
echo "=== Sending discovery ==="
xagent_control -c send_discovery -s 1002 -t 3600
sleep 2

echo ""
echo "=== Done ==="
echo "Check MQTT status:"
echo "  grep -i 'mqtt\|advisor\|register\|connect' /var/log/messages | tail -20"
echo ""
echo "To claim: open Arlo app and add device, or use cloud API with claim_code=$CLAIM"
