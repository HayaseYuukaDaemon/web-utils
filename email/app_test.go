package email

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapserver"
	"github.com/emersion/go-imap/v2/imapserver/imapmemserver"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type idleSession struct {
	imapserver.Session
	idling chan<- struct{}
}

func (s idleSession) Idle(w *imapserver.UpdateWriter, stop <-chan struct{}) error {
	select {
	case s.idling <- struct{}{}:
	default:
	}
	return s.Session.Idle(w, stop)
}

func testMailbox(t *testing.T) (*App, *imapmemserver.User, <-chan struct{}) {
	t.Helper()
	// Reuse the loopback TLS certificate and its trust store.
	cert := httptest.NewTLSServer(nil)
	cert.Close()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	user := imapmemserver.NewUser("tester", "test-password")
	if err := user.Create("INBOX", nil); err != nil {
		t.Fatal(err)
	}
	mem := imapmemserver.New()
	mem.AddUser(user)
	idling := make(chan struct{}, 1)
	server := imapserver.New(&imapserver.Options{NewSession: func(*imapserver.Conn) (imapserver.Session, *imapserver.GreetingData, error) {
		return idleSession{mem.NewSession(), idling}, nil, nil
	}})
	go server.Serve(tls.NewListener(ln, cert.TLS))
	t.Cleanup(func() { server.Close() })
	app := &App{addr: ln.Addr().String(), username: "tester", password: "test-password",
		dialer: tls.Dialer{Config: cert.Client().Transport.(*http.Transport).TLSClientConfig}}
	return app, user, idling
}

func appendMessage(t *testing.T, user *imapmemserver.User, raw string) *imap.AppendData {
	t.Helper()
	data, err := user.Append("INBOX", strings.NewReader(strings.ReplaceAll(raw, "\n", "\r\n")), &imap.AppendOptions{Flags: []imap.Flag{imap.FlagFlagged}})
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestMailTools(t *testing.T) {
	app, user, _ := testMailbox(t)
	appendMessage(t, user, "Subject: earlier\n\nsample")
	msg := appendMessage(t, user, `From: Sender <from@example.org>
Subject: example
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary=x

--x
Content-Type: text/plain; charset=iso-8859-1
Content-Transfer-Encoding: quoted-printable

caf=E9
--x
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="test.bin"
Content-Transfer-Encoding: base64

AP8=
--x--
`)
	ctx := t.Context()
	impl := &mcp.Implementation{Name: "test", Version: "1"}
	server := mcp.NewServer(impl, nil)
	if err := app.RegisterMCPTools(server); err != nil {
		t.Fatal(err)
	}
	st, ct := mcp.NewInMemoryTransports()
	ss, err := server.Connect(ctx, st, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer ss.Close()
	client, err := mcp.NewClient(impl, nil).Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	call := func(name string, args any, out any) {
		t.Helper()
		r, err := client.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
		if err != nil {
			t.Fatal(err)
		}
		if out == nil {
			if !r.IsError {
				t.Fatalf("%s unexpectedly succeeded", name)
			}
			return
		}
		if r.IsError {
			t.Fatalf("%s: %v", name, r.Content[0].(*mcp.TextContent).Text)
		}
		b, err := json.Marshal(r.StructuredContent)
		if err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(b, out); err != nil {
			t.Fatal(err)
		}
	}
	var folders folderList
	call("mail_list_folders", struct{}{}, &folders)
	if len(folders.Folders) != 1 || folders.Folders[0].Name != "INBOX" {
		t.Fatal(folders)
	}
	var results messageList
	call("mail_search", searchInput{Query: "example"}, &results)
	if len(results.Messages) != 1 || results.Messages[0].UID != msg.UID {
		t.Fatal(results)
	}
	ref := messageRef{Folder: results.Folder, UIDValidity: results.UIDValidity, UID: msg.UID}
	var content mailContent
	call("mail_read", ref, &content)
	if strings.TrimSpace(content.Body) != "café" || len(content.Attachments) != 1 || content.Attachments[0].Filename != "test.bin" {
		t.Fatal(content)
	}
	var attachment attachmentContent
	call("mail_get_attachment", attachmentInput{messageRef: ref, ID: content.Attachments[0].ID}, &attachment)
	if attachment.Data != "AP8=" {
		t.Fatal(attachment)
	}
	page := attachmentInput{messageRef: ref, ID: content.Attachments[0].ID, Limit: 1}
	call("mail_get_attachment", page, &attachment)
	if attachment.Data != "AA==" || attachment.NextOffset != 1 {
		t.Fatal(attachment)
	}
	page.Offset = attachment.NextOffset
	attachment = attachmentContent{}
	call("mail_get_attachment", page, &attachment)
	if attachment.Data != "/w==" || attachment.NextOffset != 0 {
		t.Fatal(attachment)
	}
	call("mail_list_unread", pageInput{Limit: 1}, &results)
	if len(results.Messages) != 1 || results.Messages[0].UID != msg.UID {
		t.Fatal(results)
	}
	call("mail_list_unread", pageInput{BeforeUID: msg.UID}, &results)
	if len(results.Messages) != 1 || results.Messages[0].UID >= msg.UID {
		t.Fatal(results)
	}
	stale := ref
	stale.UIDValidity++
	call("mail_mark_read", stale, nil)
	call("mail_mark_read", ref, &messageRef{})
	call("mail_mark_read", ref, &messageRef{})
	call("mail_read", ref, &content)
	if !slices.Contains(content.Flags, imap.FlagFlagged) || !slices.Contains(content.Flags, imap.FlagSeen) {
		t.Fatal(content.Flags)
	}
	call("mail_list_unread", pageInput{}, &results)
	if len(results.Messages) != 1 || results.Messages[0].UID == msg.UID {
		t.Fatal(results)
	}
	ref.UID++
	call("mail_mark_read", ref, nil)
}

func TestBodyPaging(t *testing.T) {
	app, user, _ := testMailbox(t)
	prefix := strings.Repeat("x", bodyLimit-1)
	msg := appendMessage(t, user, "Subject: large body\nContent-Type: text/plain; charset=utf-8\n\n"+prefix+"邮件")
	c, closeClient, err := app.connect(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer closeClient()
	in := readInput{messageRef: messageRef{UID: msg.UID, UIDValidity: msg.UIDValidity}}
	out, err := readMail(c, in)
	if err != nil {
		t.Fatal(err)
	}
	if out.Body != prefix || out.NextOffset != int64(len(prefix)) {
		t.Fatalf("invalid first page: length %d, offset %d", len(out.Body), out.NextOffset)
	}
	in.Offset = out.NextOffset
	out, err = readMail(c, in)
	if err != nil {
		t.Fatal(err)
	}
	if out.Body != "邮件" || out.NextOffset != 0 {
		t.Fatalf("invalid last page: %q, offset %d", out.Body, out.NextOffset)
	}
}

func TestInboxIdleAndReconnect(t *testing.T) {
	app, user, idling := testMailbox(t)
	appendMessage(t, user, "Subject: historical\n\nold")
	var cursor inboxCursor
	events := make(chan imap.UID, 4)
	start := func() (context.CancelFunc, <-chan error) {
		ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
		done := make(chan error, 1)
		go func() { done <- app.watchSession(ctx, &cursor, func(_ uint32, uid imap.UID) { events <- uid }) }()
		return cancel, done
	}
	cancel, done := start()
	defer cancel()
	select {
	case <-idling:
	case err := <-done:
		t.Fatal(err)
	case <-time.After(3 * time.Second):
		t.Fatal("IDLE not reached")
	}
	check := func(want imap.UID) {
		t.Helper()
		select {
		case got := <-events:
			if got != want {
				t.Fatalf("got UID %d, want %d", got, want)
			}
		case <-time.After(3 * time.Second):
			t.Fatal("missing arrival")
		}
	}
	check(appendMessage(t, user, "Subject: live\n\nnew").UID)
	cancel()
	<-done
	missed := appendMessage(t, user, "Subject: offline\n\nnew")
	cancel, done = start()
	defer cancel()
	check(missed.UID)
	cancel()
	<-done
}
