#!/bin/bash
# Stubbed API/MQTT rate-limit e2e against the real add-on image. Takes ~10 minutes.
# usage: run_e2e.sh <name> <A|B>   (run from this directory; image: docker build -t thermowatt-pr16:test ../..)
#   A: HTTP-200 'Daily request limit exceeded' window, commands refused in-window, recovery, Eco accepted
#   B: HTTP 429 window, bridge SIGTERM + restart inside the window (persisted backoff, deferred startup login)
# Uses a throwaway self-signed CA (REQUESTS_CA_BUNDLE) so TLS verification stays ON.
set -u
export MSYS_NO_PATHCONV=1
[ -f ca.pem ] || openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out ca.pem -days 2   -subj "/CN=myapp-connectivity.com" -addext "subjectAltName=DNS:myapp-connectivity.com"
N=$1; SCEN=$2; D="$(pwd -W 2>/dev/null || pwd)"
NET=tw-$N
docker rm -f $NET-mqtt $NET-api $NET-bridge $NET-bridge2 $NET-sub >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1; docker volume rm $NET-data >/dev/null 2>&1
docker network create $NET >/dev/null
docker run -d --name $NET-mqtt --network $NET --network-alias mqtt -v "$D/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" eclipse-mosquitto:2 >/dev/null
if [ "$SCEN" = A ]; then LF=90; LU=330; ST=200; else LF=30; LU=200; ST=429; fi
docker run -d --name $NET-api --network $NET --network-alias myapp-connectivity.com -e LIMIT_FROM=$LF -e LIMIT_UNTIL=$LU -e LIMIT_STYLE=$ST -v "$D/stub_api.py:/stub_api.py:ro" -v "$D/ca.pem:/certs/ca.pem:ro" -v "$D/key.pem:/certs/key.pem:ro" python:3.12-alpine python -u /stub_api.py >/dev/null
sleep 4
docker exec -d $NET-mqtt sh -c "mosquitto_sub -v -t 'P/#' -t 'thermowatt/#' -t 'homeassistant/sensor/STUBSN1/rate_limit_status/config' -F '%I %t %p' > /tmp/sub.log"
sleep 1
run_bridge() { docker run -d --name $1 --network $NET -e MQTT_HOST=mqtt -e MQTT_PORT=1883 -e ELEMENT_WATTAGE=3000 -e REQUESTS_CA_BUNDLE=/ca.pem -v "$D/ca.pem:/ca.pem:ro" -v $NET-data:/data --entrypoint python3 thermowatt-pr16:test -u /thermowatt_bridge.py stub@example.test stubpass >/dev/null; }
run_bridge $NET-bridge
START=$(date +%s)
at() { local target=$1; local now=$(( $(date +%s) - START )); [ $target -gt $now ] && sleep $(( target - now )); }
pub() { docker exec $NET-mqtt mosquitto_pub -t "$1" -m "$2" ; }
if [ "$SCEN" = A ]; then
  at 150; pub P/STUBSN1/CMD/MODE Off
  at 300; pub P/STUBSN1/CMD/TEMP 50
  at 520; pub P/STUBSN1/CMD/MODE Eco
  at 600
  docker stop -t 15 $NET-bridge >/dev/null; echo "bridge exit code: $(docker inspect $NET-bridge --format '{{.State.ExitCode}}')"
else
  at 100
  docker stop -t 15 $NET-bridge >/dev/null; echo "bridge1 exit code: $(docker inspect $NET-bridge --format '{{.State.ExitCode}}')"
  docker run --rm -v $NET-data:/data alpine cat /data/thermowatt_config.json | python -c "import json,sys;c=json.load(sys.stdin);print('persisted rate_limit:',c.get('rate_limit'))"
  at 110; run_bridge $NET-bridge2
  at 600
  echo "bridge2 state at t=600: $(docker inspect $NET-bridge2 --format '{{.State.Status}} restarts={{.RestartCount}}')"
  docker stop -t 15 $NET-bridge2 >/dev/null; echo "bridge2 exit code: $(docker inspect $NET-bridge2 --format '{{.State.ExitCode}}')"
fi
mkdir -p out-$N
docker logs $NET-api > out-$N/api.log 2>&1
docker logs $NET-bridge > out-$N/bridge.log 2>&1
[ "$SCEN" = B ] && docker logs $NET-bridge2 > out-$N/bridge2.log 2>&1
docker exec $NET-mqtt cat /tmp/sub.log > out-$N/mqtt.log 2>&1
docker rm -f $NET-mqtt $NET-api $NET-bridge $NET-bridge2 >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1; docker volume rm $NET-data >/dev/null 2>&1
echo "done $N"
