//go:build server && !bench

package speaker

// writeLoopMeter is the bench write-loop telemetry (writeloop_bench.go); in a
// release build it does nothing and compiles away.
type writeLoopMeter struct{}

func (*writeLoopMeter) beforeWrite() {}
func (*writeLoopMeter) afterWrite()  {}
