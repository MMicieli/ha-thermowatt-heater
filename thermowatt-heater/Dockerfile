ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20
FROM $BUILD_FROM

# Install python and dependencies
RUN apk add --no-cache python3 py3-pip

# Install python libraries
RUN pip3 install paho-mqtt requests urllib3 --break-system-packages

ENV PYTHONUNBUFFERED=1
# Copy data for add-on
COPY run.sh /
COPY thermowatt_bridge.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
