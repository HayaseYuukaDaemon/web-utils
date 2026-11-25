package main

import (
	"context"
	"database/sql"
	"log/slog"
	"net/http"
)

type DatasetApp struct {
	mux *http.ServeMux
	db  *sql.DB
}

func (a *DatasetApp) GetMux() *http.ServeMux {
	return a.mux
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
