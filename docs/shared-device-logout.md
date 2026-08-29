# Logging out on a shared device (boardroom, library, reception desk)

No lawyer-facing usage guide existed in this repo before this note (only
`README.md`, which is entirely developer/deployment-facing) — flagging that
plainly rather than assuming one existed elsewhere. This is a focused note
on exactly the shared-device scenario, not a full user manual.

## What clicking "Log out" actually does

1. Ends your MutemoOS session — your login is fully invalidated on the
   server the instant you click it, not just hidden in the browser.
2. Also asks Cloudflare to end its own separate login for your account,
   everywhere it's used (not just MutemoOS) — this is a second, independent
   login layer in front of the app itself.
3. If step 2 can't complete for some reason (rare, but see below), step 1
   still fully completes regardless. Logging out of MutemoOS itself never
   depends on step 2 succeeding.

## The one thing Logout doesn't *guarantee* on a shared browser

**Confirmed live (2026-08-29):** logging out of MutemoOS does actively end
your Cloudflare session too, not just this app's own login. Tested with a
real account, end to end — clicking Logout revoked the Cloudflare session
server-side, and reloading the app in the same browser immediately dropped
back to Cloudflare's own email-code screen, exactly as it should. This
isn't inferred from the code; it was watched happen.

That said, this depends on a live call to Cloudflare's API succeeding at
the moment you log out, so it's not an absolute guarantee the way step 1
(ending your own MutemoOS session) is. If that specific request ever fails
— a momentary connectivity issue, or a Cloudflare-side credential problem
like the one this fix resolved — step 1 still fully completes regardless,
but Cloudflare's own login can keep the browser "remembered" until its own
timer runs out (Cloudflare's session duration for this firm is currently
**30 minutes**, independent of anything in MutemoOS).

**What this means in practice on a shared machine:** even in that failure
case, the next person still lands on MutemoOS's own login screen, which
asks for a fresh code sent to *their* phone or email — they can't use
*your* MutemoOS account either way. The only residual risk is if you and
the next person happen to share the same Cloudflare login (unlikely for
two different lawyers, but worth knowing).

**For a genuinely clean handoff on a shared device**, the reliable options
are:
- Close the browser entirely (clears the Cloudflare cookie along with
  everything else), or
- Use a private/incognito window for shared-device sessions, which
  discards everything the moment it's closed.

## Inactivity timeout

MutemoOS now also logs you out automatically after **45 minutes with no
activity** (clicking around, saving a note, running a search — anything
that talks to the server), separate from and much shorter than the
7-day maximum a session can otherwise last. Forgetting to click Logout on
a shared machine no longer leaves it open for days — at most 45 minutes
of genuine inactivity.

## For firm admins: shortening Cloudflare's own session further

Cloudflare's own login timer for this firm is currently set to **30
minutes** already (checked directly in the dashboard, 2026-08-29). If a
shared device needs it tighter still, it can be shortened the same way:
**Cloudflare dashboard → Zero Trust → Access → Applications → "Mutemo
Desk" → Edit → Session Duration.**

This wasn't automated as part of this change — the API endpoint for it
(`PUT /accounts/{account_id}/access/apps/{app_id}`) could not be verified
with confidence to be a safe partial update rather than a full-document
replace of the application's configuration (its own hostname, auth
methods, everything) within a reasonable amount of investigation, and a
malformed request against the live, production-gating Access application
was judged too risky to attempt blind. The dashboard change is two clicks
and carries none of that risk.
