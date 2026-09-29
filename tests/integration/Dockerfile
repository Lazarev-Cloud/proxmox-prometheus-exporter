# A Debian with systemd as PID 1, for testing install.sh the way it runs on
# Proxmox VE (Debian 12 "bookworm" = PVE 8, Debian 13 "trixie" = PVE 9).
# Deliberately without pip: the installer must not need it (PEP 668).
#
#   docker build --build-arg DEBIAN=trixie -t installer-test tests/integration
#   docker run -d --name installer-test --privileged --cgroupns=host \
#     -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
#     -v "$PWD:/src:ro" installer-test
#   docker exec installer-test bash -c 'cp -r /src /work && bash /work/tests/integration/installer.sh'
ARG DEBIAN=trixie
FROM debian:${DEBIAN}
RUN apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
       systemd systemd-sysv dbus python3 curl ca-certificates procps iproute2 \
    && rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
