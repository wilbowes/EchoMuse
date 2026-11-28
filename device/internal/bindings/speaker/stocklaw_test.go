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

// Stock sound is the internal driver's voicing and mono by design; stereo is
// for the jack (#273). On every board, whatever the controller pushes and
// wherever the plug is, the two are never on together, and whenever stereo
// is on the chain's own params (chainForJack) have stock sound off, so the
// chain permits what the speaker asks for. Stereo is on exactly for a known
// plug on a board whose jack takes it (board.Speaker.StereoJack).
func TestStockAndStereoAreNeverBothOn(t *testing.T) {
	var params []outchain.Params
	for _, stockSound := range []bool{false, true} {
		for _, guard := range []bool{false, true} {
			for _, limiter := range []bool{false, true} {
				p := outchain.DefaultParams()
				p.StockSound, p.GuardEnabled, p.LimiterEnabled = stockSound, guard, limiter
				params = append(params, p)
			}
		}
	}
	for _, b := range board.Known {
		v := b.Hardware.Speaker.Voicing
		for _, p := range params {
			for _, active := range []bool{false, true} {
				for _, known := range []bool{false, true} {
					for _, inserted := range []bool{false, true} {
						plugged := known && inserted
						stock := stockLawOn(v, active, p, plugged)
						boardStereo := b.Hardware.Speaker.StereoJack
						stereo := stereoOut(stock, plugged, boardStereo)
						if stock && stereo {
							t.Errorf("%s %+v active=%v known=%v inserted=%v: stock sound and stereo both on", b.ID, p, active, known, inserted)
						}
						if stereo && chainForJack(p, plugged).StockSound {
							t.Errorf("%s %+v: stereo on while the chain is given stock sound", b.ID, p)
						}
						if stereo != (plugged && boardStereo) {
							t.Errorf("%s %+v active=%v known=%v inserted=%v: stereo %v", b.ID, p, active, known, inserted, stereo)
						}
					}
				}
			}
		}
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
