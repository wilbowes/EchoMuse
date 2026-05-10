package led

import (
	"bytes"
	"fmt"
)

type Led struct {
	// Specifies the target LED on the Echo Dot. Ranges between 0 and 11 (12 LEDs).
	ID int `json:"id"`
	// R color value of the LED
	R uint8 `json:"r"`
	// G color value of the LED
	G uint8 `json:"g"`
	// B color value of the LED
	B uint8 `json:"b"`
}

const format = "%02X"

// BuildArgument converts the defined R, G and B values into a hex format in order to send it to the i2C device
// Example with color blue: rgb(0, 0, 255) => 3030 3030 4646 (without empty spaces)
func (l *Led) BuildArgument() []byte {
	var buffer bytes.Buffer
	buffer.WriteString(fmt.Sprintf(format, l.R))
	buffer.WriteString(fmt.Sprintf(format, l.G))
	buffer.WriteString(fmt.Sprintf(format, l.B))
	return buffer.Bytes()
}

// SetColor sets the color of the LED struct
// Needs to be sent before calling BuildArgument
func (l *Led) SetColor(r, g, b uint8) {
	l.R = r
	l.G = g
	l.B = b
}
