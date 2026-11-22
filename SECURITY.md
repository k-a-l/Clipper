# Security policy

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. We aim to reply within 7 days.

## How Clipper handles your data

- The dashboard listens on `127.0.0.1` only. It rejects requests addressed to any other host name,
  and every change needs a per-session token embedded in the page, so other websites can't
  control it. Don't expose it to the internet. For remote access, use an SSH tunnel or a VPN.
- OAuth tokens and Page tokens are stored encrypted (Fernet) in `storage/secrets/`, using
  `TOKEN_ENCRYPTION_KEY` from your `.env`. YouTube browser cookies (if you import them) are
  stored there too. Keep that folder and `.env` private.
- Clipper sends nothing to any server except the platforms you connect (YouTube, Facebook,
  Twitch) and the sites you ask it to download from. The optional AI runs locally (Ollama).
