package bluetooth

// The Attribute Protocol, client side (Core Spec Vol 3 Part F), for the
// active proxy (#656). Encoders build a request PDU; parsers validate a
// response against the layout the spec gives it and refuse anything else —
// a peer's response is input from a device we do not control, and its
// contents are forwarded to Home Assistant.
//
// Not here: Prepare/Execute Write (long and reliable writes), Read Multiple
// and Signed Write. Nothing HA's proxy protocol asks of a v1 needs them.

import (
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
)

const (
	attOpError           = 0x01
	attOpMTUReq          = 0x02
	attOpMTURsp          = 0x03
	attOpFindInfoReq     = 0x04
	attOpFindInfoRsp     = 0x05
	attOpReadByTypeReq   = 0x08
	attOpReadByTypeRsp   = 0x09
	attOpReadReq         = 0x0A
	attOpReadRsp         = 0x0B
	attOpReadBlobReq     = 0x0C
	attOpReadBlobRsp     = 0x0D
	attOpReadByGroupReq  = 0x10
	attOpReadByGroupRsp  = 0x11
	attOpWriteReq        = 0x12
	attOpWriteRsp        = 0x13
	attOpNotification    = 0x1B
	attOpIndication      = 0x1D
	attOpConfirmation    = 0x1E
	attOpWriteCmd        = 0x52
	attDefaultMTU        = 23
	attErrAttrNotFound   = 0x0A
	attErrReqNotSupport  = 0x06
	uuidPrimaryService   = 0x2800
	uuidCharacteristic   = 0x2803
	attFindInfoFormat16  = 0x01
	attFindInfoFormat128 = 0x02
)

// UUID is an attribute type as ATT carries it: 2 or 16 bytes, little-endian.
type UUID []byte

// UUID16 builds the 16-bit form.
func UUID16(v uint16) UUID {
	return UUID{byte(v), byte(v >> 8)}
}

// Is16 reports whether u is the 16-bit UUID v.
func (u UUID) Is16(v uint16) bool {
	return len(u) == 2 && binary.LittleEndian.Uint16(u) == v
}

// Full returns the 128-bit value in canonical (big-endian) byte order, with a
// 16-bit UUID expanded on the Bluetooth Base UUID.
func (u UUID) Full() [16]byte {
	out := [16]byte{0, 0, 0, 0, 0, 0, 0x10, 0x00, 0x80, 0x00, 0x00, 0x80, 0x5F, 0x9B, 0x34, 0xFB}
	switch len(u) {
	case 2:
		out[2], out[3] = u[1], u[0]
	case 16:
		for i := 0; i < 16; i++ {
			out[i] = u[15-i]
		}
	}
	return out
}

// String renders the canonical 8-4-4-4-12 form.
func (u UUID) String() string {
	f := u.Full()
	h := hex.EncodeToString(f[:])
	return h[0:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:32]
}

// ATTError is an Error Response from the peer.
type ATTError struct {
	ReqOp  byte
	Handle uint16
	Code   byte
}

func (e *ATTError) Error() string {
	return fmt.Sprintf("att error 0x%02x on request 0x%02x handle 0x%04x", e.Code, e.ReqOp, e.Handle)
}

// isNotFound reports the Error Response that ends every discovery procedure.
func isNotFound(err error) bool {
	var e *ATTError
	return errors.As(err, &e) && e.Code == attErrAttrNotFound
}

var errATTMalformed = errors.New("malformed att pdu")

// attCheck matches a response to its request: the expected opcode passes, an
// Error Response naming this request becomes an *ATTError, anything else is
// malformed.
func attCheck(reqOp, wantOp byte, rsp []byte) error {
	if len(rsp) == 0 {
		return errATTMalformed
	}
	if rsp[0] == attOpError {
		if len(rsp) != 5 || rsp[1] != reqOp {
			return errATTMalformed
		}
		return &ATTError{ReqOp: rsp[1], Handle: binary.LittleEndian.Uint16(rsp[2:4]), Code: rsp[4]}
	}
	if rsp[0] != wantOp {
		return errATTMalformed
	}
	return nil
}

func attRange(op byte, start, end uint16, typ UUID) []byte {
	p := make([]byte, 5, 5+len(typ))
	p[0] = op
	binary.LittleEndian.PutUint16(p[1:3], start)
	binary.LittleEndian.PutUint16(p[3:5], end)
	return append(p, typ...)
}

func encodeMTUReq(mtu uint16) []byte {
	return []byte{attOpMTUReq, byte(mtu), byte(mtu >> 8)}
}

// parseMTURsp returns the server's receive MTU. Below 23 is not a legal value.
func parseMTURsp(rsp []byte) (uint16, error) {
	if err := attCheck(attOpMTUReq, attOpMTURsp, rsp); err != nil {
		return 0, err
	}
	if len(rsp) != 3 {
		return 0, errATTMalformed
	}
	mtu := binary.LittleEndian.Uint16(rsp[1:3])
	if mtu < attDefaultMTU {
		return 0, errATTMalformed
	}
	return mtu, nil
}

func encodeReadByGroupReq(start, end uint16, typ UUID) []byte {
	return attRange(attOpReadByGroupReq, start, end, typ)
}

// attGroup is one entry of a Read By Group Type Response.
type attGroup struct {
	Start, End uint16
	Value      []byte
}

func parseReadByGroupRsp(rsp []byte) ([]attGroup, error) {
	if err := attCheck(attOpReadByGroupReq, attOpReadByGroupRsp, rsp); err != nil {
		return nil, err
	}
	if len(rsp) < 2 {
		return nil, errATTMalformed
	}
	n, body := int(rsp[1]), rsp[2:]
	if n < 4 || len(body) == 0 || len(body)%n != 0 {
		return nil, errATTMalformed
	}
	var out []attGroup
	for ; len(body) > 0; body = body[n:] {
		out = append(out, attGroup{
			Start: binary.LittleEndian.Uint16(body[0:2]),
			End:   binary.LittleEndian.Uint16(body[2:4]),
			Value: body[4:n],
		})
	}
	return out, nil
}

func encodeReadByTypeReq(start, end uint16, typ UUID) []byte {
	return attRange(attOpReadByTypeReq, start, end, typ)
}

// attHandleValue is one entry of a Read By Type Response.
type attHandleValue struct {
	Handle uint16
	Value  []byte
}

func parseReadByTypeRsp(rsp []byte) ([]attHandleValue, error) {
	if err := attCheck(attOpReadByTypeReq, attOpReadByTypeRsp, rsp); err != nil {
		return nil, err
	}
	if len(rsp) < 2 {
		return nil, errATTMalformed
	}
	n, body := int(rsp[1]), rsp[2:]
	if n < 2 || len(body) == 0 || len(body)%n != 0 {
		return nil, errATTMalformed
	}
	var out []attHandleValue
	for ; len(body) > 0; body = body[n:] {
		out = append(out, attHandleValue{
			Handle: binary.LittleEndian.Uint16(body[0:2]),
			Value:  body[2:n],
		})
	}
	return out, nil
}

func encodeFindInfoReq(start, end uint16) []byte {
	return attRange(attOpFindInfoReq, start, end, nil)
}

// attInfo is one entry of a Find Information Response.
type attInfo struct {
	Handle uint16
	UUID   UUID
}

func parseFindInfoRsp(rsp []byte) ([]attInfo, error) {
	if err := attCheck(attOpFindInfoReq, attOpFindInfoRsp, rsp); err != nil {
		return nil, err
	}
	if len(rsp) < 2 {
		return nil, errATTMalformed
	}
	var n int
	switch rsp[1] {
	case attFindInfoFormat16:
		n = 4
	case attFindInfoFormat128:
		n = 18
	default:
		return nil, errATTMalformed
	}
	body := rsp[2:]
	if len(body) == 0 || len(body)%n != 0 {
		return nil, errATTMalformed
	}
	var out []attInfo
	for ; len(body) > 0; body = body[n:] {
		out = append(out, attInfo{
			Handle: binary.LittleEndian.Uint16(body[0:2]),
			UUID:   UUID(body[2:n]),
		})
	}
	return out, nil
}

func encodeReadReq(handle uint16) []byte {
	return []byte{attOpReadReq, byte(handle), byte(handle >> 8)}
}

func encodeReadBlobReq(handle, offset uint16) []byte {
	return []byte{attOpReadBlobReq, byte(handle), byte(handle >> 8), byte(offset), byte(offset >> 8)}
}

// parseReadRsp returns the value of a Read or Read Blob Response; reqOp says
// which was asked. An empty value is legal.
func parseReadRsp(reqOp byte, rsp []byte) ([]byte, error) {
	if err := attCheck(reqOp, reqOp+1, rsp); err != nil {
		return nil, err
	}
	return rsp[1:], nil
}

func encodeWrite(op byte, handle uint16, value []byte) []byte {
	p := make([]byte, 3, 3+len(value))
	p[0] = op
	binary.LittleEndian.PutUint16(p[1:3], handle)
	return append(p, value...)
}

func encodeWriteReq(handle uint16, value []byte) []byte {
	return encodeWrite(attOpWriteReq, handle, value)
}

// encodeWriteCmd is a write without response: nothing comes back.
func encodeWriteCmd(handle uint16, value []byte) []byte {
	return encodeWrite(attOpWriteCmd, handle, value)
}

func parseWriteRsp(rsp []byte) error {
	if err := attCheck(attOpWriteReq, attOpWriteRsp, rsp); err != nil {
		return err
	}
	if len(rsp) != 1 {
		return errATTMalformed
	}
	return nil
}

// parseHandleValue reads a Handle Value Notification or Indication.
func parseHandleValue(pdu []byte) (handle uint16, value []byte, indication, ok bool) {
	if len(pdu) < 3 || (pdu[0] != attOpNotification && pdu[0] != attOpIndication) {
		return 0, nil, false, false
	}
	return binary.LittleEndian.Uint16(pdu[1:3]), pdu[3:], pdu[0] == attOpIndication, true
}

// attErrorRsp builds the Error Response a client owes any request it does not
// serve, so the peer is not left waiting out its 30s transaction timeout.
func attErrorRsp(reqOp byte, handle uint16, code byte) []byte {
	return []byte{attOpError, reqOp, byte(handle), byte(handle >> 8), code}
}
