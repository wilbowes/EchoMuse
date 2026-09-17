package mixer

import (
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// Mixer control ids are positional and shift between kernels (#546), so the
// firmware reaches controls by name through this package and never spawns
// tinymix. Matches the call shape only, so prose mentioning tinymix is fine.
var tinymixSpawn = regexp.MustCompile(`exec\.Command\(\s*"tinymix"`)

func TestNothingSpawnsTinymix(t *testing.T) {
	root := filepath.Join("..", "..", "..")
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() && (d.Name() == "build" || d.Name() == "tools") {
			return filepath.SkipDir // tools are standalone diagnostics
		}
		if d.IsDir() || !strings.HasSuffix(p, ".go") || strings.HasSuffix(p, "_test.go") {
			return nil
		}
		b, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		if tinymixSpawn.Match(b) {
			t.Errorf("%s spawns tinymix; use internal/bindings/mixer", p)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
