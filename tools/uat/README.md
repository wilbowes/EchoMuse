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
- **emOS Echo on USB only.** The serial console is how the rig reads the
  device; `emos/tools/emconsole.py --list` shows what is attached.
