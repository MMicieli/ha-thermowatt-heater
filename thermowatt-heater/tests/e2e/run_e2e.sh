#!/bin/bash
# Stubbed container e2e of the REAL add-on entrypoint (Dockerfile ENTRYPOINT /init + CMD /run.sh, bashio).
# ~10 minutes each (S/S0: ~35 minutes). usage: run_e2e.sh <name> <A|B|C|D|F|G|H|S|S0>   (run from this directory)
#   image: ./build_image.sh HEAD thermowatt-pr16:test   (override with IMAGE=...; do NOT docker build from a CRLF working tree)
#   A: HTTP-200 'Daily request limit exceeded' window, commands refused in-window, recovery, Eco accepted
#   S/S0: Stage 3 (issue #17 PR #25): poll_interval=180 with demand_aware_polling=true (S) or the option absent (S0),
#      event-driven timeline: marked pre-cycle reuse, forced handback refresh, overlapping requests in both
#      orders, read-timeout recovery, a quiet period (missing HA refreshes), quota backoff. Faults are injected
#      by touching /tmp/slow and /tmp/limit in the stub API container. No vendor API, no heater, no HA.
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

# Stub cloud API. A and B are the original opaque-token rate-limit scenarios (unchanged).
# C/D/F/G (issue #17 / PR #21) use synthetic JWT access tokens with short, controlled expiry:
#   C  proactive refresh ON, 300 s tokens, no quota: exactly one refresh before expiry, no 401, restart evidence
#   D  option ABSENT (an existing install's config), 300 s tokens: reactive 401 -> refresh -> replay only
#   F  proactive ON, first /refresh answered HTTP 500: one failed attempt, then the reactive fallback
#   G  proactive ON, 150 s tokens, quota window 30-100 s: no auth/command traffic during backoff, restart evidence
#   H  Stage 2: poll_interval=300, retained refresh ignored, burst of refresh requests -> one status poll, quota window
#      300-400 s: offline after 300 s without a poll, no request during backoff, online after recovery (opaque tokens)
JWT_ENV=""
case "$SCEN" in
  A) LF=90; LU=330; ST=200 ;;
  B) LF=30; LU=200; ST=429 ;;
  C|D) LF=-1; LU=-1; ST=200; JWT_ENV="-e JWT_LIFETIME=300" ;;
  F)   LF=-1; LU=-1; ST=200; JWT_ENV="-e JWT_LIFETIME=300 -e REFRESH_FAIL_FIRST=1" ;;
  G)   LF=30; LU=100; ST=200; JWT_ENV="-e JWT_LIFETIME=150" ;;
  H)   LF=300; LU=400; ST=200 ;;
  S|S0) LF=-1; LU=-1; ST=200 ;;
  *) echo "unknown scenario $SCEN (A|B|C|D|F|G|H|S|S0)"; exit 2 ;;
esac
docker run -d --name $NET-api --network $NET --network-alias myapp-connectivity.com -e LIMIT_FROM=$LF -e LIMIT_UNTIL=$LU -e LIMIT_STYLE=$ST $JWT_ENV \
  -v "$D/stub_api.py:/stub_api.py:ro" -v "$D/ca.pem:/certs/ca.pem:ro" -v "$D/key.pem:/certs/key.pem:ro" python:3.12-alpine python -u /stub_api.py >/dev/null

# Stub Supervisor: add-on options + MQTT service discovery (what run.sh reads through bashio)
OPTS='{"email":"stub@example.test","password":"stubpass-SECRET","element_wattage":3000}'
# C, F, G enable the new option; A, B and D keep the original options (no proactive_token_refresh key).
case "$SCEN" in H) OPTS='{"email":"stub@example.test","password":"stubpass-SECRET","element_wattage":3000,"poll_interval":300}' ;; esac
case "$SCEN" in S) OPTS='{"email":"stub@example.test","password":"stubpass-SECRET","element_wattage":3000,"poll_interval":180,"demand_aware_polling":true}' ;; esac
case "$SCEN" in S0) OPTS='{"email":"stub@example.test","password":"stubpass-SECRET","element_wattage":3000,"poll_interval":180}' ;; esac
case "$SCEN" in C|F|G) OPTS='{"email":"stub@example.test","password":"stubpass-SECRET","element_wattage":3000,"proactive_token_refresh":true}' ;; esac
MQ="{\"host\":\"mqtt\",\"port\":1883,\"username\":\"tw-user\",\"password\":\"$MQPASS\",\"ssl\":false,\"protocol\":\"3.1.1\"}"
docker run -d --name $NET-sup --network $NET --network-alias supervisor -e SUPERVISOR_TOKEN=$TOKEN -e ADDON_OPTIONS_JSON="$OPTS" -e MQTT_SERVICE_JSON="$MQ" \
  -v "$D/supervisor_stub.py:/s.py:ro" python:3.12-alpine python -u /s.py >/dev/null
sleep 4
docker exec -d $NET-mqtt sh -c "mosquitto_sub -u tw-user -P $MQPASS -v -t 'P/#' -t 'thermowatt/#' -F '%I %t %p' > /tmp/sub.log"
sleep 1

# The real entrypoint: image ENTRYPOINT (/init) + CMD (/run.sh). No --entrypoint override.
run_bridge() { docker run -d --name $1 --network $NET -e SUPERVISOR_TOKEN=$TOKEN -e REQUESTS_CA_BUNDLE=/ca.pem \
  -v "$D/ca.pem:/ca.pem:ro" -v $NET-data:/data "$IMAGE" >/dev/null; }
# H: a RETAINED refresh request exists before the bridge starts; it must not cause a poll.
{ [ "$SCEN" = H ] || [ "$SCEN" = S ] || [ "$SCEN" = S0 ]; } && docker exec $NET-mqtt mosquitto_pub -r -u tw-user -P $MQPASS -t P/STUBSN1/refresh -m 1
run_bridge $NET-bridge
START=$(date +%s)
at() { local target=$1; local now=$(( $(date +%s) - START )); [ $target -gt $now ] && sleep $(( target - now )); }
pub() { docker exec $NET-mqtt mosquitto_pub -u tw-user -P $MQPASS -t "$1" -m "$2"; }
exit_code() { docker inspect "$1" --format '{{.State.ExitCode}}'; }
META="$OUT/meta.json"
# --- Stage 3 helpers: all timing is read back from the stub API's own log, never guessed ---
ev() { echo "$(date +%s.%N) $1" >> "$OUT/events.log"; }
st_info() {  # "<count of /status requests> <seconds since the last one>"
  docker logs --timestamps $NET-api 2>&1 | $PY -c "
import sys, re, datetime as d
n = 0; last = None
for l in sys.stdin:
    if '\"/status\"' in l and '\"slow\"' not in l:
        n += 1; last = l.split(' ', 1)[0]
age = 9999.0
if last:
    last = re.sub(r'(\.\d{6})\d*Z', r'\1+00:00', last)
    age = (d.datetime.now(d.timezone.utc) - d.datetime.fromisoformat(last)).total_seconds()
print(n, int(age))"
}
wait_status_count() { local i=0; until [ "$(st_info | cut -d' ' -f1)" -ge "$1" ]; do sleep 2; i=$((i+1)); [ $i -gt 900 ] && { echo "timeout waiting for status count $1"; break; }; done; }
wait_age() { local i=0; until [ "$(st_info | cut -d' ' -f2)" -ge "$1" ]; do sleep 1; i=$((i+1)); [ $i -gt 900 ] && { echo "timeout waiting for age $1"; break; }; done; }
pub2() { docker exec $NET-mqtt sh -c "mosquitto_pub -u tw-user -P $MQPASS -t P/STUBSN1/refresh -m $1; mosquitto_pub -u tw-user -P $MQPASS -t P/STUBSN1/refresh -m $2"; }
if [ "$SCEN" = S ] || [ "$SCEN" = S0 ]; then
  : > "$OUT/events.log"
  ev start
  wait_status_count 2                                   # boot poll + first timed poll (legacy cadence, no refresh seen yet)
  wait_age 40;  ev reuse_precycle;  pub P/STUBSN1/refresh precycle     # marked, last poll 40 s old: S reuses, S0 polls
  wait_age 150; ev forced_1;        pub P/STUBSN1/refresh 1            # handback-style plain "1": always a real poll
  wait_age 60;  ev overlap_precycle_then_1; pub2 precycle 1
  wait_age 70;  ev overlap_1_then_precycle; pub2 1 precycle
  docker exec $NET-api touch /tmp/slow; ev slow_start    # /status answered after 16 s > 15 s read timeout
  wait_age 30;  ev slow_forced;     pub P/STUBSN1/refresh 1
  sleep 75;     docker exec $NET-api rm -f /tmp/slow; ev slow_end
  sleep 90;     ev quiet_start                          # HA publishes no refreshes at all from here until quiet_end
  sleep 120;    ev recovered_check                      # = slow_end + 210 s >= verifier RECOVERY_DEADLINE_S (206 s: 180 s poll + 20 s HTTP timeout + TOL)
  sleep 930;    ev quiet_end                            # same absolute quiet_end as before (slow_end + 1140 s)
  docker exec $NET-api touch /tmp/limit; ev quota_start
  i=0; until docker logs $NET-api 2>&1 | grep -q '"limited": true'; do sleep 2; i=$((i+1)); [ $i -gt 200 ] && break; done
  ev quota_limited; pub P/STUBSN1/refresh 1
  sleep 20;     ev quota_pub2;      pub P/STUBSN1/refresh precycle
  sleep 30;     docker exec $NET-api rm -f /tmp/limit; ev limit_off
  sleep 200;    ev end
  docker stop -t 20 $NET-bridge >/dev/null
  echo "{\"exit_code\": $(exit_code $NET-bridge)}" > "$META"
elif [ "$SCEN" = A ]; then
  at 150; pub P/STUBSN1/CMD/MODE Off
  at 300; pub P/STUBSN1/CMD/TEMP 50
  at 520; pub P/STUBSN1/CMD/MODE Eco
  at 600
  docker stop -t 20 $NET-bridge >/dev/null
  echo "{\"exit_code\": $(exit_code $NET-bridge)}" > "$META"
elif [ "$SCEN" = H ]; then
  at 40; for i in 1 2 3 4 5; do pub P/STUBSN1/refresh 1; done   # burst: one coalesced poll
  at 520
  docker stop -t 20 $NET-bridge >/dev/null
  echo "{\"exit_code\": $(exit_code $NET-bridge)}" > "$META"
elif [ "$SCEN" = D ] || [ "$SCEN" = F ]; then
  at 400
  docker stop -t 20 $NET-bridge >/dev/null
  echo "{\"exit_code\": $(exit_code $NET-bridge)}" > "$META"
elif [ "$SCEN" = C ] || [ "$SCEN" = G ]; then
  # one boot, controlled stop, restart on the same /data volume (persisted ledger/episodes), second stop
  if [ "$SCEN" = C ]; then STOP1=400; RUN2=70; else STOP1=300; RUN2=45; fi
  if [ "$SCEN" = G ]; then at 100; pub P/STUBSN1/CMD/MODE Off; fi   # inside the backoff: must be refused locally
  at $STOP1
  docker stop -t 20 $NET-bridge >/dev/null
  FIRST_EXIT=$(exit_code $NET-bridge)
  docker logs $NET-api > "$OUT/api.run1.log" 2>&1
  RUN1_COUNT=$(grep -c '^{' "$OUT/api.run1.log")
  RUN1_END=$(grep '^{' "$OUT/api.run1.log" | tail -1 | $PY -c "import json,sys;print(json.loads(sys.stdin.read())['t'])")
  CFG=$(docker run --rm -v $NET-data:/data alpine cat /data/thermowatt_config.json)
  LEDGER=$(printf '%s' "$CFG" | $PY -c "import json,sys;print(json.dumps(json.load(sys.stdin).get('request_ledger')))")
  EPIS=$(printf '%s' "$CFG" | $PY -c "import json,sys;print(json.dumps(json.load(sys.stdin).get('quota_episodes')))")
  at $((STOP1 + 10)); run_bridge $NET-bridge2
  at $((STOP1 + 10 + RUN2))
  docker stop -t 20 $NET-bridge2 >/dev/null
  echo "{\"first_exit_code\": $FIRST_EXIT, \"second_exit_code\": $(exit_code $NET-bridge2), \"api_requests_run1\": $RUN1_COUNT, \"run1_end_t\": $RUN1_END, \"persisted_ledger\": $LEDGER, \"persisted_episodes\": $EPIS}" > "$META"
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
docker logs --timestamps $NET-api > "$OUT/api.ts.log" 2>&1
docker logs $NET-sup > "$OUT/supervisor.log" 2>&1
docker logs $NET-bridge > "$OUT/bridge.log" 2>&1
case "$SCEN" in B|C|G) docker logs $NET-bridge2 > "$OUT/bridge2.log" 2>&1 ;; esac
docker exec $NET-mqtt cat /tmp/sub.log > "$OUT/mqtt.log" 2>&1

# Verify BEFORE scrubbing (the verifier asserts the secrets never appear), then scrub for sharing.
$PY verify_e2e.py "$SCEN" "$OUT"; RC=$?
sed -i -E "s/(stubpass-SECRET|tw-pass-SECRET|stub-supervisor-token-SECRET)/[REDACTED]/g; s/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+/[email]/g" "$OUT"/*.log
rm -rf "$OUT/mq"
cleanup
echo "done $N rc=$RC"
exit $RC
