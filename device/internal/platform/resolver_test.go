package platform

import (
	"net"
	"testing"
)

func TestTheResolverFollowsTheBase(t *testing.T) {
	for base, want := range map[string]bool{EmOS: true, FireOS: false, Unknown: true} {
		r := &net.Resolver{}
		UseResolverFor(r, base)
		if r.PreferGo != want {
			t.Errorf("%s: PreferGo %v, want %v", base, r.PreferGo, want)
		}
	}
}
