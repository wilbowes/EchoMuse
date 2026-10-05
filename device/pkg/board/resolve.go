package board

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Finding hardware by name. Each takes a root ("" on a device, a fixture
// directory in tests) and refuses an ambiguous answer: two parts with one
// name means the name does not identify the part, and picking the first is
// the enumeration-order guess these exist to remove.

// InputEvent returns /dev/input/eventN for the input device named name in
// /proc/bus/input/devices.
func InputEvent(root, name string) (string, error) {
	if name == "" {
		return "", errNoName
	}
	f, err := os.Open(filepath.Join(root, "/proc/bus/input/devices"))
	if err != nil {
		return "", err
	}
	defer f.Close()

	var found []string
	match := false
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		switch {
		case strings.HasPrefix(line, "N: Name="):
			// The kernel prints the name between quotes with no escaping.
			match = line == `N: Name="`+name+`"`
		case strings.HasPrefix(line, "H: Handlers=") && match:
			for _, h := range strings.Fields(strings.TrimPrefix(line, "H: Handlers=")) {
				if n := strings.TrimPrefix(h, "event"); n != h && isNumber(n) {
					found = append(found, "/dev/input/"+h)
				}
			}
		case line == "":
			match = false
		}
	}
	if err := sc.Err(); err != nil {
		return "", err
	}
	switch len(found) {
	case 1:
		return found[0], nil
	case 0:
		return "", fmt.Errorf("no input device named %q", name)
	}
	return "", fmt.Errorf("%d input devices named %q", len(found), name)
}

// PCMDevice returns the card and device of the PCM whose stream name is name,
// from /proc/asound/pcm, and which has the wanted direction. A line there is
// "CC-DD: <stream name> <dai name> : <name> : playback N : capture N".
func PCMDevice(root, name string, capture bool) (*PCMAddr, error) {
	if name == "" {
		return nil, errNoName
	}
	f, err := os.Open(filepath.Join(root, "/proc/asound/pcm"))
	if err != nil {
		return nil, err
	}
	defer f.Close()

	dir := "playback"
	if capture {
		dir = "capture"
	}
	var found []PCMAddr
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Split(sc.Text(), " : ")
		addr, id, ok := strings.Cut(fields[0], ": ")
		if !ok || (id != name && !strings.HasPrefix(id, name+" ")) {
			continue
		}
		has := false
		for _, f := range fields[1:] {
			if strings.HasPrefix(strings.TrimSpace(f), dir+" ") {
				has = true
			}
		}
		if !has {
			continue
		}
		c, d, ok := strings.Cut(addr, "-")
		card, err1 := strconv.Atoi(c)
		dev, err2 := strconv.Atoi(d)
		if !ok || err1 != nil || err2 != nil {
			continue
		}
		found = append(found, PCMAddr{card, dev})
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	switch len(found) {
	case 1:
		return &found[0], nil
	case 0:
		return nil, fmt.Errorf("no %s PCM named %q", dir, name)
	}
	return nil, fmt.Errorf("%d %s PCMs named %q", len(found), dir, name)
}

// I2CDevice returns the sysfs directory of the i2c client whose `name` is
// driver.
func I2CDevice(root, driver string) (string, error) {
	if driver == "" {
		return "", errNoName
	}
	names, err := filepath.Glob(filepath.Join(root, "/sys/bus/i2c/devices/*/name"))
	if err != nil {
		return "", err
	}
	var found []string
	for _, n := range names {
		b, err := os.ReadFile(n)
		if err == nil && strings.TrimSpace(string(b)) == driver {
			found = append(found, filepath.Dir(n))
		}
	}
	switch len(found) {
	case 1:
		return found[0], nil
	case 0:
		return "", fmt.Errorf("no i2c device named %q", driver)
	}
	return "", fmt.Errorf("%d i2c devices named %q", len(found), driver)
}

var errNoName = errors.New("the board states no name for it")

func isNumber(s string) bool {
	_, err := strconv.Atoi(s)
	return err == nil && s != ""
}
