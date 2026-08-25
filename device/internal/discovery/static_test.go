package discovery

import (
	"os"
	"path/filepath"
	"testing"
)

func TestConfiguredEndpointsParsing(t *testing.T) {
	tests := []struct {
		name    string
		json    string
		want    []*ServerInfo
		wantErr bool
	}{
		{
			name: "single endpoint with TLS",
			json: `{"endpoints":[{"host":"10.20.40.110","port":8767,"tls_port":8770}]}`,
			want: []*ServerInfo{
				{Host: "10.20.40.110", Port: 8767, Addr: "10.20.40.110:8767", TLSPort: 8770},
			},
		},
		{
			name: "static, backup and DNS name in one ordered list",
			json: `{"endpoints":[
				{"host":"10.20.40.110","port":8767,"tls_port":8770},
				{"host":"10.20.40.111","port":8767,"tls_port":8770},
				{"host":"controller.example.internal","port":8767,"tls_port":8770}
			]}`,
			want: []*ServerInfo{
				{Host: "10.20.40.110", Port: 8767, Addr: "10.20.40.110:8767", TLSPort: 8770},
				{Host: "10.20.40.111", Port: 8767, Addr: "10.20.40.111:8767", TLSPort: 8770},
				{Host: "controller.example.internal", Port: 8767, Addr: "controller.example.internal:8767", TLSPort: 8770},
			},
		},
		{
			name: "ipv6, no TLS",
			json: `{"endpoints":[{"host":"fd00::110","port":8767,"tls_port":0}]}`,
			want: []*ServerInfo{
				{Host: "fd00::110", Port: 8767, Addr: "[fd00::110]:8767", TLSPort: 0},
			},
		},
		{name: "missing endpoints key", json: `{}`, wantErr: true},
		{name: "empty endpoints array", json: `{"endpoints":[]}`, wantErr: true},
		{name: "missing host", json: `{"endpoints":[{"port":8767}]}`, wantErr: true},
		{name: "bad port", json: `{"endpoints":[{"host":"controller","port":0}]}`, wantErr: true},
		{name: "bad TLS port", json: `{"endpoints":[{"host":"controller","port":8767,"tls_port":70000}]}`, wantErr: true},
		{
			name:    "second endpoint invalid still fails the whole file",
			json:    `{"endpoints":[{"host":"controller","port":8767},{"host":"","port":8767}]}`,
			wantErr: true,
		},
		{name: "invalid JSON", json: `{`, wantErr: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "controller.json")
			if err := os.WriteFile(path, []byte(tt.json), 0600); err != nil {
				t.Fatal(err)
			}
			got, err := configuredEndpointsFromPath(path)
			if (err != nil) != tt.wantErr {
				t.Fatalf("configuredEndpointsFromPath() error = %v, wantErr %v", err, tt.wantErr)
			}
			if tt.wantErr {
				return
			}
			if len(got) != len(tt.want) {
				t.Fatalf("configuredEndpointsFromPath() = %#v, want %#v", got, tt.want)
			}
			for i := range got {
				if *got[i] != *tt.want[i] {
					t.Fatalf("endpoint[%d] = %#v, want %#v", i, got[i], tt.want[i])
				}
			}
		})
	}
}

func TestConfiguredEndpointsAbsentFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "does-not-exist.json")
	got, err := configuredEndpointsFromPath(path)
	if err != nil {
		t.Fatalf("configuredEndpointsFromPath() error = %v, want nil", err)
	}
	if got != nil {
		t.Fatalf("configuredEndpointsFromPath() = %#v, want nil", got)
	}
}
