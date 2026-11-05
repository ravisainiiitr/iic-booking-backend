# In-app internet voice calls

Researchers call the **Lab Operator**, **Officer In Charge (OIC)** or **Accounts In Charge** of a booking, instrument or
invoice from the portal (web) and the Android / iOS app. Calls use the internet (WebRTC), never the phone network or
WhatsApp, and are **not recorded**. Phone numbers are never shown or used.

## Architecture

```
 caller (web / app WebView)                 Django (ASGI)                    callee (web / app WebView)
 ───────────────────────────               ──────────────                    ──────────────────────────
 POST /api/calls/  ───────────────►  authorise, resolve callee,
                                     CallSession(ringing)  ── ws/calls/ "call.incoming" ──►  ring in the page
                                                           ── FCM / APNs VoIP push ───────►  Android call notification
                                                                                             / iOS CallKit screen
                                     POST /calls/<id>/accept/  ◄────────────────────────── Answer (mic opens now)
 "call.update accepted" ◄──────────  CallSession(accepted)
 mic opens, createOffer ── ws "signal" ──►  relay (participants only, accepted calls only) ──► answer, ICE
 ◄═══════════════════ audio: peer to peer (Opus, echo cancellation, noise suppression, AGC) ═══════════════════►
                        STUN (free public servers) for addresses; TURN only if configured
 POST /calls/<id>/end/ ───────────►  CallSession(ended, duration)  ── "call.update" ──►  both sides close
```

- **Backend**: `iic_booking/calls/` — `models.py` (CallSession, CallEvent, CallDevice), `recipients.py` (who may be
  called), `service.py` (state machine), `consumers.py` (`ws/calls/`), `push.py` (FCM, APNs VoIP), `views.py`.
- **Web**: `src/lib/callEngine.ts` (WebRTC + signalling), `src/components/calls/*` (provider, call screen, call
  buttons), `src/pages/CallHistory.tsx` (`/calls`, `/calls/answer/:id`).
- **Android** (`iic-booking-android`): `IicCallPlugin`, `IicCallMessagingService`, `CallNotifications`,
  `CallActionReceiver`, `CallForegroundService`, `CallPayload`.
- **iOS**: `ios/App/App/IicCallPlugin.swift` (PushKit + CallKit), `Info.plist`, `App.entitlements`.

### Who can be called (never hard-coded, never chosen by the client)

| Item | Roles offered | Source |
| --- | --- | --- |
| Booking (`context=booking`) | Lab Operator, OIC, Accounts In Charge | The booking's equipment (a mode without its own staff uses its base equipment's) |
| Invoice / payment (`context=invoice`) | Accounts In Charge | Active Finance users of the equipment's department |
| Instrument (`context=equipment`) | Lab Operator, OIC | `EquipmentOperator` (primary before secondary), `EquipmentManager` |

The caller must be able to see the booking (`bookings_visible_to`) or the instrument (catalogue visibility). A
`callee_id` sent by the client is only accepted if it is one of these candidates. Fallbacks: nobody reachable as Lab
Operator / Accounts In Charge → "Call the Officer In Charge instead"; nobody at all → support ticket.
"Reachable" = online in the portal (calls WebSocket, 90 s presence) or has an app install registered for push.

### Call states

`ringing → accepted → ended`; `ringing → declined | cancelled | missed (ring time, 45 s)`; `ringing/accepted → failed`;
a callee already in a call → `busy`. Every change is made under a row lock and is idempotent, so simultaneous
accept / decline / end cannot corrupt a call (first wins). Calls left open more than 4 hours are closed as ended.
Stored per call: caller, callee, role, item, reason, times, duration, outcome, failure code, platforms.
`CallEvent` keeps the audit trail. No audio, no SDP and no IP addresses are stored.

### Security

- Token-authenticated WebSocket; signals relayed only between the two participants of an **accepted** call, only
  offer/answer/candidate/bye shapes, at most 16 KB.
- Rate limits: 6 calls per caller per 10 minutes, 3 to the same person.
- Push tokens: one row per app install, refreshed on each app start, revoked on sign-out or when FCM/APNs reject them;
  never logged or returned. Decline from the phone's call screen uses a signed, call-bound token valid for the ring time.
- TURN (when enabled): short-lived credentials (coturn `use-auth-secret`), the shared secret stays on the server.

## Zero-cost setup and its limit

Calls are **peer to peer** with free public STUN (`stun.l.google.com`, `stun.cloudflare.com`). No relay server is
run. Most home, office and mobile networks connect directly; **some restrictive networks (symmetric NAT, strict
institute firewalls, some mobile carriers) cannot**. Those calls end as *failed* with "Your network may block direct
internet calls; try another network, or raise a support ticket" (`failure_code` `ice_failed` / `ice_timeout`, visible
in Django admin and call history). Expect this to be the main reason for failed calls; the failure rate shows whether a
TURN relay is worth adding later.

Bandwidth: Opus voice ~ 30–50 kbit/s per direction (~0.3 MB per minute per side), all between the two devices.
Server load: signalling only (a few KB per call).

## Configuration (backend environment, `.envs/.production/.django`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `CALLS_ENABLED` | `False` | Master switch |
| `CALLS_TEST_ACCOUNTS_ONLY` | `True` | Pilot: caller **and** callee must be test accounts |
| `CALLS_RING_SECONDS` | `45` | Ring time before "No answer" |
| `CALLS_STUN_URLS` | Google + Cloudflare STUN | Comma-separated |
| `CALLS_TURN_URLS`, `CALLS_TURN_SECRET`, `CALLS_TURN_TTL_SECONDS` | empty, empty, 600 | Optional TURN (both URL and secret needed) |
| `CALLS_FCM_SERVICE_ACCOUNT_FILE` | empty | Path to the Firebase service-account JSON (outside the repository) |
| `CALLS_APNS_KEY_FILE`, `CALLS_APNS_KEY_ID`, `CALLS_APNS_TEAM_ID` | empty | APNs auth key (.p8) for VoIP pushes |
| `CALLS_APNS_BUNDLE_ID`, `CALLS_APNS_USE_SANDBOX` | `ac.in.iitr.iicbooking`, `True` | `False` for App Store builds |

The WebSocket `ws/calls/` is served by the existing ASGI workers and Redis channel layer, through the same proxy path
as `ws/notifications/`. No new ports or security-group rules are needed for the zero-cost setup.

## Firebase (Android incoming calls) — steps for the IIC team

1. https://console.firebase.google.com → **Add project** "IITR Equipment Booking" (Analytics not needed).
2. **Add app → Android** twice, with package names `ac.in.iitr.iicbooking` and `ac.in.iitr.iicbooking.debug` (the debug
   build fails if its package is missing). Then download `google-services.json`, which now lists both.
3. Give the file to the app build **without committing it**: add it as the GitHub secret
   `ANDROID_GOOGLE_SERVICES_JSON_BASE64` (`base64 -w0 google-services.json`) in `iic-booking-android`; the build writes it
   to `android/app/google-services.json`. Locally, copy it to
   `android/app/google-services.json` (git-ignored).
4. **Project settings → Service accounts → Generate new private key** (JSON). Add it as the GitHub secret
   `CALLS_FCM_SERVICE_ACCOUNT_JSON_BASE64` (`base64 -w0 key.json`) in `iic-booking-backend` and run the workflow
   **Apply Calls FCM Key**, which sets `CALLS_FCM_SERVICE_ACCOUNT_B64` on the server. Never commit the key. Delete the
   downloaded file afterwards.
5. Cloud Messaging API (V1) is enabled by default for new projects; the legacy server key is not used.

## Apple (iOS incoming calls) — needs the Apple Developer account

1. Identifier `ac.in.iitr.iicbooking`: enable **Push Notifications**.
2. **Keys → +** → enable **Apple Push Notifications service (APNs)** → download the `.p8` (once). Note the Key ID and
   Team ID. Store the `.p8` on the server outside the repository; set `CALLS_APNS_KEY_FILE`, `CALLS_APNS_KEY_ID`,
   `CALLS_APNS_TEAM_ID` (`CALLS_APNS_USE_SANDBOX=True` for development / TestFlight debug builds).
3. Provisioning profile with Push enabled; `aps-environment` is already in `App.entitlements`; the
   **Voice over IP** and **Audio** background modes are already in `Info.plist`.
4. Build and sign in Xcode on a Mac: `cd ios/App && xcodebuild -project App.xcodeproj -scheme App -configuration
   Release -sdk iphoneos -archivePath build/App.xcarchive archive` then export for TestFlight. CI only checks a
   simulator build without signing.

## Rollout

1. Deploy backend (migration `calls 0001`) and website; calling stays **off**.
2. Set `CALLS_ENABLED=True` (`CALLS_TEST_ACCOUNTS_ONLY=True`). Assign a **test** Lab Operator / OIC / Accounts In
   Charge to a test instrument (pilot calls only connect test accounts on both sides).
3. Install the new Android build (with `google-services.json`) on the test phone; sign in as the test operator.
4. Run the test-device checklist below. Then set `CALLS_TEST_ACCOUNTS_ONLY=False` to open it to everyone.
Rollback: `CALLS_ENABLED=False` (buttons disappear, the WebSocket refuses, pushes stop). The migration can stay.

## Test-device checklist (Samsung Galaxy S24 Ultra; iPhone when available)

| # | Test | Web | Android | iOS |
| --- | --- | --- | --- | --- |
| 1 | Researcher calls from a booking → the test Lab Operator's browser rings → Answer → audio both ways | ☐ | ☐ | ☐ |
| 2 | Mute / unmute, speaker / earpiece, End; history shows "ended" with duration | ☐ | ☐ | ☐ |
| 3 | Operator offline, OIC online → "Call the Officer In Charge instead" | ☐ | | |
| 4 | Payment page → Call Accounts In Charge | ☐ | ☐ | ☐ |
| 5 | App closed / phone locked → call notification (Android) / CallKit (iOS) → Answer → audio | | ☐ | ☐ |
| 6 | Decline from the lock screen → caller sees "Declined" | | ☐ | ☐ |
| 7 | No answer for 45 s → caller "No answer", callee's notification disappears | ☐ | ☐ | ☐ |
| 8 | Callee already in a call → "Busy" | ☐ | | |
| 9 | Switch Wi-Fi → mobile data during a call → "Reconnecting…" then back, or "failed" with the network message | ☐ | ☐ | ☐ |
| 10 | Bluetooth headset connected → call audio uses it | | ☐ | ☐ |
| 11 | Microphone permission refused → clear message, call declined | ☐ | ☐ | ☐ |
| 12 | Restrictive network (e.g. institute Wi-Fi to mobile data) → note whether it connects | ☐ | ☐ | ☐ |

## Monitoring and troubleshooting

- Django admin / `CallSession`: outcomes and `failure_code`; many `ice_failed` → network blocking (consider TURN).
- Logs (no tokens): `FCM call push refused: http=… status=…`, `APNs call push refused: http=… reason=…`,
  `Calls: could not reach the WebSocket layer`.
- No ringing on Android: check `CallDevice` rows for the user (registered?), notification permission, battery
  optimisation, and that `google-services.json` was in the build.
- iOS VoIP pushes require the app to report a CallKit call each time; the plugin does, so Apple will not throttle it.

## Known limitations

- Peer-to-peer only: some networks cannot connect (see above). TURN is supported but not deployed (zero-cost).
- Incoming calls while the app is closed need the Firebase project (Android) and the Apple account + APNs key (iOS).
- Android app lock: answering from a locked phone still shows the app lock prompt first when app lock is on.
- iOS: built and type-checked in CI for the simulator only; CallKit/PushKit and WKWebView audio need a real iPhone
  and a signed build to verify.
