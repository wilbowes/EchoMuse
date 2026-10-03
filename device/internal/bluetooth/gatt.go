package bluetooth

// GATT discovery over an ATT bearer (Core Spec Vol 3 Part G 4.4–4.7): primary
// services, their characteristics, and each characteristic's descriptors —
// the table Home Assistant asks a proxy for once per connection (#656).
//
// Every procedure is a loop the PEER ends, by answering Attribute Not Found
// or by reaching the end of the range. So each loop also insists that handles
// only ever go up: a peer that repeats itself would otherwise hold a
// connection slot forever.

import (
	"encoding/binary"
	"errors"
)

// Requester carries one ATT transaction: a request out, its response back.
type Requester interface {
	Request(req []byte) ([]byte, error)
}

// Descriptor is one characteristic descriptor.
type Descriptor struct {
	Handle uint16
	UUID   UUID
}

// Characteristic is one characteristic and the descriptors after its value.
type Characteristic struct {
	Handle      uint16 // the declaration
	ValueHandle uint16
	Properties  byte
	UUID        UUID
	Descriptors []Descriptor
}

// Service is one primary service.
type Service struct {
	Start, End      uint16
	UUID            UUID
	Characteristics []Characteristic
}

var errGATTOrder = errors.New("gatt: peer's handles are out of order")

// DiscoverServices walks the peer's whole attribute table.
func DiscoverServices(r Requester) ([]Service, error) {
	services, err := discoverPrimary(r)
	if err != nil {
		return nil, err
	}
	for i := range services {
		s := &services[i]
		if s.Characteristics, err = discoverCharacteristics(r, s.Start, s.End); err != nil {
			return nil, err
		}
		for j := range s.Characteristics {
			c := &s.Characteristics[j]
			// Descriptors sit between this value and the next declaration,
			// or the end of the service.
			end := s.End
			if j+1 < len(s.Characteristics) {
				end = s.Characteristics[j+1].Handle - 1
			}
			if c.ValueHandle >= end {
				continue
			}
			if c.Descriptors, err = discoverDescriptors(r, c.ValueHandle+1, end); err != nil {
				return nil, err
			}
		}
	}
	return services, nil
}

func discoverPrimary(r Requester) ([]Service, error) {
	var out []Service
	start := uint16(1)
	for {
		rsp, err := r.Request(encodeReadByGroupReq(start, 0xFFFF, UUID16(uuidPrimaryService)))
		if err != nil {
			return nil, err
		}
		groups, err := parseReadByGroupRsp(rsp)
		if isNotFound(err) {
			return out, nil
		}
		if err != nil {
			return nil, err
		}
		for _, g := range groups {
			if len(g.Value) != 2 && len(g.Value) != 16 {
				return nil, errATTMalformed
			}
			if g.Start < start || g.End < g.Start {
				return nil, errGATTOrder
			}
			out = append(out, Service{Start: g.Start, End: g.End, UUID: UUID(g.Value)})
			if g.End == 0xFFFF {
				return out, nil
			}
			start = g.End + 1
		}
	}
}

func discoverCharacteristics(r Requester, start, end uint16) ([]Characteristic, error) {
	var out []Characteristic
	for start <= end {
		rsp, err := r.Request(encodeReadByTypeReq(start, end, UUID16(uuidCharacteristic)))
		if err != nil {
			return nil, err
		}
		entries, err := parseReadByTypeRsp(rsp)
		if isNotFound(err) {
			return out, nil
		}
		if err != nil {
			return nil, err
		}
		for _, e := range entries {
			// Declaration value: properties(1), value handle(2), UUID(2|16).
			if len(e.Value) != 5 && len(e.Value) != 19 {
				return nil, errATTMalformed
			}
			vh := binary.LittleEndian.Uint16(e.Value[1:3])
			if e.Handle < start || e.Handle > end || vh <= e.Handle || vh > end {
				return nil, errGATTOrder
			}
			out = append(out, Characteristic{
				Handle:      e.Handle,
				ValueHandle: vh,
				Properties:  e.Value[0],
				UUID:        UUID(e.Value[3:]),
			})
			if e.Handle == 0xFFFF {
				return out, nil
			}
			start = e.Handle + 1
		}
	}
	return out, nil
}

func discoverDescriptors(r Requester, start, end uint16) ([]Descriptor, error) {
	var out []Descriptor
	for start <= end {
		rsp, err := r.Request(encodeFindInfoReq(start, end))
		if err != nil {
			return nil, err
		}
		infos, err := parseFindInfoRsp(rsp)
		if isNotFound(err) {
			return out, nil
		}
		if err != nil {
			return nil, err
		}
		for _, in := range infos {
			if in.Handle < start || in.Handle > end {
				return nil, errGATTOrder
			}
			out = append(out, Descriptor{Handle: in.Handle, UUID: in.UUID})
			if in.Handle == 0xFFFF {
				return out, nil
			}
			start = in.Handle + 1
		}
	}
	return out, nil
}
