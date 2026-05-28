package main

import (
	"context"
	"log"
	"math"
	"os"
	"os/exec"
	"strconv"
	"time"

	internalbuttons "github.com/wilbowes/EchoMuse/internal/bindings/buttons"
	"github.com/wilbowes/EchoMuse/internal/bindings/mic"
	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/server"
	pkgbuttons "github.com/wilbowes/EchoMuse/pkg/buttons"
	"github.com/wilbowes/EchoMuse/pkg/led"
)

func main() {
	log.SetOutput(os.Stdout)
	log.Printf("EchoMuse %s starting", client.Version)

	deviceID := client.GetSerialNo()
	log.Printf("Device ID: %s", deviceID)

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

	buttonController.SetVolumeCallback(func(direction string) {
		if direction == "up" {
			s.VolumeStepUp()
		} else {
			s.VolumeStepDown()
		}
	})
	buttonController.SetMuteCallback(func() {
		s.MuteToggle()
	})

	ctx := context.Background()

	dataClient := client.NewDataClient(deviceID, microphone, pcmSpeaker)

	// Direction callback — update LED ring to show estimated source angle
	dataClient.OnDirectionChanged(func(angle float64) {
		s.SetDirectionLEDs(angle)
	})
	controlClient := client.NewControlClient(
		deviceID,
		func(leds []led.Led) { s.SetLEDs(leds) },
		func(lockMic bool) { dataClient.StartMic(lockMic) },
		func() { dataClient.StopMic() },
	)

	// Button events — forward to controller via control plane
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

	// Disconnected — orange pulse
	var pulseCancel context.CancelFunc
	controlClient.OnDisconnected(func() {
		if pulseCancel != nil {
			pulseCancel()
		}
		pulseCtx, cancel := context.WithCancel(ctx)
		pulseCancel = cancel
		go pulseOrange(pulseCtx, s)
	})

	// Pending approval — slow white pulse
	controlClient.OnPending(func() {
		if pulseCancel != nil {
			pulseCancel()
		}
		pulseCtx, cancel := context.WithCancel(ctx)
		pulseCancel = cancel
		go pulseWhite(pulseCtx, s)
	})

	// Connected — stop pulse, clear LEDs, hand ring back to direction arc
	controlClient.OnConnected(func() {
		if pulseCancel != nil {
			pulseCancel()
			pulseCancel = nil
		}
		s.SetLEDs(allLEDs(0, 0, 0))
		s.LEDModeDirection()
	})

	// Config applied — apply hardware changes via tinymix
	controlClient.OnConfigApplied(func(msg config.ConfigMessage) {
		applyHardwareConfig(msg)
	})

	// Mute state change — notify controller so dashboard can reflect it
	// When unmuting, release ring back to direction arc
	s.SetMuteChangeCallback(func(muted bool) {
		controlClient.SendMuteState(muted)
		// Direction arc is controlled by beam lock state, not mute state.
		// SetDirectionLEDs claims direction mode when beam is locked,
		// and clears LEDs when beam unlocks (angle == -1).
	})

	log.Println("Ready")
	time.Sleep(2 * time.Second)

	go func() {
		if err := controlClient.Run(ctx, dataClient); err != nil && err != context.Canceled {
			log.Printf("Control client stopped: %v", err)
		}
	}()

	select {}
}

// applyHardwareConfig runs tinymix commands for fields that map to hardware.
// Called whenever the controller pushes a config message.
func applyHardwareConfig(msg config.ConfigMessage) {
	if msg.AdcDigitalGain > 0 {
		tinymix("89", strconv.Itoa(msg.AdcDigitalGain), strconv.Itoa(msg.AdcDigitalGain))
		tinymix("107", strconv.Itoa(msg.AdcDigitalGain), strconv.Itoa(msg.AdcDigitalGain))
		tinymix("125", strconv.Itoa(msg.AdcDigitalGain), strconv.Itoa(msg.AdcDigitalGain))
		tinymix("143", strconv.Itoa(msg.AdcDigitalGain), strconv.Itoa(msg.AdcDigitalGain))
	}
	if msg.AdcMicpga > 0 {
		tinymix("92", strconv.Itoa(msg.AdcMicpga), strconv.Itoa(msg.AdcMicpga))
		tinymix("110", strconv.Itoa(msg.AdcMicpga), strconv.Itoa(msg.AdcMicpga))
		tinymix("128", strconv.Itoa(msg.AdcMicpga), strconv.Itoa(msg.AdcMicpga))
		tinymix("146", strconv.Itoa(msg.AdcMicpga), strconv.Itoa(msg.AdcMicpga))
	}
	if msg.StartupVolume > 0 {
		tinymix("61", strconv.Itoa(msg.StartupVolume), strconv.Itoa(msg.StartupVolume))
	}
}

func tinymix(ctl string, args ...string) {
	cmdArgs := append([]string{"-D", "0", ctl}, args...)
	out, err := exec.Command("tinymix", cmdArgs...).CombinedOutput()
	if err != nil {
		log.Printf("[tinymix] ctl %s failed: %v — %s", ctl, err, string(out))
	}
}

func allLEDs(r, g, b uint8) []led.Led {
	leds := make([]led.Led, 12)
	for i := range leds {
		leds[i] = led.Led{ID: i, R: r, G: g, B: b}
	}
	return leds
}

// pulseOrange — sine-wave orange pulse while disconnected from server.
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
			br := minBr + (maxBr-minBr)*(0.5+0.5*math.Sin(2*math.Pi*t))
			s.SetLEDs(allLEDs(uint8(255*br), uint8(40*br), 0))
			step = (step + 1) % (periodMs / stepMs)
		}
	}
}

// pulseWhite — slow white pulse while pending controller approval.
// Slower and dimmer than orange to be visually distinct.
func pulseWhite(ctx context.Context, s *server.Server) {
	const (
		minBr    = 0.02
		maxBr    = 0.35
		periodMs = 3000 // slower than orange
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
			br := minBr + (maxBr-minBr)*(0.5+0.5*math.Sin(2*math.Pi*t))
			v := uint8(255 * br)
			s.SetLEDs(allLEDs(v, v, v))
			step = (step + 1) % (periodMs / stepMs)
		}
	}
}
