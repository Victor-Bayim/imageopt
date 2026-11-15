package httpapi

import (
	"encoding/json"
	"net/http"
	"time"

	"ready-serving-layout-trap-v4/app/internal/bootstrap"
	"ready-serving-layout-trap-v4/app/internal/infer"
)

func NewHandler(state *bootstrap.AppState) http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/ready", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if !state.IsReady() {
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "bootstrapping"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	mux.HandleFunc("/debug/state", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		writeJSON(w, http.StatusOK, state.Snapshot())
	})

	mux.HandleFunc("/predict", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		defer r.Body.Close()

		var request infer.Request
		decoder := json.NewDecoder(r.Body)
		if err := decoder.Decode(&request); err != nil {
			http.Error(w, "invalid json body", http.StatusBadRequest)
			return
		}
		response, err := infer.Predict(state, request)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		writeJSON(w, http.StatusOK, response)
	})

	return mux
}

func NewServer(addr string, state *bootstrap.AppState) *http.Server {
	return &http.Server{
		Addr:              addr,
		Handler:           NewHandler(state),
		ReadHeaderTimeout: 5 * time.Second,
	}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
