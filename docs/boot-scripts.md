# Boot scripts: running your own things on an Echo

An Echo runs every `*.sh` file in this folder each time it starts:

```text
/data/local/etc/echomuse/boot.d/
```

The folder does not exist by default, so an Echo runs nothing extra until you
create it. It is per Echo, and it survives firmware updates. Editing
`start_server.sh` on the Echo does not work for this: the controller puts its
own copy back when the Echo connects.

**A boot script runs as root on every start, so anything it does is your
responsibility.** If you break something, delete the script from the dashboard
Console or the USB console and restart the Echo.

## How scripts run

- **What runs:** files ending in `.sh`, run with `/system/bin/sh`. They do not
  need to be executable.
- **When:** at start-up, just before the firmware starts. On emOS the network
  is already up.
- **Together:** each script starts in the background at the same time, so one
  that hangs or fails holds up nothing else. If the order matters, put the
  steps in one script.
- **Run again:** a script can run more than once per boot if the Echo's start
  script restarts, so it has to cope with its work being done already.
- **Logs:** a script's output goes to `/tmp`, as `/tmp/boot-dropbear.sh.log` for
  `dropbear.sh`, and is lost on reboot. A line in
  `/data/local/etc/echomuse/supervisor.log` records that it started.

Add a script from the device's Console tab in the dashboard:

```text
mkdir -p /data/local/etc/echomuse/boot.d
cat > /data/local/etc/echomuse/boot.d/hello.sh <<'EOF'
echo "hello from boot"
EOF
```

It runs on the next restart. To remove it, delete the file.

## Example: SSH with dropbear (emOS)

EchoMuse ships no SSH server, and the dashboard Console is the supported remote
shell. If you want `ssh`, you can add dropbear yourself. **This puts a root
login on your network**, and emOS otherwise has nothing listening at all. The
setup below allows SSH keys only, from your own network only.

This example is for emOS. On FireOS, `/etc` is read-only, so the script below
cannot create the login files.

### 1. Build a static binary

On a computer with Docker. Check for a newer release at
[matt.ucc.asn.au/dropbear](https://matt.ucc.asn.au/dropbear/) and change the
version if there is one.

```bash
mkdir -p echo-ssh && cd echo-ssh && docker run --rm --platform linux/arm/v7 -v "$PWD":/out alpine:3.20 sh -euc '
  apk add --no-cache build-base linux-headers curl tar bzip2
  cd /tmp
  curl -fsSL https://matt.ucc.asn.au/dropbear/releases/dropbear-2025.88.tar.bz2 | tar xj
  cd dropbear-2025.88
  P="/sbin:/system/bin:/system/xbin"
  printf "#define DEFAULT_PATH \"%s\"\n#define DEFAULT_ROOT_PATH \"%s\"\n" "$P" "$P" > localoptions.h
  ./configure --enable-static --disable-zlib --disable-syslog --disable-lastlog --disable-utmp --disable-utmpx --disable-wtmp --disable-wtmpx --disable-pututline --disable-pututxline
  make -j4 PROGRAMS="dropbear dropbearkey" MULTI=1 STATIC=1
  strip dropbearmulti
  cp dropbearmulti /out/'
```

The `localoptions.h` line matters: dropbear's default `PATH` has none of the
folders an Echo keeps its commands in.

### 2. Copy it to the Echo

Serve the folder from your computer:

```bash
python3 -m http.server 8000
```

Then in the Echo's Console, using your computer's IP address rather than its
name, because some emOS builds cannot look up names with `wget`:

```text
D=/data/local/etc/dropbear
mkdir -p $D/.ssh
wget -O $D/dropbearmulti http://192.168.1.50:8000/dropbearmulti
```

### 3. Add your key

Paste your public key, usually `~/.ssh/id_ed25519.pub`, and create the Echo's
host key:

```text
echo 'ssh-ed25519 AAAA... you@computer' > $D/.ssh/authorized_keys
chmod 700 $D $D/.ssh $D/dropbearmulti
chmod 600 $D/.ssh/authorized_keys
$D/dropbearmulti dropbearkey -t ed25519 -f $D/host_key
```

### 4. Add the boot script

Set `LAN` to your own network. `ip route` on the Echo shows it.

```text
mkdir -p /data/local/etc/echomuse/boot.d
cat > /data/local/etc/echomuse/boot.d/dropbear.sh <<'EOF'
LAN=192.168.1.0/24
D=/data/local/etc/dropbear
rm -f /etc/passwd /etc/shells
echo "root:x:0:0:root:$D:/system/bin/sh" > /etc/passwd
echo /system/bin/sh > /etc/shells
iptables -C INPUT -p tcp -s $LAN --dport 22 -j ACCEPT 2>/dev/null ||
    iptables -I INPUT -p tcp -s $LAN --dport 22 -j ACCEPT
[ -f /run/dropbear.pid ] && kill -0 $(cat /run/dropbear.pid) 2>/dev/null && exit 0
exec $D/dropbearmulti dropbear -r $D/host_key -P /run/dropbear.pid -s -p 22
EOF
sh /data/local/etc/echomuse/boot.d/dropbear.sh
```

The last line starts it now, so you can test it before restarting the Echo:

```bash
ssh root@<echo-ip>
```

### Why the script does each thing

- **`/etc/passwd` and `/etc/shells`:** dropbear needs them to find `root` and
  accept its shell, and emOS does not set them up. `/etc` is in RAM, so the
  script writes them on every boot.
- **`iptables`:** emOS drops inbound traffic by default. The rule lets in port
  22 from `LAN` only, and is also gone after a reboot. IPv6 stays blocked.
- **`-s`:** turns off password logins.
- **The home folder is on `/data`:** `/root` would be in RAM, and your key would
  disappear on every reboot.

### Removing it

```text
rm /data/local/etc/echomuse/boot.d/dropbear.sh
kill $(cat /run/dropbear.pid)
iptables -D INPUT -p tcp -s 192.168.1.0/24 --dport 22 -j ACCEPT
rm -r /data/local/etc/dropbear
```

Use the same `LAN` value you put in the script.
