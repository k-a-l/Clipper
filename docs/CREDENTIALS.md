# Getting your own credentials

Clipper can make clips with **no accounts at all**: clips are saved on your computer. Connect
accounts only for what you want:

| You want to… | You need | Time |
|---|---|---|
| Post to **YouTube Shorts** | a Google Cloud OAuth client (free) | ~10 min |
| Post to **Facebook Reels** | a Meta developer app + a Facebook Page you manage (free) | ~15 min |
| Use the **Twitch** tab in Discover | a Twitch developer app (free) | ~3 min |
| Record YouTube lives that say *"Sign in to confirm you're not a bot"* | your browser's YouTube sign-in (no app) | ~1 min |

Everything goes into **`~/Clipper/.env`** (Windows: `%USERPROFILE%\Clipper\.env`). The
installer creates that file. Restart Clipper after editing it.

> **Never share or commit your `.env` or the `storage/secrets/` folder.** They give access to
> your channels. Tokens Clipper saves are encrypted with `TOKEN_ENCRYPTION_KEY`, but the key
> sits in `.env` too. If one leaks: delete the token in the provider's console, then create a
> new key with `clipper gen-key` and reconnect.

---

## 0. Encryption key (always first)

```bash
clipper gen-key
```

Copy the line it prints into `.env`:

```ini
TOKEN_ENCRYPTION_KEY=paste-the-44-character-key-here
```

The installer already did this. Keep the same key from then on: changing it makes saved
logins unreadable, so you'd have to reconnect every account.

---

## 1. YouTube (upload Shorts)

Clipper uploads through the official **YouTube Data API v3** with your own Google Cloud
project, so the daily upload quota is yours alone.

1. Open **<https://console.cloud.google.com/>** and sign in with the Google account that owns
   (or manages) the YouTube channel.
2. Top bar → project picker → **New project** → name it e.g. `clipper` → **Create**. Select it.
3. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
4. **Google Auth Platform** (older consoles call this the *OAuth consent screen*) → **Get started**:
   * App name: anything (e.g. `Clipper`). User support email: yours.
   * Audience: **External**.
   * Contact email: yours → **Create**.
5. **Google Auth Platform → Data access → Add or remove scopes**. Add these three, then save:
   * `https://www.googleapis.com/auth/youtube.upload`
   * `https://www.googleapis.com/auth/youtube.readonly`
   * `https://www.googleapis.com/auth/youtube.force-ssl` (comments / thumbnails)
6. **Google Auth Platform → Audience → Test users → Add users**: add your own Google account.
7. **Google Auth Platform → Clients → Create client**:
   * Application type: **Desktop app** → name `Clipper desktop` → **Create**.
   * Copy the **Client ID** and **Client secret**.
8. Put them in `.env`:

   ```ini
   YOUTUBE_CLIENT_ID=your-client-id.apps.googleusercontent.com
   YOUTUBE_CLIENT_SECRET=your-client-secret
   YOUTUBE_PRIVACY_STATUS=private   # switch to public once you like the results
   ```
9. Connect the channel: in the dashboard **Connections → Add or reconnect a channel**, or in a
   terminal run `clipper auth-youtube`. A browser window opens. Pick the channel (brand channels
   are listed there) and allow access. You'll see *"Google hasn't verified this app"*: that's
   expected for your own app. Choose **Advanced → Go to Clipper (unsafe)**.

**Sign-ins that don't expire after 7 days:** while the app is in *Testing*, Google expires
refresh tokens after 7 days. Go to **Google Auth Platform → Audience → Publish app** (status
*In production*), then reconnect once. You don't need Google's verification for your own use.

**Custom thumbnails** need a verified channel: <https://www.youtube.com/verify>.

**Quota:** 10,000 units a day per project. An upload costs about 1,600, so roughly 6 uploads a
day. The dashboard's YouTube quota meter shows what's used.

---

## 2. Facebook Reels (post to a Page)

Reels are posted to a **Facebook Page** (not a personal profile) through the Graph API.

1. Make sure you're an admin of the Page (create one at <https://www.facebook.com/pages/create>).
2. Go to **<https://developers.facebook.com/apps>** → **Create app**:
   * Use case: **Other** → type **Business** (or *"Manage everything on your Page"* if it's offered).
   * Name it (e.g. `Clipper`) → **Create app**.
3. **App settings → Basic**: copy the **App ID** and click **Show** to get the **App secret**.
4. Find your **Page ID**: open your Page → **About → Page transparency**, or
   <https://www.facebook.com/your-page> → *About*. It's a long number.
5. Put them in `.env`:

   ```ini
   FACEBOOK_APP_ID=your-app-id
   FACEBOOK_APP_SECRET=your-app-secret
   FACEBOOK_PAGE_ID=your-page-id
   FACEBOOK_VIDEO_STATE=DRAFT       # PUBLISHED once you're happy
   ```
6. Get a user token with Page permissions in the **Graph API Explorer**
   (<https://developers.facebook.com/tools/explorer/>):
   * *Meta App*: your app. *User or Page*: **User Token**.
   * Add permissions **`pages_show_list`**, **`pages_read_engagement`**, **`pages_manage_posts`**.
   * **Generate Access Token** → log in → select your Page → allow → copy the token.
7. Hand it to Clipper. Either:
   * Dashboard: **Connections → Facebook**: paste the token → **Find my Pages** → tick the Page(s), or
   * Terminal: `clipper auth-facebook --user-token "EAAG..."`
     (macOS: `pbpaste | clipper auth-facebook --user-token-stdin`).

   Because the App secret is set, Clipper swaps it for a **long-lived Page token**. The dashboard
   shows *never expires* only when Facebook confirms it.

**Making posts visible to everyone:** while the Meta app is in **Development** mode, Reels may
only be visible to people with a role on the app. To go public, switch the app to **Live** (top
of the app dashboard). Meta asks for a **privacy policy URL**: any page you host that explains
what the app does with data is fine for personal use.

---

## 3. Twitch app (optional: Discover panel)

Recording Twitch streams needs **no** credentials. This is only for browsing *Discover → Twitch*.

1. <https://dev.twitch.tv/console/apps> → **Register your application**.
2. Name: anything unique. OAuth redirect URL: `http://localhost`. Category: *Application Integration*.
   Client type: **Confidential**.
3. **Manage** → copy the **Client ID** → **New secret** → copy it.
4. Either in the dashboard (**Connections → Twitch**) or in `.env`:

   ```ini
   TWITCH_CLIENT_ID=...
   TWITCH_CLIENT_SECRET=...
   ```

---

## 4. YouTube "Sign in to confirm you're not a bot" (recording / downloading)

YouTube sometimes blocks anonymous downloads from home connections. Give Clipper your
browser's YouTube sign-in:

* Dashboard: **Connections → YouTube sign-in → Import from browser** (Chrome, Safari, Brave,
  Edge, Firefox…). You must be signed in to youtube.com in that browser.
* Or export a `cookies.txt` with a *"Get cookies.txt LOCALLY"* browser extension on youtube.com
  and paste it in the same panel.

The cookies are stored in `storage/secrets/` on your computer only and are used just for
youtube.com requests. Use an account you don't mind associating with downloads.

---

## Checklist

```ini
TOKEN_ENCRYPTION_KEY=...        # clipper gen-key
YOUTUBE_CLIENT_ID=...           # Google Cloud → Clients → Desktop app
YOUTUBE_CLIENT_SECRET=...
FACEBOOK_APP_ID=...             # developers.facebook.com → App settings → Basic
FACEBOOK_APP_SECRET=...
FACEBOOK_PAGE_ID=...
# TWITCH_CLIENT_ID=...          # optional
# TWITCH_CLIENT_SECRET=...
```

Then `clipper ui --open` → **Connections** shows each account as connected, or says what's missing.

## Only post what you have the rights to

Clipper only queues clips for posting from sources you mark **"I own this or have permission"**.
Reposting other creators' streams without permission breaks YouTube's and Meta's rules: it can
get videos removed, demoted as "reused content", or the channel struck. Many streamers run
official clipping programs. Ask them.
