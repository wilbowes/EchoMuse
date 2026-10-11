package atomicfile

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestWriteReplacesAndLeavesNoTemp(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	if err := Write(path, []byte("one"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Write(path, []byte("two"), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(path)
	if err != nil || string(got) != "two" {
		t.Fatalf("read %q, %v; want two", got, err)
	}
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatalf("temp file left behind: %v", err)
	}
	fi, _ := os.Stat(path)
	if fi.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v, want 0600", fi.Mode().Perm())
	}
}

func TestWriteEmptyData(t *testing.T) {
	path := filepath.Join(t.TempDir(), "empty")
	if err := Write(path, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if fi, err := os.Stat(path); err != nil || fi.Size() != 0 {
		t.Fatalf("stat %v, %v", fi, err)
	}
}

// A failed write must leave the old file as it was.
func TestWriteFailureKeepsTheOldFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	if err := Write(path, []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	// A directory where the temp file goes makes the open fail.
	if err := os.Mkdir(path+".tmp", 0o755); err != nil {
		t.Fatal(err)
	}
	if err := Write(path, []byte("new"), 0o600); err == nil {
		t.Fatal("want an error")
	}
	if got, _ := os.ReadFile(path); string(got) != "old" {
		t.Fatalf("old file now %q", got)
	}
}

func TestWriteMissingDirectory(t *testing.T) {
	if err := Write(filepath.Join(t.TempDir(), "absent", "f"), []byte("x"), 0o600); err == nil {
		t.Fatal("want an error")
	}
}

// Every rename in the firmware goes through Write. A bare os.Rename is how a
// file was lost to a power cut, four times over (#849, #863). Read from the
// syntax tree, so a comment naming the call does not count.
func TestNoBareRenameInTheFirmware(t *testing.T) {
	root := filepath.Join("..", "..")
	fset := token.NewFileSet()
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if n := d.Name(); n == "atomicfile" || n == "GoTinyAlsa" || n == "tools" || n == "build" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(p, ".go") || strings.HasSuffix(p, "_test.go") {
			return nil
		}
		f, err := parser.ParseFile(fset, p, nil, 0)
		if err != nil {
			return err
		}
		ast.Inspect(f, func(n ast.Node) bool {
			sel, ok := n.(*ast.SelectorExpr)
			if !ok {
				return true
			}
			if x, ok := sel.X.(*ast.Ident); ok && x.Name == "os" && sel.Sel.Name == "Rename" {
				t.Errorf("%s: os.Rename; use atomicfile.Write", fset.Position(sel.Pos()))
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
