package homebridge

import (
	"crypto/sha256"
	"encoding/json"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func manageRequest(s *Server, action, admin, token, arl string) *httptest.ResponseRecorder {
	b, _ := json.Marshal(map[string]string{"profile_token": token, "arl": arl})
	r := httptest.NewRequest("POST", "/admin/"+action, strings.NewReader(string(b)))
	r.Header.Set("Authorization", "Bearer "+admin)
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	return w
}
func TestRenewalPreservesIdentityAndRevocationPersists(t *testing.T) {
	s, path := enrollmentFixture(t)
	w := enrollRequest(s, adminToken, "Member", strings.Repeat("a", 32))
	var got map[string]any
	json.Unmarshal(w.Body.Bytes(), &got)
	token := got["profile_token"].(string)
	if w = manageRequest(s, "reconnect", token, token, strings.Repeat("b", 32)); w.Code != 401 {
		t.Fatal(w.Code)
	}
	s.enrollment.factory = func(arl string) Client {
		uid := "123"
		if arl == strings.Repeat("b", 32) {
			uid = "456"
		}
		return &memberClient{uid: uid}
	}
	if w = manageRequest(s, "reconnect", adminToken, token, strings.Repeat("b", 32)); w.Code != 409 {
		t.Fatal(w.Code)
	}
	if w = manageRequest(s, "reconnect", adminToken, token, strings.Repeat("c", 32)); w.Code != 200 {
		t.Fatal(w.Code, w.Body)
	}
	if w = manageRequest(s, "revoke", adminToken, token, ""); w.Code != 200 {
		t.Fatal(w.Code)
	}
	if w = request(s, "GET", "/v1/account", token); w.Code != 401 {
		t.Fatal(w.Code)
	}
	if w = manageRequest(s, "revoke", adminToken, token, ""); w.Code != 200 {
		t.Fatal("idempotency", w.Code)
	}
	raw, _ := os.ReadFile(path)
	if strings.Contains(string(raw), strings.Repeat("c", 32)) {
		t.Fatal("revoked ARL retained")
	}
	fresh, _, _ := fixture(t)
	if e := fresh.EnableEnrollment(adminToken, path, nil); e != nil {
		t.Fatal(e)
	}
	if fresh.accounts[sha256.Sum256([]byte(token))] != nil {
		t.Fatal("revoked account restored")
	}
}
func TestVerifiedIdentitySurvivesRestart(t *testing.T) {
	s, path := enrollmentFixture(t)
	s.rememberIdentity("alice", "123")
	fresh, _, _ := fixture(t)
	if e := fresh.EnableEnrollment(adminToken, path, nil); e != nil {
		t.Fatal(e)
	}
	if fresh.enrollment.identities["alice"] != "123" {
		t.Fatal("identity not persisted")
	}
}
