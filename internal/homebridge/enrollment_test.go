package homebridge

import (
	"encoding/json"
	"github.com/Cycl0o0/OpenDeezer/v3/sdk/deezer"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type memberClient struct {
	fakeClient
	uid string
}

func (c *memberClient) Account() deezer.Account {
	return deezer.Account{UserID: c.uid, Name: c.name, LoggedIn: c.uid != ""}
}

const adminToken = "admin-only-credential-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

func enrollRequest(s *Server, token, label, arl string) *httptest.ResponseRecorder {
	body, _ := json.Marshal(map[string]string{"label": label, "arl": arl})
	r := httptest.NewRequest("POST", "/admin/enroll", strings.NewReader(string(body)))
	r.Header.Set("Authorization", "Bearer "+token)
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	return w
}
func enrollmentFixture(t *testing.T) (*Server, string) {
	s, _, _ := fixture(t)
	path := filepath.Join(t.TempDir(), "profiles.json")
	if e := s.EnableEnrollment(adminToken, path, nil); e != nil {
		t.Fatal(e)
	}
	s.enrollment.factory = func(arl string) Client {
		uid := "123"
		if arl == strings.Repeat("z", 32) {
			uid = ""
		}
		return &memberClient{fakeClient: fakeClient{name: "Member"}, uid: uid}
	}
	return s, path
}
func TestEnrollmentPrivilegeAndValidation(t *testing.T) {
	s, path := enrollmentFixture(t)
	if w := enrollRequest(s, tokenA, "New", strings.Repeat("a", 32)); w.Code != 401 {
		t.Fatal(w.Code)
	}
	if w := enrollRequest(s, adminToken, "New", "short"); w.Code != 400 {
		t.Fatal(w.Code)
	}
	if w := enrollRequest(s, adminToken, "New", strings.Repeat("z", 32)); w.Code != 422 {
		t.Fatal(w.Code)
	}
	if _, e := os.Stat(path); !os.IsNotExist(e) {
		t.Fatal("invalid enrollment persisted")
	}
}
func TestEnrollmentPersistsIsolatesAndDeduplicates(t *testing.T) {
	s, path := enrollmentFixture(t)
	arl := strings.Repeat("a", 32)
	w := enrollRequest(s, adminToken, "New", arl)
	if w.Code != 201 {
		t.Fatal(w.Code, w.Body)
	}
	var got map[string]any
	json.Unmarshal(w.Body.Bytes(), &got)
	if strings.Contains(w.Body.String(), arl) {
		t.Fatal("ARL leaked")
	}
	token := got["profile_token"].(string)
	if request(s, "GET", "/v1/account", token).Code != 200 {
		t.Fatal("new token unusable")
	}
	before, _ := os.ReadFile(path)
	w = enrollRequest(s, adminToken, "Renamed", arl)
	if w.Code != 200 {
		t.Fatal(w.Code)
	}
	after, _ := os.ReadFile(path)
	if string(before) != string(after) {
		t.Fatal("duplicate mutated store")
	}
	restored, _, _ := fixture(t)
	if e := restored.EnableEnrollment(adminToken, path, nil); e != nil {
		t.Fatal(e)
	}
	restored.accountsMu.RLock()
	n := len(restored.accounts)
	restored.accountsMu.RUnlock()
	if n != 3 {
		t.Fatal(n)
	}
	if request(s, "GET", "/v1/account", adminToken).Code != 401 {
		t.Fatal("admin token used as playback credential")
	}
}
func TestEnrollmentStorageFailureDoesNotActivate(t *testing.T) {
	s, _ := enrollmentFixture(t)
	s.enrollment.path = filepath.Join(t.TempDir(), "absent", "file.json")
	w := enrollRequest(s, adminToken, "New", strings.Repeat("a", 32))
	if w.Code != 500 {
		t.Fatal(w.Code)
	}
	if len(s.accounts) != 2 {
		t.Fatal("unpersisted account active")
	}
}
func TestEnrollmentLabelConflict(t *testing.T) {
	s, _ := enrollmentFixture(t)
	if w := enrollRequest(s, adminToken, "Alice", strings.Repeat("a", 32)); w.Code != 409 {
		t.Fatal(w.Code)
	}
}
