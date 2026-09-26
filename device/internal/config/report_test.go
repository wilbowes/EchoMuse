package config

import (
	"encoding/json"
	"testing"
)

func TestRecordReceivedMergesAndHidesThePassword(t *testing.T) {
	received = map[string]json.RawMessage{}
	RecordReceived([]byte(`{"type":"config","owwThreshold":0.5,"consolePassword":"100000:ab:cd"}`))
	RecordReceived([]byte(`{"type":"config","duckDb":-12}`))
	if string(received["owwThreshold"]) != "0.5" || string(received["duckDb"]) != "-12" {
		t.Fatalf("not merged: %v", received)
	}
	if _, ok := received["type"]; ok {
		t.Fatal("type recorded")
	}
	if string(received["consolePassword"]) != `"<set>"` {
		t.Fatalf("password recorded as %s", received["consolePassword"])
	}
	RecordReceived([]byte(`{"consolePassword":""}`))
	if string(received["consolePassword"]) != `""` {
		t.Fatalf("a cleared password reads %s", received["consolePassword"])
	}
}
