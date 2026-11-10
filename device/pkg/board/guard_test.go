package board

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// Where a part is belongs in a board's Hardware and nowhere else (#541): an
// event number, an i2c bus address or a PCM device number written into a
// binding is right on one board and silently wrong on the next. This reads
// string literals and call arguments from the syntax tree, so comments that
// mention such a path are not matched.
var numberedPath = regexp.MustCompile(`/dev/input/event\d|\bi2c-\d+/|\b\d+-00[0-9a-f]{2}\b|/pcmC?\d+D?\d*[pc]\b`)

func TestNoBindingOpensHardwareByNumber(t *testing.T) {
	root := filepath.Join("..", "..")
	fset := token.NewFileSet()
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			switch d.Name() {
			case "build", "tools", "testdata", "compiler":
				return filepath.SkipDir // tools are standalone diagnostics
			}
			return nil
		}
		if !strings.HasSuffix(p, ".go") || strings.HasSuffix(p, "_test.go") {
			return nil
		}
		if filepath.Base(filepath.Dir(p)) == "board" {
			return nil // the profiles themselves
		}
		f, err := parser.ParseFile(fset, p, nil, parser.SkipObjectResolution)
		if err != nil {
			return err
		}
		ast.Inspect(f, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.BasicLit:
				if n.Kind == token.STRING && numberedPath.MatchString(n.Value) {
					t.Errorf("%s: %s names hardware by number; put it in pkg/board",
						fset.Position(n.Pos()), n.Value)
				}
			case *ast.CallExpr:
				sel, ok := n.Fun.(*ast.SelectorExpr)
				if !ok || sel.Sel.Name != "NewDevice" || len(n.Args) < 2 {
					return true
				}
				for _, a := range n.Args[:2] {
					if lit, ok := a.(*ast.BasicLit); ok && lit.Kind == token.INT {
						t.Errorf("%s: PCM opened by number; resolve it through pkg/board",
							fset.Position(n.Pos()))
					}
				}
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

// What differs between boards is stated in a profile and read by the
// bindings; nothing outside this package asks WHICH board it is running on.
// A branch on identity puts one board's behaviour where the next board's
// author cannot see it, and is how a port becomes a pile of special cases.
// Reporting the id (IDOf in a log or the register message) is fine.
func TestNothingBranchesOnBoardIdentity(t *testing.T) {
	boards := map[string]bool{}
	for _, b := range Known {
		boards[b.ID] = true
	}
	root := filepath.Join("..", "..")
	fset := token.NewFileSet()
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			switch d.Name() {
			case "build", "tools", "testdata", "compiler", "board":
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(p, ".go") || strings.HasSuffix(p, "_test.go") {
			return nil
		}
		f, err := parser.ParseFile(fset, p, nil, parser.SkipObjectResolution)
		if err != nil {
			return err
		}
		ast.Inspect(f, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.SelectorExpr:
				if x, ok := n.X.(*ast.Ident); ok && x.Name == "board" {
					for _, b := range Known {
						if strings.EqualFold(n.Sel.Name, b.ID) {
							t.Errorf("%s: board.%s; state the difference in the profile", fset.Position(n.Pos()), n.Sel.Name)
						}
					}
				}
			case *ast.BinaryExpr:
				if n.Op != token.EQL && n.Op != token.NEQ {
					return true
				}
				for _, side := range []ast.Expr{n.X, n.Y} {
					if lit, ok := side.(*ast.BasicLit); ok && lit.Kind == token.STRING && boards[strings.Trim(lit.Value, "`\"")] {
						t.Errorf("%s: compares against board id %s; state the difference in the profile", fset.Position(n.Pos()), lit.Value)
					}
				}
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}
