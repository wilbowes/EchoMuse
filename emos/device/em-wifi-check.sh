#!/bin/sh
# em-wifi-check.sh — em-wifi's security_of() against every flag form
# wpa_supplicant prints.
#
# Sources the real script, so this cannot drift from what ships. The vectors
# are wifi-security-cases.txt, which states the grammar they follow.
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

# The cases are shared with the dashboard wizard's classifier
# (controller/tests/wifi_scan.test.mjs), so the two cannot disagree.
cases=0
while IFS="$(printf '\t')" read -r want flags; do
    case "$want" in ''|\#*) continue ;; esac
    expect "$flags" "$want"
    cases=$((cases + 1))
done < "$here/wifi-security-cases.txt"
[ "$cases" -ge 30 ] || { echo "FAIL  only $cases cases read"; fail=1; }

# A typed name reaches the conf as its exact bytes.
expect_hex() {
    got=$(text_hex "$1")
    if [ "$got" = "$2" ]; then
        printf 'ok    hex   %s\n' "$1"
    else
        printf 'FAIL  hex   %s -> %s (want %s)\n' "$1" "$got" "$2"
        fail=1
    fi
}
expect_hex 'net'              6e6574
expect_hex 'My Home WiFi'     4d7920486f6d652057694669
expect_hex 'a"b\c'            6122625c63
expect_hex "$(printf 'caf\303\251')" 636166c3a9
expect_hex ' x '              207820

# Every network block probes by name, or a network hidden after setup is lost
# at the next boot.
for psk_line in 'psk="12345678"' ''; do
    if network_block 6e6574 "$psk_line" WPA-PSK | grep -qx '        scan_ssid=1'; then
        printf 'ok    scan_ssid=1 (%s)\n' "${psk_line:-open}"
    else
        printf 'FAIL  no scan_ssid=1 (%s)\n' "${psk_line:-open}"
        fail=1
    fi
done
if [ "$(network_block 6e6574 '' NONE | grep -c psk)" != 0 ]; then
    echo 'FAIL  an open block carries a psk line'
    fail=1
fi

exit $fail
