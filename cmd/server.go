package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	internalbuttons "github.com/wilbowes/EchoMuse/internal/bindings/buttons"
	"github.com/wilbowes/EchoMuse/internal/bindings/mic"
	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/server"
	pkgbuttons "github.com/wilbowes/EchoMuse/pkg/buttons"
	"github.com/wilbowes/EchoMuse/pkg/led"
)

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

	// Volume buttons handled locally by EchoGo
	buttonController.SetVolumeCallback(func(direction string) {
		if direction == "up" {
			s.VolumeStepUp()
		} else {
			s.VolumeStepDown()
		}
	})

	// Mute button handled locally by EchoGo
	buttonController.SetMuteCallback(func() {
		s.MuteToggle()
	})

	ctx := context.Background()

	// Clara client — discovers server via mDNS, receives LED commands
	claraClient := client.NewClient(func(ledsRaw json.RawMessage) {
		var leds []led.Led
		if err := json.Unmarshal(ledsRaw, &leds); err != nil {
			log.Printf("LED unmarshal error: %v", err)
			return
		}
		s.SetLEDs(leds)
	})

	// Subscribe to Dot button events — forward to Clara server
	// Volume and mute events are intercepted locally and never reach this callback
	_, err = buttonController.SubscribeToButton(func(event pkgbuttons.ButtonClickEvent) {
		// Block dot button (action button) when muted
		if event.ClickType == pkgbuttons.DotClick && s.IsMuted() {
			log.Println("Dot button blocked — mic is muted")
			return
		}
		claraClient.SendButton(event)
	})
	if err != nil {
		log.Fatalf("Button subscription failed: %v", err)
	}

	// Connect to Clara server in background — reconnects automatically on drop
	go func() {
		if err := claraClient.Run(ctx); err != nil && err != context.Canceled {
			log.Printf("Clara client stopped: %v", err)
		}
	}()

	log.Println("Starting server")

	if err := s.Serve(); err != nil {
		if strings.Contains(err.Error(), "address already in use") {
			log.Println("Server is already running, killing")
			response, err := http.Get(fmt.Sprintf("http://localhost:%d/kill", server.Port))
			if err != nil {
				log.Fatalf("Failed to send request to server: %v", err)
			}
			body, _ := io.ReadAll(response.Body)
			log.Println("Kill response from server:", string(body))
			response.Body.Close()
			time.Sleep(time.Millisecond * 100)
			log.Println("Now starting this instance")
			if err = s.Serve(); err != nil {
				log.Fatalf("Failed to start server: %v", err)
			}
		}
		log.Fatal(err)
	}
}