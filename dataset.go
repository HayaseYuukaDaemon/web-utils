package main

import (
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

func (a *DatasetApp) initDB(dbPath string) {
	// Initialize the database
}

func NewDatasetApp(dbPath string) *DatasetApp {
	mux := http.NewServeMux()
	app := &DatasetApp{
		mux: mux,
	}
	slog.Info("Dataset app initialized")
	return app
}
