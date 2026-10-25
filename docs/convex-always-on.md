# Convex always-on deployment

The production app is `https://nautical-wolverine-162.convex.site/`. Convex serves
the static web export and the call API from this origin. Cartesia calls its webhook
and four assigned tools on the same origin. Convex subscriptions use the matching
`https://nautical-wolverine-162.convex.cloud` URL. Neither a local Python process nor
a Cloudflare tunnel is in the production call path.

The `.convex.site` hostname belongs to the production deployment and remains the
same across code pushes. Removing that deployment would remove the address. A custom
branded domain is a separate choice; it is not required for a durable address.

## Deploying changes

1. Set the production Convex environment variables before placing any calls:
   `CALL4ME_VIEWER_TOKEN`, `CALL4ME_BACKEND_TOKEN`, `CALL4ME_TOOL_SECRET`,
   `CALL4ME_ANSWER_KEY` (64 hex characters), `CARTESIA_API_KEY`,
   `CARTESIA_AGENT_ID`, and `CARTESIA_NUMBER_ID`. Keep values out of Git and shell
   history. The tool secret must match Cartesia's existing webhook/tool secret.
2. Run `npm test`, `npm run typecheck`, then `npx convex deploy` from the repository
   root.
3. Build the static app from `web/` with
   `NEXT_PUBLIC_CONVEX_URL=https://nautical-wolverine-162.convex.cloud` and an empty
   `NEXT_PUBLIC_API_BASE`, using `npm run build`.
4. From the repository root, set
   `CONVEX_URL=https://nautical-wolverine-162.convex.cloud` and run
   `node scripts/upload_convex_site.mjs`. This updates the files in Convex storage.
5. If the Cartesia URLs need restoring, run
   `PYTHONPATH=. .venv/bin/python scripts/switch_cartesia_to_convex.py`. It changes
   only the four assigned tool URLs and their webhook URL. It verifies that the
   agent configuration is identical before and after; it does not edit the voice,
   model, instructions or tool assignments.

Open `/health` and the app root on the production `.convex.site` URL, then confirm
`/calls` requires the access code. A real call is still needed to verify carrier
audio, menu tones and PIN tones after deployment.

## Private answers

The app sends a PIN or other private answer to its same-origin HTTPS endpoint. An
HTTP action encrypts it with AES-GCM before writing to Convex. The waiting Cartesia
tool decrypts it, deletes the ciphertext from Convex, and returns the answer once.
An abandoned ciphertext expires after 90 seconds; a 30-second cleanup interval
keeps its maximum retention near two minutes. Finishing a call also deletes its
remaining answers and typed messages. Raw values are never written to call records,
captions, audit logs or live replay. Cartesia necessarily receives the answer to
speak it or send its keypad tones; call data at Cartesia is deleted on completion,
with a retry job if that delete fails.

The mobile home-screen app installed from an old temporary tunnel has a different
origin. Open the production URL on the phone and add that page to the home screen
to use the always-on deployment. The access code remains the same.
