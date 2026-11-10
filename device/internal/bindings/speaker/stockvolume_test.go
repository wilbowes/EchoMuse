package speaker

import (
	"math"
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/board"
)

// biscuit is the voicing em_volume.STOCK_CEILING_DB is held to.
var biscuit = board.BiscuitVoicing()

// Held to em_volume.stock_level_db: the two must give the same dB for every
// level.
func TestStockVolumeMatchesTheController(t *testing.T) {
	want := map[int]float64{1: -53, 13: -47, 47: -30, 64: -21.5, 89: -9, 114: 3.5, 126: 9.5, 127: 10}
	for level, db := range want {
		got, ok := StockVolumeDb(level, biscuit)
		if !ok || math.Abs(got-db) > 1e-9 {
			t.Errorf("level %d: %v dB, want %v", level, got, db)
		}
	}
	if _, ok := StockVolumeDb(0, biscuit); ok {
		t.Error("level 0 must be silence")
	}
	if StockVolumeGain(0, biscuit) != 0 {
		t.Error("level 0 gain must be 0")
	}
}

// The buttons step 8 levels from 47 (internal/server volume.go); every press
// must be the same 4dB, all the way to the top.
func TestEveryButtonPressIsFourDb(t *testing.T) {
	for level := 47; level+8 <= 127; level += 8 {
		a, _ := StockVolumeDb(level, biscuit)
		b, _ := StockVolumeDb(level+8, biscuit)
		if math.Abs(b-a-4) > 1e-9 {
			t.Errorf("press %d -> %d: %v dB, want 4", level, level+8, b-a)
		}
	}
}

func TestStockVolumeNeverFallsAsLevelRises(t *testing.T) {
	prev := math.Inf(-1)
	for level := 1; level <= 127; level++ {
		db, _ := StockVolumeDb(level, biscuit)
		if db < prev {
			t.Fatalf("level %d: %v dB after %v", level, db, prev)
		}
		prev = db
	}
}

// The response boost never cuts: under the stock law the volume exceeds 1 from
// level 107, and 1/volume there would turn the voice DOWN under the music.
func TestResponseCapNeverCuts(t *testing.T) {
	if got := cappedResponseGain(4, 4.8); got != 1 {
		t.Errorf("volume 4.8, requested 4: got %v, want 1", got)
	}
	if got := cappedResponseGain(4, 0.5); got != 2 {
		t.Errorf("volume 0.5, requested 4: got %v, want 2", got)
	}
}

// The volume tone follows the law the music is under, so a press previews
// the level the music will play at.
func TestCueFollowsTheLawInForce(t *testing.T) {
	for _, level := range []int{1, 47, 89, 127} {
		if got, want := CueVolumeGain(level, board.BiscuitVoicing()), math.Min(StockVolumeGain(level, biscuit), 1); got != want {
			t.Errorf("stock, level %d: %v, want %v", level, got, want)
		}
		if got, want := CueVolumeGain(level, nil), VolumeGain(level); got != want {
			t.Errorf("plain, level %d: %v, want %v", level, got, want)
		}
	}
}

// A board's CueTrimDb lowers the tone at every level, the top included.
func TestCueTrim(t *testing.T) {
	v := *board.BiscuitVoicing()
	v.CueTrimDb = -6
	trim := math.Pow(10, -6.0/20)
	for _, level := range []int{57, 89, 127} {
		want := math.Min(StockVolumeGain(level, &v), 1) * trim
		if got := CueVolumeGain(level, &v); math.Abs(got-want) > 1e-12 {
			t.Errorf("level %d: %v, want %v", level, got, want)
		}
	}
}

// DbPerLevel steepens the law below the ceiling and leaves full volume there.
func TestLawSlope(t *testing.T) {
	v := *biscuit
	v.DbPerLevel = 40.0 / 70
	for level, want := range map[int]float64{57: v.CeilingDb - 40, 127: v.CeilingDb} {
		if got, _ := StockVolumeDb(level, &v); math.Abs(got-want) > 1e-9 {
			t.Errorf("level %d: %v dB, want %v", level, got, want)
		}
	}
}
