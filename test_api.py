import os, tempfile, threading
os.environ["ANNAM_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["ANNAM_JWT_SECRET"] = "test-secret"
import app as A

c = A.app.test_client()
H = lambda t: {"Authorization": "Bearer " + t}

def reg(email, role, ngo=None):
    r = c.post("/api/auth/register", json={"email": email, "password": "secret1", "role": role, "ngo": ngo})
    assert r.status_code == 201, r.json
    return r.json["token"]

def test_flow():
    d, n1, n2, v = reg("d@x.com", "Donor"), reg("n1@x.com", "NGO", "N1"), reg("n2@x.com", "NGO", "N2"), reg("v@x.com", "Volunteer")
    assert c.post("/api/auth/register", json={"email": "d@x.com", "password": "secret1", "role": "Donor"}).status_code == 409
    assert c.post("/api/auth/login", json={"email": "d@x.com", "password": "bad"}).status_code == 401
    # role checks
    body = {"type": "Rice", "qty": 10, "shelf_life_hours": 2, "lat": 13.05, "lon": 80.25}
    assert c.post("/api/donations", json=body, headers=H(n1)).status_code == 403
    assert c.post("/api/donations", json=body).status_code == 401
    assert c.post("/api/donations", json={**body, "qty": -1}, headers=H(d)).status_code == 400
    assert c.post("/api/donations", json={**body, "lat": 999}, headers=H(d)).status_code == 400
    r = c.post("/api/donations", json=body, headers=H(d)); assert r.status_code == 201
    did = r.json["donation"]["id"]
    assert r.json["donation"]["near"][0]["km"] <= 10
    # visibility
    assert [x["id"] for x in c.get("/api/donations", headers=H(d)).json["donations"]] == [did]
    assert did not in [x["id"] for x in c.get("/api/donations", headers=H(v)).json["donations"]]
    assert c.post(f"/api/donations/{did}/pickup", headers=H(v)).status_code == 409
    # claim: first wins
    assert c.post(f"/api/donations/{did}/claim", headers=H(n1)).status_code == 200
    r = c.post(f"/api/donations/{did}/claim", headers=H(n2)); assert r.status_code == 409 and "claimed" in r.json["error"]
    assert c.post(f"/api/donations/{did}/claim", headers=H(v)).status_code == 403
    # dispatch + PIN
    assert c.post(f"/api/donations/{did}/pickup", headers=H(v)).status_code == 200
    assert c.post(f"/api/donations/{did}/deliver", json={"pin": "wrong"}, headers=H(v)).status_code == 403
    v2 = reg("v2@x.com", "Volunteer")
    assert c.post(f"/api/donations/{did}/deliver", json={"pin": A.DEFAULT_PIN}, headers=H(v2)).status_code == 409
    r = c.post(f"/api/donations/{did}/deliver", json={"pin": A.DEFAULT_PIN}, headers=H(v)); assert r.json["donation"]["status"] == "DELIVERED"
    assert c.get("/api/stats").json["counts"]["DELIVERED"] == 1

def test_expiry():
    d, n = reg("d2@x.com", "Donor"), reg("n3@x.com", "NGO", "N3")
    r = c.post("/api/donations", json={"type": "Soup", "qty": 5, "shelf_life_hours": 0.1, "lat": 12.99, "lon": 80.21}, headers=H(d))
    did = r.json["donation"]["id"]
    A.sqlite3.connect(A.DB_PATH).execute("UPDATE donations SET expires_at=1 WHERE id=?", (did,)).connection.commit()
    r = c.post(f"/api/donations/{did}/claim", headers=H(n)); assert r.status_code == 409 and "expired" in r.json["error"]

def test_geo_and_public_mask():
    d = reg("far@x.com", "Donor")
    r = c.post("/api/donations", json={"type": "Far", "qty": 1, "shelf_life_hours": 1, "lat": 28.6, "lon": 77.2}, headers=H(d))
    assert r.json["donation"]["near"] == []
    pub = c.get("/api/donations").json["donations"]
    assert all("far@x.com" not in x["donor"] for x in pub)

def test_claim_race():
    d = reg("d3@x.com", "Donor")
    toks = [reg(f"r{i}@x.com", "NGO", "N1") for i in range(8)]
    did = c.post("/api/donations", json={"type": "Race", "qty": 1, "shelf_life_hours": 2, "lat": 13.08, "lon": 80.27}, headers=H(d)).json["donation"]["id"]
    codes = []
    def go(t): codes.append(A.app.test_client().post(f"/api/donations/{did}/claim", headers=H(t)).status_code)
    th = [threading.Thread(target=go, args=(t,)) for t in toks]
    [t.start() for t in th]; [t.join() for t in th]
    assert codes.count(200) == 1 and codes.count(409) == 7, codes

def test_bad_token():
    assert c.get("/api/me", headers=H("garbage")).status_code == 401
