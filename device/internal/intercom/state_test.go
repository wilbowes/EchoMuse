package intercom

import (
	"errors"
	"testing"
)

func TestOfferFromIdleEntersCall(t *testing.T) {
	m := NewMachine()
	sess := Session{ID: "s1", PeerID: "kitchen", PeerLabel: "Kitchen", Token: "tok"}
	if err := m.Offer(sess); err != nil {
		t.Fatalf("Offer: %v", err)
	}
	if got := m.State(); got != StateInCall {
		t.Fatalf("state = %q, want in_call", got)
	}
	cur, ok := m.Current()
	if !ok || cur != sess {
		t.Fatalf("Current = %+v, %v; want %+v, true", cur, ok, sess)
	}
}

func TestOfferSameSessionIsIdempotent(t *testing.T) {
	m := NewMachine()
	sess := Session{ID: "s1", PeerID: "kitchen"}
	if err := m.Offer(sess); err != nil {
		t.Fatalf("Offer: %v", err)
	}
	// A retried offer — same session, different token — must not fail.
	if err := m.Offer(Session{ID: "s1", PeerID: "kitchen", Token: "tok2"}); err != nil {
		t.Fatalf("re-Offer same session: %v", err)
	}
	if got := m.State(); got != StateInCall {
		t.Fatalf("state = %q, want in_call", got)
	}
}

func TestOfferDifferentSessionWhileInCall(t *testing.T) {
	m := NewMachine()
	if err := m.Offer(Session{ID: "s1"}); err != nil {
		t.Fatalf("Offer: %v", err)
	}
	if err := m.Offer(Session{ID: "s2"}); !errors.Is(err, ErrNotIdle) {
		t.Fatalf("Offer second session: err = %v, want ErrNotIdle", err)
	}
}

func TestEndIsIdempotent(t *testing.T) {
	m := NewMachine()
	sess := Session{ID: "s1", PeerID: "kitchen"}
	if err := m.Offer(sess); err != nil {
		t.Fatalf("Offer: %v", err)
	}
	ended, err := m.End()
	if err != nil || ended != sess {
		t.Fatalf("End = %+v, %v; want %+v, nil", ended, err, sess)
	}
	if got := m.State(); got != StateIdle {
		t.Fatalf("state = %q, want idle", got)
	}
	// A duplicated end — the controller fans the end out to both ends,
	// and a late one must be a no-op, not an error.
	ended, err = m.End()
	if err != nil || ended != (Session{}) {
		t.Fatalf("second End = %+v, %v; want zero session, nil", ended, err)
	}
	if _, ok := m.Current(); ok {
		t.Fatal("Current after End: want no session")
	}
}

func TestEndWhenIdleIsNoop(t *testing.T) {
	m := NewMachine()
	if _, err := m.End(); err != nil {
		t.Fatalf("End when idle: %v", err)
	}
}

func TestSuspendResumeRoundTrip(t *testing.T) {
	m := NewMachine()
	if err := m.Suspend(); !errors.Is(err, ErrNoSession) {
		t.Fatalf("Suspend when idle: err = %v, want ErrNoSession", err)
	}
	if err := m.Offer(Session{ID: "s1"}); err != nil {
		t.Fatalf("Offer: %v", err)
	}
	if err := m.Suspend(); err != nil {
		t.Fatalf("Suspend: %v", err)
	}
	if got := m.State(); got != StateSuspended {
		t.Fatalf("state = %q, want suspended", got)
	}
	if err := m.Suspend(); !errors.Is(err, ErrNotInCall) {
		t.Fatalf("second Suspend: err = %v, want ErrNotInCall", err)
	}
	if err := m.Resume(); err != nil {
		t.Fatalf("Resume: %v", err)
	}
	if got := m.State(); got != StateInCall {
		t.Fatalf("state = %q, want in_call", got)
	}
	if err := m.Resume(); !errors.Is(err, ErrNotSuspended) {
		t.Fatalf("Resume when in call: err = %v, want ErrNotSuspended", err)
	}
	// The session survives suspend/resume, and End still works from
	// the suspended state.
	if err := m.Suspend(); err != nil {
		t.Fatalf("Suspend: %v", err)
	}
	if _, err := m.End(); err != nil {
		t.Fatalf("End from suspended: %v", err)
	}
	if got := m.State(); got != StateIdle {
		t.Fatalf("state = %q, want idle", got)
	}
}
