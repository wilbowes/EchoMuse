# Moving from the Docker container to the Home Assistant add-on

You are running the EchoMuse controller as a standalone Docker container and
you want it to run as a Home Assistant add-on instead. This is how to do that
without losing your devices, your settings, or the Home Assistant entities you
have already built automations against.

> **Terminology.** Home Assistant is in the middle of renaming "add-ons" to
> "apps". Depending on your version you may see either word, and the paths in
> this guide appear in both spellings. They are the same thing.

**Read the whole page before you start.** The single most important step —
copying `tls/` — happens early, and skipping it leaves every device unable to
connect in a way that is not obvious, and putting it right means a visit to each one.

---

## What actually has to move

Everything the controller remembers lives in one directory, next to the
database file. In a standard `docker-compose` install that is
`controller/data/` on your host:

```
data/
├── echomuse.db      ← devices, settings, users, history
├── tls/             ← the certificate authority your devices trust
├── oww_models/      ← custom wake word models, if you trained any
└── recordings/      ← saved utterances, if you turned that on
```

Copy **the whole directory**. Taking only the database is the most common way
to get this wrong.

### Why `tls/` is the one that bites

Each device stores a copy of your controller's certificate authority and
refuses to talk to anything that cannot prove it holds the matching key. That
CA is generated **once, on first start**, and a fresh controller generates a
**new one**.

So if you start the add-on with an empty data directory, it creates a CA your
devices have never seen. They then dial `wss://`, fail to verify it, and drop
the connection — repeatedly, silently, with nothing in the dashboard to
explain it, because they never get far enough to appear in the dashboard at
all.

Turning off `require_device_tls` does **not** rescue this. Devices decide to
use TLS from the mDNS record the controller advertises, not from that setting.

Recovering means going to each Dot. On firmware v2.17.0 or later, hold its
action button for 5 seconds and approve the pairing request in the dashboard.
On older firmware, or with `require_device_tls` on, connect it over USB and
push fresh credentials. Copying four files avoids it.

---

## Before you start

**1. Write down your controller version.**

It is shown in the dashboard header, or:

```bash
docker exec echomuse-controller printenv EM_CONTROLLER_VERSION
```

(If that prints `dev` you built the image yourself, and only you know what is
in it.)

The add-on must be the **same version or newer**. Database upgrades only go
forwards; a controller that meets a database from a newer version refuses to
start rather than guess. If your container is somehow ahead of the published
add-on, wait for the add-on to catch up.

Going up several versions at once is fine and expected — the controller
applies every upgrade it is missing in one start, and takes its own backup
first (`echomuse.db.pre-v<N>.bak`, beside the database).

**2. Write down your settings.**

Your `.env` file does **not** come across. The add-on has its own options
screen and you will re-enter these by hand:

| `.env` | Add-on option |
|---|---|
| `SERVER_IP` | **Controller LAN IP address** |
| `SERVER_HOST` | **Bind address** |
| `MDNS_NAME` | **mDNS service name** |
| `OWW_MODEL` | **Wake word model** |
| `OWW_THRESHOLD` | **Wake word sensitivity** |
| `DEVICE_APPROVAL` | **Device approval policy** |
| `REQUIRE_DEVICE_TLS` | **Require encrypted device connections** |
| `ESPHOME_PROJECT_VERSION` | **ESPHome project version** |
| `EM_ESPHOME_PORT_BASE` | **ESPHome satellite base port** |
| `DEBUG` | **Debug logging** |
| `LOG_LEVELS` | **Per-area log levels** |
| `EM_EXTRA_CA_CERT` | **Private CA certificate** |
| `EM_TTS_WAIT_TIMEOUT` | **Reply wait (seconds)** |

Four have no option, deliberately or otherwise:

- `DB_PATH` and `API_PORT` are **fixed** in the add-on. The database has to
  live where Home Assistant keeps add-on storage, and the dashboard port has
  to match the one Home Assistant proxies.
- `SERVER_PORT` and `SERVER_TLS_PORT` are **not yet exposed**
  ([#163](https://github.com/wilbowes/EchoMuse/issues/163)). If you changed
  either from the default, say so on that issue before migrating — your
  devices are configured to reach those ports.

**`SERVER_IP` will usually change**, because the controller is moving to your
Home Assistant machine. Set it to that machine's LAN address, not the old
one. Devices find the controller by mDNS, so they will follow — see
[Your devices](#your-devices-find-the-new-controller-on-their-own) below.

**3. Take a backup.** You are copying, not moving, so your original data
directory is your rollback. Do not delete it until you are happy.

---

## The steps

### 1. Stop the standalone controller

```bash
cd /path/to/EchoMuse/controller
docker compose down
```

**This is not optional and it is not just tidiness.** Two controllers on one
network both advertise themselves, and a device takes the first one it finds
that verifies — there is no way to tell them apart
([#106](https://github.com/wilbowes/EchoMuse/issues/106)). Leaving the old one
running means devices attach to whichever answered first, which will look like
the migration randomly half-worked.

It also matters for Home Assistant: it only re-points an ESPHome device at a
new address when the old one has actually gone away.

### 2. Install the add-on, then stop it

Add the repository (**Settings → Add-ons → Add-on Store → ⋮ → Repositories**):

```
https://github.com/wilbowes/EchoMuse
```

Install **EchoMuse**, then **start it once and stop it again**. That first
start creates the storage directory you are about to copy into. Do not fill in
options yet, and do not open the dashboard — if you let it run it will
generate the CA you are about to replace. (No harm done if you do; you are
overwriting it in the next step.)

### 3. Copy your data in

The add-on's storage is not visible from Samba or the File Editor, so this
step needs shell access to the Home Assistant host. Install the **Advanced SSH
& Web Terminal** community add-on and turn **Protection mode off** in its
configuration — without that it cannot see the host filesystem.

Find the directory. The path changed when Home Assistant renamed add-ons to
apps, so check both:

```bash
ls -d /mnt/data/supervisor/apps/data/*controller 2>/dev/null || \
ls -d /mnt/data/supervisor/addons/data/*controller 2>/dev/null
```

The folder name is a hash followed by `_controller` — the hash is derived from
the repository URL and differs between installations, so use the wildcard
rather than copying someone else's path.

Get your `data/` directory onto the Home Assistant machine (`scp`, a USB
stick, a `/share` folder — whatever suits), then:

```bash
DEST=$(ls -d /mnt/data/supervisor/{apps,addons}/data/*controller 2>/dev/null | head -1)
cp -a /path/to/your/data/. "$DEST"/
ls -la "$DEST" "$DEST/tls"
```

That last line is the check that matters. You should see `echomuse.db` and a
`tls/` directory containing **four** files. If `tls/` has fewer, stop and
find the rest — the server certificate and the CA must match each other.

### 4. Enter your settings and start it

Fill in the options from step 2 of *Before you start*, remembering to update
**Controller LAN IP address** to the Home Assistant machine's address. Start the add-on and
watch its log.

`Opening database: /data/echomuse.db` should be followed by your device names
appearing as they connect. If the add-on is newer than your container you will
also see `Running N migration(s) from vX`, a `Backed up vX schema to …` line
before it, and `Schema migrated to vY` after — that is the upgrade doing
exactly what it should.

Then open the dashboard from the sidebar. Note the warning about who opens it
first, in [Signing in changes](#signing-in-changes) below.

### 5. Check your devices

Power-cycle one Dot, or just wait — devices retry on their own. Within a
minute or two it should appear in the dashboard as connected, showing
`wss (TLS)` in the **Link** row on its Status tab.

If it appears as **pending approval** instead of connected, your database did
not come across. The controller is treating it as a device it has never met.
Stop, check step 3, and try again — approving it here would create a second
record for the same hardware on fresh settings.

---

## Your devices find the new controller on their own

You do not need to touch any Dot. They discover the controller by mDNS, so
they follow it to the new machine on their next reconnect. The certificate
they already hold still verifies, because you copied `tls/`.

## Home Assistant re-points itself, if you copied the database

Each EchoMuse device appears in Home Assistant as an ESPHome device on its own
port. Those port numbers live in the database, so copying it keeps every
device on the port Home Assistant already knows.

Home Assistant identifies them by a MAC address derived from the Dot's serial
number, and when it sees that MAC at a new address it **updates the existing
entry rather than creating a new one** — provided the old one is no longer
answering, which is why step 1 mattered. Your entities, their `entity_id`s,
and every automation built on them survive.

If you did not copy the database, Home Assistant sees unfamiliar devices on
unfamiliar ports and offers them as new discoveries. Your automations will be
pointing at entities that no longer exist. This is not recoverable by
re-approving; restore the database.

## Signing in changes

On the standalone container you signed in with a local EchoMuse account. Under
the add-on, Home Assistant has already authenticated you and hands your
identity to EchoMuse, so there is no login screen and no **Sign out** button —
signing out would sign you straight back in.

**The first person to open the dashboard becomes the admin.** Everyone else in
your household who opens it gets read-only access.

**Open it yourself, first, before telling anyone it exists.** Home Assistant
shows the sidebar entry to admin users, so anyone with an admin Home Assistant
account can get there before you.

If someone else does, an admin can put it right under **Settings → Users**,
which lists every account and promotes or demotes it. The catch is that the
screen is admin-only, so it has to be *them* doing it, on your behalf. The
last admin cannot be demoted, so there is no way to lock everyone out.

Your old local accounts are still in the database and are not deleted, but
they are not used while you are coming in through Home Assistant.

Read-only is meaningful here: recordings and transcripts are admin-only, and
the dashboard offers a root shell to every device.

---

## If you want to go back

Your original `data/` directory is untouched — the add-on copied from it and
writes to its own storage. To return to the container: stop the add-on, then
`docker compose up -d` as before.

The one caveat is direction of travel. If the add-on ran long enough to apply
a database upgrade, the copy *it* has moved forward while your original did
not. Going back means going back to your original data as it was, and losing
anything that happened in between. Decide within a day rather than a month.

---

## Gotchas, in one place

- **Copy `tls/` or nothing works**, in a way that gives you no useful error
  and needs a visit to every Dot to put right.
- **Stop the old controller first.** Two controllers on one network are
  indistinguishable to a device, and Home Assistant will not re-point a device
  that still answers at its old address.
- **The add-on must not be older than your container.** Database upgrades are
  one-way.
- **`.env` does not migrate.** Re-enter the options by hand, and update
  `SERVER_IP` to the Home Assistant machine.
- **`SERVER_PORT` / `SERVER_TLS_PORT` have no add-on option yet**
  ([#163](https://github.com/wilbowes/EchoMuse/issues/163)). If you changed
  them, ask before migrating.
- **Open the dashboard yourself first** — the first Home Assistant user
  through the door becomes the admin.
- **Early Access is a separate add-on with separate storage.** Installing it
  is *another* migration with all of the above again, `tls/` included.
  Switching channels is not a toggle.
- **`docker compose pull` on the old install can quietly bring it back.** If
  you have automation that pulls and restarts, disable it, or the two
  controllers problem returns weeks later when you have forgotten about it.

## If you get stuck

Open an issue with a support bundle attached (**Settings → Support → Collect
bundle** in the dashboard, then Download). It contains no recordings, no
transcripts, no network names
and no account names — see [support-bundle.md](support-bundle.md) for exactly
what is and is not in it.
