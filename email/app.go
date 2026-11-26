package email

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/emersion/go-imap/v2/imapclient"
	"github.com/emersion/go-message/charset"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type App struct {
	addr, username, password string
	dialer                   tls.Dialer
}

// FromEnv returns nil when IMAP has not been configured.
func FromEnv() (*App, error) {
	addr, user, pass := os.Getenv("IMAP_ADDR"), os.Getenv("IMAP_USERNAME"), os.Getenv("IMAP_PASSWORD")
	if addr == "" && user == "" && pass == "" {
		return nil, nil
	}
	host, port, err := net.SplitHostPort(addr)
	if err != nil || host == "" || port == "" || user == "" || pass == "" {
		return nil, errors.New("set IMAP_ADDR (host:port), IMAP_USERNAME and IMAP_PASSWORD together")
	}
	app := &App{addr: addr, username: user, password: pass, dialer: tls.Dialer{
		NetDialer: &net.Dialer{Timeout: 15 * time.Second},
		Config:    &tls.Config{MinVersion: tls.VersionTLS12},
	}}
	slog.Info("Email app initialized", "addr", addr)
	return app, nil
}

func (a *App) AppName() string {
	return "email"
}

func (a *App) GetMux() *http.ServeMux {
	return nil
}

func imapError(ctx context.Context, operation string, err error) error {
	// Closing the socket to interrupt a command can surface as EOF.
	if cause := context.Cause(ctx); cause != nil {
		err = cause
	}
	return fmt.Errorf("%s: %w", operation, err)
}

func (a *App) connect(ctx context.Context, updates *imapclient.UnilateralDataHandler) (*imapclient.Client, func(), error) {
	conn, err := a.dialer.DialContext(ctx, "tcp", a.addr)
	if err != nil {
		return nil, nil, imapError(ctx, "IMAP TLS dial", err)
	}
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	c := imapclient.New(conn, &imapclient.Options{
		UnilateralDataHandler: updates,
		WordDecoder:           &mime.WordDecoder{CharsetReader: charset.Reader},
	})
	closeClient := func() {
		stop()
		c.Close()
	}
	if err := c.WaitGreeting(); err != nil {
		closeClient()
		return nil, nil, imapError(ctx, "IMAP greeting", err)
	}
	if err := c.Login(a.username, a.password).Wait(); err != nil {
		closeClient()
		return nil, nil, imapError(ctx, "IMAP LOGIN", err)
	}
	return c, closeClient, nil
}

// Each call owns its connection and selected mailbox, independently of IDLE.
func addTool[I, O any](a *App, m *mcp.Server, name, description string, readOnly bool, run func(*imapclient.Client, I) (O, error)) {
	mcp.AddTool(m, &mcp.Tool{Name: name, Description: description, Annotations: &mcp.ToolAnnotations{ReadOnlyHint: readOnly}},
		func(ctx context.Context, _ *mcp.CallToolRequest, in I) (*mcp.CallToolResult, O, error) {
			ctx, cancel := context.WithTimeout(ctx, time.Minute)
			defer cancel()
			c, closeClient, err := a.connect(ctx, nil)
			if err != nil {
				var zero O
				return nil, zero, err
			}
			defer closeClient()
			out, err := run(c, in)
			return nil, out, err
		})
}

func (a *App) RegisterMCPTools(m *mcp.Server) error {
	addTool(a, m, "mail_list_folders", "List IMAP folders and their attributes.", true, listFolders)
	addTool(a, m, "mail_list_unread", "List unread messages, newest UID first.", true, listUnread)
	addTool(a, m, "mail_search", "Search message headers and body for text, newest UID first.", true, searchMail)
	addTool(a, m, "mail_read", "Read a message and list attachment IDs, preserving unread state. Continue the body with next_offset when present.", true, readMail)
	addTool(a, m, "mail_mark_read", "Mark a message as read, preserving other flags.", false, markRead)
	addTool(a, m, "mail_get_attachment", "Get attachment bytes as base64, preserving unread state. Continue with next_offset when present.", true, getAttachment)
	return nil
}

func folderName(name string) string {
	if name == "" || strings.EqualFold(name, "INBOX") {
		return "INBOX"
	}
	return name
}
