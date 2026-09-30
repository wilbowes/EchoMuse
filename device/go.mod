module github.com/wilbowes/EchoMuse

go 1.24.0

require (
	github.com/Binozo/GoTinyAlsa v1.0.3
	github.com/flynn/noise v1.1.0
	github.com/gorilla/websocket v1.5.3
	github.com/grandcat/zeroconf v1.0.0
	github.com/gvalkov/golang-evdev v0.0.0-20220815104727-7e27d6ce89b6
	github.com/mewkiz/flac v1.0.14
	golang.org/x/crypto v0.46.0
	golang.org/x/sys v0.41.0
)

require (
	github.com/cenkalti/backoff v2.2.1+incompatible // indirect
	github.com/miekg/dns v1.1.41 // indirect
)

replace github.com/Binozo/GoTinyAlsa => ../GoTinyAlsa

require (
	github.com/icza/bitio v1.1.0 // indirect
	github.com/mewkiz/pkg v0.0.0-20250417130911-3f050ff8c56d // indirect
	github.com/mewpkg/term v0.0.0-20241026122259-37a80af23985 // indirect
	golang.org/x/net v0.48.0 // indirect
	golang.org/x/sync v0.13.0 // indirect
)
