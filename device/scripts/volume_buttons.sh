#!/system/bin/sh

LEDS=12
MAX=175

get_volume() {
    tinymix -D 0 61 > /data/local/tmp/vol.txt
    vol=$(cat /data/local/tmp/vol.txt)
    vol="${vol#*: }"
    vol="${vol%% *}"
    echo $vol
}

set_volume() {
    tinymix -D 0 61 $1 $1
}

post_leds() {
    level=$1
    lit=$(( level * LEDS / MAX ))
    [ $lit -gt $LEDS ] && lit=$LEDS
    [ $lit -lt 0 ] && lit=0

    json='['
    i=0
    while [ $i -lt $LEDS ]; do
        [ $i -gt 0 ] && json="$json,"
        if [ $i -lt $lit ]; then
            json="$json{\"id\":$i,\"r\":0,\"g\":200,\"b\":200}"
        else
            json="$json{\"id\":$i,\"r\":0,\"g\":0,\"b\":0}"
        fi
        i=$(( i + 1 ))
    done
    json="$json]"

    /system/bin/curl -s -X POST http://localhost:6996/leds/set \
        -H 'Content-Type: application/json' \
        -d "$json"
}

clear_leds() {
    json='['
    i=0
    while [ $i -lt $LEDS ]; do
        [ $i -gt 0 ] && json="$json,"
        json="$json{\"id\":$i,\"r\":0,\"g\":0,\"b\":0}"
        i=$(( i + 1 ))
    done
    json="$json]"

    /system/bin/curl -s -X POST http://localhost:6996/leds/set \
        -H 'Content-Type: application/json' \
        -d "$json"
}

# Wait for EchoGo to be ready
until /system/bin/curl -sf http://localhost:6996/ > /dev/null 2>&1; do
    sleep 5
done

VOL=$(get_volume)

/system/bin/getevent /dev/input/event2 | while read type code value; do
    if [ "$code" = "0073" ] && [ "$value" = "00000000" ]; then
        VOL=$(( VOL + 17 > MAX ? MAX : VOL + 17 ))
        set_volume $VOL
        post_leds $VOL
        sleep 2
        clear_leds
    elif [ "$code" = "0072" ] && [ "$value" = "00000000" ]; then
        VOL=$(( VOL - 17 < 0 ? 0 : VOL - 17 ))
        set_volume $VOL
        post_leds $VOL
        sleep 2
        clear_leds
    fi
done
