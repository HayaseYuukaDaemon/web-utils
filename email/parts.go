package email

import (
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapclient"
	"github.com/emersion/go-message"
)

const (
	bodyLimit      = 256 << 10
	attachmentPage = 1 << 20
)

type partInfo struct {
	ID          string `json:"id"`
	Type        string `json:"type"`
	Filename    string `json:"filename,omitempty"`
	EncodedSize uint32 `json:"encoded_size"`
	path        []int
	mime        *imap.BodyStructureSinglePart
}

func describeParts(structure imap.BodyStructure) (body *partInfo, attachments []partInfo) {
	structure.Walk(func(path []int, part imap.BodyStructure) bool {
		single, ok := part.(*imap.BodyStructureSinglePart)
		if !ok {
			return true
		}
		ids := make([]string, len(path))
		for i, n := range path {
			ids[i] = strconv.Itoa(n)
		}
		p := partInfo{ID: strings.Join(ids, "."), Type: single.MediaType(), Filename: single.Filename(),
			EncodedSize: single.Size, path: slices.Clone(path), mime: single}
		disp := single.Disposition()
		if p.Filename != "" || (disp != nil && strings.EqualFold(disp.Value, "attachment")) || (p.Type != "text/plain" && p.Type != "text/html") {
			attachments = append(attachments, p)
		} else if body == nil || (p.Type == "text/plain" && body.Type == "text/html") {
			body = &p
		}
		return false
	})
	return body, attachments
}

func loadStructure(c *imapclient.Client, ref messageRef) (*imapclient.FetchMessageBuffer, error) {
	if err := ref.selectFolder(c, true); err != nil {
		return nil, err
	}
	m, err := ref.fetch(c, &imap.FetchOptions{Envelope: true, Flags: true, BodyStructure: &imap.FetchItemBodyStructure{Extended: true}})
	if err == nil && (m.Envelope == nil || m.BodyStructure == nil) {
		return nil, errors.New("server omitted message metadata")
	}
	return m, err
}

// sectionReader fetches the transfer-encoded part in ranges so even a large
// attachment can be decoded without buffering the complete message.
type sectionReader struct {
	client *imapclient.Client
	ref    messageRef
	path   []int
	offset int64
	buffer []byte
	eof    bool
}

func (r *sectionReader) Read(dest []byte) (int, error) {
	if len(r.buffer) == 0 {
		if r.eof {
			return 0, io.EOF
		}
		const chunk = 256 << 10
		section := &imap.FetchItemBodySection{Part: r.path, Peek: true, Partial: &imap.SectionPartial{Offset: r.offset, Size: chunk}}
		m, err := r.ref.fetch(r.client, &imap.FetchOptions{BodySection: []*imap.FetchItemBodySection{section}})
		if err != nil {
			return 0, err
		}
		r.buffer = m.FindBodySection(section)
		if r.buffer == nil {
			return 0, errors.New("server omitted the requested MIME part")
		}
		r.offset += int64(len(r.buffer))
		r.eof = len(r.buffer) < chunk
		if len(r.buffer) == 0 {
			return 0, io.EOF
		}
	}
	n := copy(dest, r.buffer)
	r.buffer = r.buffer[n:]
	return n, nil
}

// Only body text is transcoded. Text attachments retain their original bytes.
func readPart(c *imapclient.Client, ref messageRef, p *partInfo, isBody bool, offset, limit int64) ([]byte, bool, error) {
	var h message.Header
	if isBody {
		h.SetContentType(p.Type, p.mime.Params)
	} else {
		h.SetContentType("application/octet-stream", nil)
	}
	h.Set("Content-Transfer-Encoding", p.mime.Encoding)
	entity, err := message.New(h, &sectionReader{client: c, ref: ref, path: p.path})
	if err != nil {
		return nil, false, err
	}
	if offset > 0 {
		if _, err := io.CopyN(io.Discard, entity.Body, offset); err != nil {
			return nil, false, fmt.Errorf("seek to decoded offset %d: %w", offset, err)
		}
	}
	data, err := io.ReadAll(io.LimitReader(entity.Body, limit+1))
	if err != nil {
		return nil, false, err
	}
	if int64(len(data)) <= limit {
		return data, false, nil
	}
	if isBody {
		// Keep the final UTF-8 character intact across body pages.
		for end := limit; end > max(0, limit-utf8.UTFMax); end-- {
			if utf8.RuneStart(data[end]) {
				limit = end
				break
			}
		}
	}
	return data[:limit], true, nil
}

type readInput struct {
	messageRef
	Offset int64 `json:"offset,omitempty" jsonschema:"Body byte offset from the previous next_offset. Defaults to 0."`
}

type mailContent struct {
	summary
	Body        string     `json:"body"`
	BodyType    string     `json:"body_type,omitempty"`
	NextOffset  int64      `json:"next_offset,omitempty" jsonschema:"Pass as offset to continue reading the body when present."`
	Attachments []partInfo `json:"attachments,omitempty"`
}

func readMail(c *imapclient.Client, in readInput) (mailContent, error) {
	if in.Offset < 0 {
		return mailContent{}, errors.New("offset must be non-negative")
	}
	ref := in.messageRef
	m, err := loadStructure(c, ref)
	if err != nil {
		return mailContent{}, err
	}
	body, attachments := describeParts(m.BodyStructure)
	out := mailContent{summary: summarize(m), Attachments: attachments}
	if body != nil {
		data, more, err := readPart(c, ref, body, true, in.Offset, bodyLimit)
		if err != nil {
			return out, err
		}
		out.Body, out.BodyType = string(data), body.Type
		if more {
			out.NextOffset = in.Offset + int64(len(data))
		}
	}
	return out, nil
}

type attachmentInput struct {
	messageRef
	ID     string `json:"id" jsonschema:"Attachment ID from mail_read."`
	Offset int64  `json:"offset,omitempty" jsonschema:"Start at this decoded byte offset. Defaults to 0."`
	Limit  int64  `json:"limit,omitempty" jsonschema:"Maximum decoded bytes per call (default and maximum 1048576)."`
}
type attachmentContent struct {
	partInfo
	Data       string `json:"data" jsonschema:"Base64-encoded bytes for this page."`
	NextOffset int64  `json:"next_offset,omitempty" jsonschema:"Pass as offset to read the next page when present."`
}

func getAttachment(c *imapclient.Client, in attachmentInput) (attachmentContent, error) {
	if in.Offset < 0 || in.Limit < 0 || in.Limit > attachmentPage {
		return attachmentContent{}, errors.New("invalid attachment offset or limit")
	}
	if in.Limit == 0 {
		in.Limit = attachmentPage
	}
	m, err := loadStructure(c, in.messageRef)
	if err != nil {
		return attachmentContent{}, err
	}
	_, attachments := describeParts(m.BodyStructure)
	for _, p := range attachments {
		if p.ID == in.ID {
			data, more, err := readPart(c, in.messageRef, &p, false, in.Offset, in.Limit)
			if err != nil {
				return attachmentContent{}, err
			}
			out := attachmentContent{partInfo: p, Data: base64.StdEncoding.EncodeToString(data)}
			if more {
				out.NextOffset = in.Offset + int64(len(data))
			}
			return out, nil
		}
	}
	return attachmentContent{}, fmt.Errorf("attachment %q not found", in.ID)
}
