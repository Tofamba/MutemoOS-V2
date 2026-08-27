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
3. If step 2 can't complete for any reason (a real possibility — see
   below), step 1 still fully completes. Logging out of MutemoOS itself
   never depends on step 2 succeeding.

## The one thing Logout does *not* guarantee on a shared browser

Cloudflare's own login (the screen you saw before MutemoOS ever loaded,
asking for your email and a one-time code) has **its own separate timer**,
set independently of anything in MutemoOS. Logging out of MutemoOS asks
Cloudflare to end that too, but if that specific request fails (e.g. a
momentary connectivity issue, or a permissions change on our side that
hasn't been re-verified since this was written), Cloudflare's own login can
keep the browser "remembered" until its own timer runs out.

**What this means in practice on a shared machine:** after you log out of
MutemoOS, the next person may not be asked to sign in to Cloudflare again
right away — they'll land on MutemoOS's own login screen, which still asks
for a fresh code sent to *their* phone or email, so they can't use *your*
MutemoOS account. But if you and the next person share the same
Cloudflare login (unlikely for two different lawyers, but worth knowing),
skipping Cloudflare's prompt is possible within that window.

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

If a shared device is used often enough that even the above isn't tight
enough, Cloudflare's own login timer can be shortened directly:
**Cloudflare Zero Trust dashboard → Access → Applications → (this firm's
MutemoOS application) → Edit → Session Duration.**

This wasn't automated as part of this change — the API endpoint for it
(`PUT /accounts/{account_id}/access/apps/{app_id}`) could not be verified
with confidence to be a safe partial update rather than a full-document
replace of the application's configuration (its own hostname, auth
methods, everything) within a reasonable amount of investigation, and a
malformed request against the live, production-gating Access application
was judged too risky to attempt blind. The dashboard change is two clicks
and carries none of that risk.
