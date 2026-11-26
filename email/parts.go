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
	header      message.Header
	attachment  bool
}

func (p *partInfo) classify(disposition string) {
	p.attachment = p.Filename != "" || strings.EqualFold(disposition, "attachment") || (p.Type != "text/plain" && p.Type != "text/html")
}

func (p *partInfo) preferredTo(selected *partInfo) bool {
	return selected == nil || (p.Type == "text/plain" && selected.Type == "text/html")
}

var errBodyStructure = errors.New("unusable IMAP BODYSTRUCTURE")

type partVisitor func(*partInfo, io.Reader) error
type partSource func(*imapclient.Client, messageRef, partVisitor) (summary, error)

func imapParts(c *imapclient.Client, ref messageRef, visit partVisitor) (summary, error) {
	if err := ref.selectFolder(c, true); err != nil {
		return summary{}, err
	}
	m, err := ref.fetch(c, &imap.FetchOptions{Envelope: true, Flags: true, BodyStructure: &imap.FetchItemBodyStructure{Extended: true}})
	if err != nil {
		// go-imap's wire syntax error type is internal. Only retry a malformed
		// FETCH response, never a missing UID, IMAP NO/BAD or transport error.
		if strings.HasPrefix(err.Error(), "in response-data:") && strings.Contains(err.Error(), "imapwire:") {
			return summary{}, fmt.Errorf("%w: %w", errBodyStructure, err)
		}
		return summary{}, err
	}
	if m.BodyStructure == nil {
		return summary{}, errBodyStructure
	}
	if m.Envelope == nil {
		return summary{}, errors.New("server omitted message envelope")
	}
	var body *partInfo
	var parts []*partInfo
	m.BodyStructure.Walk(func(path []int, part imap.BodyStructure) bool {
		single, ok := part.(*imap.BodyStructureSinglePart)
		if !ok {
			return true
		}
		ids := make([]string, len(path))
		for i, n := range path {
			ids[i] = strconv.Itoa(n)
		}
		p := &partInfo{ID: strings.Join(ids, "."), Type: single.MediaType(), Filename: single.Filename(), EncodedSize: single.Size, path: slices.Clone(path)}
		p.header.SetContentType(p.Type, single.Params)
		p.header.Set("Content-Transfer-Encoding", single.Encoding)
		var disposition string
		if disp := single.Disposition(); disp != nil {
			disposition = disp.Value
		}
		p.classify(disposition)
		if p.attachment {
			parts = append(parts, p)
		} else if p.preferredTo(body) {
			body = p
		}
		return false
	})
	if body != nil {
		parts = append(parts, body)
	}
	for _, p := range parts {
		if err := visit(p, &sectionReader{client: c, ref: ref, path: p.path}); err != nil {
			return summary{}, err
		}
	}
	return summarize(m), nil
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
func readPart(r io.Reader, p *partInfo, offset, limit int64) ([]byte, bool, error) {
	h := p.header
	if p.attachment {
		h = h.Copy()
		h.SetContentType("application/octet-stream", nil)
	}
	entity, err := message.New(h, r)
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
	if !p.attachment {
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
	Body        string      `json:"body"`
	BodyType    string      `json:"body_type,omitempty"`
	NextOffset  int64       `json:"next_offset,omitempty" jsonschema:"Pass as offset to continue reading the body when present."`
	Attachments []*partInfo `json:"attachments,omitempty"`
}

func (parts partSource) readMail(c *imapclient.Client, in readInput) (mailContent, error) {
	if in.Offset < 0 {
		return mailContent{}, errors.New("offset must be non-negative")
	}
	var out mailContent
	var body *partInfo
	var bodyErr error
	meta, err := parts(c, in.messageRef, func(p *partInfo, r io.Reader) error {
		if p.attachment {
			out.Attachments = append(out.Attachments, p)
		} else if p.preferredTo(body) {
			// A plain-text alternative later in the stream supersedes HTML,
			// even if this offset was beyond the shorter HTML version.
			body = p
			data, more, err := readPart(r, p, in.Offset, bodyLimit)
			bodyErr = err
			out.Body, out.BodyType, out.NextOffset = string(data), p.Type, 0
			if more {
				out.NextOffset = in.Offset + int64(len(data))
			}
		}
		return nil
	})
	if err != nil {
		return mailContent{}, err
	}
	out.summary = meta
	return out, bodyErr
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

func (parts partSource) getAttachment(c *imapclient.Client, in attachmentInput) (attachmentContent, error) {
	if in.Offset < 0 || in.Limit < 0 || in.Limit > attachmentPage {
		return attachmentContent{}, errors.New("invalid attachment offset or limit")
	}
	if in.Limit == 0 {
		in.Limit = attachmentPage
	}
	var matched *partInfo
	var out attachmentContent
	_, err := parts(c, in.messageRef, func(p *partInfo, r io.Reader) error {
		if !p.attachment || p.ID != in.ID {
			return nil
		}
		matched = p
		data, more, err := readPart(r, p, in.Offset, in.Limit)
		if err != nil {
			return err
		}
		out.Data = base64.StdEncoding.EncodeToString(data)
		if more {
			out.NextOffset = in.Offset + int64(len(data))
		}
		return nil
	})
	if err != nil {
		return attachmentContent{}, err
	}
	if matched == nil {
		return attachmentContent{}, fmt.Errorf("attachment %q not found", in.ID)
	}
	out.partInfo = *matched
	return out, nil
}
