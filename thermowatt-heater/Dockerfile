ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20
FROM $BUILD_FROM

# Install python and dependencies
RUN apk add --no-cache python3 py3-pip

# Install python libraries (pinned for reproducible builds).
# paho-mqtt must be >=2 for CallbackAPIVersion.VERSION2 used by the bridge.
RUN pip3 install "paho-mqtt>=2.1,<3" "requests>=2.31,<3" "urllib3>=2,<3" --break-system-packages

ENV PYTHONUNBUFFERED=1
# Copy data for add-on
COPY run.sh /
COPY thermowatt_bridge.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
