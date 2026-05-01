package led

import (
	"bytes"
	"testing"
)

const minColor = 48
const maxColor = 70

func TestLed_BuildArgument(t *testing.T) {
	var colorTests = []struct {
		led      Led // input
		expected []byte
	}{
		{Led{R: 0, G: 0, B: 255}, []byte{minColor, minColor, minColor, minColor, maxColor, maxColor}},
		{Led{R: 0, G: 255, B: 255}, []byte{minColor, minColor, maxColor, maxColor, maxColor, maxColor}},
		{Led{R: 255, G: 255, B: 255}, []byte{maxColor, maxColor, maxColor, maxColor, maxColor, maxColor}},
		{Led{R: 255, G: 255, B: 0}, []byte{maxColor, maxColor, maxColor, maxColor, minColor, minColor}},
		{Led{R: 255, G: 0, B: 0}, []byte{maxColor, maxColor, minColor, minColor, minColor, minColor}},
		{Led{R: 0, G: 0, B: 0}, []byte{minColor, minColor, minColor, minColor, minColor, minColor}},
	}
	for _, colorTest := range colorTests {
		got := colorTest.led.BuildArgument()
		if !bytes.Equal(got, colorTest.expected) {
			t.Errorf("led.BuildArgument(R: %d, G: %d, B: %d) = %x; want %x", colorTest.led.R, colorTest.led.G, colorTest.led.B, got, colorTest.expected)
		}
	}
}

func TestLed_SetColor(t *testing.T) {
	for i := 0; i < 256; i++ {
		hexI := uint8(i)
		expected := Led{R: hexI, G: hexI, B: hexI}
		led := Led{}
		led.SetColor(hexI, hexI, hexI)

		if led != expected {
			t.Errorf("led.SetColor(%d, %d, %d) != Led{R: %d, G: %d, B: %d}", hexI, hexI, hexI, hexI, hexI, hexI)
		}
	}
}
