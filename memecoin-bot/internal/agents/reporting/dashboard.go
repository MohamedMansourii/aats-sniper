package reporting

import (
	"crypto/subtle"
	"encoding/json"
	"net/http"
	"os"
	"sync/atomic"
	"time"

	"memecoin-bot/internal/config"

	"github.com/gorilla/mux"
	"github.com/sirupsen/logrus"
)

// Server exposes the dashboard and JSON API.
type Server struct {
	cfg    config.DashboardConfig
	state  *State
	log    *logrus.Logger
	paused atomic.Bool // manual start/stop override
}

func NewServer(cfg config.DashboardConfig, state *State, log *logrus.Logger) *Server {
	return &Server{cfg: cfg, state: state, log: log}
}

// Paused reports the manual override state (the orchestrator consults this).
func (s *Server) Paused() bool { return s.paused.Load() }

func (s *Server) Handler(frontendDir string) http.Handler {
	r := mux.NewRouter()
	api := r.PathPrefix("/api").Subrouter()
	api.HandleFunc("/health", s.health).Methods(http.MethodGet)
	api.HandleFunc("/status", s.status).Methods(http.MethodGet)
	api.HandleFunc("/candidates", s.candidates).Methods(http.MethodGet)
	api.HandleFunc("/metrics", s.metrics).Methods(http.MethodGet)
	api.HandleFunc("/control/start", s.requireAuth(s.start)).Methods(http.MethodPost)
	api.HandleFunc("/control/stop", s.requireAuth(s.stop)).Methods(http.MethodPost)
	r.PathPrefix("/").Handler(http.FileServer(http.Dir(frontendDir)))
	return r
}

func (s *Server) ListenAndServe(frontendDir string) error {
	srv := &http.Server{
		Addr:              s.cfg.ListenAddress,
		Handler:           s.Handler(frontendDir),
		ReadHeaderTimeout: 5 * time.Second,
	}
	s.log.WithField("addr", s.cfg.ListenAddress).Info("dashboard listening")
	return srv.ListenAndServe()
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, map[string]string{"status": "ok"})
}

func (s *Server) status(w http.ResponseWriter, _ *http.Request) {
	snap := s.state.Snapshot()
	writeJSON(w, map[string]any{
		"mode":         snap.Mode,
		"dry_run":      snap.DryRun,
		"auto_execute": snap.AutoExecute,
		"paused":       s.paused.Load(),
		"risk":         snap.Risk,
		"uptime":       snap.UptimeSeconds,
	})
}

func (s *Server) candidates(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, s.state.Candidates())
}

func (s *Server) metrics(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, s.state.Snapshot())
}

func (s *Server) start(w http.ResponseWriter, _ *http.Request) {
	s.paused.Store(false)
	s.log.Warn("trading manually STARTED via API")
	writeJSON(w, map[string]string{"status": "started"})
}

func (s *Server) stop(w http.ResponseWriter, _ *http.Request) {
	s.paused.Store(true)
	s.log.Warn("trading manually STOPPED via API")
	writeJSON(w, map[string]string{"status": "stopped"})
}

// requireAuth gates control endpoints behind HTTP basic auth. The username
// comes from config; the password from the DASHBOARD_PASSWORD env var. If no
// password is configured the control endpoints are disabled (fail closed).
func (s *Server) requireAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		want := os.Getenv("DASHBOARD_PASSWORD")
		if want == "" || s.cfg.AuthUsername == "" {
			http.Error(w, "control endpoints disabled: set auth_username and DASHBOARD_PASSWORD", http.StatusForbidden)
			return
		}
		user, pass, ok := r.BasicAuth()
		if !ok ||
			subtle.ConstantTimeCompare([]byte(user), []byte(s.cfg.AuthUsername)) != 1 ||
			subtle.ConstantTimeCompare([]byte(pass), []byte(want)) != 1 {
			w.Header().Set("WWW-Authenticate", `Basic realm="trading-bot"`)
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next(w, r)
	}
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}
