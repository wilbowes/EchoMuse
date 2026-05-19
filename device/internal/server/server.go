package server

import (
	"fmt"
	"github.com/wilbowes/EchoMuse/internal"
	internalLed "github.com/wilbowes/EchoMuse/internal/bindings/led"
	"github.com/wilbowes/EchoMuse/pkg/buttons"
	"github.com/wilbowes/EchoMuse/pkg/led"
	"github.com/wilbowes/EchoMuse/pkg/mic"
	"github.com/wilbowes/EchoMuse/pkg/speaker"
	"github.com/gin-gonic/gin"
	"golang.org/x/sys/unix"
	"log"
	"net/http"
	"os"
	"sync"
	"time"
)

const Port = internal.Port

type Server struct {
	router           *gin.Engine
	ledController    led.Controller
	ledMu            sync.Mutex
	buttonController buttons.Controller
	mic              mic.Microphone
	speaker          speaker.Speaker
	volume           *volumeController
	mute             *muteController
}

func NewServer(buttonController buttons.Controller, microphone mic.Microphone, speaker speaker.Speaker) *Server {
	gin.SetMode(gin.ReleaseMode)
	router := gin.Default()

	server := &Server{
		buttonController: buttonController,
		mic:              microphone,
		speaker:          speaker,
	}

	// Volume controller uses a getter so it handles the nil-during-boot window safely
	server.volume = newVolumeController(func() led.Controller {
		server.ledMu.Lock()
		defer server.ledMu.Unlock()
		return server.ledController
	})

	// Mute controller — same LED getter pattern
	server.mute = newMuteController(func() led.Controller {
		server.ledMu.Lock()
		defer server.ledMu.Unlock()
		return server.ledController
	}, nil)

	// Give volume controller access to mute state so it can restore the red ring
	server.volume.isMuted = func() bool {
		return server.mute.IsMuted()
	}

	router.GET("/", server.rootHandler)
	router.GET("/kill", server.killHandler)
	router.GET("/ping", server.pingHandler)
	router.POST("/leds/set", server.ledsHandler)
	router.GET("/buttons", server.buttonHandler)
	router.GET("/microphone", server.microphoneHandler)
	router.GET("/vad_stream", server.vadStreamHandler)
	router.POST("/speaker", server.speakerHandler)
	router.GET("/volume", server.getVolumeHandler)
	router.POST("/volume", server.setVolumeHandler)
	router.GET("/shell", server.shellHandler)

	server.router = router

	go func() {
		uptime, err := getUptime()
		// Reduced from 90 seconds as server is started at the end of the boot cycle anyway.
		minUptime := time.Second * 5

		if err != nil || uptime < minUptime {
			// If we start too soon the native bootup from the echo will break (LEDs will spin forever)
			stillWait := minUptime - uptime
			log.Printf("Uptime is currently at %0.2fs, waiting %0.2fs for LED setup\n", uptime.Seconds(), stillWait.Seconds())
			time.Sleep(stillWait)
		}

		ledController, err := internalLed.NewDefaultController()
		if err != nil {
			log.Fatalf("Failed to initialize LED controller: %v", err)
		}

		server.ledMu.Lock()
		server.ledController = ledController
		server.ledMu.Unlock()
		clearLeds(ledController)
	}()

	return server
}

func (s *Server) Serve() error {
	return s.router.Run(fmt.Sprintf(":%d", Port))
}

// VolumeStepUp increases volume one step — called by button handler.
func (s *Server) VolumeStepUp() {
	s.volume.StepUp()
}

// VolumeStepDown decreases volume one step — called by button handler.
func (s *Server) VolumeStepDown() {
	s.volume.StepDown()
}

// MuteToggle toggles mic mute state — called by button handler.
func (s *Server) MuteToggle() {
	s.mute.Toggle()
}

// IsMuted returns true when the mic is muted — used to block dot button.
func (s *Server) IsMuted() bool {
	return s.mute.IsMuted()
}

func (s *Server) rootHandler(c *gin.Context) {
	c.Data(http.StatusOK, "text/html; charset=utf-8", []byte("Echo up and running"))
}

func (s *Server) killHandler(c *gin.Context) {
	c.Data(http.StatusOK, "text/html; charset=utf-8", []byte("Bye bye"))
	c.Writer.Flush()
	go func() {
		os.Exit(0)
	}()
}

func (s *Server) pingHandler(c *gin.Context) {
	c.Status(http.StatusOK)

	if s.ledController == nil {
		return
	}
	go func() {
		numLEDs, err := s.ledController.GetNumLEDs()
		if err != nil {
			log.Printf("Error getting number of LEDs: %v\n", err)
			return
		}

		for i := 0; i < numLEDs; i++ {
			leds := make([]led.Led, numLEDs)

			for j := 0; j < numLEDs; j++ {
				if i == j {
					leds[j] = led.Led{
						ID: j,
						R:  255,
						G:  255,
						B:  255,
					}
				} else {
					leds[j] = led.Led{
						ID: j,
						R:  0,
						G:  0,
						B:  0,
					}
				}
			}

			if err := s.ledController.SetLEDs(leds...); err != nil {
				log.Printf("Error setting LEDs: %v\n", err)
				return
			}
			time.Sleep(time.Millisecond * 25)
		}

		// Turnoff
		for i := 255; i >= 0; i -= 25 {
			brightness := uint8(i)
			if brightness < 6 {
				brightness = 0
			}
			if err := s.ledController.SetLEDs(led.Led{
				ID: numLEDs - 1,
				R:  brightness,
				G:  brightness,
				B:  brightness,
			}); err != nil {
				log.Printf("Error setting LEDs: %v\n", err)
			}
			time.Sleep(time.Millisecond * 13)
		}

	}()
}

func clearLeds(ledController led.Controller) {
	// Clear LEDs
	numLEDs, err := ledController.GetNumLEDs()
	if err != nil {
		log.Fatalf("Failed to get number of LEDs: %v", err)
		return
	}

	leds := make([]led.Led, numLEDs)
	for i := 0; i < numLEDs; i++ {
		leds[i] = led.Led{
			ID: i,
			R:  0,
			G:  0,
			B:  0,
		}
	}
	if err = ledController.SetLEDs(leds...); err != nil {
		log.Fatalf("Failed to set LEDs: %v", err)
		return
	}
}

func getUptime() (time.Duration, error) {
	var info unix.Sysinfo_t
	if err := unix.Sysinfo(&info); err != nil {
		return time.Duration(0), err
	}
	return time.Second * time.Duration(info.Uptime), nil
}

// SetLEDs applies LED state directly — called by the Clara client when server sends LED commands.
func (s *Server) SetLEDs(leds []led.Led) {
	s.ledMu.Lock()
	lc := s.ledController
	s.ledMu.Unlock()
	if lc == nil {
		return
	}
	if err := lc.SetLEDs(leds...); err != nil {
		log.Printf("SetLEDs error: %v", err)
	}
}
