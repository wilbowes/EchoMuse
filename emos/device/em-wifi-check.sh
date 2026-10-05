#!/bin/sh
# em-wifi-check.sh — em-wifi's security_of() against every flag form
# wpa_supplicant prints.
#
# Sources the real script, so this cannot drift from what ships. The vectors
# follow wpa_supplicant_ie_txt() and the scan-result flag writers in
# wpa_supplicant/ctrl_iface.c, hostap 2.10 (the version emOS builds): each
# bracket is [<proto>-<AKMs joined by +>-<ciphers joined by +>(-preauth)].
#
# Run: sh emos/device/em-wifi-check.sh    (exit 1 on any mismatch)
set -u
here=$(dirname "$0")
EM_WIFI_LIB=1
. "$here/em-wifi"

fail=0
expect() {
    got=$(security_of "$1")
    if [ "$got" = "$2" ]; then
        printf 'ok    %-5s %s\n' "$got" "$1"
    else
        printf 'FAIL  %-5s %s (want %s)\n' "$got" "$1" "$2"
        fail=1
    fi
}

# Joinable with a password.
expect '[WPA2-PSK-CCMP][ESS]'                        psk
expect '[WPA-PSK-TKIP][ESS]'                         psk
expect '[WPA-PSK-TKIP][WPA2-PSK-CCMP][WPS][ESS]'     psk
expect '[WPA2-PSK-TKIP+CCMP-preauth][ESS]'           psk
# WPA2/WPA3 transition mode: the bug this function exists for.
expect '[WPA2-PSK+SAE-CCMP][ESS]'                    psk
expect '[WPA2-PSK+FT/PSK+SAE+FT/SAE-CCMP][ESS]'      psk
# PSK offered alongside the SHA256 variant: plain PSK is enough.
expect '[WPA2-PSK+PSK-SHA256-CCMP][ESS]'             psk

# Open, including the open half of an Enhanced Open transition pair.
expect '[ESS]'                                       open
expect ''                                            open
expect '[WPS][ESS]'                                  open
expect '[OWE-TRANS-OPEN][ESS]'                       open

# Not joinable, each for its own reason.
expect '[WPA2-SAE-CCMP][ESS]'                        wpa3
expect '[WPA2-SAE+FT/SAE-CCMP][ESS]'                 wpa3
expect '[RSN-SAE-CCMP][MESH]'                        wpa3
expect '[WPA2-EAP-CCMP][ESS]'                        eap
expect '[WPA2-EAP-SUITE-B-192-GCMP-256][ESS]'        eap
expect '[WPA2-FT/EAP+EAP-SHA256-CCMP][ESS]'          eap
expect '[WPA2-OWE-CCMP][OWE-TRANS][ESS]'             owe
expect '[WEP][ESS]'                                  wep
# PSK-only variants the driver does not list; a token test that saw "PSK"
# inside them would offer a network that then fails at association.
expect '[WPA2-PSK-SHA256-CCMP][ESS]'                 other
expect '[WPA2-FT/PSK-CCMP][ESS]'                     other
expect '[WPA2-DPP-CCMP][ESS]'                        other
expect '[OSEN-OSEN-CCMP][ESS]'                       other
# An RSN element wpa_supplicant could not parse.
expect '[WPA2-?][ESS]'                               other

exit $fail
