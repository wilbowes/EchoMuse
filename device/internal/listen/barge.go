package listen

// Flusher discards the reply that is queued to play.
type Flusher interface{ Flush() }

// CutReplyOnBarge stops the reply the moment the Echo's own barge fires. The
// controller flushes every barge over playback whoever wins the claim, but a
// round trip later: on VVV 2026-10-23 its speaker_flush landed 1.3s late on a
// stalled link, and the barge session recorded the rest of the reply. Its
// flush stays, for firmware without this. With barge-in off the controller
// declines the wake, so the reply plays on.
func CutReplyOnBarge(spk Flusher, barge bool, bargeIn *bool) bool {
	if !barge || bargeIn == nil || !*bargeIn {
		return false
	}
	spk.Flush()
	return true
}
