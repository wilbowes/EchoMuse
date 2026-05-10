package buttons

type ButtonType string

type Button struct {
	internalName string
	Type         ButtonType `json:"type"`
}

type ClickType uint16

const (
	DotClick        ClickType  = 138
	VolumeUpClick   ClickType  = 115
	VolumeDownClick ClickType  = 114
	MuteClick       ClickType  = 113
	DotButton       ButtonType = "Dot"
	VolumeButton    ButtonType = "Volume"
)

func (c *ClickType) String() string {
	switch *c {
	case DotClick:
		return "dot"
	case VolumeUpClick:
		return "volume_up"
	case VolumeDownClick:
		return "volume_down"
	case MuteClick:
		return "mute"
	default:
		return "unknown"
	}
}