package homebridge

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"unicode"

	"github.com/Cycl0o0/OpenDeezer/v3/sdk/deezer"
)

// ManagedProfile contains secrets and must only be stored in the private data directory.
type ManagedProfile struct {
	ID       string `json:"id"`
	Label    string `json:"label"`
	ARL      string `json:"arl"`
	Token    string `json:"token"`
	UserID   string `json:"user_id,omitempty"`
	Disabled bool   `json:"disabled,omitempty"`
}
type enrollment struct {
	mu         sync.Mutex
	admin      [32]byte
	path       string
	managed    []ManagedProfile
	identities map[string]string
	tokens     map[string]string
	factory    func(string) Client
}

// EnableEnrollment is called before serving requests. Original profiles stay untouched.
func (s *Server) EnableEnrollment(admin, path string, original []Profile) error {
	if len(admin) < 32 || path == "" {
		return errors.New("invalid enrollment configuration")
	}
	e := &enrollment{admin: sha256.Sum256([]byte(admin)), path: path, tokens: map[string]string{}, identities: map[string]string{}, factory: func(arl string) Client {
		c := deezer.New(arl)
		c.SetQuality(deezer.QualityNormal)
		return c
	}}
	for _, p := range original {
		e.tokens[p.ID] = p.Token
	}
	if ids, err := os.ReadFile(path + ".identities.json"); err == nil {
		if json.Unmarshal(ids, &e.identities) != nil || e.identities == nil {
			return errors.New("profile identities invalid")
		}
	} else if !os.IsNotExist(err) {
		return errors.New("profile identities unreadable")
	}
	raw, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return errors.New("managed profiles unreadable")
	}
	if err == nil {
		if json.Unmarshal(raw, &e.managed) != nil {
			return errors.New("managed profiles invalid")
		}
		for _, p := range e.managed {
			if !slugPattern.MatchString(p.ID) || len(p.Token) < 32 || (!p.Disabled && p.ARL == "") || p.Label == "" || (e.tokens[p.ID] != "" && e.tokens[p.ID] != p.Token) {
				return errors.New("managed profile invalid")
			}
			h := sha256.Sum256([]byte(p.Token))
			if s.accounts[h] != nil && s.accounts[h].ID != p.ID {
				return errors.New("duplicate managed token")
			}
			if p.Disabled {
				delete(s.accounts, h)
			} else {
				s.accounts[h] = &profile{Profile: Profile{ID: p.ID, Label: p.Label, Client: e.factory(p.ARL)}, calls: make(chan struct{}, 4), streams: make(chan struct{}, 2)}
			}
			e.tokens[p.ID] = p.Token
		}
	}
	if len(s.accounts) > 20 {
		return errors.New("too many profiles")
	}
	s.enrollment = e
	return nil
}

func (s *Server) enroll(w http.ResponseWriter, r *http.Request) {
	e := s.enrollment
	if e == nil {
		problem(w, 404, "enrollment_disabled")
		return
	}
	h := sha256.Sum256([]byte(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")))
	if subtle.ConstantTimeCompare(h[:], e.admin[:]) != 1 {
		problem(w, 401, "unauthorized")
		return
	}
	if r.Method != http.MethodPost {
		problem(w, 405, "method_not_allowed")
		return
	}
	if !e.mu.TryLock() {
		problem(w, 429, "enrollment_busy")
		return
	}
	defer e.mu.Unlock()
	var in struct {
		Label string `json:"label"`
		ARL   string `json:"arl"`
	}
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	dec.DisallowUnknownFields()
	if dec.Decode(&in) != nil || dec.Decode(new(any)) != io.EOF {
		problem(w, 400, "invalid_input")
		return
	}
	in.Label = strings.TrimSpace(in.Label)
	in.ARL = strings.TrimSpace(in.ARL)
	if len([]rune(in.Label)) < 1 || len([]rune(in.Label)) > 40 || strings.IndexFunc(in.Label, unicode.IsControl) >= 0 || len(in.ARL) < 32 || len(in.ARL) > 1024 || strings.ContainsAny(in.ARL, " \r\n\t") {
		problem(w, 400, "invalid_input")
		return
	}
	c := e.factory(in.ARL)
	if c.Login() != nil || !c.Account().LoggedIn || c.Account().UserID == "" {
		problem(w, 422, "invalid_session")
		return
	}
	s.accountsMu.RLock()
	existing := make([]*profile, 0, len(s.accounts))
	for _, p := range s.accounts {
		existing = append(existing, p)
	}
	s.accountsMu.RUnlock()
	for _, p := range existing {
		p.loginMu.Lock()
		if !p.ready {
			p.ready = p.Client.Login() == nil
		}
		uid := p.Client.Account().UserID
		p.loginMu.Unlock()
		if uid == c.Account().UserID {
			jsonReply(w, 200, map[string]any{"id": p.ID, "label": p.Label, "profile_token": e.tokens[p.ID], "existing": true})
			return
		}
		if strings.EqualFold(p.Label, in.Label) {
			problem(w, 409, "label_in_use")
			return
		}
	}
	if len(existing) >= 20 {
		problem(w, 409, "profile_limit")
		return
	}
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		problem(w, 500, "random_unavailable")
		return
	}
	token := hex.EncodeToString(b[:])
	id := "member_" + token[:12]
	item := ManagedProfile{ID: id, Label: in.Label, ARL: in.ARL, Token: token, UserID: c.Account().UserID}
	next := append(append([]ManagedProfile{}, e.managed...), item)
	raw, err := json.Marshal(next)
	if err == nil {
		err = writePrivateAtomic(e.path, raw)
	}
	if err != nil {
		problem(w, 500, "storage_failed")
		return
	}
	e.managed = next
	e.tokens[id] = token
	s.accountsMu.Lock()
	s.accounts[sha256.Sum256([]byte(token))] = &profile{Profile: Profile{ID: id, Label: in.Label, Client: c}, ready: true, calls: make(chan struct{}, 4), streams: make(chan struct{}, 2)}
	s.accountsMu.Unlock()
	jsonReply(w, 201, map[string]any{"id": id, "label": in.Label, "profile_token": token, "existing": false})
}

func writePrivateAtomic(path string, data []byte) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".profiles-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if err = f.Chmod(0600); err == nil {
		_, err = f.Write(data)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}
