package led

const ledCount = 12

var Leds []Led

func init() {
	Leds = make([]Led, ledCount)
	for i := 0; i < ledCount; i++ {
		Leds[i] = Led{
			ID: i,
			R:  0,
			G:  0,
			B:  0,
		}
	}
}
