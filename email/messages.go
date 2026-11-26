package email

import (
	"cmp"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapclient"
)

type folderInfo struct {
	Name       string             `json:"name"`
	Attributes []imap.MailboxAttr `json:"attributes,omitempty"`
}

type folderList struct {
	Folders []folderInfo `json:"folders"`
}

func listFolders(c *imapclient.Client, _ struct{}) (folderList, error) {
	boxes, err := c.List("", "*", nil).Collect()
	out := folderList{Folders: make([]folderInfo, 0, len(boxes))}
	for _, box := range boxes {
		out.Folders = append(out.Folders, folderInfo{Name: box.Mailbox, Attributes: box.Attrs})
	}
	return out, err
}

type pageInput struct {
	Folder    string   `json:"folder,omitempty" jsonschema:"Folder name. Defaults to INBOX."`
	Limit     int      `json:"limit,omitempty" jsonschema:"Number of messages (default 20 and maximum 100)."`
	BeforeUID imap.UID `json:"before_uid,omitempty" jsonschema:"Only return UIDs smaller than this value. Use the last UID to get the next page."`
}

type searchInput struct {
	pageInput
	Query string `json:"query" jsonschema:"Text to find in the headers or body."`
}

type messageRef struct {
	Folder      string   `json:"folder,omitempty" jsonschema:"Folder name from the listing. Defaults to INBOX."`
	UIDValidity uint32   `json:"uid_validity" jsonschema:"Mailbox UID validity returned by the listing."`
	UID         imap.UID `json:"uid" jsonschema:"Message UID within this mailbox."`
}

type summary struct {
	UID     imap.UID    `json:"uid"`
	Subject string      `json:"subject"`
	From    []string    `json:"from,omitempty"`
	To      []string    `json:"to,omitempty"`
	Date    time.Time   `json:"date"`
	Flags   []imap.Flag `json:"flags,omitempty"`
}

func addresses(list []imap.Address) []string {
	var out []string
	for _, addr := range list {
		if s := addr.Addr(); s != "" {
			out = append(out, s)
		}
	}
	return out
}

func summarize(m *imapclient.FetchMessageBuffer) summary {
	env := m.Envelope
	return summary{UID: m.UID, Subject: env.Subject, From: addresses(env.From), To: addresses(env.To), Date: env.Date, Flags: m.Flags}
}

type messageList struct {
	Folder      string    `json:"folder"`
	UIDValidity uint32    `json:"uid_validity"`
	Messages    []summary `json:"messages"`
}

func listUnread(c *imapclient.Client, in pageInput) (messageList, error) {
	return listMessages(c, in, imap.SearchCriteria{NotFlag: []imap.Flag{imap.FlagSeen}})
}

func searchMail(c *imapclient.Client, in searchInput) (messageList, error) {
	if strings.TrimSpace(in.Query) == "" {
		return messageList{}, errors.New("query must contain text")
	}
	return listMessages(c, in.pageInput, imap.SearchCriteria{Text: []string{in.Query}})
}

func listMessages(c *imapclient.Client, in pageInput, criteria imap.SearchCriteria) (messageList, error) {
	if in.Limit < 0 || in.Limit > 100 {
		return messageList{}, errors.New("limit must be between 1 and 100")
	}
	if in.Limit == 0 {
		in.Limit = 20
	}
	out := messageList{Folder: folderName(in.Folder), Messages: []summary{}}
	box, err := c.Select(out.Folder, &imap.SelectOptions{ReadOnly: true}).Wait()
	if err != nil {
		return out, err
	}
	out.UIDValidity = box.UIDValidity
	if in.BeforeUID == 1 {
		return out, nil
	}
	if in.BeforeUID > 1 {
		criteria.UID = []imap.UIDSet{{{Start: 1, Stop: in.BeforeUID - 1}}}
	}
	matches, err := c.UIDSearch(&criteria, nil).Wait()
	if err != nil {
		return out, err
	}
	uids := matches.AllUIDs()
	if len(uids) == 0 {
		return out, nil
	}
	uids = uids[max(0, len(uids)-in.Limit):]
	msgs, err := c.Fetch(imap.UIDSetNum(uids...), &imap.FetchOptions{UID: true, Envelope: true, Flags: true}).Collect()
	if err != nil {
		return out, err
	}
	for _, m := range msgs {
		if m.Envelope != nil {
			out.Messages = append(out.Messages, summarize(m))
		}
	}
	slices.SortFunc(out.Messages, func(a, b summary) int { return cmp.Compare(b.UID, a.UID) })
	return out, nil
}

func (ref messageRef) selectFolder(c *imapclient.Client, readOnly bool) error {
	if ref.UID == 0 || ref.UIDValidity == 0 {
		return errors.New("uid and uid_validity must be positive")
	}
	box, err := c.Select(folderName(ref.Folder), &imap.SelectOptions{ReadOnly: readOnly}).Wait()
	if err != nil {
		return err
	}
	if box.UIDValidity != ref.UIDValidity {
		return errors.New("folder UID validity changed; list messages again")
	}
	return nil
}

func (ref messageRef) fetch(c *imapclient.Client, opt *imap.FetchOptions) (*imapclient.FetchMessageBuffer, error) {
	opt.UID = true
	msgs, err := c.Fetch(imap.UIDSetNum(ref.UID), opt).Collect()
	if err != nil {
		return nil, err
	}
	for _, m := range msgs {
		if m.UID == ref.UID {
			return m, nil
		}
	}
	return nil, fmt.Errorf("message UID %d not found", ref.UID)
}

func markRead(c *imapclient.Client, ref messageRef) (messageRef, error) {
	if err := ref.selectFolder(c, false); err != nil {
		return ref, err
	}
	msgs, err := c.Store(imap.UIDSetNum(ref.UID), &imap.StoreFlags{Op: imap.StoreFlagsAdd, Flags: []imap.Flag{imap.FlagSeen}}, nil).Collect()
	if err != nil {
		return ref, err
	}
	for _, m := range msgs {
		if m.UID == ref.UID && slices.Contains(m.Flags, imap.FlagSeen) {
			return ref, nil
		}
	}
	// Some servers omit the flag update when the message is already seen.
	m, err := ref.fetch(c, &imap.FetchOptions{Flags: true})
	if err == nil && !slices.Contains(m.Flags, imap.FlagSeen) {
		err = errors.New("server did not set the seen flag")
	}
	return ref, err
}
