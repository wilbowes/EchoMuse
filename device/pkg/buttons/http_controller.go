package buttons

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"github.com/wilbowes/EchoMuse/internal"
	"net/http"
	"strings"
)

type HttpController struct {
	baseUrl string
}

func NewDefaultHttpController() *HttpController {
	return &HttpController{
		baseUrl: fmt.Sprintf("http://localhost:%d", internal.Port),
	}
}

func NewHttpController(baseUrl string) *HttpController {
	return &HttpController{baseUrl: baseUrl}
}

func (h *HttpController) Init() error {
	return nil
}

func (h *HttpController) SubscribeToButton(callback ButtonClickCallback) (*EventSubscription, error) {
	res, err := http.Get(fmt.Sprintf("%s/buttons", h.baseUrl))
	if err != nil {
		return nil, err
	}

	ctx, cancel := context.WithCancel(context.Background())
	sub := NewEventSubscription(cancel)

	go func() {
		defer res.Body.Close()

		scanner := bufio.NewScanner(res.Body)
		for scanner.Scan() {
			if ctx.Err() != nil {
				return
			}
			line := scanner.Text()

			if strings.HasPrefix(line, "data:") {
				rawJsonPayload := strings.Split(line, "data:")[1]
				var event ButtonClickEvent
				if err := json.Unmarshal([]byte(rawJsonPayload), &event); err != nil {
					// TODO: Error handling
					return
				}
				callback(event)
			}
		}

		if err := scanner.Err(); err != nil {
			// TODO: How to handle this
			return
		}
	}()

	return sub, nil
}

func (h *HttpController) GetDotButton() Button {
	return Button{
		Type: DotButton,
	}
}

func (h *HttpController) GetVolumeButton() Button {
	return Button{
		Type: VolumeButton,
	}
}
