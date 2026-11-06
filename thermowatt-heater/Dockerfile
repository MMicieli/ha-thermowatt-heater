ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20
FROM $BUILD_FROM

# Install python and dependencies
RUN apk add --no-cache python3 py3-pip

# Install python libraries, pinned EXACTLY (direct + transitive) to the versions validated
# for this release on Alpine 3.20 / Python 3.12 so a Supervisor rebuild on the production
# host cannot silently float. paho-mqtt must be >=2 for CallbackAPIVersion.VERSION2.
RUN pip3 install --break-system-packages \
    "paho-mqtt==2.2.0" "requests==2.34.2" "urllib3==2.8.0" \
    "certifi==2026.10.23" "idna==3.20" "charset-normalizer==3.5.2"

ENV PYTHONUNBUFFERED=1
# Copy data for add-on
COPY run.sh /
COPY thermowatt_bridge.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
