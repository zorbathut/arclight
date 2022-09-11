ARG BASEIMAGE

# Hacky image to do some setup necessary *before* the ue4-docker stuff happens.
FROM ${BASEIMAGE} as baseimage

# Install a DNS server and point it to 8.8.8.8.
# This way, if it gets 127.0.0.1 from the host, it'll work anyway.
RUN powershell -Command "Install-WindowsFeature DNS" && dnscmd /resetforwarders 8.8.8.8

