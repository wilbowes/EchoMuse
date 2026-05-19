package server

import (
	"io"
	"log"
	"net/http"
	"os/exec"
	"sync"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
)

var shellUpgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

// shellHandler handles WS /shell — spawns sh as root (binary already runs as
// root via Magisk init service) and pipes its stdin/stdout as raw binary frames.
//
// Only one shell session is allowed at a time. A second connection while one
// is active receives a 409 and is closed immediately.
var shellMu sync.Mutex
var shellActive bool

func (s *Server) shellHandler(c *gin.Context) {
	shellMu.Lock()
	if shellActive {
		shellMu.Unlock()
		c.Status(http.StatusConflict)
		return
	}
	shellActive = true
	shellMu.Unlock()

	defer func() {
		shellMu.Lock()
		shellActive = false
		shellMu.Unlock()
	}()

	conn, err := shellUpgrader.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		log.Printf("[shell] WebSocket upgrade failed: %v", err)
		return
	}
	defer conn.Close()

	log.Println("[shell] Session opened")

	cmd := exec.Command("/system/bin/sh")
	stdin, err := cmd.StdinPipe()
	if err != nil {
		log.Printf("[shell] StdinPipe: %v", err)
		return
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		log.Printf("[shell] StdoutPipe: %v", err)
		return
	}
	cmd.Stderr = cmd.Stdout // merge stderr into stdout

	if err := cmd.Start(); err != nil {
		log.Printf("[shell] cmd.Start: %v", err)
		return
	}

	done := make(chan struct{})

	// stdout → WebSocket
	go func() {
		defer close(done)
		buf := make([]byte, 4096)
		for {
			n, err := stdout.Read(buf)
			if n > 0 {
				if werr := conn.WriteMessage(websocket.BinaryMessage, buf[:n]); werr != nil {
					log.Printf("[shell] write to WS: %v", werr)
					break
				}
			}
			if err != nil {
				if err != io.EOF {
					log.Printf("[shell] stdout read: %v", err)
				}
				break
			}
		}
	}()

	// WebSocket → stdin
	go func() {
		for {
			_, data, err := conn.ReadMessage()
			if err != nil {
				log.Printf("[shell] read from WS: %v", err)
				break
			}
			if _, err := stdin.Write(data); err != nil {
				log.Printf("[shell] write to stdin: %v", err)
				break
			}
		}
		stdin.Close()
	}()

	// Wait for shell to exit or connection to drop
	<-done
	cmd.Wait()
	log.Println("[shell] Session closed")
}
