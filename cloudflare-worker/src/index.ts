// Freis Farm SMS reply webhook.
//
// TextBelt POSTs here when a recipient replies to an outbound alert.
// We verify the HMAC signature (so randos can't inject fake replies),
// then fire a `repository_dispatch` event so the farm-trader repo's
// collect-reply workflow picks it up and updates state/confirmations.json.
//
// Required secrets (set via `wrangler secret put`):
//   TEXTBELT_KEY  — same key used by evaluate.py; HMAC secret
//   GITHUB_TOKEN  — fine-grained PAT, Contents + Actions r/w on farm-trader
//
// Required vars (set in wrangler.toml under [vars]):
//   GITHUB_REPO  — "owner/name", e.g. "jnimbles03/farm-trader"

export interface Env {
  TEXTBELT_KEY: string;
  GITHUB_TOKEN: string;
  GITHUB_REPO: string;
}

interface TextBeltReply {
  textId?: string;
  fromNumber?: string;
  text?: string;
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    // Health check — easy to curl after deploy
    if (req.method === "GET") {
      return new Response("freis-farm-sms-reply OK\n", { status: 200 });
    }
    if (req.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    const rawBody = await req.text();

    // ---- 1. Verify HMAC ----
    // TextBelt sends `X-textbelt-timestamp` + `X-textbelt-signature` where
    // signature = hex(HMAC-SHA256(key=API_KEY, msg=timestamp+rawBody)).
    const timestamp = req.headers.get("X-textbelt-timestamp");
    const signature = req.headers.get("X-textbelt-signature");
    if (!timestamp || !signature) {
      return new Response("Missing signature headers", { status: 401 });
    }
    const expected = await hmacHex(env.TEXTBELT_KEY, timestamp + rawBody);
    if (!timingSafeEqual(expected, signature)) {
      return new Response("Invalid signature", { status: 401 });
    }

    // ---- 2. Parse payload ----
    let payload: TextBeltReply;
    try {
      payload = JSON.parse(rawBody);
    } catch {
      return new Response("Bad JSON", { status: 400 });
    }
    const { fromNumber, text } = payload;
    if (!fromNumber || typeof text !== "string") {
      return new Response("Missing fromNumber or text", { status: 400 });
    }

    // ---- 3. Fire repository_dispatch ----
    // GitHub's client_payload is capped at 10 fields / 64KB, which is
    // plenty for our (phone, text, timestamp) triplet.
    const dispatch = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
          "Accept":        "application/vnd.github+json",
          "Content-Type":  "application/json",
          "User-Agent":    "freis-farm-sms-reply-worker",
        },
        body: JSON.stringify({
          event_type: "sms_reply",
          client_payload: {
            phone:       fromNumber,
            text:        text,
            received_at: new Date().toISOString(),
            textbelt_id: payload.textId ?? null,
          },
        }),
      },
    );

    if (!dispatch.ok) {
      const body = await dispatch.text();
      console.error(`github dispatch failed: ${dispatch.status} ${body}`);
      return new Response(
        `GitHub dispatch failed: ${dispatch.status}`,
        { status: 502 },
      );
    }

    console.log(`queued sms_reply for ${fromNumber}: ${text.slice(0, 40)}`);
    return new Response("OK\n", { status: 200 });
  },
};

async function hmacHex(key: string, message: string): Promise<string> {
  const keyData  = new TextEncoder().encode(key);
  const msgData  = new TextEncoder().encode(message);
  const imported = await crypto.subtle.importKey(
    "raw", keyData, { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", imported, msgData);
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// Constant-time comparison to dodge timing attacks — paranoid but free.
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}
