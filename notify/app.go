package notify

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"maps"
	"net/http"
	"slices"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type NotifyApp struct {
	mux       *http.ServeMux
	notifiers map[string]Notifier
}

type NotifyType string

const NotifyTypeQQ NotifyType = "qq"

type NotifyRequest struct {
	Title string `json:"title"`
	Body  string `json:"body,omitempty"`
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

func (a *NotifyApp) AppName() string {
	return "notify"
}

func (a *NotifyApp) RegisterMCPTools(m *mcp.Server) error {
	type input struct {
		ID string `json:"id" jsonschema:"Notifier instance ID (for example qq)"`
		NotifyRequest
	}
	mcp.AddTool(m, &mcp.Tool{
		Name:        "notify_send",
		Description: "Send a notification with a title and optional body to the specified notifier.",
	}, func(ctx context.Context, _ *mcp.CallToolRequest, in input) (*mcp.CallToolResult, any, error) {
		notifier, ok := a.notifiers[in.ID]
		if !ok {
			return nil, nil, fmt.Errorf("no such notifier")
		}
		if in.Title == "" {
			return nil, nil, fmt.Errorf("must has a title")
		}
		if err := notifier.Notify(ctx, in.NotifyRequest); err != nil {
			return nil, nil, fmt.Errorf("error during notify: %w", err)
		}
		return nil, nil, nil
	})

	type notifierInfo struct {
		ID          string     `json:"id"`
		Type        NotifyType `json:"type"`
		Description string     `json:"description"`
	}
	type listResult struct {
		Notifiers []notifierInfo `json:"notifiers"`
	}
	mcp.AddTool(m, &mcp.Tool{
		Name:        "notify_list",
		Description: "List registered notifiers and their instance IDs for notify_send.",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
	}, func(context.Context, *mcp.CallToolRequest, struct{}) (*mcp.CallToolResult, listResult, error) {
		notifiers := make([]notifierInfo, 0, len(a.notifiers))
		for _, id := range slices.Sorted(maps.Keys(a.notifiers)) {
			n := a.notifiers[id]
			notifiers = append(notifiers, notifierInfo{ID: id, Type: n.Type(), Description: n.Desc()})
		}
		return nil, listResult{Notifiers: notifiers}, nil
	})
	return nil
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
