package server

import (
	"github.com/wilbowes/EchoMuse/pkg/buttons"
	"github.com/gin-gonic/gin"
	"io"
	"net/http"
)

func (s *Server) buttonHandler(c *gin.Context) {
	eventChan := make(chan buttons.ButtonClickEvent)
	done := false

	defer func() {
		done = true
		close(eventChan)
	}()

	btnSub, err := s.buttonController.SubscribeToButton(func(clickEvent buttons.ButtonClickEvent) {
		defer func() {
			if r := recover(); r != nil {

			}
		}()
		if !done {
			eventChan <- clickEvent
		}
	})
	defer btnSub.Cancel()

	if err != nil {
		c.AbortWithError(http.StatusInternalServerError, err)
		return
	}

	c.Stream(func(w io.Writer) bool {
		btnEvent, ok := <-eventChan
		if !ok {
			return false
		}
		c.SSEvent("button", btnEvent)
		return true
	})
	done = true
}
