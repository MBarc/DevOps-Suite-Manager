"""Auth route tests: login, logout, session protection."""


def test_login_page_returns_200(anon_client):
    resp = anon_client.get("/login")
    assert resp.status_code == 200
    assert "Login" in resp.text or "login" in resp.text.lower()


def test_login_success_redirects(anon_client):
    resp = anon_client.post(
        "/login",
        data={"username": "testadmin", "password": "testpass", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_login_wrong_password_returns_401(anon_client):
    resp = anon_client.post(
        "/login",
        data={"username": "testadmin", "password": "wrongpass", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert "Invalid credentials" in resp.text


def test_login_unknown_user_returns_401(anon_client):
    resp = anon_client.post(
        "/login",
        data={"username": "nobody", "password": "anything", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 401


def test_protected_route_redirects_unauthenticated(anon_client):
    resp = anon_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_hosts_requires_auth(anon_client):
    resp = anon_client.get("/hosts", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_docs_requires_auth(anon_client):
    resp = anon_client.get("/docs", follow_redirects=False)
    assert resp.status_code == 303


def test_logout_redirects_to_login(auth_client):
    resp = auth_client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_authenticated_dashboard_returns_200(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 200


def test_session_persists_across_requests(auth_client):
    r1 = auth_client.get("/hosts")
    r2 = auth_client.get("/docs")
    assert r1.status_code == 200
    assert r2.status_code == 200
