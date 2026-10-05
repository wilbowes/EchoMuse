package listen

// State values, reported to the controller as listen_state. The DataClient
// holds the same strings as its ListenStream/ListenLocal/ListenDegraded.
const (
	StateStream   = "stream"
	StateLocal    = "local"
	StateDegraded = "degraded"
)

// Resolve decides what the device does with its wake stream.
//
// Private listening needs all three: the operator asked for on-device wake
// word (owwOnDevice=on), the controller understands sessions, and a scorer is
// loaded. The first two missing mean STREAM, which is what the device did
// before this existed and what an older controller depends on. The third
// missing means DEGRADED, never stream: a device asked to listen privately
// that cannot score must not quietly start sending its microphone instead.
// The button still works, and scorerErr says why to whoever looks.
func Resolve(onDevice bool, controllerHasSessions bool, scorerLoaded bool,
	scorerErr string) (state, reason string) {
	if !onDevice || !controllerHasSessions {
		return StateStream, ""
	}
	if !scorerLoaded {
		if scorerErr == "" {
			scorerErr = "wake word runtime not loaded"
		}
		return StateDegraded, scorerErr
	}
	return StateLocal, ""
}
