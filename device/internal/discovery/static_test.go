package discovery

import (
	"os"
	"path/filepath"
	"testing"
)

func TestConfiguredServerParsing(t *testing.T) {
	tests := []struct {
		name    string
		json    string
		want    *ServerInfo
		wantErr bool
	}{
		{
			name: "ipv4 with TLS",
			json: `{"host":"10.20.40.110","port":8767,"tls_port":8770}`,
			want: &ServerInfo{Host: "10.20.40.110", Port: 8767, Addr: "10.20.40.110:8767", TLSPort: 8770},
		},
		{
			name: "ipv6",
			json: `{"host":"fd00::110","port":8767,"tls_port":0}`,
			want: &ServerInfo{Host: "fd00::110", Port: 8767, Addr: "[fd00::110]:8767", TLSPort: 0},
		},
		{name: "missing host", json: `{"port":8767}`, wantErr: true},
		{name: "bad port", json: `{"host":"controller","port":0}`, wantErr: true},
		{name: "bad TLS port", json: `{"host":"controller","port":8767,"tls_port":70000}`, wantErr: true},
		{name: "invalid JSON", json: `{`, wantErr: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "controller.json")
			if err := os.WriteFile(path, []byte(tt.json), 0600); err != nil {
				t.Fatal(err)
			}
			got, err := configuredServerFromPath(path)
			if (err != nil) != tt.wantErr {
				t.Fatalf("configuredServerFromPath() error = %v, wantErr %v", err, tt.wantErr)
			}
			if tt.wantErr {
				return
			}
			if *got != *tt.want {
				t.Fatalf("configuredServerFromPath() = %#v, want %#v", got, tt.want)
			}
		})
	}
}
