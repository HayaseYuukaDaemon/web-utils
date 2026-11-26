package email

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapclient"
)

// Watch reports messages that arrive in INBOX after the first successful
// selection. The cursor survives reconnects within this process.
func (a *App) Watch(ctx context.Context) {
	var cursor inboxCursor
	delay := time.Second
	for ctx.Err() == nil {
		start := time.Now()
		err := a.watchSession(ctx, &cursor, func(validity uint32, uid imap.UID) {
			slog.Info("mail received", "folder", "INBOX", "uid_validity", validity, "uid", uid)
		})
		if ctx.Err() != nil {
			return
		}
		if time.Since(start) > time.Minute && !errors.Is(err, context.DeadlineExceeded) {
			delay = time.Second
		}
		slog.Warn("IMAP listener disconnected", "error", err, "retry_in", delay)
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
		delay = min(2*delay, time.Minute)
	}
}

type inboxCursor struct {
	validity uint32
	last     imap.UID
}

func (a *App) watchSession(ctx context.Context, cursor *inboxCursor, arrived func(uint32, imap.UID)) error {
	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	// Bound command waits even when the peer leaves the socket open.
	timeout := time.AfterFunc(time.Minute, func() { cancel(context.DeadlineExceeded) })
	defer timeout.Stop()
	changed := make(chan struct{}, 1)
	c, closeClient, err := a.connect(ctx, &imapclient.UnilateralDataHandler{Mailbox: func(data *imapclient.UnilateralDataMailbox) {
		if data.NumMessages != nil {
			select {
			case changed <- struct{}{}:
			default:
			}
		}
	}})
	if err != nil {
		return err
	}
	defer closeClient()
	timeout.Reset(time.Minute)
	box, err := c.Select("INBOX", &imap.SelectOptions{ReadOnly: true}).Wait()
	if err != nil {
		return imapError(ctx, "IMAP EXAMINE INBOX", err)
	}
	if box.UIDValidity == 0 || box.UIDNext == 0 {
		return errors.New("INBOX lacks UIDVALIDITY or UIDNEXT")
	}
	if cursor.validity != box.UIDValidity {
		cursor.validity, cursor.last = box.UIDValidity, box.UIDNext-1
	}

	caps := c.Caps()
	if caps == nil {
		return imapError(ctx, "IMAP CAPABILITY", errors.New("no capabilities received"))
	}
	idleSupported := caps.Has(imap.CapIdle)
	ready := false
	for ctx.Err() == nil {
		if cursor.last < ^imap.UID(0) {
			uids := imap.UIDSet{{Start: cursor.last + 1, Stop: 0}}
			timeout.Reset(time.Minute)
			data, err := c.UIDSearch(&imap.SearchCriteria{UID: []imap.UIDSet{uids}}, nil).Wait()
			if err != nil {
				return imapError(ctx, "IMAP UID SEARCH", err)
			}
			for _, uid := range data.AllUIDs() {
				// n:* can include the current highest UID even when it is below n.
				if uid > cursor.last {
					arrived(cursor.validity, uid)
					cursor.last = uid
				}
			}
		}

		var idle *imapclient.IdleCommand
		wait := 30 * time.Second
		if idleSupported {
			timeout.Reset(time.Minute)
			idle, err = c.Idle()
			if err != nil {
				return imapError(ctx, "IMAP enter IDLE", err)
			}
			wait = 25 * time.Minute
		}
		if !ready {
			mode := "poll"
			if idleSupported {
				mode = "IDLE"
			}
			slog.Info("IMAP listener ready", "folder", "INBOX", "mode", mode)
			ready = true
		}
		timeout.Reset(wait + time.Minute)
		select {
		case <-ctx.Done():
		case <-c.Closed():
		case <-changed:
		case <-time.After(wait):
		}
		timeout.Reset(time.Minute)
		if idle != nil {
			if err := idle.Close(); err != nil {
				return imapError(ctx, "IMAP leave IDLE", err)
			}
			if err := idle.Wait(); err != nil {
				return imapError(ctx, "IMAP IDLE completion", err)
			}
		}
		select {
		case <-c.Closed():
			return errors.New("IMAP connection closed")
		default:
		}
	}
	return ctx.Err()
}
