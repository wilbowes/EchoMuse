package buttons

type Controller interface {
	Init() error
	SubscribeToButton(callback ButtonClickCallback) (*EventSubscription, error)
	GetDotButton() Button
	GetVolumeButton() Button
	SetVolumeCallback(cb func(direction string))
	SetMuteCallback(cb func())
}