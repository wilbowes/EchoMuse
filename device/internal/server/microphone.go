package server

import (
	"github.com/gin-gonic/gin"
	"net/http"
)

func (s *Server) microphoneHandler(c *gin.Context) {
	audioCallback := func(audioData []byte) {
		written, err := c.Writer.Write(audioData)

		if err != nil || len(audioData) != written {
			return
		}
	}
	if err := s.mic.Listen(audioCallback, c.Request.Context()); err != nil {
		c.AbortWithError(http.StatusInternalServerError, err)
		return
	}
	c.Writer.WriteHeader(http.StatusOK)
}
