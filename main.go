package main

import (
	"context"
	"database/sql"
	_ "embed"
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

	"github.com/modelcontextprotocol/go-sdk/mcp"
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
	w.WriteHeader(status)
	if value != nil {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(value)
	}
}

type Middleware struct {
	mux          *http.ServeMux
	db           *sql.DB
	rootToken    string
	deployDomain string
}

func (m Middleware) BindRoutes() error {
	return nil
}

type SubjectID struct{}

func (m Middleware) auth(r *http.Request) (string, error) {
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

	if token == m.rootToken {
		return "root", nil
	}

	var subjectID string
	err := m.db.QueryRowContext(r.Context(), "SELECT subject_id FROM auth_records WHERE token = ?", token).Scan(&subjectID)
	if err != nil {
		return "", fmt.Errorf("failed get auth record: %w", err)
	}
	return subjectID, nil
}

func (m Middleware) logRequest(subjectID string, r *http.Request) error {
	authenticated := false
	if subjectID != "" {
		authenticated = true
	}

	_, err := m.db.ExecContext(r.Context(), `
		INSERT INTO auth_logs (authenticated, identity, interface)
		VALUES (?, ?, ?)`,
		authenticated, subjectID, r.Method+" "+r.URL.EscapedPath(),
	)
	if err != nil {
		slog.Error("write auth log", "error", err)
		return fmt.Errorf("audit unavailable: %w", err)
	}
	return nil
}

func (m Middleware) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	headers := w.Header()
	headers.Set("Access-Control-Allow-Origin", "*")
	headers.Set("Access-Control-Expose-Headers", "*")
	if r.Method == http.MethodOptions {
		headers.Add("Vary", "Origin")
		headers.Add("Vary", "Access-Control-Request-Method")
		headers.Add("Vary", "Access-Control-Request-Headers")
		if method := r.Header.Get("Access-Control-Request-Method"); method != "" && r.Header.Get("Origin") != "" {
			headers.Set("Access-Control-Allow-Methods", method)
			if requestedHeaders := r.Header.Get("Access-Control-Request-Headers"); requestedHeaders != "" {
				headers.Set("Access-Control-Allow-Headers", requestedHeaders)
			}
			w.WriteHeader(http.StatusNoContent)
			return
		}
	}

	if strings.HasPrefix(r.URL.Path, "/.well-known/") {
		switch r.URL.Path {
		case "/.well-known/oauth-protected-resource/api/mcp":
		case "/.well-known/oauth-authorization-server":
			// 这里期望由 nginx 等 gateway 剥离 /api/ 前缀
			writeJSON(w, 200, map[string]any{
				"issuer":                                "https://" + m.deployDomain,
				"authorization_endpoint":                fmt.Sprintf("https://%s/api/oauth/authorize", m.deployDomain),
				"token_endpoint":                        "https://fanzha.hayaseyuuka.cn/api/oauth/token",
				"response_types_supported":              []string{"code"},
				"grant_types_supported":                 []string{"authorization_code"},
				"code_challenge_methods_supported":      []string{"S256"},
				"token_endpoint_auth_methods_supported": []string{"client_secret_basic"},
			})
		default:
			w.WriteHeader(404)
		}
		return
	}

	if m.rootToken == "" {
		m.mux.ServeHTTP(w, r)
		return
	}
	subjectID, err := m.auth(r)
	auditErr := m.logRequest(subjectID, r)
	if auditErr != nil {
		writeJSON(w, 500, map[string]string{"error": "audit error", "detail": auditErr.Error()})
		return
	}
	if err != nil {
		writeJSON(w, 401, map[string]string{"error": "unauthorized", "detail": err.Error()})
		return
	}
	authCtx := context.WithValue(r.Context(), SubjectID{}, subjectID)
	m.mux.ServeHTTP(w, r.WithContext(authCtx))
}

type UtilApp interface {
	GetMux() *http.ServeMux
}

//go:embed schema.sql
var schemaSQL string

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

	if os.Getenv("ROOT_TOKEN") == "" {
		slog.Warn("no root token, allow all requests")
	}

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

	const DB_PATH = "runtime.sqlite"

	db, err := sql.Open("sqlite", DB_PATH)
	if err != nil {
		panic(fmt.Errorf("failed to open sqlite db: %w", err))
	}

	initCtx, cancel := context.WithTimeout(context.Background(), time.Second*10)
	defer cancel()

	_, err = db.ExecContext(initCtx, schemaSQL)
	if err != nil {
		panic(fmt.Errorf("failed to init db: %w", err))
	}

	proxyApp := NewProxyApp(&http.Client{Transport: nonProxyTransport})

	vaultApp, err := NewVaultApp(initCtx, db)
	if err != nil {
		panic(fmt.Errorf("failed to init vault app: %w", err))
	}

	datasetApp, err := NewDatasetApp(initCtx, db)
	if err != nil {
		panic(fmt.Errorf("failed to init dataset app: %w", err))
	}

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

	mcpApp := mcp.NewServer(&mcp.Implementation{Name: "web-utils", Version: "0.0.1"}, nil)

	mux.Handle("/mcp", mcp.NewStreamableHTTPHandler(
		func(r *http.Request) *mcp.Server { return mcpApp },
		&mcp.StreamableHTTPOptions{Stateless: true, JSONResponse: true},
	))

	middleware := Middleware{
		mux: mux,
		db:  db,
		rootToken: func() string {
			if os.Getenv("ROOT_TOKEN") != "" {
				return os.Getenv("ROOT_TOKEN")
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
