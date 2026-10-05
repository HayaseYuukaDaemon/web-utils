package main

import (
	"log/slog"
	"net/http"
)

type VaultApp struct {
	mux *http.ServeMux
}

func (a *VaultApp) GetMux() *http.ServeMux {
	return a.mux
}

func NewVaultApp() *VaultApp {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /", nil)

	slog.Info("Vault app initialized")
	return &VaultApp{
		mux: mux,
	}
}
