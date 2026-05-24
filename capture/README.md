# Capture (Phase B.2)

Mobile reference-photo **check-in tool** for the Player Sort system. A volunteer
picks a shoot, filters the roster to the player in front of them, taps the name,
and captures a close-up face photo that uploads as that player's reference **for
that shoot**. See `../PHASE_B2_ROSTER_CAPTURE.md`.

Built on the B.1 prototype (rear camera → framing oval → snap → upload to the
A.2 reference endpoint). B.2 adds shoot + player selection, a filterable roster
with live capture status, and replace / remove scoped per shoot.

## Run it

1. **Backend** (separate terminal, from `../backend`):
   ```
   uvicorn app.main:app --reload --port 8020
   ```
   Restart it after pulling new backend code so the B.2 endpoints
   (`/references/shoot/{job_id}`, `/roster/{job_id}/reference-status`) are live.

2. **Capture app** (from this folder):
   ```
   npm install
   npm run dev          # serves http://localhost:8022
   ```

3. **HTTPS tunnel** (separate terminal) — the camera (`getUserMedia`) only works
   in a secure context, so the phone must load the app over `https`:
   ```
   cloudflared tunnel --url http://localhost:8022
   ```
   (or `ngrok http 8022`). Open the printed `https://…` URL on your phone/tablet.

**No environment variables are needed** — the target player and shoot are chosen
in the UI. (B.1's `VITE_REF_PLAYER_ID` has been removed.)

## Using it

1. **Pick a shoot** from the dropdown (non-archived jobs from `GET /api/jobs`).
2. **Find the player** — stack the **team filter**, **name search**, and the
   **"Needs photo"** toggle (which hides players who already have a photo *for
   this shoot*). A **✓** marks players already captured this shoot; it updates
   live as you capture/remove.
3. **Tap the player** → frame their face in the oval → **Use photo**. On success
   you're returned to the roster automatically, that player now ✓.
4. **Retake / replace:** tapping a ✓ player shows "…already has a photo — this
   will replace it"; capturing again replaces **this shoot's** photo (other
   shoots are untouched). The confirm button reads **Replace photo**.
5. **Remove:** on a ✓ player, **"Remove photo instead"** (with a confirmation)
   deletes this shoot's photo and clears the ✓.

## Seed a test shoot (multi-team — to exercise filters / search / toggle)

The roster needs **no images** — any job works. With the backend running:

1. **Create a shoot (Job).** Make one in the editor app's wizard pointing at any
   folder, **or**:
   ```
   curl.exe -s -X POST http://localhost:8020/api/jobs -H "Content-Type: application/json" -d "{\"name\":\"Test Shoot\",\"root_path\":\"C:/path/to/any/folder\",\"has_lines\":false,\"auto_run\":false}"
   ```
   Note the returned `job_id` (also visible in `GET /api/jobs`).

2. **Upload a 3-team roster** — save this as `test-roster.csv`:
   ```
   Ava-Nguyen,Lions
   Mason-Reyes,Lions
   Sofia-Petrov,Lions
   Liam-OConnor,Lions
   Coach-Lions,Lions
   Ethan-Kim,Tigers
   Maya-Johnson,Tigers
   Noah-Alvarez,Tigers
   Coach-Tigers,Tigers
   Zoe-Martin,Bears
   Lucas-Brandt,Bears
   Harper-Singh,Bears
   Olivia-Day,Bears
   Coach-Bears,Bears
   ```
   ```
   curl.exe -F "file=@test-roster.csv;type=text/csv" http://localhost:8020/api/players/roster/<JOB_ID>
   ```

3. Open the app, pick the shoot, and capture a few players to get a mix of ✓ and
   "needs photo" for testing the toggle.

**Reset capture status for a re-test:** use the in-app **Remove photo**, or
`DELETE /api/players/<PLAYER_ID>/references/shoot/<JOB_ID>` (player ids via
`GET /api/players/roster/<JOB_ID>`).
