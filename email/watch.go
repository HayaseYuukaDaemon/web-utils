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
		if time.Since(start) > time.Minute {
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
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	// Bound command waits even when the peer leaves the socket open.
	timeout := time.AfterFunc(time.Minute, cancel)
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
	box, err := c.Select("INBOX", &imap.SelectOptions{ReadOnly: true}).Wait()
	if err != nil {
		return err
	}
	if box.UIDValidity == 0 || box.UIDNext == 0 {
		return errors.New("INBOX lacks UIDVALIDITY or UIDNEXT")
	}
	if cursor.validity != box.UIDValidity {
		cursor.validity, cursor.last = box.UIDValidity, box.UIDNext-1
	}

	idleSupported := c.Caps().Has(imap.CapIdle)
	for ctx.Err() == nil {
		if cursor.last < ^imap.UID(0) {
			uids := imap.UIDSet{{Start: cursor.last + 1, Stop: 0}}
			data, err := c.UIDSearch(&imap.SearchCriteria{UID: []imap.UIDSet{uids}}, nil).Wait()
			if err != nil {
				return err
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
			idle, err = c.Idle()
			if err != nil {
				return err
			}
			wait = 25 * time.Minute
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
				return err
			}
			if err := idle.Wait(); err != nil {
				return err
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
