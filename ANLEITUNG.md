# Echo Dot 2. Gen → EchoMuse — Komplette Anleitung

Alles vorbereitet: VM läuft, Dateien sind da, Controller-Umgebung installiert.

---

## SCHRITT 0 — Kabel prüfen (wichtigster Punkt!)

Der Dot muss als USB-Gerät sichtbar sein. Die meisten Micro-USB-Kabel
sind Nur-Lade-Kabel und scheitern hier.

```bash
# 1. Dot vom Strom trennen
# 2. Action-Button oben gedrückt HALTEN
# 3. Bei gehaltenem Button das USB-Kabel an den Mac stecken (DIREKT, kein Hub!)
# 4. 3 Sekunden warten, Button loslassen
# 5. Sofort prüfen:
system_profiler SPUSBDataType | grep -iB2 -A6 -E "amazon|lab126"
```

- **Gerät erscheint** → weiter zu Schritt 1 ✓
- **Nichts erscheint** → anderes Kabel (Datenkabel! z.B. von externer
  Festplatte / Kindle / PS4-Controller). Test: Handy am selben Kabel muss
  auch am Mac erscheinen.

Hinweis: Im normal eingeschalteten Zustand meldet sich der Dot NICHT per
USB — die Sichtbarkeit kommt nur während der Button-Sequenz.

---

## SCHRITT 1 — Controller starten (Terminal 1, offen lassen!)

```bash
cd ~/Desktop/echomuse/upstream/controller
../.venv-controller/bin/python -u em_start.py
```

- Kurz nach dem Start erscheint im Log ein **Kasten mit dem Setup-Token**
  → notieren (wird gleich im Dashboard gebraucht)
- Dashboard-Adresse: **http://localhost:8768**
- Dieses Terminal muss die ganze Zeit offen bleiben

---

## SCHRITT 2 — Dot in die VM durchreichen (Terminal 2)

```bash
cd ~/Desktop/echomuse/upstream
./vm-unlock.sh attach
```

Folgt der Aufforderung: Action-Button-Sequenz wiederholen (Strom weg →
Button halten → Kabel rein → loslassen), dann Enter.

Erfolgskontrolle: `lsusb` in der VM zeigt ein Gerät mit ID `1949:…`

---

## SCHRITT 3 — Der geführte Unlock (Terminal 2, läuft weiter)

```bash
./vm-unlock.sh guide
```

Der Guide stellt Fragen. Die Antworten:

| Frage | Antwort |
|---|---|
| „Bereits entsperrt (TWRP bootbar)?" | `n` |
| „Unlock JETZT durchführen?" | `ja` (exakt so tippen) |
| „Serial zur Bestätigung" | Serial aus der Ausgabe oben abschreiben und eintippen |

Dann läuft **brick.sh** automatisch (mehrere Minuten, nicht unterbrechen!).

### Danach im Guide:

1. Er sagt: „Dot sollte jetzt in TWRP sein" → Enter
2. **Am Bildschirm des Terminals**: ADB-Sideload-Anweisungen erscheinen
   - Im TWRP-Menü am Gerät: **Advanced → ADB Sideload → Swipe**
   - Wenn „Sideload started" erscheint → im Terminal Enter
   - Die FireOS-Firmware wird geflasht (dauert)
3. `f1r30s.zip` wird automatisch installiert
   (**PFLICHT nach jedem Firmware-Flash — ohne ihn bootet das OS nicht!**)
4. Am Gerät: **Reboot System** wählen → im Terminal Enter
5. Build-Verifikation: muss `272.6.8.0_user_680767620` zeigen

---

## SCHRITT 4 — Ins Dashboard und provisionieren

1. Browser öffnen: **http://localhost:8768**
2. Setup-Token aus Schritt 1 eingeben → Admin-Konto anlegen
3. Tab **Provisioning** öffnen
4. Chrome fragt nach USB-Freigabe für den Dot → **erlauben**
   (der Dot muss dafür wieder per USB an den Mac — der Wizard holt sich
   die Firmware selbst)
5. Wizard-Schritten folgen: WiFi-Zugangsdaten eingeben, Firmware installieren
6. Wenn der Wizard durch ist: Dot erscheint im Dashboard → **Approve**

---

## SCHRITT 5 — Home Assistant

- Der Dot erscheint automatisch als ESPHome-Gerät in HA
- Voraussetzung: HA mit eingerichteter **Assist-Pipeline** (STT + TTS)
- Wake Word sagen → sprechen → Antwort kommt aus dem Dot

---

## FEHLERBEHEBUNG

| Symptom | Lösung |
|---|---|
| Schritt 0 zeigt nichts | Anderes Kabel; direkt am Mac-Port; Button-Sequenz exakt |
| `attach` findet nichts | Sequenz wiederholen; `./vm-unlock.sh status` prüfen |
| brick.sh schlägt fehl | Dot ist ggf. in TWRP = rettbar. XDA-Thread:
  xdaforums.com/t/4761416 — Fehlermeldung dort suchen |
| Wizard-Zeitüberschreitung | Retry-Button; `./vm-unlock.sh detach && ./vm-unlock.sh attach` |
| Controller-Log zeigt Fehler | Nichts stoppen — erst Log-Zeile lesen/abschicken |

## NOTFALL

Dot in TWRP = alles gut, neu flashbar. Dot tot mit schwarzem Ring =
Gehäuse öffnen und Kontakte brücken (XDA-Thread „unbrick").
