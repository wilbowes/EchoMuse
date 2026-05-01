package mic

import (
	"context"
	"fmt"
	"github.com/wilbowes/EchoMuse/internal"
	"net/http"
)

type HttpMicrophone struct {
	baseUrl string
	buffer  []byte
}

func NewDefaultHttpMicrophone() Microphone {
	return NewHttpMicrophone(fmt.Sprintf("http://localhost:%d", internal.Port))
}

func NewHttpMicrophone(baseUrl string) Microphone {
	return &HttpMicrophone{
		baseUrl: baseUrl,
		buffer:  make([]byte, 4096),
	}
}

func (h *HttpMicrophone) Init() error {
	return nil
}

func (h *HttpMicrophone) Listen(callback AudioCallback, context context.Context) error {
	res, err := http.Get(fmt.Sprintf("%s/microphone", h.baseUrl))

	if err != nil {
		return err
	}

	defer res.Body.Close()

	for {
		if context.Err() != nil {
			return context.Err()
		}

		n, err := res.Body.Read(h.buffer)
		if err != nil {
			return err
		}

		callback(h.buffer[:n])
	}
}
