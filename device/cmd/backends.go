//go:build server

package main

import (
	"github.com/wilbowes/EchoMuse/internal/bindings/mic"
	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/profile"
	pkgmic "github.com/wilbowes/EchoMuse/pkg/mic"
	pkgspeaker "github.com/wilbowes/EchoMuse/pkg/speaker"
)

// Backend selection.
//
// biscuit stays on the tinyalsa bindings it has always run; devices that opt
// into UseALSABackend get the dependency-free ALSA client. Keeping both means
// adding a second device cannot regress the first, and the two can be compared
// on the same hardware if the tinyalsa path ever needs retiring.

// micBackend is the union of what the server and the data client each need
// from a capture backend: the callback interface and the fan-out one.
type micBackend interface {
	pkgmic.Microphone
	pkgmic.Subscribable
}

func newMicrophone(p *profile.Profile) (micBackend, error) {
	if p.UseALSABackend {
		m := mic.NewProfileMicrophone(p)
		if err := m.Init(); err != nil {
			return nil, err
		}
		return m, nil
	}
	return mic.NewMicrophone()
}

// speakerBackend is the playback interface plus the per-stream stats hook the
// control plane reports upstream.
type speakerBackend interface {
	pkgspeaker.Speaker
	OnStreamStats(cb func(speaker.StreamStats))
}

func newSpeaker(p *profile.Profile, echoTap func([]byte), levelTap func(rms float64)) (speakerBackend, error) {
	if p.UseALSABackend {
		s := speaker.NewProfileSpeaker(p, echoTap, levelTap)
		if err := s.Init(); err != nil {
			return nil, err
		}
		return s, nil
	}
	return speaker.NewPcmSpeaker(echoTap, levelTap)
}
