# Controller dev tools

Programmatic device access via the controller API, for development sessions
without a dashboard. All three run **inside the controller container** (they
mint a temporary session token directly in the SQLite `sessions` table, use
it as `Authorization: Bearer`, and delete it afterwards):

```bash
docker cp controller/tools/devshell.py echomuse-controller:/tmp/
docker exec echomuse-controller python /tmp/devshell.py "<shell command>" ["<another>" ...]
```

- **devshell.py** — run commands on a device over the `/shell` proxy (PTY
  mode; output includes echoed input — device is mksh + busybox, no
  tail/sed/head, use `busybox <applet>`). Device ID is hardcoded at the top.
- **ota.py** — push a locally built binary: `docker cp device/build/server
  echomuse-controller:/tmp/server-new` first, then
  `python /tmp/ota.py <device_id>` (upload → `/api/devices/{id}/update`).
- **pull_so.py** — pull a file off the device (busybox base64 over the
  shell, echo disabled, split end-markers). Writes the decoded file to
  stdout: `python /tmp/pull_so.py /system/lib64/libled_hal.so > out.so`.
