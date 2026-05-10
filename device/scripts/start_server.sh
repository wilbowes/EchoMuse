#!/system/bin/sh
i=0
while [ $i -lt 120 ]; do
    pid=$(ps | grep echoaudio | grep -v grep)
    if [ -n "$pid" ]; then
        sleep 5
        break
    fi
    sleep 2
    i=$((i + 2))
done
ip link set p2p0 down
# Prevent WiFi suspension
echo "EchoMuse" > /sys/power/wake_lock
# Speaker mixer init
tinymix -D 0 56 On
tinymix -D 0 64 1 1
tinymix -D 0 88 On
tinymix -D 0 61 100 100
# Mic gain — ADC_A channel 0, used for VAD and voice turns
tinymix -D 0 89 100 100
tinymix -D 0 92 60 60
/data/local/bin/volume_buttons.sh &
kill $(ps | grep ledcontroller | grep -v grep)
exec /data/local/bin/server > /tmp/server.log 2>&1
