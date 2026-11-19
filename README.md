# Annam – Backend API

Node.js + Express + PostgreSQL backend for the supplied Annam Food Waste Redistribution & NGO Coordination Platform.

## Features

- Registration and login for Donor, NGO and Volunteer roles
- Password hashing with bcrypt and signed JWT sessions
- Donation creation with food type, portions, location and shelf-life
- Nearby NGO matching using Haversine distance (10 km by default)
- Atomic donation claim so two NGOs cannot reserve the same donation
- Volunteer pickup and PIN-verified delivery
- Delivery receipt data and live donation status counts
- Automatic expiry of unclaimed donations
- Request validation, Helmet security headers, CORS and API rate limiting

## Requirements

- Node.js 20+
- PostgreSQL 14+

## Setup

1. Create a PostgreSQL database named `annam`.
2. Copy `.env.example` to `.env` and set your database password and a long random `JWT_SECRET`.
3. Install dependencies:

   ```bash
   npm install
   ```

4. Create tables and insert the sample NGO records:

   ```bash
   npm run db:init
   npm run db:seed
   ```

5. Start the API:

   ```bash
   npm run dev
   ```

6. Check `http://localhost:4000/api/health`.

## API endpoints

All request and response bodies use JSON. Protected endpoints require:

`Authorization: Bearer YOUR_JWT_TOKEN`

| Method | Endpoint | Access | Purpose |
|---|---|---|---|
| GET | `/api/health` | Public | API/database health |
| POST | `/api/auth/register` | Public | Create account |
| POST | `/api/auth/login` | Public | Login |
| GET | `/api/auth/me` | Signed in | Current account |
| GET | `/api/ngos` | Public | Verified NGOs |
| GET | `/api/ngos/nearby?lat=13.05&lon=80.25&radiusKm=10` | Public | Nearby NGOs |
| GET | `/api/donations` | Signed in | Role-filtered donations |
| GET | `/api/donations/:id` | Signed in | Donation details |
| POST | `/api/donations` | Donor | Post a donation |
| POST | `/api/donations/:id/claim` | NGO | Atomically reserve donation |
| POST | `/api/donations/:id/pickup` | Volunteer | Start pickup |
| POST | `/api/donations/:id/deliver` | Assigned volunteer | Verify PIN and close delivery |
| GET | `/api/stats` | Public | Pipeline counts |

## Example requests

### Register donor

```bash
curl -X POST http://localhost:4000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"name":"Demo Donor","email":"donor@example.com","password":"demoPass123","role":"Donor"}'
```

### Register NGO

Use an NGO ID returned by `GET /api/ngos`:

```json
{
  "name": "NGO Staff",
  "email": "ngo@example.com",
  "password": "demoPass123",
  "role": "NGO",
  "ngoId": "N1"
}
```

### Create donation

```json
{
  "foodType": "Vegetable meals",
  "quantity": 20,
  "latitude": 13.05,
  "longitude": 80.25,
  "shelfLifeHours": 4
}
```

The response includes a six-digit `verificationPin`. Share it securely with the receiving NGO; the backend stores only its bcrypt hash.

### Claim / pickup / delivery

- NGO: `POST /api/donations/1/claim`
- Volunteer: `POST /api/donations/1/pickup`
- Assigned volunteer: `POST /api/donations/1/deliver` with `{ "pin": "123456" }`

## Connect the supplied HTML frontend

The uploaded HTML currently simulates accounts and donations inside browser memory. This API is a separate backend; it will not become live until the frontend JavaScript is changed to call it.

Set the API base URL in the frontend:

```js
const API = 'http://localhost:4000/api';
let token = localStorage.getItem('annam_token');

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {})
    }
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
```

After successful register/login, save the token:

```js
token = data.token;
localStorage.setItem('annam_token', token);
```

Replace the current browser-only `users` and `D` data operations with the endpoints above. For example, load donations with `api('/donations')`, post with `api('/donations', { method:'POST', body:JSON.stringify(formData) })`, and refresh the list after a successful action.

**Important:** Run the HTML through a local web server (not `file://`) and set `CORS_ORIGIN` to that server's origin. The supplied HTML's pre-filled demo credentials and hard-coded demo delivery PIN are prototype-only; remove them when wiring the production UI. Real NGO identity verification, secure PIN delivery, HTTPS, backups, monitoring, and privacy review are still required before public deployment.

## Notes

- The sample NGO names and coordinates are copied from the supplied prototype and are demonstration data, not verified real-world service locations.
- The current frontend uses a browser-generated map illustration. For production maps, integrate a real map provider and protect API keys.
- This project provides the backend API and database schema; hosting and a live PostgreSQL instance are not included.
