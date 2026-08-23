# BRIEFING: Echo Dot Gen 2 Unlock — Blocker „restricted on locked hw"

## ZIEL

Echo Dot Gen 2 (codename „biscuit") rooten mit R0rt1z2's amonet-biscuit-Exploit,
damit EchoMuse (Open-Source-Firmware-Ersatz) installiert werden kann.

## AKTUELLER BLOCKER

```
$ sudo /usr/bin/fastboot flash brick bin/brick-NS6570.img
Sending sparse 'brick' 1/1 (240 KB)   OKAY [0.018s]
Writing 'brick'                       FAILED (remote: 'the command you input is restricted on locked hw')
```

Der Bootloader (LK) verweigert das Schreiben auf die „brick"-Partition,
WEIL DIE HARDWARE GESPERRT ist. Der FACTFACT-Handshake (siehe unten) hat
das nicht gelöst.

## GERÄT

- Amazon Echo Dot Gen 2 („biscuit", MT8163 SoC)
- Serial: G090L91181740BWC
- Firmware: Fire OS 6.5.7.0 (NS6570/6071), version code 12383141252
- lk_build_desc: 63cb91b-20221007_072309 ← wird von brick.sh als UNTERSTÜTZT erkannt

## HOST (Raspberry Pi 4)

- SSH: christian@192.168.178.76 (Port 22, Key-Auth eingerichtet:
  ~/.ssh/id_ed25519_pi auf dem Mac; Passwort falls nötig: <Pi-Sudo-Passwort — siehe Passwort-Manager/Vaultwarden>)
- Hostname: casaos-pi, Debian 12 (bookworm), ARM64 (aarch64), Kernel 6.12
- fastboot: /usr/bin/fastboot, Version **34.0.5-debian**
  ⚠️ Der Guide fordert fastboot **v36.0.0** (platform-tools)!
  Google liefert KEINE offiziellen linux-arm64 platform-tools.
- adb: /usr/bin/adb (funktioniert)
- Alle Dateien liegen in: /home/christian/EchoMuse/

## DATEIEN (alle vorhanden, Pi + Mac ~/Downloads/echomuse-dot/)

| Datei | Größe | Quelle |
|---|---|---|
| amonet-biscuit-v1.0.0.zip | 19 MB | Community-Mirror (hard-hacks-vault-wcs auf GitHub) |
| update-kindle-csm_biscuit-272.6.8.0_user_680767620.bin | 397 MB | Amazon CloudFront (original, SHA256 beginnt 6ababc51) |
| f1r30s.zip | 404 KB | Community-Mirror |
| Magisk-v17.3.zip | 4,2 MB | GitHub topjohnwu |
| server | 10,7 MB | EchoMuse Release v2.12.0 (ARM binary für den Dot) |

⚠️ amonet-biscuit liegt nur als **v1.0.0** vor! Die Doku verlangt **v1.1.0**
(XDA-Attachment, braucht Forum-Konto). Unterschied unbekannt — möglicherweise
enthält v1.1.0 genau den fehlenden Unlock-Schritt.

## WAS FUNKTIONIERT

1. Fastboot-Modus erreichen (Button-Sequenz):
   - Dot komplett vom Strom, 10s warten
   - Action-Button oben HALTEN
   - USB-Kabel einstecken (bei gehaltenem Button)
   - WEITERHALTEN bis der Ring GRÜN leuchtet (= Fastboot-Modus!), dann loslassen
2. Gerät sichtbar: `lsusb` → `ID 0bb4:0c01 HTC Dream` (= generische Android-Fastboot-ID)
3. `sudo fastboot getvar lk_build_desc` → antwortet korrekt
4. Preloader-Handshake: `cd ~/EchoMuse/amonet/amonet && sudo ./boot-fastboot.sh`
   → wartet auf ttyACM0 → bei neuem Anstecken: „Preloader ready, sending FACTFACT"
   → wird vom Preloader akzeptiert!

## WAS NICHT FUNKTIONIERT

```
sudo ./brick.sh
→ Detected Fire OS 6.5.7.0 (NS6570/6071)
→ Will use brick image: bin/brick-NS6570.img
→ YES eingeben
→ "Bricking PL Header, check LEDs!"
→ (Fehler versteckt hinter || true — mit sichtbarem Aufruf:)
sudo /usr/bin/fastboot flash brick bin/brick-NS6570.img
→ FAILED (remote: 'restricted on locked hw')
```

Kein Regenbogen-Ring. Nach Abziehen/Anstecken bootet der Dot normal
(grüner Ring) = der Preloader-Header wurde NICHT korruptiert.

## WAS BEREITS VERSUCHT WURDE

1. FACTFACT-Handshake VOR dem Flash ✓ (kein Unterschied)
2. System-fastboot statt gebündeltes Binary (das gebündelte ist x86-64 und
   läuft nicht auf ARM64! „Exec format error")
3. Sichtbarer Flash-Aufruf zur Fehlerdiagnose (siehe oben)
4. Mehrere Button-Sequenz-Wiederholungen

## OFFENE FRAGEN / HYPOTHESEN

1. **fastboot-Version?** Guide fordert v36.0.0, Pi hat v34.0.5-debian.
   Google liefert keine linux-arm64 platform-tools — Alternative suchen:
   - Debian trixie/sid android-tools Paket
   - Third-party ARM64 build von platform-tools v36+
2. **v1.0.0 vs v1.1.0?** Das Original-Paket von XDA enthält evtl. einen
   zusätzlichen Unlock-Schritt (z.B. anderes/oem-Kommando nach dem Handshake).
   XDA-Thread: https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/
   (Braucht Forum-Konto zum Download der Anhänge.)
3. **Reihenfolge?** Muss FACTFACT in einem bestimmten Zustand gesendet werden
   (z.B. während Boot statt im Fastboot)?
4. **Gibt es ein oem-Kommando?** z.B. `fastboot oem unlock`, `fastboot oem
   factory`, oder ähnliches, das den Lock löst bevor brick.sh läuft.

## BEFEHLE FÜR DIE DIAGNOSE (Pi)

```bash
ssh christian@192.168.178.76

# Gerät in Fastboot bringen (Button-Sequenz oben), dann:
sudo /usr/bin/fastboot getvar all          # ALLE Variablen ansehen
sudo /usr/bin/fastboot oem unlock          # Unlock-Versuch
sudo /usr/bin/fastboot oem factory         # Factory-Mode-Versuch

# brick.sh liegt in: ~/EchoMuse/amonet/amonet/brick.sh
# Nutzt /usr/bin/fastboot (bereits gepatcht von bin/fastboot)
```

## KONTEXT / RESSOURCEN

- GitHub-Issue mit Wil (Maintainer): https://github.com/wilbowes/EchoMuse/issues/318
  (Frage nach seiner exakten Sequenz — noch keine Antwort)
- XDA-Thread (Autor des Exploits): https://xdaforums.com/t/unlock-root-twrp-unbrick-amazon-echo-dot-2nd-gen-2016-biscuit.4761416/
  (403 für Scraper — Browser mit Konto nötig)
- Blog-Post mit Erfolgslauf: https://danieldb.uk/posts/alexa-2/
  (Erklärt kamakiri/ketten-des-vertrauens, nutzte mtkclient + EchoCLI)
- EchoCLI (Automatisierung): https://github.com/Dragon863/EchoCLI
- Hard-Hacks Guide (unsere Hauptquelle):
  https://github.com/TheOutcastVirus/hard-hacks-vault-wcs/blob/main/alexa/ROOTING_GUIDE.md
- mtkclient (kamakiri-Exploit-Framework): https://github.com/bkerler/mtkclient
  (ist auch im hard-hacks-repo enthalten unter alexa/mtkclient!)

## ALTERNATIVER ANSATZ (falls fastboot-Flash unlösbar)

Der Hard-Hacks-Guide nutzt **mtkclient** für den Tethered Boot:
```bash
cd ~/EchoMuse/mtkclient    # im Repo enthalten!
python3 mtk plstage --ptype=kamakiri2 --preloader=../amonet/preloader_no_hdr.bin
```
mtkclient kommuniziert direkt mit dem MediaTek-Bootrom über die serielle
Schnittstelle (0e8d:0003) — umgeht LK/Fastboot komplett. Vielleicht kann
mtkclient auch das initiale Unlock/Brick durchführen, ohne dass der
gesperrte Fastboot dazwischenfunkt.

## NACH DEM ERFOLGREICHEN UNLOCK (zur Erinnerung, Reihenfolge)

1. brick.sh → Regenbogen-Ring
2. bootrom-step.sh → BROM-Zugang
3. fastboot-step.sh → flasht TWRP (cyan-blinkende LED)
4. FireOS 5.5.5.4 .bin per TWRP flashen (Datei: update-kindle-csm_biscuit-…bin)
5. SOFORT danach f1r30s.zip in TWRP flashen (aktiviert ADB, sperrt OTA)
6. Reboot → adb shell → Root-Shell
7. start_server.sh + server-Binary installieren (EchoMuse-Firmware)
