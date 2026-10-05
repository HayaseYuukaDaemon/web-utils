package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"slices"
	"strings"
	"syscall"
	"time"

	"github.com/pterm/pterm"
	"gopkg.in/yaml.v3"
)

func parseLogLevel(s string) (pterm.LogLevel, error) {
	if s == "" {
		return pterm.LogLevelInfo, nil
	}
	switch s {
	case "DEBUG":
		return pterm.LogLevelDebug, nil
	case "INFO":
		return pterm.LogLevelInfo, nil
	case "WARN":
		return pterm.LogLevelWarn, nil
	}
	return 0, fmt.Errorf("Invalid level")
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

type Middleware struct {
	mux       *http.ServeMux
	authToken string
}

func (m Middleware) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if m.authToken != "" {
		reqAuthToken, found := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
		if !found {
			queryToken := r.URL.Query().Get("token")
			if queryToken != m.authToken {
				http.Error(w, "Invalid authorization token", 401)
				return
			}
		} else {
			if reqAuthToken != m.authToken {
				http.Error(w, "Invalid authorization token", 401)
				return
			}
		}
	}
	m.mux.ServeHTTP(w, r)
}

type UtilApp interface {
	GetMux() *http.ServeMux
}

func main() {
	level, err := parseLogLevel(os.Getenv("LOG_LEVEL"))
	if err != nil {
		panic(err)
	}
	logOpt := pterm.DefaultLogger
	logOpt.Level = level
	slog.SetDefault(slog.New(pterm.NewSlogHandler(&logOpt)))
	slog.Debug("Debug start")
	slog.Info("Start")

	if len(os.Args) > 1 {
		if slices.Contains(os.Args, "test-cia") {
			var c yaml.Node
			f, err := os.Open("cia.yaml")
			if err != nil {
				panic(err)
			}
			defer f.Close()
			if err := yaml.NewDecoder(f).Decode(&c); err != nil {
				panic(err)
			}
			pf, err := os.Open("patch.yaml")
			if err != nil {
				panic(err)
			}
			defer pf.Close()
			var p ProviderPatch
			if err := yaml.NewDecoder(pf).Decode(&p); err != nil {
				panic(err)
			}
			pp, err := ProcessCIAProxy(&c, &p)
			if err != nil {
				panic(err)
			}
			if err := os.WriteFile("out.yaml", pp, 0o600); err != nil {
				panic(err)
			}
		}
		return
	}

	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	proxyApp := NewProxyApp(&http.Client{Transport: transport})

	mux := http.NewServeMux()
	mux.Handle("GET /proxy/", http.StripPrefix("/proxy", proxyApp.mux))

	middleware := Middleware{
		mux: mux,
		authToken: func() string {
			if os.Getenv("AUTH_TOKEN") != "" {
				return os.Getenv("AUTH_TOKEN")
			}
			return ""
		}(),
	}

	server := &http.Server{
		Addr: func() string {
			if os.Getenv("LISTEN_ADDR") != "" {
				return os.Getenv("LISTEN_ADDR")
			} else {
				return "0.0.0.0:8080"
			}
		}(),
		Handler:           middleware,
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		slog.Info("service listening", "addr", server.Addr)
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("http server failed", "error", err)
			stop()
		} else {
			slog.Info("Server close")
		}
	}()

	<-ctx.Done()

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		slog.Error("http server shutdown failed", "error", err)
	} else {
		slog.Info("Shutdown")
	}
}
