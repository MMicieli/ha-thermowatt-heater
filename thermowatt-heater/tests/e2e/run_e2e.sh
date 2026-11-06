#!/bin/bash
# Stubbed container e2e of the REAL add-on entrypoint (Dockerfile ENTRYPOINT /init + CMD /run.sh, bashio).
# ~10 minutes. usage: run_e2e.sh <name> <A|B>   (run from this directory)
#   image: ./build_image.sh HEAD thermowatt-pr16:test   (override with IMAGE=...; do NOT docker build from a CRLF working tree)
#   A: HTTP-200 'Daily request limit exceeded' window, commands refused in-window, recovery, Eco accepted
#   B: HTTP 429 window; container stopped (SIGTERM) and restarted inside the window (persisted backoff)
# Simulated Supervisor (options + MQTT service discovery), authenticated Mosquitto, TLS verification ON via a
# throwaway CA (REQUESTS_CA_BUNDLE). Exit status is that of verify_e2e.py (non-zero on any violation).
# Logs written to out-<name>/ are scrubbed of credentials and e-mail addresses before they are kept.
set -u
export MSYS_NO_PATHCONV=1
N=$1; SCEN=$2; IMAGE=${IMAGE:-thermowatt-pr16:test}
PY=$(command -v python3 || command -v python)
D="$(pwd -W 2>/dev/null || pwd)"
NET=tw-$N; OUT="out-$N"
TOKEN=stub-supervisor-token-SECRET; MQPASS=tw-pass-SECRET
[ -f ca.pem ] || openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out ca.pem -days 2 \
  -subj "/CN=myapp-connectivity.com" -addext "subjectAltName=DNS:myapp-connectivity.com" 2>/dev/null
cleanup() { docker rm -f $NET-mqtt $NET-api $NET-sup $NET-bridge $NET-bridge2 >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1; docker volume rm $NET-data >/dev/null 2>&1; }
cleanup; rm -rf "$OUT"; mkdir -p "$OUT/mq"
docker network create $NET >/dev/null

# Authenticated broker
docker run --rm -v "$D/$OUT/mq:/mq" eclipse-mosquitto:2 sh -c \
  "mosquitto_passwd -b -c /mq/passwd tw-user $MQPASS && chown mosquitto:mosquitto /mq/passwd && chmod 0700 /mq/passwd" >/dev/null 2>&1
printf 'listener 1883\nallow_anonymous false\npassword_file /mosquitto/config/passwd\n' > "$OUT/mq/mosquitto.conf"
docker run -d --name $NET-mqtt --network $NET --network-alias mqtt -v "$D/$OUT/mq:/mosquitto/config:ro" eclipse-mosquitto:2 >/dev/null

# Stub cloud API
if [ "$SCEN" = A ]; then LF=90; LU=330; ST=200; else LF=30; LU=200; ST=429; fi
docker run -d --name $NET-api --network $NET --network-alias myapp-connectivity.com -e LIMIT_FROM=$LF -e LIMIT_UNTIL=$LU -e LIMIT_STYLE=$ST \
  -v "$D/stub_api.py:/stub_api.py:ro" -v "$D/ca.pem:/certs/ca.pem:ro" -v "$D/key.pem:/certs/key.pem:ro" python:3.12-alpine python -u /stub_api.py >/dev/null

# Stub Supervisor: add-on options + MQTT service discovery (what run.sh reads through bashio)
OPTS='{"email":"stub@example.test","password":"stubpass-SECRET","element_wattage":3000}'
MQ="{\"host\":\"mqtt\",\"port\":1883,\"username\":\"tw-user\",\"password\":\"$MQPASS\",\"ssl\":false,\"protocol\":\"3.1.1\"}"
docker run -d --name $NET-sup --network $NET --network-alias supervisor -e SUPERVISOR_TOKEN=$TOKEN -e ADDON_OPTIONS_JSON="$OPTS" -e MQTT_SERVICE_JSON="$MQ" \
  -v "$D/supervisor_stub.py:/s.py:ro" python:3.12-alpine python -u /s.py >/dev/null
sleep 4
docker exec -d $NET-mqtt sh -c "mosquitto_sub -u tw-user -P $MQPASS -v -t 'P/#' -t 'thermowatt/#' -F '%I %t %p' > /tmp/sub.log"
sleep 1

# The real entrypoint: image ENTRYPOINT (/init) + CMD (/run.sh). No --entrypoint override.
run_bridge() { docker run -d --name $1 --network $NET -e SUPERVISOR_TOKEN=$TOKEN -e REQUESTS_CA_BUNDLE=/ca.pem \
  -v "$D/ca.pem:/ca.pem:ro" -v $NET-data:/data "$IMAGE" >/dev/null; }
run_bridge $NET-bridge
START=$(date +%s)
at() { local target=$1; local now=$(( $(date +%s) - START )); [ $target -gt $now ] && sleep $(( target - now )); }
pub() { docker exec $NET-mqtt mosquitto_pub -u tw-user -P $MQPASS -t "$1" -m "$2"; }
exit_code() { docker inspect "$1" --format '{{.State.ExitCode}}'; }
META="$OUT/meta.json"
if [ "$SCEN" = A ]; then
  at 150; pub P/STUBSN1/CMD/MODE Off
  at 300; pub P/STUBSN1/CMD/TEMP 50
  at 520; pub P/STUBSN1/CMD/MODE Eco
  at 600
  docker stop -t 20 $NET-bridge >/dev/null
  echo "{\"exit_code\": $(exit_code $NET-bridge)}" > "$META"
else
  at 100
  docker stop -t 20 $NET-bridge >/dev/null
  FIRST_EXIT=$(exit_code $NET-bridge)
  PERSISTED=$(docker run --rm -v $NET-data:/data alpine cat /data/thermowatt_config.json | $PY -c "import json,sys;print(json.dumps(json.load(sys.stdin).get('rate_limit')))")
  at 110; run_bridge $NET-bridge2
  at 600
  S2=$(docker inspect $NET-bridge2 --format '{{.State.Status}}'); R2=$(docker inspect $NET-bridge2 --format '{{.RestartCount}}')
  docker stop -t 20 $NET-bridge2 >/dev/null
  echo "{\"first_exit_code\": $FIRST_EXIT, \"second_exit_code\": $(exit_code $NET-bridge2), \"second_status\": \"$S2\", \"second_restart_count\": $R2, \"persisted_rate_limit\": $PERSISTED}" > "$META"
fi
docker logs $NET-api > "$OUT/api.log" 2>&1
docker logs $NET-sup > "$OUT/supervisor.log" 2>&1
docker logs $NET-bridge > "$OUT/bridge.log" 2>&1
[ "$SCEN" = B ] && docker logs $NET-bridge2 > "$OUT/bridge2.log" 2>&1
docker exec $NET-mqtt cat /tmp/sub.log > "$OUT/mqtt.log" 2>&1

# Verify BEFORE scrubbing (the verifier asserts the secrets never appear), then scrub for sharing.
$PY verify_e2e.py "$SCEN" "$OUT"; RC=$?
sed -i -E "s/(stubpass-SECRET|tw-pass-SECRET|stub-supervisor-token-SECRET)/[REDACTED]/g; s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+/[email]/g" "$OUT"/*.log
rm -rf "$OUT/mq"
cleanup
echo "done $N rc=$RC"
exit $RC
