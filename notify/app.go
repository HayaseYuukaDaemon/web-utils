package notify

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
)

type NotifyApp struct {
	mux       *http.ServeMux
	notifiers map[string]Notifier
}

type NotifyType string

const NotifyTypeQQ NotifyType = "qq"

type NotifyRequest struct {
	Title string `json:"title"`
	Body  string `json:"body"`
}

type Notifier interface {
	Type() NotifyType
	InstanceID() string
	Desc() string
	Notify(ctx context.Context, req NotifyRequest) error
}

func (a *NotifyApp) GetMux() *http.ServeMux {
	return a.mux
}

func (a *NotifyApp) RegisterNotifier[NC Notifier](nc NC) error {
	_, ok := a.notifiers[nc.InstanceID()]
	if ok {
		return fmt.Errorf("this id alreay registered")
	}
	a.notifiers[nc.InstanceID()] = nc
	return nil
}

func NewNotifyApp(notifiers []Notifier) (*NotifyApp, error) {
	mux := http.NewServeMux()
	app := &NotifyApp{mux: mux, notifiers: map[string]Notifier{}}

	for _, notifier := range notifiers {
		if err := app.RegisterNotifier(notifier); err != nil {
			return nil, err
		}
	}

	mux.HandleFunc("POST /{id}", func(w http.ResponseWriter, r *http.Request) {
		notifierID := r.PathValue("id")
		if notifierID == "" {
			http.Error(w, "no notifier id", 400)
			return
		}
		notifier, ok := app.notifiers[notifierID]
		if !ok {
			http.Error(w, "no such notifier", 404)
			return
		}
		var notifyReq NotifyRequest
		if err := json.NewDecoder(r.Body).Decode(&notifyReq); err != nil {
			http.Error(w, "failed to build notify req: "+err.Error(), 400)
			return
		}
		if notifyReq.Title == "" {
			http.Error(w, "must has a title", 400)
			return
		}
		if err := notifier.Notify(r.Context(), notifyReq); err != nil {
			http.Error(w, "error during notify: "+err.Error(), 500)
			return
		}
		w.WriteHeader(200)
	})

	slog.Info("Notify app initialized")
	return app, nil
}
