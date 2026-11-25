package main

import (
	"context"
	"encoding/json"
	"fmt"
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

func validateVaultSchema(ctx context.Context, db *sql.DB) error {
	const table = "vault_configs"
	if db == nil {
		return fmt.Errorf("validate %s: nil database", table)
	}

	type column struct {
		Name    string
		Type    string
		NotNull bool
		PK      int // 0 表示非主键，1 表示主键中的第一列
		Hidden  int // 0 表示普通列
	}
	want := []column{
		{Name: "name", Type: "TEXT", PK: 1},
		{Name: "platform", Type: "TEXT", NotNull: true},
		{Name: "symbols", Type: "TEXT", NotNull: true},
		{Name: "length", Type: "INTEGER", NotNull: true},
	}

	const query = `
		SELECT x.name, upper(x.type), x."notnull", x.pk, x.hidden
		FROM pragma_table_list AS t
		CROSS JOIN pragma_table_xinfo(t.name, t.schema) AS x
		WHERE t.schema = 'main' AND t.name = ? AND t.type = 'table'
		ORDER BY x.cid`

	rows, err := db.QueryContext(ctx, query, table)
	if err != nil {
		return fmt.Errorf("inspect %s: %w", table, err)
	}
	defer rows.Close()

	i := 0
	for rows.Next() {
		var got column
		if err := rows.Scan(&got.Name, &got.Type, &got.NotNull, &got.PK, &got.Hidden); err != nil {
			return fmt.Errorf("read %s column %d: %w", table, i+1, err)
		}
		if i >= len(want) {
			return fmt.Errorf("%s: unexpected column %q at position %d", table, got.Name, i+1)
		}
		if got != want[i] {
			return fmt.Errorf("%s column %d: got %+v, want %+v", table, i+1, got, want[i])
		}
		i++
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("iterate %s columns: %w", table, err)
	}
	if i == 0 {
		return fmt.Errorf("expected an ordinary table named main.%s", table)
	}
	if i < len(want) {
		return fmt.Errorf("%s: missing column %q at position %d", table, want[i].Name, i+1)
	}
	return nil
}

func NewVaultApp(ctx context.Context, db *sql.DB) (*VaultApp, error) {
	if err := validateVaultSchema(ctx, db); err != nil {
		return nil, err
	}

	mux := http.NewServeMux()
	app := &VaultApp{
		mux: mux,
		db:  db,
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
		if rows.Err() != nil {
			http.Error(w, rows.Err().Error(), 500)
			return
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
	return app, nil
}
