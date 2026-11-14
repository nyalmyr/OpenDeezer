// opendeezer-home is a headless multi-account streaming bridge.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/Cycl0o0/OpenDeezer/v3/internal/homebridge"
	"github.com/Cycl0o0/OpenDeezer/v3/sdk/deezer"
)

type config struct {
	Listen   string `json:"listen"`
	Profiles []struct {
		ID        string `json:"id"`
		Label     string `json:"label"`
		ARLFile   string `json:"arl_file"`
		TokenFile string `json:"token_file"`
	} `json:"profiles"`
}

func main() {
	path := flag.String("config", "/config/config.json", "configuration file; credentials must be in separate files")
	health := flag.Bool("healthcheck", false, "check local service and exit")
	flag.Parse()
	if *health {
		c := http.Client{Timeout: 2 * time.Second}
		resp, e := c.Get("http://127.0.0.1:8731/healthz")
		if e != nil {
			os.Exit(1)
		}
		resp.Body.Close()
		if resp.StatusCode != 200 {
			os.Exit(1)
		}
		return
	}
	raw, e := os.ReadFile(*path)
	if e != nil {
		fatal("configuration unreadable")
	}
	var cfg config
	if e = json.Unmarshal(raw, &cfg); e != nil {
		fatal("configuration invalid")
	}
	if cfg.Listen == "" {
		cfg.Listen = "127.0.0.1:8731"
	}
	profiles := make([]homebridge.Profile, 0, len(cfg.Profiles))
	for _, p := range cfg.Profiles {
		arl, e := os.ReadFile(p.ARLFile)
		if e != nil {
			fatal("credential file unreadable")
		}
		token, e := os.ReadFile(p.TokenFile)
		if e != nil {
			fatal("access token file unreadable")
		}
		cl := deezer.New(strings.TrimSpace(string(arl)))
		cl.SetQuality(deezer.QualityNormal)
		profiles = append(profiles, homebridge.Profile{ID: p.ID, Label: p.Label, Token: strings.TrimSpace(string(token)), Client: cl})
	}
	handler, e := homebridge.New(profiles)
	if e != nil {
		fatal(e.Error())
	}
	if adminFile := os.Getenv("OPENDEEZER_ADMIN_TOKEN_FILE"); adminFile != "" {
		admin, err := os.ReadFile(adminFile)
		if err != nil {
			fatal("enrollment token unreadable")
		}
		if err = handler.EnableEnrollment(strings.TrimSpace(string(admin)), os.Getenv("OPENDEEZER_MANAGED_PROFILES_FILE"), profiles); err != nil {
			fatal(err.Error())
		}
	}
	server := &http.Server{Addr: cfg.Listen, Handler: handler, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(c)
	}()
	fmt.Println("OpenDeezer Home 0.3.0 ready; credentials and request paths are not logged")
	if e = server.ListenAndServe(); e != nil && e != http.ErrServerClosed {
		fatal("listener failed")
	}
}
func fatal(msg string) { fmt.Fprintln(os.Stderr, msg); os.Exit(1) }
