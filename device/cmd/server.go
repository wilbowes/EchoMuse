package main

import (
	"context"
	"log"
	"math"
	"os"
	"time"

	internalbuttons "github.com/wilbowes/EchoMuse/internal/bindings/buttons"
	"github.com/wilbowes/EchoMuse/internal/bindings/mic"
	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/server"
	pkgbuttons "github.com/wilbowes/EchoMuse/pkg/buttons"
	"github.com/wilbowes/EchoMuse/pkg/led"
)

const echoDotID = "echo-dot"

func main() {
	log.SetOutput(os.Stdout)
	log.Println("Initializing")

	buttonController, err := internalbuttons.NewButtonController()
	if err != nil {
		log.Fatalf("Failed to initialize Button controller: %v", err)
	}

	microphone, err := mic.NewMicrophone()
	if err != nil {
		log.Fatalf("Failed to initialize Microphone: %v", err)
	}

	pcmSpeaker, err := speaker.NewPcmSpeaker()
	if err != nil {
		log.Fatalf("Failed to initialize PCM Speaker: %v", err)
	}

	s := server.NewServer(buttonController, microphone, pcmSpeaker)

	// Volume buttons handled locally
	buttonController.SetVolumeCallback(func(direction string) {
		if direction == "up" {
			s.VolumeStepUp()
		} else {
			s.VolumeStepDown()
		}
	})

	// Mute button handled locally
	buttonController.SetMuteCallback(func() {
		s.MuteToggle()
	})

	ctx := context.Background()

	// Data client — mic streaming and speaker playback over /data WS
	dataClient := client.NewDataClient(echoDotID, microphone, pcmSpeaker)

	// Control client — registration, LEDs, buttons, mic lifecycle over /control WS
	controlClient := client.NewControlClient(
		echoDotID,
		func(leds []led.Led) {
			s.SetLEDs(leds)
		},
		func() { dataClient.StartMic() },
		func() { dataClient.StopMic() },
	)

	// Subscribe to Dot button events — forward to Clara server via control plane
	_, err = buttonController.SubscribeToButton(func(event pkgbuttons.ButtonClickEvent) {
		log.Printf("Button event: clickType=%d down=%v", event.ClickType, event.Down)
		if event.ClickType == pkgbuttons.DotClick && s.IsMuted() {
			log.Println("Dot button blocked — mic is muted")
			return
		}
		controlClient.SendButton(event)
	})
	if err != nil {
		log.Fatalf("Button subscription failed: %v", err)
	}

	// Disconnected state — gently pulse orange ring
	var pulseCancel context.CancelFunc
	controlClient.OnDisconnected(func() {
		if pulseCancel != nil {
			pulseCancel()
		}
		pulseCtx, cancel := context.WithCancel(ctx)
		pulseCancel = cancel
		go pulseOrange(pulseCtx, s)
	})
	controlClient.OnConnected(func() {
		if pulseCancel != nil {
			pulseCancel()
			pulseCancel = nil
		}
		s.SetLEDs(allLEDs(0, 0, 0))
	})

	log.Println("Ready")
	time.Sleep(2 * time.Second) // allow network stack to settle before mDNS

	// Control client orchestrates discovery and signals data client after registration
	go func() {
		if err := controlClient.Run(ctx, dataClient); err != nil && err != context.Canceled {
			log.Printf("Clara client stopped: %v", err)
		}
	}()

	select {} // block forever
}

// allLEDs returns all 12 LEDs set to the given colour.
func allLEDs(r, g, b uint8) []led.Led {
	leds := make([]led.Led, 12)
	for i := range leds {
		leds[i] = led.Led{ID: i, R: r, G: g, B: b}
	}
	return leds
}

// pulseOrange runs a gentle sine-wave brightness pulse on all LEDs in orange
// until ctx is cancelled. Used to indicate the device is disconnected from
// the Clara server.
func pulseOrange(ctx context.Context, s *server.Server) {
	const (
		minBr    = 0.05
		maxBr    = 0.6
		periodMs = 2000
		stepMs   = 50
	)
	ticker := time.NewTicker(stepMs * time.Millisecond)
	defer ticker.Stop()

	step := 0
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			t := float64(step) / float64(periodMs/stepMs)
			brightness := minBr + (maxBr-minBr)*(0.5+0.5*math.Sin(2*math.Pi*t))
			r := uint8(255 * brightness)
			g := uint8(40 * brightness)
			b := uint8(0)
			s.SetLEDs(allLEDs(r, g, b))
			step = (step + 1) % (periodMs / stepMs)
		}
	}
}
