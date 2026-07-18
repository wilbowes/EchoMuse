#!/system/bin/sh
# EchoMuse debloat — Magisk service.d boot script.
#
# Installed to /sbin/.core/img/.core/service.d/echomuse-debloat.sh (0755) by
# the provisioning wizard (Debloat step). Runs on every boot after Magisk
# mounts: `stop <service>` does not persist across reboots, so init-launched
# daemons must be re-stopped each boot. The `pm hide` half of the debloat is
# persistent and lives in debloat_packages.txt (applied once at provisioning).
#
# Recipe proven on Lounge 2026-07-15 (−130MB RAM, cpu_avg −2‑3pp, no voice
# regressions). Delete this file to end the experiment on a device.
#
# The sleep lets Android boot fully first — stopping these daemons mid-boot
# races init's own property triggers and some restart if stopped too early.
sleep 45

for svc in vitals_service perfmonitord perfrecoveryd shblemeshd meshmgrservice drm; do
    stop "$svc"
done
