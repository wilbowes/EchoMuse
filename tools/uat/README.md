# Release UAT

Every release: check that each dashboard control does what it says, on a real
Echo, and ship the outcome with the release (`docs/uat-results/<version>.md`,
plus a published copy). Human steps (voice, audio, buttons) run as a guided
script with a person at the device.

```sh
python3 rig.py up --image ghcr.io/wilbowes/echomuse-controller:<candidate>
python3 rig.py attach <USB Echo serial>        # moves its credentials aside
python3 run_controls.py --out controls.json    # every fleet-config control
python3 a11y.py --out a11y.json                # axe-core, WCAG 2.1 AA
python3 report.py <version> --controls controls.json --a11y a11y.json
python3 rig.py detach <serial> && python3 rig.py down
```

- **Isolated.** The controller runs on a Docker bridge network, so its mDNS
  never reaches other Echoes; the attached Echo reaches it through an endpoint
  file with `mdns: false`. `detach` restores the Echo's own credentials and
  endpoint file exactly.
- **`rig.py up` does not start the browser yet**, though `down` removes it.
  Start it once per run:
  `docker run -d --name em-uat-browser --network host --ipc host mcr.microsoft.com/playwright/python:v1.49.1-noble sh -c "pip install -q playwright==1.49.1 && playwright run-server --port 3111 --host 127.0.0.1"`.
- **Attach only to a rig that does not know the Echo.** `attach` moves the
  Echo's credentials aside and then approves it; on a rig that still holds
  its row the approval answers 409 with the Echo already cut off from its own
  controller. `rig.py up` again (it starts from an empty database), then
  attach.
- **The browser runs in a container** (`mcr.microsoft.com/playwright/python`,
  `playwright run-server` on :3111); the host needs only the client,
  `pip install playwright==1.49.1`.
- **The Echo reports what it got.** Firmware writes `/tmp/em-config.json`
  after every config push: what it received and what it is running. A control
  is `applied` when the running value changed, `received` when it arrived but
  its effect is not in that snapshot.
- **The inventory is read from `dashboard.jsx`** (`inventory.py`), so a new
  control is either checked or listed as missing. Controls are found by role
  and label, which is also what a screen reader uses: a control the rig cannot
  find is usually one a keyboard user cannot reach either.
- **Every UAT Echo on USB.** emOS Echoes are read over their serial console
  (`emos/tools/emconsole.py --list`), FireOS ones over adb as root with
  Magisk's busybox; `rig.Echo(serial)` picks by whether adb lists the serial.
  Attach each (they get labels `UAT <last4>`); controls, the report and the
  accessibility scan cover every attached Echo.
- **The provisioning wizard deletes an endpoint file** when the fleet's
  address list is empty, so an Echo re-provisioned while attached (e.g. an
  emOS upgrade) falls back to mDNS and leaves the rig: rewrite its
  `controller.json` before continuing.
