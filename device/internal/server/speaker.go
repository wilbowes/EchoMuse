package server

import (
	"io"
	"github.com/gin-gonic/gin"
	"log"
)

func (s *Server) speakerHandler(c *gin.Context) {
	defer c.Request.Body.Close()
	data, err := io.ReadAll(c.Request.Body)
	if err != nil {
		log.Printf("Failed to read request body: %v\n", err)
		return
	}
	log.Printf("speakerHandler received %d bytes", len(data))
	if err = s.speaker.Pump(data); err != nil {
		log.Printf("Failed to pump audio to speaker: %v\n", err)
	}
}
