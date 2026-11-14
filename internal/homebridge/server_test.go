package homebridge

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Cycl0o0/OpenDeezer/v3/sdk/deezer"
)

type fakeClient struct {
	name           string
	preview        bool
	err            error
	logins         int
	refreshPreview bool
}

func (c *fakeClient) Login() error { c.logins++; return c.err }
func (c *fakeClient) Account() deezer.Account {
	return deezer.Account{Name: c.name, Offer: "Deezer Free"}
}
func (c *fakeClient) Favorites() ([]deezer.Track, error) {
	return []deezer.Track{{ID: "1", Name: c.name}}, c.err
}
func (c *fakeClient) Playlists() ([]deezer.Playlist, error) {
	return []deezer.Playlist{{ID: "2", Name: c.name}}, c.err
}
func (c *fakeClient) PlaylistTracks(string) ([]deezer.Track, error) { return c.Favorites() }
func (c *fakeClient) Track(id string) (deezer.Track, error) {
	return deezer.Track{ID: id, Name: c.name, DurationMS: 180000}, c.err
}
func (c *fakeClient) Search(string) (*deezer.SearchResults, error) {
	return &deezer.SearchResults{Tracks: []deezer.Track{{ID: "1", Name: c.name}}}, c.err
}
func (c *fakeClient) PrepareStream(id string) (*deezer.StreamPlan, error) {
	return &deezer.StreamPlan{TrackID: id, Format: "MP3_128", Preview: c.preview, CDNURL: "SECRET_URL", Refresh: func() (*deezer.StreamPlan, error) {
		return &deezer.StreamPlan{Format: "MP3_128", Preview: c.refreshPreview}, nil
	}}, c.err
}

const tokenA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const tokenB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

func fixture(t *testing.T) (*Server, *fakeClient, *fakeClient) {
	t.Helper()
	a, b := &fakeClient{name: "Alice"}, &fakeClient{name: "Bob"}
	s, e := New([]Profile{{ID: "alice", Label: "Alice", Token: tokenA, Client: a}, {ID: "bob", Label: "Bob", Token: tokenB, Client: b}})
	if e != nil {
		t.Fatal(e)
	}
	s.download = func(_ context.Context, _ *deezer.StreamPlan, w io.Writer) error {
		_, e := w.Write([]byte("audio"))
		return e
	}
	return s, a, b
}
func request(s *Server, method, path, token string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, nil)
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	return w
}
func resolve(t *testing.T, s *Server, token string) string {
	t.Helper()
	w := request(s, "POST", "/v1/resolve/1", token)
	if w.Code != 200 {
		t.Fatalf("resolve %d %s", w.Code, w.Body)
	}
	var out struct {
		Lease string `json:"lease"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	return out.Lease
}
func TestUnauthenticatedCannotAccessOrTriggerLogin(t *testing.T) {
	s, a, _ := fixture(t)
	for _, tok := range []string{"", "wrong"} {
		if w := request(s, "GET", "/v1/playlists", tok); w.Code != 401 {
			t.Fatal(w.Code)
		}
	}
	if a.logins != 0 {
		t.Fatal("auth attempted before access check")
	}
}
func TestAccountLibrariesAreIsolated(t *testing.T) {
	s, _, _ := fixture(t)
	for _, x := range []struct{ token, name, other string }{{tokenA, "Alice", "Bob"}, {tokenB, "Bob", "Alice"}} {
		w := request(s, "GET", "/v1/playlists", x.token)
		if w.Code != 200 || !strings.Contains(w.Body.String(), x.name) || strings.Contains(w.Body.String(), x.other) {
			t.Fatal(w.Body)
		}
	}
}
func TestLeaseCannotBeUsedByOtherAccountAndIsSingleUse(t *testing.T) {
	s, _, _ := fixture(t)
	key := resolve(t, s, tokenA)
	if w := request(s, "GET", "/v1/audio/"+key, tokenB); w.Code != 404 {
		t.Fatal(w.Code)
	}
	w := request(s, "GET", "/v1/audio/"+key, tokenA)
	if w.Code != 200 || w.Body.String() != "audio" {
		t.Fatal(w.Body)
	}
	if w = request(s, "GET", "/v1/audio/"+key, tokenA); w.Code != 404 {
		t.Fatal("replay allowed")
	}
}
func TestExpiredLeaseRefused(t *testing.T) {
	s, _, _ := fixture(t)
	now := time.Now()
	s.now = func() time.Time { return now }
	key := resolve(t, s, tokenA)
	now = now.Add(3 * time.Minute)
	if w := request(s, "GET", "/v1/audio/"+key, tokenA); w.Code != 404 {
		t.Fatal(w.Code)
	}
}
func TestPreviewNeverPresentedAsFullTrack(t *testing.T) {
	s, a, _ := fixture(t)
	a.preview = true
	w := request(s, "POST", "/v1/resolve/1", tokenA)
	if w.Code != 422 || !strings.Contains(w.Body.String(), "full_track_unavailable") {
		t.Fatal(w.Body)
	}
}
func TestRefreshCannotDowngradeToPreview(t *testing.T) {
	s, a, _ := fixture(t)
	a.refreshPreview = true
	key := resolve(t, s, tokenA)
	if _, e := s.leases[key].plan.Refresh(); e == nil {
		t.Fatal("preview accepted")
	}
}
func TestUpstreamSecretsAreNotReturned(t *testing.T) {
	s, a, _ := fixture(t)
	_ = request(s, "GET", "/v1/account", tokenA)
	a.err = errors.New("secret ARL CDN URL")
	w := request(s, "GET", "/v1/playlists", tokenA)
	if w.Code != 502 || strings.Contains(w.Body.String(), "secret") {
		t.Fatal(w.Body)
	}
}
func TestResolveDoesNotLeakSignedURL(t *testing.T) {
	s, _, _ := fixture(t)
	w := request(s, "POST", "/v1/resolve/1", tokenA)
	if strings.Contains(w.Body.String(), "SECRET_URL") {
		t.Fatal(w.Body)
	}
}
func TestPendingLeasesBoundedPerAccount(t *testing.T) {
	s, _, _ := fixture(t)
	for i := 0; i < 32; i++ {
		resolve(t, s, tokenA)
	}
	if w := request(s, "POST", "/v1/resolve/1", tokenA); w.Code != 429 {
		t.Fatal(w.Code)
	}
	resolve(t, s, tokenB)
}
func TestInvalidIDsAndMethods(t *testing.T) {
	s, _, _ := fixture(t)
	for _, path := range []string{"/v1/tracks/no", "/v1/resolve/abc", "/v1/playlists/x/tracks", "/v1/unknown"} {
		if w := request(s, "GET", path, tokenA); w.Code != 404 {
			t.Fatal(path, w.Code)
		}
	}
	if w := request(s, "DELETE", "/v1/playlists", tokenA); w.Code != 405 {
		t.Fatal(w.Code)
	}
}
func TestEmptyStreamFailsBeforeHTTP200(t *testing.T) {
	s, _, _ := fixture(t)
	s.download = func(context.Context, *deezer.StreamPlan, io.Writer) error { return nil }
	key := resolve(t, s, tokenA)
	if w := request(s, "GET", "/v1/audio/"+key, tokenA); w.Code != 502 {
		t.Fatal(w.Code)
	}
}
func TestPartialStreamFailureAbortsConnection(t *testing.T) {
	s, _, _ := fixture(t)
	s.download = func(_ context.Context, _ *deezer.StreamPlan, w io.Writer) error {
		_, _ = w.Write([]byte("partial"))
		return errors.New("failure")
	}
	key := resolve(t, s, tokenA)
	defer func() {
		if recover() != http.ErrAbortHandler {
			t.Error("truncation not signaled")
		}
	}()
	request(s, "GET", "/v1/audio/"+key, tokenA)
}
func TestClientCancellationPropagatesToDownload(t *testing.T) {
	s, _, _ := fixture(t)
	key := resolve(t, s, tokenA)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	s.download = func(ctx context.Context, _ *deezer.StreamPlan, _ io.Writer) error {
		if ctx.Err() == nil {
			t.Error("context not cancelled")
		}
		return ctx.Err()
	}
	r := httptest.NewRequest("GET", "/v1/audio/"+key, nil).WithContext(ctx)
	r.Header.Set("Authorization", "Bearer "+tokenA)
	s.ServeHTTP(httptest.NewRecorder(), r)
}
func TestConfigurationRejectsSharedOrWeakTokens(t *testing.T) {
	for _, tok := range []string{tokenA, "short"} {
		_, e := New([]Profile{{ID: "alice", Label: "Alice", Token: tokenA, Client: &fakeClient{}}, {ID: "bob", Label: "Bob", Token: tok, Client: &fakeClient{}}})
		if e == nil {
			t.Fatal("unsafe config accepted")
		}
	}
}
