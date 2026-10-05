package platform

import (
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// extCSD builds the kernel's hex dump of a 512-byte EXT_CSD with the health
// bytes set, at the offsets JESD84-B51 defines.
func extCSD(rev, preEOL, lifeA, lifeB byte) string {
	b := make([]byte, extCSDLen)
	b[192], b[267], b[268], b[269] = rev, preEOL, lifeA, lifeB
	return hex.EncodeToString(b) + "\n"
}

func TestParseExtCSDReadsTheJEDECOffsets(t *testing.T) {
	e := parseExtCSD(extCSD(7, 1, 2, 11))
	if e == nil || e.Rev != 7 || e.PreEOL != 1 || e.LifeA != 2 || e.LifeB != 11 {
		t.Fatalf("got %+v", e)
	}
}

func TestParseExtCSDRefusesAnythingButTheWholeRegister(t *testing.T) {
	full := strings.TrimSpace(extCSD(7, 1, 1, 1))
	for name, dump := range map[string]string{
		"empty":     "",
		"short":     full[:len(full)-2],
		"long":      full + "00",
		"not hex":   strings.Replace(full, "0", "z", 1),
		"odd chars": full[:len(full)-1],
	} {
		if e := parseExtCSD(dump); e != nil {
			t.Errorf("%s: parsed %+v", name, e)
		}
	}
}

func write(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

// biscuit's layout, measured 2026-10-01 on both kernels, with an SD card
// enumerated FIRST so a by-number reader would pick the wrong device.
func TestReadEmmcFindsTheEmmcByType(t *testing.T) {
	root := t.TempDir()
	write(t, root+"/sys/bus/mmc/devices/mmc0:aaaa/type", "SD\n")
	write(t, root+"/sys/kernel/debug/mmc0/mmc0:aaaa/ext_csd", extCSD(8, 3, 9, 9))
	write(t, root+"/sys/bus/mmc/devices/mmc1:0001/type", "MMC\n")
	write(t, root+"/sys/bus/mmc/devices/mmc1:0001/name", "FJ25AB\n")
	write(t, root+"/sys/bus/mmc/devices/mmc1:0001/date", "08/2017\n")
	write(t, root+"/sys/bus/mmc/devices/mmc1:0001/manfid", "0x000015\n")
	write(t, root+"/sys/kernel/debug/mmc1/mmc1:0001/ext_csd", extCSD(7, 1, 2, 2))

	e := ReadEmmc(root)
	want := Emmc{Rev: 7, PreEOL: 1, LifeA: 2, LifeB: 2, Name: "FJ25AB", Date: "08/2017", ManfID: "0x000015"}
	if e == nil || *e != want {
		t.Fatalf("got %+v, want %+v", e, want)
	}
}

func TestReadEmmcWithoutDebugfsIsNil(t *testing.T) {
	root := t.TempDir()
	write(t, root+"/sys/bus/mmc/devices/mmc0:0001/type", "MMC\n")
	if e := ReadEmmc(root); e != nil {
		t.Fatalf("got %+v with no ext_csd", e)
	}
	if e := ReadEmmc(t.TempDir()); e != nil {
		t.Fatalf("got %+v with no mmc devices", e)
	}
}

func TestBootReasonFromTheCmdline(t *testing.T) {
	root := t.TempDir()
	write(t, root+"/proc/cmdline", "console=ttyS0 boot_reason=0 androidboot.bootreason=wdt_by_pass_pwk androidboot.hardware=mt8163\n")
	if got := BootReason(root); got != "wdt_by_pass_pwk" {
		t.Fatalf("got %q", got)
	}
	if got := BootReason(t.TempDir()); got != "" {
		t.Fatalf("no cmdline: got %q", got)
	}
}

// A cmdline at the 32-bit limit may be cut mid-argument, and a cut reason
// still looks like a reason — so the last argument of a full cmdline is
// dropped, while the same value earlier in the line is trusted.
func TestBootReasonDropsAValueTheTruncationMayHaveCut(t *testing.T) {
	pad := "x=" + strings.Repeat("y", cmdlineTruncatedAt)
	if got := cmdlineValue(pad+" androidboot.bootreason=power_k\n", "androidboot.bootreason"); got != "" {
		t.Errorf("last argument of a full cmdline: got %q", got)
	}
	if got := cmdlineValue("androidboot.bootreason=power_key "+pad+"\n", "androidboot.bootreason"); got != "power_key" {
		t.Errorf("earlier argument of a full cmdline: got %q", got)
	}
}
