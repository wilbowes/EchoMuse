package speaker

type Speaker interface {
	Init() error
	Pump(data []byte) error
	PumpPeriod(data []byte) error
	Close()
}
