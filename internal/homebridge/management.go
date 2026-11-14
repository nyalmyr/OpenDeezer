package homebridge

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"io"
	"net/http"
	"strings"
)

func (s *Server) manageAccount(w http.ResponseWriter, r *http.Request) {
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
		Token string `json:"profile_token"`
		ARL   string `json:"arl"`
	}
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	dec.DisallowUnknownFields()
	if dec.Decode(&in) != nil || dec.Decode(new(any)) != io.EOF {
		problem(w, 400, "invalid_input")
		return
	}
	key := sha256.Sum256([]byte(in.Token))
	s.accountsMu.RLock()
	p := s.accounts[key]
	s.accountsMu.RUnlock()
	if p == nil {
		for _, stored := range e.managed {
			if stored.Token == in.Token && stored.Disabled && r.URL.Path == "/admin/revoke" {
				jsonReply(w, 200, map[string]any{"updated": true, "revoked": true, "profile_id": stored.ID})
				return
			}
		}
		problem(w, 404, "profile_not_found")
		return
	}
	p.loginMu.Lock()
	uid := p.Client.Account().UserID
	p.loginMu.Unlock()
	if uid == "" {
		uid = e.identities[p.ID]
	}
	for _, stored := range e.managed {
		if stored.ID == p.ID && stored.UserID != "" {
			uid = stored.UserID
		}
	}
	item := ManagedProfile{ID: p.ID, Label: p.Label, Token: in.Token, UserID: uid}
	var replacement Client
	if r.URL.Path == "/admin/reconnect" {
		in.ARL = strings.TrimSpace(in.ARL)
		if len(in.ARL) < 32 || len(in.ARL) > 1024 || strings.ContainsAny(in.ARL, " \r\n\t") {
			problem(w, 400, "invalid_input")
			return
		}
		replacement = e.factory(in.ARL)
		if replacement.Login() != nil || !replacement.Account().LoggedIn {
			problem(w, 422, "invalid_session")
			return
		}
		if uid == "" {
			problem(w, 409, "original_identity_unknown")
			return
		}
		if replacement.Account().UserID != uid {
			problem(w, 409, "different_deezer_account")
			return
		}
		item.ARL = in.ARL
	} else {
		item.Disabled = true
	}
	next := make([]ManagedProfile, 0, len(e.managed)+1)
	for _, stored := range e.managed {
		if stored.ID != p.ID {
			next = append(next, stored)
		}
	}
	next = append(next, item)
	raw, _ := json.Marshal(next)
	if writePrivateAtomic(e.path, raw) != nil {
		problem(w, 500, "storage_failed")
		return
	}
	e.managed = next
	s.accountsMu.Lock()
	if item.Disabled {
		delete(s.accounts, key)
	} else {
		s.accounts[key] = &profile{Profile: Profile{ID: p.ID, Label: p.Label, Client: replacement}, ready: true, calls: make(chan struct{}, 4), streams: make(chan struct{}, 2)}
	}
	s.accountsMu.Unlock()
	// Invalidate pending stream tickets; an already playing response is allowed to finish.
	s.mu.Lock()
	for k, lease := range s.leases {
		if lease.owner == p {
			delete(s.leases, k)
		}
	}
	s.mu.Unlock()
	jsonReply(w, 200, map[string]any{"updated": true, "revoked": item.Disabled, "profile_id": p.ID})
}

// Persist a verified account identity once, allowing safe renewal after session expiry/restart.
func (s *Server) rememberIdentity(id, uid string) {
	e := s.enrollment
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.identities[id] == uid {
		return
	}
	next := make(map[string]string, len(e.identities)+1)
	for k, v := range e.identities {
		next[k] = v
	}
	next[id] = uid
	raw, _ := json.Marshal(next)
	if writePrivateAtomic(e.path+".identities.json", raw) == nil {
		e.identities = next
	}
}
