package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"

	"database/sql"

	_ "modernc.org/sqlite"
)

type VaultApp struct {
	db  *sql.DB
	mux *http.ServeMux
}

type VaultConfig struct {
	Platform string `json:"platform"`
	Symbols  string `json:"symbols"`
	Length   int    `json:"length"`
}

func (a *VaultApp) GetMux() *http.ServeMux {
	return a.mux
}

func (a *VaultApp) initDB(ctx context.Context, path string) error {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return err
	}
	if _, err := db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS vault_configs (
			name TEXT PRIMARY KEY,
			platform TEXT NOT NULL,
			symbols TEXT NOT NULL,
			length INTEGER NOT NULL
		)`); err != nil {
		return err
	}
	a.db = db
	return nil
}

func NewVaultApp(dbPath string) *VaultApp {
	mux := http.NewServeMux()
	app := &VaultApp{
		mux: mux,
	}

	if err := app.initDB(context.Background(), dbPath); err != nil {
		panic(err)
	}

	mux.HandleFunc("GET /configs", func(w http.ResponseWriter, r *http.Request) {
		// TODO: Implement the logic to retrieve all configurations
		tx, err := app.db.BeginTx(r.Context(), &sql.TxOptions{ReadOnly: true})
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		defer tx.Rollback()
		rows, err := tx.QueryContext(r.Context(), "SELECT * FROM vault_configs")
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		defer rows.Close()
		if rows.Err() != nil {
			http.Error(w, rows.Err().Error(), 500)
			return
		}
		configs := map[string]VaultConfig{}
		for rows.Next() {
			var config VaultConfig
			var configName string
			if err := rows.Scan(&configName, &config.Platform, &config.Symbols, &config.Length); err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			configs[configName] = config
		}
		writeJSON(w, 200, configs)
	})
	mux.HandleFunc("PUT /configs/{configName}", func(w http.ResponseWriter, r *http.Request) {
		configName := r.PathValue("configName")
		if configName == "" {
			http.Error(w, "qnmd没名字", 400)
			return
		}
		var config VaultConfig
		if err := json.NewDecoder(r.Body).Decode(&config); err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		tx, err := app.db.BeginTx(r.Context(), nil)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		if _, err := tx.ExecContext(r.Context(), "INSERT INTO vault_configs (platform, symbols, length, name) VALUES (?, ?, ?, ?)", config.Platform, config.Symbols, config.Length, configName); err != nil {
			tx.Rollback()
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		if err := tx.Commit(); err != nil {
			tx.Rollback()
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.WriteHeader(200)
	})
	mux.HandleFunc("DELETE /configs/{configName}", func(w http.ResponseWriter, r *http.Request) {
		configName := r.PathValue("configName")
		if configName == "" {
			http.Error(w, "qnmd没名字", 400)
			return
		}
		tx, err := app.db.BeginTx(r.Context(), nil)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		if _, err := tx.ExecContext(r.Context(), "DELETE FROM vault_configs WHERE name = ?", configName); err != nil {
			tx.Rollback()
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		if err := tx.Commit(); err != nil {
			tx.Rollback()
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.WriteHeader(200)
	})

	slog.Info("Vault app initialized")
	return app
}
