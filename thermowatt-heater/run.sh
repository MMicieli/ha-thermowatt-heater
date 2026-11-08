#!/usr/bin/with-contenv bashio
set -e


EMAIL=$(bashio::config 'email')
PASSWORD=$(bashio::config 'password')
ELEMENT_WATTAGE=$(bashio::config 'element_wattage' '3000')

# Proactive token refresh is OFF unless the add-on option is explicitly true. Anything
# else (option absent, false, malformed) exports 0, so the bridge's default also holds
# when an older saved configuration has no such key.
if bashio::config.true 'proactive_token_refresh'; then
    THERMOWATT_PROACTIVE_REFRESH=1
else
    THERMOWATT_PROACTIVE_REFRESH=0
fi
export THERMOWATT_PROACTIVE_REFRESH

export MQTT_HOST
export ELEMENT_WATTAGE
export MQTT_PORT
export MQTT_USER
export MQTT_PASSWORD

MQTT_HOST="$(bashio::services mqtt "host")"
MQTT_PORT="$(bashio::services mqtt "port")"
MQTT_USER="$(bashio::services mqtt "username")"
MQTT_PASSWORD="$(bashio::services mqtt "password")"

bashio::log.info "Starting Thermowatt Bridge for $EMAIL (proactive token refresh: $THERMOWATT_PROACTIVE_REFRESH)..."

python3 -u /thermowatt_bridge.py "$EMAIL" "$PASSWORD"
