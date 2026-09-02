package client

import (
	"testing"

	"github.com/wilbowes/EchoMuse/internal/aec"
)

// Adverts moved off the control plane because that is where liveness is
// measured: SendBleAdverts wrote under connMu on the control connection, the
// same mutex and TCP stream as the RTT echo and the keepalive pong (#404).
//
// The two things worth pinning are the ones whose failure is SILENT. Sending
// 0x06 to a controller that cannot read it drops every advertisement with no
// error anywhere, and sending one while the mic is streaming puts telemetry
// in front of a voice turn's audio on a stream that cannot preempt.

func TestControllerFeaturesAreReadFromTheAck(t *testing.T) {
	c := &ControlClient{}

	// No ack seen yet: every feature must read false, so a device that has
	// not negotiated keeps using the path that already worked.
	if c.HasFeature(FeatureBleAdvertsData) {
		t.Fatal("a client with no ack must not claim a controller feature")
	}

	c.setFeatures([]string{FeatureBleAdvertsData})
	if !c.HasFeature(FeatureBleAdvertsData) {
		t.Fatal("feature announced on the ack was not recorded")
	}
	if c.HasFeature("something_else") {
		t.Fatal("unannounced feature read as present")
	}

	// A reconnect can land on a different controller, or the same one after
	// a downgrade. The set must be REPLACED, not merged, or a device carries
	// a capability the controller in front of it does not have.
	c.setFeatures(nil)
	if c.HasFeature(FeatureBleAdvertsData) {
		t.Fatal("features must be replaced on re-ack, not merged")
	}
}

func TestAdvertsYieldToTheMicStream(t *testing.T) {
	d := NewDataClient("ble-test", newFanoutMic(), nil, aec.New())

	// No connection: refused, and the caller is told so. This is the drop
	// path — deliberately NOT a fall back to the control plane, which would
	// put bulk telemetry on the liveness channel exactly when the link is
	// already struggling.
	if d.SendBleAdverts([]byte(`{"adverts":[]}`)) {
		t.Fatal("sent adverts with no data connection")
	}

	// With a mic stream running the answer must be the same, before any
	// write is attempted: within one TCP stream a frame already written
	// cannot be preempted, so not writing it is the only real choice.
	d.micMu.Lock()
	d.micActive = true
	d.micMu.Unlock()
	if d.SendBleAdverts([]byte(`{"adverts":[]}`)) {
		t.Fatal("sent adverts while the mic was streaming")
	}
}
