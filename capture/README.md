# Capture (Phase B.1)

Mobile capture-and-upload prototype for the Player Sort reference-photo system:
open the device camera, frame a player's face in a fixed oval, snap, and upload
to the existing reference endpoint. See `../PHASE_B1_CAPTURE_PROTOTYPE.md`.

Built section-by-section with on-device verification. **Section 1 is the
scaffold only** — a placeholder screen that proves the app runs and is reachable
on a phone over an HTTPS tunnel. The camera arrives in Section 2.

## Run it

1. **Backend** (separate terminal, from `../backend`):
   ```
   uvicorn app.main:app --reload --port 8020
   ```

2. **Capture app** (from this folder):
   ```
   npm install
   npm run dev          # serves http://localhost:8022
   ```

3. **HTTPS tunnel** (separate terminal) — the camera (`getUserMedia`, used from
   Section 2 on) only works in a secure context, so the phone must load the app
   over `https`. cloudflared needs no account:
   ```
   cloudflared tunnel --url http://localhost:8022
   ```
   (or `ngrok http 8022`). It prints a public `https://<random>.trycloudflare.com`
   URL.

4. On your **phone/tablet**, open that `https://…` URL. You should see the dark
   "Capture — coming up" placeholder card, with **no certificate warning**.

## Configure the test player (needed from Section 5, not Section 1)

Uploads target one hardcoded player id. Copy `.env.example` to `.env` and set
`VITE_REF_PLAYER_ID` to a real `Player` row's id. To seed one with no extra
tooling: create a job in the editor app, then upload a one-line roster CSV
(`Test-Player,Test-Team`) via `POST /api/players/roster/{job_id}`, then
`GET /api/players` to read the assigned id. (Not needed for the Section 1
scaffold check.)
