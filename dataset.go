package main

import (
	"context"
	"database/sql"
	"log/slog"
	"net/http"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type DatasetApp struct {
	mux *http.ServeMux
	db  *sql.DB
}

func (a *DatasetApp) GetMux() *http.ServeMux {
	return a.mux
}

func (a *DatasetApp) AppName() string {
	return "dataset"
}

func (a *DatasetApp) RegisterMCPTools(m *mcp.Server) error {
	return nil
}

func NewDatasetApp(ctx context.Context, db *sql.DB) (*DatasetApp, error) {
	// 验证数据库schema

	mux := http.NewServeMux()
	app := &DatasetApp{
		mux: mux,
		db:  db,
	}
	slog.Info("Dataset app initialized")
	return app, nil
}
