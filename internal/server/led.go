package server

import (
	"github.com/wilbowes/EchoMuse/pkg/led"
	"github.com/gin-gonic/gin"
	"net/http"
)

func (s *Server) ledsHandler(c *gin.Context) {
	s.ledMu.Lock()
	lc := s.ledController
	s.ledMu.Unlock()

	if lc == nil {
		c.String(http.StatusInternalServerError, "Waiting for LED setup")
		return
	}
	var leds []led.Led

	if err := c.ShouldBind(&leds); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	if err := lc.SetLEDs(leds...); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}

	c.Status(http.StatusOK)
}
