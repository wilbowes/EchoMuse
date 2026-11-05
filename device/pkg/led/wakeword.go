package led

// Dim next to the mute ring's (180,0,0), so the two reds cannot be mistaken
// for each other across a room.
const wakeWordOffRed = 40

// WakeWordOffFrame is the idle ring while the wake word is off (#990):
// every other LED dim red, steady. The dark gaps and the unlit mute button
// separate it from the mute ring's solid red; a turn, the volume arc, mute
// and the link state all paint over it.
func WakeWordOffFrame() []Led {
	frame := make([]Led, ledCount)
	for i := range frame {
		frame[i] = Led{ID: i}
		if i%2 == 0 {
			frame[i].R = wakeWordOffRed
		}
	}
	return frame
}
