package buttons

type Controller interface {
	Init() error
	SubscribeToButton(callback ButtonClickCallback) (*EventSubscription, error)
	GetDotButton() Button
	GetVolumeButton() Button
	SetVolumeCallback(cb func(direction string))
	// SetVolumeEdgeCallback sees every press and release of a volume key
	// before the volume step. Returning true on a release consumes it, so a
	// key used in a combination does not also change the volume.
	SetVolumeEdgeCallback(cb func(direction string, down bool) (consume bool))
	SetMuteCallback(cb func())
}