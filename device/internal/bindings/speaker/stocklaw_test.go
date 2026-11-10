package speaker

import (
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

func TestStockLawNeedsAVoicing(t *testing.T) {
	p := outchain.DefaultParams()
	if stockLawOn(nil, true, p, false) {
		t.Fatal("stock-sound law on a board with no voicing")
	}
	if !stockLawOn(board.BiscuitVoicing(), true, p, false) {
		t.Fatal("stock-sound law off on a board with a voicing")
	}
	if stockLawOn(board.BiscuitVoicing(), false, p, false) {
		t.Fatal("stock-sound law on while the controller processes the audio")
	}
	if stockLawOn(board.BiscuitVoicing(), true, p, true) {
		t.Fatal("stock-sound law on with a plug in")
	}
}

// Every board without a voicing must keep the plain law.
func TestEveryBoardWithoutAVoicingKeepsThePlainLaw(t *testing.T) {
	for _, b := range board.Known {
		v := b.Hardware.Speaker.Voicing
		if got := stockLawOn(v, true, outchain.DefaultParams(), false); got != (v != nil) {
			t.Errorf("%s: stock law %v with voicing %v", b.ID, got, v != nil)
		}
	}
}
