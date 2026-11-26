package email

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"math"
	"mime/multipart"
	"strconv"
	"strings"

	"github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapclient"
	"github.com/emersion/go-message"
	"github.com/emersion/go-message/mail"
	"github.com/emersion/go-message/textproto"
)

// sectionReader with an empty part path fetches BODY.PEEK[] in chunks.
// Parse that stream directly so attachments do not have to be cached on disk.
func rawParts(c *imapclient.Client, ref messageRef, visit partVisitor) (summary, error) {
	if err := ref.selectFolder(c, true); err != nil {
		return summary{}, err
	}
	m, err := ref.fetch(c, &imap.FetchOptions{Flags: true})
	if err != nil {
		return summary{}, err
	}

	header, body, err := readRawHeader(&sectionReader{client: c, ref: ref})
	if err != nil {
		return summary{}, err
	}
	subject, _ := header.Subject()
	date, _ := header.Date()
	meta := summary{UID: ref.UID, Flags: m.Flags, Subject: subject, Date: date}
	rawAddresses := func(field string) []string {
		var out []string
		addrs, _ := header.AddressList(field)
		for _, addr := range addrs {
			out = append(out, addr.Address)
		}
		return out
	}
	meta.From, meta.To = rawAddresses("From"), rawAddresses("To")
	return meta, walkRawParts(header.Header, body, "", 0, visit)
}

// Some old messages omit the blank line between headers and body. Put the
// first non-header line back into the body, including its original line ending.
func readRawHeader(r io.Reader) (mail.Header, io.Reader, error) {
	br := bufio.NewReader(r)
	var fields bytes.Buffer
	var body io.Reader = br
	var restOfHeaderLine bool
	for {
		line, err := br.ReadSlice('\n')
		if err != nil && err != io.EOF && err != bufio.ErrBufferFull {
			return mail.Header{}, nil, err
		}
		if len(line) == 0 {
			break
		}
		if !restOfHeaderLine {
			if bytes.Equal(line, []byte("\r\n")) || bytes.Equal(line, []byte("\n")) {
				break
			}
			continued := fields.Len() > 0 && (line[0] == ' ' || line[0] == '\t')
			if !continued && !headerLine(line) {
				body = io.MultiReader(bytes.NewReader(line), br)
				break
			}
		}
		if fields.Len()+len(line) > 1<<20 {
			return mail.Header{}, nil, errors.New("raw message headers exceed 1 MiB")
		}
		fields.Write(line)
		restOfHeaderLine = err == bufio.ErrBufferFull
	}
	h, err := textproto.ReadHeader(bufio.NewReader(&fields))
	if err != nil {
		return mail.Header{}, nil, fmt.Errorf("read raw message header: %w", err)
	}
	return mail.Header{Header: message.Header{Header: h}}, body, nil
}

func headerLine(line []byte) bool {
	for i, b := range line {
		if b == ':' {
			return i > 0
		}
		if b < 33 || b > 126 {
			break
		}
	}
	return false
}

func walkRawParts(h message.Header, r io.Reader, path string, depth int, visit partVisitor) error {
	typ, params, err := h.ContentType()
	if err != nil {
		return fmt.Errorf("invalid MIME content type: %w", err)
	}
	if strings.HasPrefix(typ, "multipart/") {
		if params["boundary"] == "" {
			return errors.New("missing MIME multipart boundary")
		}
		if depth >= 100 {
			return errors.New("MIME multipart nesting too deep")
		}
		mr := multipart.NewReader(r, params["boundary"])
		for i := 1; ; i++ {
			child, err := mr.NextRawPart()
			if err == io.EOF {
				return nil
			}
			if err != nil {
				return err
			}
			childID := strconv.Itoa(i)
			if path != "" {
				childID = path + "." + childID
			}
			header := message.HeaderFromMap(child.Header)
			if typ == "multipart/digest" && header.Get("Content-Type") == "" {
				header.SetContentType("message/rfc822", nil)
			}
			if err := walkRawParts(header, child, childID, depth+1, visit); err != nil {
				return err
			}
		}
	}
	if path == "" {
		path = "1"
	}
	disp, dispositionParams, _ := h.ContentDisposition()
	filename := dispositionParams["filename"]
	if filename == "" {
		filename = params["name"]
	}
	p := &partInfo{ID: path, Type: typ, Filename: filename, header: h}
	p.classify(disp)
	if !p.attachment {
		return visit(p, r)
	}
	// Count transfer-encoded octets, including bytes consumed by the visitor.
	count := &io.LimitedReader{R: r, N: math.MaxInt64}
	if err := visit(p, count); err != nil {
		return err
	}
	if _, err := io.Copy(io.Discard, count); err != nil {
		return err
	}
	p.EncodedSize = uint32(math.MaxInt64 - count.N)
	return nil
}
