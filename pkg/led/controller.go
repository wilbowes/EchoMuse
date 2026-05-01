package led

type Controller interface {
	Init() error
	GetNumLEDs() (int, error)
	SetLEDs(led ...Led) error
}
