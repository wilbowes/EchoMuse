// Package bluetooth implements a passive BLE scanner over MediaTek's raw
// HCI transport (/dev/stpbt) for the ESPHome bluetooth_proxy feature.
//
// The MT8163's combo chip is exposed by the WMT driver stack as a char
// device speaking H4-style framing (packet-type byte + HCI packet); opening
// it triggers BT function-on and firmware patch download. The device is
// effectively single-owner, so the Android Bluetooth stack (Bluedroid) must
// be disabled before use — see ensureBluedroidDisabled. Hardware-validated
// 2026-07-12 on Office: HCI Reset → LE Set Scan Parameters → LE Set Scan
// Enable produced a stream of LE Advertising Reports from raw land with the
// chip's default event masks (no Set Event Mask needed — the vendor patches
// leave LE Meta events enabled).
package bluetooth

import (
	"encoding/binary"
	"fmt"
	"net"
)

const (
	h4TypeCommand = 0x01
	h4TypeACL     = 0x02
	h4TypeSCO     = 0x03
	h4TypeEvent   = 0x04

	evtCommandComplete = 0x0E
	evtCommandStatus   = 0x0F
	evtLEMeta          = 0x3E

	leSubeventAdvReport = 0x02

	// Opcodes (OGF<<10 | OCF)
	opReset            = 0x03<<10 | 0x0003
	opReadBdAddr       = 0x04<<10 | 0x0009
	opLESetScanParams  = 0x08<<10 | 0x000B
	opLESetScanEnable  = 0x08<<10 | 0x000C
)

// Advert is one received LE advertisement (or scan response), shaped for
// direct JSON marshalling onto the control WebSocket (Data → base64).
type Advert struct {
	Addr     string `json:"addr"`     // AA:BB:CC:DD:EE:FF (wire bytes reversed)
	AddrType int    `json:"addrType"` // 0 public, 1 random (HCI Address_Type)
	Rssi     int    `json:"rssi"`     // dBm, negative
	Data     []byte `json:"data"`     // raw AD payload
}

// buildCommand assembles an H4 command packet: type, opcode LE16, plen, params.
func buildCommand(opcode uint16, params []byte) []byte {
	pkt := make([]byte, 4+len(params))
	pkt[0] = h4TypeCommand
	binary.LittleEndian.PutUint16(pkt[1:3], opcode)
	pkt[3] = byte(len(params))
	copy(pkt[4:], params)
	return pkt
}

// h4Parser reassembles complete HCI packets from an arbitrary byte stream.
// stpbt reads usually return whole packets, but nothing in the driver
// contract guarantees it, so this tolerates both fragmentation and multiple
// packets per read. Only event and ACL packets are expected inbound.
type h4Parser struct {
	buf []byte
}

// Feed appends b and returns any complete packets (including the leading
// H4 type byte). Unknown packet types desynchronise the stream — there is
// no resync marker in H4 — so the parser drops the whole buffer and starts
// clean rather than emitting garbage forever.
func (p *h4Parser) Feed(b []byte) [][]byte {
	p.buf = append(p.buf, b...)
	var pkts [][]byte
	for {
		if len(p.buf) == 0 {
			return pkts
		}
		var total int
		switch p.buf[0] {
		case h4TypeEvent:
			if len(p.buf) < 3 {
				return pkts
			}
			total = 3 + int(p.buf[2])
		case h4TypeACL:
			if len(p.buf) < 5 {
				return pkts
			}
			total = 5 + int(binary.LittleEndian.Uint16(p.buf[3:5]))
		case h4TypeSCO:
			if len(p.buf) < 4 {
				return pkts
			}
			total = 4 + int(p.buf[3])
		default:
			p.buf = nil
			return pkts
		}
		if len(p.buf) < total {
			return pkts
		}
		pkt := make([]byte, total)
		copy(pkt, p.buf[:total])
		pkts = append(pkts, pkt)
		p.buf = p.buf[total:]
	}
}

// commandComplete summarises an HCI Command Complete (or Command Status)
// event for request/response matching during init.
type commandComplete struct {
	opcode uint16
	status byte
	params []byte // return parameters after the status byte
}

// parseCommandComplete extracts opcode/status from a Command Complete or
// Command Status event packet (H4-framed). Returns ok=false for other events.
func parseCommandComplete(pkt []byte) (commandComplete, bool) {
	if len(pkt) < 3 || pkt[0] != h4TypeEvent {
		return commandComplete{}, false
	}
	code, params := pkt[1], pkt[3:]
	switch code {
	case evtCommandComplete:
		// num_cmd_pkts(1), opcode(2), status(1), return params...
		if len(params) < 4 {
			return commandComplete{}, false
		}
		return commandComplete{
			opcode: binary.LittleEndian.Uint16(params[1:3]),
			status: params[3],
			params: params[4:],
		}, true
	case evtCommandStatus:
		// status(1), num_cmd_pkts(1), opcode(2)
		if len(params) < 4 {
			return commandComplete{}, false
		}
		return commandComplete{
			opcode: binary.LittleEndian.Uint16(params[2:4]),
			status: params[0],
		}, true
	}
	return commandComplete{}, false
}

// parseAdvReports extracts advertisements from an LE Advertising Report
// event packet (H4-framed). Non-report events return nil. Each report is
// laid out sequentially: event_type(1), addr_type(1), addr(6 LE),
// data_len(1), data, rssi(1 signed) — confirmed against live captures.
func parseAdvReports(pkt []byte) []Advert {
	if len(pkt) < 4 || pkt[0] != h4TypeEvent || pkt[1] != evtLEMeta {
		return nil
	}
	params := pkt[3:]
	if len(params) < 2 || params[0] != leSubeventAdvReport {
		return nil
	}
	numReports := int(params[1])
	body := params[2:]
	adverts := make([]Advert, 0, numReports)
	for i := 0; i < numReports; i++ {
		if len(body) < 9 {
			return adverts
		}
		addrType := int(body[1])
		mac := make(net.HardwareAddr, 6)
		for j := 0; j < 6; j++ { // HCI sends the address little-endian
			mac[j] = body[7-j]
		}
		dataLen := int(body[8])
		if len(body) < 9+dataLen+1 {
			return adverts
		}
		data := make([]byte, dataLen)
		copy(data, body[9:9+dataLen])
		adverts = append(adverts, Advert{
			Addr:     mac.String(),
			AddrType: addrType,
			Rssi:     int(int8(body[9+dataLen])),
			Data:     data,
		})
		body = body[9+dataLen+1:]
	}
	return adverts
}

// scanParams builds the LE Set Scan Parameters payload for a passive scan.
// interval/window are in milliseconds (spec units of 0.625ms).
func scanParams(intervalMs, windowMs int) []byte {
	toUnits := func(ms int) uint16 {
		u := ms * 1000 / 625
		if u < 0x0004 {
			u = 0x0004
		}
		if u > 0x4000 {
			u = 0x4000
		}
		return uint16(u)
	}
	p := make([]byte, 7)
	p[0] = 0x00 // passive
	binary.LittleEndian.PutUint16(p[1:3], toUnits(intervalMs))
	binary.LittleEndian.PutUint16(p[3:5], toUnits(windowMs))
	p[5] = 0x00 // own address: public
	p[6] = 0x00 // filter policy: accept all
	return p
}

// formatBdAddr renders a Read_BD_ADDR return parameter (6 bytes LE).
func formatBdAddr(params []byte) string {
	if len(params) < 6 {
		return ""
	}
	return fmt.Sprintf("%02X:%02X:%02X:%02X:%02X:%02X",
		params[5], params[4], params[3], params[2], params[1], params[0])
}
