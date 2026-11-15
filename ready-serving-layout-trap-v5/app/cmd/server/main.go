package main

import (
	"log"

	"ready-serving-layout-trap-v5/app/internal/bootstrap"
	"ready-serving-layout-trap-v5/app/internal/httpapi"
)

func main() {
	state, err := bootstrap.LoadAll()
	if err != nil {
		log.Fatalf("bootstrap failed: %v", err)
	}

	server := httpapi.NewServer("0.0.0.0:8000", state)
	log.Printf("ready-serving-layout-trap-v5 listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil {
		log.Fatalf("server exited: %v", err)
	}
}
