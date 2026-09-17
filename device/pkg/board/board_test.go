package board

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func mkroot(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for p, content := range files {
		full := filepath.Join(root, p)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestDetectMatchesDeviceTypeExactly(t *testing.T) {
	cases := map[string]*Board{
		"A3S5BH2HU6VAYF\x00": Biscuit, // as the kernel returns it
		"A3S5BH2HU6VAYF\n":   Biscuit,
		"\x00A3S5BH2HU6VAYF": nil,
		"A3S5BH2HU6VAYF":     Biscuit,
		"A3S5BH2HU6VAYFX":    nil,
		"a3s5bh2hu6vayf":     nil,
		"":                   nil,
		"\n":                 nil,
	}
	for id, want := range cases {
		root := mkroot(t, map[string]string{"/proc/idme/device_type_id": id})
		if got := Detect(root); got != want {
			t.Errorf("Detect(%q) = %v, want %v", id, got, want)
		}
	}
}

func TestNoIdmeIsNoBoard(t *testing.T) {
	// A non-Amazon board has no idme at all, and must touch nothing.
	if b := Detect(t.TempDir()); b != nil {
		t.Fatalf("got %v", b.ID)
	}
	if IDOf(nil) != "unknown" {
		t.Fatal("nil board must report unknown")
	}
}

// thermalRoot builds a sysfs tree with the given zone and cooler types.
func thermalRoot(t *testing.T, zones, coolers []string, extra map[string]string) string {
	files := map[string]string{}
	for i, z := range zones {
		d := fmt.Sprintf("%s/thermal_zone%d", thermalDir, i)
		files[d+"/type"] = z + "\n"
		for j := 0; j < 12; j++ {
			files[fmt.Sprintf("%s/trip_point_%d_temp", d, j)] = "50000\n"
		}
	}
	for i, c := range coolers {
		d := fmt.Sprintf("%s/cooling_device%d", thermalDir, i)
		files[d+"/type"] = c + "\n"
		files[d+"/levels"] = "1 1\n"
	}
	for k, v := range extra {
		files[k] = v
	}
	return mkroot(t, files)
}

func TestMissingCoolerWritesNothing(t *testing.T) {
	coolers := []string{"mtktscpu-sysrst", "cpu02", "cpu_adaptive_0", "cpu_adaptive_1", "thermal_budget"} // no cpu_adaptive_2
	root := thermalRoot(t, []string{"mtktscpu", "tmp103"}, coolers, map[string]string{
		"/proc/hps/up_threshold":             "50\n",
		"/proc/hps/down_threshold":           "30\n",
		"/proc/driver/thermal/clatm_setting": "untouched\n",
		tzcpuPath:                            "untouched\n",
	})
	if _, err := Apply(root, biscuitTuning); err == nil {
		t.Fatal("expected refusal")
	}
	for p, want := range map[string]string{
		"/proc/hps/up_threshold": "50",
		tzcpuPath:                "untouched",
		thermalDir + "/thermal_zone1/trip_point_0_temp": "50000",
	} {
		if got := read(filepath.Join(root, p)); got != want {
			t.Errorf("%s = %q after a refusal, want %q", p, got, want)
		}
	}
}

func TestDuplicateTypeIsAmbiguousNotGuessed(t *testing.T) {
	root := thermalRoot(t, []string{"tmp103", "tmp103"}, nil, nil)
	z, err := byType(root, "thermal_zone")
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := z["tmp103"]; ok {
		t.Fatal("a type listed twice must not resolve")
	}
}

func TestValuesAndTripsAreWrittenAndVerified(t *testing.T) {
	tu := &Tuning{
		Values:    []Value{{"/proc/hps/up_threshold", "80"}},
		ZoneTrips: []ZoneTrips{{Zone: "tmp103", Trips: map[int]int{0: 56500, 5: 59000}}},
	}
	root := thermalRoot(t, []string{"mtktscpu", "tmp103"}, nil,
		map[string]string{"/proc/hps/up_threshold": "50\n"})
	if _, err := Apply(root, tu); err != nil {
		t.Fatal(err)
	}
	for p, want := range map[string]string{
		"/proc/hps/up_threshold":                        "80",
		thermalDir + "/thermal_zone1/trip_point_0_temp": "56500",
		thermalDir + "/thermal_zone1/trip_point_5_temp": "59000",
		thermalDir + "/thermal_zone0/trip_point_0_temp": "50000", // other zone untouched
	} {
		if got := read(filepath.Join(root, p)); got != want {
			t.Errorf("%s = %q, want %q", p, got, want)
		}
	}
}

func TestReadBackMismatchIsReported(t *testing.T) {
	// A plain file cannot reproduce the kernel's tzcpu read-back format, so
	// the verification must say the write did not take.
	tu := &Tuning{Proc: []ProcWrite{{Path: tzcpuPath, Lines: []string{"x"}, Expect: []string{"interval=250"}}}}
	root := thermalRoot(t, nil, nil, map[string]string{tzcpuPath: ""})
	if _, err := Apply(root, tu); err == nil {
		t.Fatal("expected a verification failure")
	}
}

// The tzcpu line decides when the SoC resets. A slip that put a low trip on
// the reset cooler would reboot the device the moment it was written.
func TestBiscuitCPUProfileIsSafe(t *testing.T) {
	var line string
	for _, p := range biscuitTuning.Proc {
		if p.Path == tzcpuPath {
			line = p.Lines[0]
		}
	}
	f := strings.Fields(line)
	if len(f) != 1+10*3+2 {
		t.Fatalf("tzcpu has %d fields, want 33", len(f))
	}
	n, _ := strconv.Atoi(f[0])
	if n != 5 {
		t.Fatalf("active trips = %d", n)
	}
	var highest int
	for i := 0; i < 10; i++ {
		temp, err := strconv.Atoi(f[1+3*i])
		if err != nil {
			t.Fatal(err)
		}
		cooler := f[3+3*i]
		if i >= n {
			if cooler != "no-cooler" {
				t.Errorf("inactive trip %d bound to %s", i, cooler)
			}
			continue
		}
		if temp < 84000 {
			t.Errorf("active trip %d at %d, below stock's lowest", i, temp)
		}
		if temp > highest {
			highest = temp
		}
		if !contains(biscuitTuning.Coolers, cooler) {
			t.Errorf("cooler %s is not in the preflight list", cooler)
		}
		if strings.HasSuffix(cooler, "sysrst") && temp != 117000 {
			t.Errorf("reset trip at %d", temp)
		}
	}
	if f[1] != "117000" || f[3] != "mtktscpu-sysrst" || highest != 117000 {
		t.Error("the reset must be the first and highest trip")
	}
}

func TestBiscuitBoardSensorMatchesStock(t *testing.T) {
	z := biscuitTuning.ZoneTrips[0]
	if z.Zone != "tmp103" {
		t.Fatal(z.Zone)
	}
	prev := 0
	for i := 0; i < 6; i++ {
		v := z.Trips[i]
		if v < 56500 || v > 60000 || v <= prev {
			t.Errorf("trip %d = %d", i, v)
		}
		prev = v
	}
}

func contains(s []string, v string) bool {
	for _, x := range s {
		if x == v {
			return true
		}
	}
	return false
}
