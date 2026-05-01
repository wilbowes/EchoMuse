package led

import (
	"bytes"
	"encoding/json"
	"fmt"
	"github.com/wilbowes/EchoMuse/internal"
	"io"
	"net/http"
)

type HttpController struct {
	baseUrl string
}

func NewDefaultHttpController() *HttpController {
	return NewHttpController(fmt.Sprintf("http://localhost:%d", internal.Port))
}

func NewHttpController(baseUrl string) *HttpController {
	return &HttpController{
		baseUrl: baseUrl,
	}
}

func (h *HttpController) Init() error {
	return nil // Server already init LEDs
}

func (h *HttpController) GetNumLEDs() (int, error) {
	return len(Leds), nil
}

func (h *HttpController) SetLEDs(led ...Led) error {
	body, err := json.Marshal(led)
	if err != nil {
		return err
	}
	r, err := http.NewRequest(http.MethodPost, fmt.Sprintf("%s/Leds/set", h.baseUrl), bytes.NewBuffer(body))
	if err != nil {
		return err
	}

	r.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(r)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, err = io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	return nil
}
