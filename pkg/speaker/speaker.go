package speaker

type Speaker interface {
	Init() error
	Pump(data []byte) error
	Close()
}
