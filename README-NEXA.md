# Nexa v2rayNG rewarded-connect build

This branch contains a reproducible patch-and-build workflow for the Nexa Android client. It downloads the official v2rayNG source at the pinned commit below, applies the patch in CI, builds a debug APK, and runs the local credit-ledger instrumentation tests.

## Source and build

- Upstream: https://github.com/2dust/v2rayNG
- Pinned upstream commit: `ef06bac9942a8550217ebe057daefd28964d436d`
- Application ID: `com.nexa.v2rayng`
- Kotlin namespace: `com.v2ray.ang` (kept unchanged)
- Build workflow: `.github/workflows/build-nexa.yml`
- Patch script: `scripts/patch_nexa_v2rayng.py`

A push to `nexa-v2rayng-rewarded` triggers the workflow. The resulting APK is uploaded as the `Nexa-v2rayNG-debug` artifact. This is a debug-signed APK, not a production release signed with a private release keystore.

## Configure Tapsell

In GitHub, open **Settings → Secrets and variables → Actions** and add:

| Secret name | Value |
|---|---|
| `TAPSELL_APP_ID` | Tapsell Mediation App Key |
| `TAPSELL_REWARDED_ZONE_ID` | Rewarded Video placement / Zone ID |

The identifiers previously provided in chat are intentionally not embedded in this public repository because their App Key / Zone ID mapping has not been confirmed. Add the values from the Tapsell dashboard to the matching secrets. Until these are configured, the app compiles with an empty placement configuration and cannot load rewarded ads at runtime.

The patch uses the official Tapsell Mediation Android API at version `1.4.0-alpha04`, matching the current official sample repository. This is an alpha release and must be rechecked against a stable SDK release before public production distribution.

## Client-side credit policy

- A new install begins with zero credits.
- A rewarded ad grants exactly three local credits only after both `onAdClosed(COMPLETED)` and `onRewarded()` are received for the same ad response.
- The ad-response ID is stored in SQLite with a unique constraint to reject duplicate callbacks.
- SQLite transactions reserve and settle credits across the UI and `:daemon` processes.
- A successful new service/tunnel start consumes one credit.
- Failed setup before the session becomes active refunds its reservation.
- Network handover and a restart of the same still-active session do not consume another credit.
- Manual stop closes the active session; a later start requires another credit.
- Always-on / null-intent starts may resume an existing active session but may not create a new session.

The ledger is stored under `noBackupFilesDir`, so Android Auto Backup does not restore the credit database to a later installation.

## Security limitations

This is deliberately a **client-only** design. There is no backend ledger, no Server-Side Verification (SSV), and no server-issued connection authorization.

The SDK callbacks are trusted by the unmodified app but are not independently verifiable by a Nexa server. A modified APK, rooted device, runtime instrumentation, or direct modification of local app state can bypass client-side credit checks. The app cannot stop a user from importing the same third-party V2Ray configuration into another client. The arbitrary VPN servers entered by users are outside Nexa's control.

The app does not lock Home, Back, Force Stop, system navigation, or other Android controls. If the ad is skipped, dismissed, unavailable, fails, lacks a reward callback, or validated Internet connectivity is lost during the ad session, no local reward is issued and the new connection is rejected.

## Tests and verification

The Android instrumentation suite currently covers:
- duplicate reward IDs,
- three successful connection sessions,
- refund of a failed start reservation,
- concurrent reservation attempts,
- rejection of a null/system start when no active session exists.

CI also builds the APK. Real Tapsell playback, device lifecycle edge cases, Android Always-on behavior, OEM background restrictions, Root mode, and real-device network-loss scenarios still require physical-device testing. Passing a build or the SQLite tests does not prove those system-level scenarios.
