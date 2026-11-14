// Package homebridge adapts the existing OpenDeezer engine to Music Assistant.
// It owns no player or queue: Music Assistant owns playback, including multiroom.
package homebridge

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/Cycl0o0/OpenDeezer/v3/sdk/deezer"
)

type Client interface {
	Login() error
	Account() deezer.Account
	Favorites() ([]deezer.Track, error)
	Playlists() ([]deezer.Playlist, error)
	PlaylistTracks(string) ([]deezer.Track, error)
	Track(string) (deezer.Track, error)
	Search(string) (*deezer.SearchResults, error)
	PrepareStream(string) (*deezer.StreamPlan, error)
}

type Profile struct {
	ID     string
	Label  string
	Token  string
	Client Client
}

type profile struct {
	Profile
	loginMu sync.Mutex
	ready   bool
	calls   chan struct{}
	streams chan struct{}
}
type lease struct {
	owner   *profile
	plan    *deezer.StreamPlan
	expires time.Time
}
type Server struct {
	accounts   map[[32]byte]*profile
	accountsMu sync.RWMutex
	enrollment *enrollment
	mu         sync.Mutex
	leases     map[string]lease
	download   func(context.Context, *deezer.StreamPlan, io.Writer) error
	now        func() time.Time
}

var idPattern = regexp.MustCompile(`^[0-9]{1,20}$`)
var slugPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,39}$`)

func New(profiles []Profile) (*Server, error) {
	s := &Server{accounts: make(map[[32]byte]*profile), leases: make(map[string]lease), download: deezer.DownloadTrackContext, now: time.Now}
	ids := make(map[string]bool)
	if len(profiles) == 0 || len(profiles) > 20 {
		return nil, errors.New("configure between 1 and 20 profiles")
	}
	for _, p := range profiles {
		if !slugPattern.MatchString(p.ID) || ids[p.ID] || len(p.Token) < 32 || strings.ContainsAny(p.Token, "\r\n") || p.Client == nil || p.Label == "" {
			return nil, errors.New("invalid or duplicate profile; token must have at least 32 characters")
		}
		h := sha256.Sum256([]byte(p.Token))
		if _, ok := s.accounts[h]; ok {
			return nil, errors.New("each profile needs a distinct token")
		}
		ids[p.ID] = true
		p.Token = ""
		s.accounts[h] = &profile{Profile: p, calls: make(chan struct{}, 4), streams: make(chan struct{}, 2)}
	}
	return s, nil
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if r.URL.Path == "/admin/enroll" {
		s.enroll(w, r)
		return
	}
	if r.URL.Path == "/admin/reconnect" || r.URL.Path == "/admin/revoke" {
		s.manageAccount(w, r)
		return
	}
	if r.URL.Path == "/healthz" && r.Method == http.MethodGet {
		jsonReply(w, 200, map[string]string{"status": "ok", "version": "0.3.0"})
		return
	}
	raw := r.Header.Get("Authorization")
	if !strings.HasPrefix(raw, "Bearer ") {
		problem(w, 401, "unauthorized")
		return
	}
	s.accountsMu.RLock()
	p := s.accounts[sha256.Sum256([]byte(strings.TrimPrefix(raw, "Bearer ")))]
	s.accountsMu.RUnlock()
	if p == nil {
		problem(w, 401, "unauthorized")
		return
	}
	select {
	case p.calls <- struct{}{}:
		defer func() { <-p.calls }()
	default:
		problem(w, 429, "busy")
		return
	}
	p.loginMu.Lock()
	if !p.ready {
		p.ready = p.Client.Login() == nil
	}
	ready := p.ready
	uid := p.Client.Account().UserID
	p.loginMu.Unlock()
	if ready && uid != "" && s.enrollment != nil {
		s.rememberIdentity(p.ID, uid)
	}
	if !ready {
		problem(w, 503, "account_login_failed")
		return
	}
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) < 2 || parts[0] != "v1" {
		problem(w, 404, "not_found")
		return
	}
	if r.Method != http.MethodGet && !(r.Method == http.MethodPost && parts[1] == "resolve") {
		problem(w, 405, "method_not_allowed")
		return
	}
	var data any
	var err error
	switch {
	case len(parts) == 2 && parts[1] == "account":
		a := p.Client.Account()
		data = map[string]any{"id": p.ID, "label": p.Label, "offer": a.Offer, "premium": a.Premium}
	case len(parts) == 2 && parts[1] == "playlists":
		data, err = p.Client.Playlists()
	case len(parts) == 2 && parts[1] == "favorites":
		data, err = p.Client.Favorites()
	case len(parts) == 2 && parts[1] == "search":
		q := strings.TrimSpace(r.URL.Query().Get("q"))
		if q == "" || len(q) > 240 {
			problem(w, 400, "invalid_query")
			return
		}
		data, err = p.Client.Search(q)
	case len(parts) == 4 && parts[1] == "playlists" && parts[3] == "tracks" && idPattern.MatchString(parts[2]):
		data, err = p.Client.PlaylistTracks(parts[2])
	case len(parts) == 3 && parts[1] == "tracks" && idPattern.MatchString(parts[2]):
		data, err = p.Client.Track(parts[2])
	case len(parts) == 3 && parts[1] == "resolve" && r.Method == http.MethodPost && idPattern.MatchString(parts[2]):
		s.resolve(w, r, p, parts[2])
		return
	case len(parts) == 3 && parts[1] == "audio":
		s.audio(w, r, p, parts[2])
		return
	default:
		problem(w, 404, "not_found")
		return
	}
	if err != nil {
		s.upstreamError(w, p, err)
		return
	}
	jsonReply(w, 200, data)
}

func (s *Server) upstreamError(w http.ResponseWriter, p *profile, err error) {
	if errors.Is(err, deezer.ErrARLExpired) {
		p.loginMu.Lock()
		p.ready = false
		p.loginMu.Unlock()
		problem(w, 503, "reauthentication_required")
		return
	}
	// Never serialize upstream errors: they can include signed URLs or cookies.
	problem(w, 502, "deezer_unavailable")
}

func (s *Server) resolve(w http.ResponseWriter, r *http.Request, p *profile, id string) {
	plan, err := p.Client.PrepareStream(id)
	if err != nil {
		s.upstreamError(w, p, err)
		return
	}
	if plan == nil || plan.Preview {
		problem(w, 422, "full_track_unavailable")
		return
	}
	if plan.Format != "MP3_128" && plan.Format != "MP3_64" && plan.Format != "MP3_MISC" {
		problem(w, 422, "unsupported_audio_format")
		return
	}
	// Do not let a CDN refresh silently replace a full track with a preview.
	if refresh := plan.Refresh; refresh != nil {
		plan.Refresh = func() (*deezer.StreamPlan, error) {
			fresh, e := refresh()
			if e != nil {
				return nil, e
			}
			if fresh == nil || fresh.Preview || fresh.Format != plan.Format {
				return nil, errors.New("full stream changed")
			}
			return fresh, nil
		}
	}
	var b [24]byte
	if _, err = rand.Read(b[:]); err != nil {
		problem(w, 500, "random_unavailable")
		return
	}
	key := hex.EncodeToString(b[:])
	s.mu.Lock()
	count := 0
	for k, v := range s.leases {
		if !v.expires.After(s.now()) {
			delete(s.leases, k)
		} else if v.owner == p {
			count++
		}
	}
	if count >= 32 {
		s.mu.Unlock()
		problem(w, 429, "too_many_pending_streams")
		return
	}
	s.leases[key] = lease{owner: p, plan: plan, expires: s.now().Add(2 * time.Minute)}
	s.mu.Unlock()
	jsonReply(w, 200, map[string]any{"lease": key, "format": plan.Format, "preview": false, "expires_in": 120})
}

func (s *Server) audio(w http.ResponseWriter, r *http.Request, p *profile, key string) {
	s.mu.Lock()
	l, ok := s.leases[key]
	if ok && l.owner == p {
		delete(s.leases, key)
	}
	s.mu.Unlock()
	if !ok || l.owner != p || !l.expires.After(s.now()) {
		problem(w, 404, "stream_expired_or_unknown")
		return
	}
	if r.Header.Get("Range") != "" {
		problem(w, 416, "seeking_not_supported")
		return
	}
	select {
	case p.streams <- struct{}{}:
		defer func() { <-p.streams }()
	default:
		problem(w, 429, "stream_limit")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Minute)
	defer cancel()
	// Only declare HTTP success once the first actual audio bytes arrive.
	sink := &audioWriter{w: w}
	err := s.download(ctx, l.plan, sink)
	if err != nil {
		if !sink.started {
			problem(w, 502, "audio_transfer_failed")
			return
		}
		panic(http.ErrAbortHandler) // truncated audio must not look like a normal EOF
	}
	if !sink.started {
		problem(w, 502, "empty_audio")
	}
}

type audioWriter struct {
	w       http.ResponseWriter
	started bool
}

func (a *audioWriter) Write(b []byte) (int, error) {
	if len(b) == 0 {
		return 0, nil
	}
	if !a.started {
		a.w.Header().Set("Content-Type", "audio/mpeg")
		a.w.WriteHeader(200)
		a.started = true
	}
	_ = http.NewResponseController(a.w).SetWriteDeadline(time.Now().Add(20 * time.Second))
	n, err := a.w.Write(b)
	if f, ok := a.w.(http.Flusher); ok {
		f.Flush()
	}
	return n, err
}
func problem(w http.ResponseWriter, code int, message string) {
	jsonReply(w, code, map[string]string{"error": message})
}
func jsonReply(w http.ResponseWriter, code int, data any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(data)
}
