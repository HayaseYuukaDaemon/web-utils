package main

import (
	"log/slog"
	"net/http"
)

type NotifyApp struct {
	mux    *http.ServeMux
	client *http.Client
}

func (a *NotifyApp) GetMux() *http.ServeMux {
	return a.mux
}

func NewNotifyApp(c *http.Client) *NotifyApp {
	if c == nil {
		c = http.DefaultClient
	}
	mux := http.NewServeMux()
	slog.Info("Notify app initialized")
	return &NotifyApp{mux: mux, client: c}
}
