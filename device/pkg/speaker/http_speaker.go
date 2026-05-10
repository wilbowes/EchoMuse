package speaker

import (
	"context"
	"fmt"
	"github.com/wilbowes/EchoMuse/internal"
	"io"
	"net/http"
)

type HttpSpeaker struct {
	baseUrl    string
	pipeWriter *io.PipeWriter
	httpCtx    context.Context
}

func NewDefaultHttpSpeaker() Speaker {
	return NewHttpSpeaker(fmt.Sprintf("http://localhost:%d", internal.Port))
}

func NewHttpSpeaker(baseUrl string) Speaker {
	return &HttpSpeaker{baseUrl: baseUrl}
}

func (h *HttpSpeaker) Init() error {
	pipeReader, pipeWriter := io.Pipe()
	var cancel context.CancelCauseFunc
	h.httpCtx, cancel = context.WithCancelCause(context.Background())
	go func() {
		defer pipeWriter.Close()
		defer pipeReader.Close()
		_, err := http.Post(fmt.Sprintf("%s/speaker", h.baseUrl), "application/octet-stream", pipeReader)
		if err != nil {
			cancel(err)
		}
		// TODO: Maybe also cancel using custom error?
	}()
	h.pipeWriter = pipeWriter
	return nil
}

func (h *HttpSpeaker) Pump(data []byte) error {
	if h.pipeWriter == nil {
		if err := h.Init(); err != nil {
			return err
		}
	}

	if h.httpCtx.Err() != nil {
		return h.httpCtx.Err()
	}
	if _, err := h.pipeWriter.Write(data); err != nil {
		return err
	}
	return nil
}

func (h *HttpSpeaker) Close() {
	if h.pipeWriter != nil {
		h.pipeWriter.Close()
	}
}

// PumpPeriod is not implemented for HttpSpeaker — streaming playback
// is only supported by PcmSpeaker over the WS path.
func (h *HttpSpeaker) PumpPeriod(data []byte) error {
	return h.Pump(data)
}
