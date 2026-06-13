module github.com/wilbowes/EchoMuse

go 1.24.0

require (
	github.com/Binozo/GoTinyAlsa v1.0.3
	github.com/gorilla/websocket v1.5.3
	github.com/grandcat/zeroconf v1.0.0
	github.com/gvalkov/golang-evdev v0.0.0-20220815104727-7e27d6ce89b6
	golang.org/x/sys v0.32.0
)

require (
	github.com/cenkalti/backoff v2.2.1+incompatible // indirect
	github.com/miekg/dns v1.1.41 // indirect
)

replace github.com/Binozo/GoTinyAlsa => ../GoTinyAlsa

require (
	golang.org/x/net v0.39.0 // indirect
	golang.org/x/sync v0.13.0 // indirect
)
