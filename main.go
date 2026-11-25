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
	"web-utils/notify"

	"github.com/pterm/pterm"
	"gopkg.in/yaml.v3"

	_ "modernc.org/sqlite"
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
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
	w.WriteHeader(status)
	if value != nil {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(value)
	}
}

type Middleware struct {
	mux       *http.ServeMux
	authToken string
}

func (m Middleware) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if m.authToken == "" {
		m.mux.ServeHTTP(w, r)
		return
	}
	token := ""
	reqAuthToken, found := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	if !found {
		cookieToken, err := r.Cookie("token")
		if err != nil {
			token = r.URL.Query().Get("token")
		} else {
			token = cookieToken.Value
		}
	} else {
		token = reqAuthToken
	}
	if token != m.authToken {
		http.Error(w, "Invalid authorization token", 401)
		return
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

	nonProxyTransport := http.DefaultTransport.(*http.Transport).Clone()
	nonProxyTransport.Proxy = nil
	proxyApp := NewProxyApp(&http.Client{Transport: nonProxyTransport})
	vaultApp := NewVaultApp("vault.db")
	datasetApp := NewDatasetApp("dataset.db")
	var notifiers []notify.Notifier
	qqMail := os.Getenv("QQ_MAIL")
	if qqMail != "" {
		authCode := os.Getenv("QQ_AUTHCODE")
		if authCode == "" {
			panic("auth code not set")
		}
		qqNotifier := notify.NewQQNotifier(qqMail, authCode, "qq")
		notifiers = append(notifiers, qqNotifier)
		slog.Info("QQ notifier registered", "mail", qqMail)
	}
	notifyApp, err := notify.NewNotifyApp(notifiers)
	if err != nil {
		panic(fmt.Errorf("failed to init notify app: %w", err))
	}

	mux := http.NewServeMux()
	mux.Handle("/proxy/", http.StripPrefix("/proxy", proxyApp.GetMux()))
	mux.Handle("/vault/", http.StripPrefix("/vault", vaultApp.GetMux()))
	mux.Handle("/dataset/", http.StripPrefix("/dataset", datasetApp.GetMux()))
	mux.Handle("/notify/", http.StripPrefix("/notify", notifyApp.GetMux()))

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
