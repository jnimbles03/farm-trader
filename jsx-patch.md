# Patch to `freis-farm-v5.jsx`

## What's broken today

Lines 319–340 of `freis-farm-v5.jsx` call `https://api.anthropic.com/v1/messages` directly from the browser. That request is CORS-blocked and has no API key attached, so `fetchGrainPrices()` always throws, always returns `null`, and the site always falls back to the hardcoded `FALLBACK = { corn: 4.35, soy: 11.15, wheat: 5.21 }` from line 317.

Every "live" tick the family is seeing right now is that fallback. It has been since we shipped.

## Fix

After the GitHub Actions pipeline is running and GitHub Pages is serving `prices.json`, replace the whole `fetchGrainPrices` function with this:

```js
const PRICES_URL = "https://jnimbles03.github.io/farm-trader/prices.json";

async function fetchGrainPrices() {
  try {
    const res = await fetch(PRICES_URL, { cache: "no-store" });
    if (!res.ok) return null;
    const p = await res.json();
    return (p.corn && p.soy && p.wheat) ? p : null;
  } catch { return null; }
}
```

Nineteen lines down to seven. No API keys in the browser, no CORS roulette, no prompt-in-the-browser.

If you're using a custom domain for Pages (e.g. `prices.meyerinterests.com`), point `PRICES_URL` at that instead.

## Diff view

```diff
 /* ─── FALLBACK PRICES ─── */
 const FALLBACK = { corn: 4.35, soy: 11.15, wheat: 5.21, date: "fallback" };

-async function fetchGrainPrices() {
-  try {
-    const res = await fetch("https://api.anthropic.com/v1/messages", {
-      method: "POST",
-      headers: { "Content-Type": "application/json" },
-      body: JSON.stringify({
-        model: "claude-sonnet-4-20250514", max_tokens: 1000,
-        tools: [{ type: "web_search_20250305", name: "web_search" }],
-        messages: [{ role: "user",
-          content: `Search for the latest CBOT grain futures settlement prices. Return ONLY a JSON object — no markdown, no backticks, no explanation:
-{"corn": <nearest corn futures $/bu>, "soy": <nearest soybean futures $/bu>, "wheat": <nearest wheat futures $/bu>, "corn_basis_il": <typical N. Illinois country elevator corn basis like -0.25>, "soy_basis_il": <typical N. Illinois soy basis like -0.30>, "date": "<date of prices>"}`
-        }]
-      })
-    });
-    const data = await res.json();
-    const text = data.content?.map(b => b.type === "text" ? b.text : "").join("").trim();
-    if (!text) return null;
-    const clean = text.replace(/```json|```/g, "").trim();
-    const p = JSON.parse(clean);
-    return (p.corn && p.soy && p.wheat) ? p : null;
-  } catch { return null; }
-}
+const PRICES_URL = "https://jnimbles03.github.io/farm-trader/prices.json";
+
+async function fetchGrainPrices() {
+  try {
+    const res = await fetch(PRICES_URL, { cache: "no-store" });
+    if (!res.ok) return null;
+    const p = await res.json();
+    return (p.corn && p.soy && p.wheat) ? p : null;
+  } catch { return null; }
+}
```

## About the response

- `corn`, `soy`, `wheat` are Yahoo continuous front-month closes, ~10 min delayed. Good enough for a family dashboard. Not for execution.
- `corn_dec`, `soy_nov`, `wheat_jul` are specific contract months, same source. Ignored by the current parser — harmless extras.
- `corn_basis_il` / `soy_basis_il` come back `null`. Yahoo doesn't carry IL cash basis; the React site's existing optional chaining handles the null. When we wire a real basis feed (Ritchie Grain via Bushel, DTN) we'll backfill these.
- `generated_at` + `date`: the site already just displays whatever string is in `date`.

## Staleness caveat — read this

This JSON file is refreshed **at most every 15 minutes** (whenever the GitHub Action's cron tick runs, and only Mon–Fri during market hours). If the family loads the site Saturday afternoon, they're reading Friday's close. That's fine, but you'll probably want to show the `generated_at` timestamp on the page so it's obvious.

Consider adding this below the price tiles:

```jsx
<div className="text-xs text-stone-500 mt-1">
  Quotes refreshed {new Date(prices.generated_at).toLocaleString()}
</div>
```

## Verify after the first Action run

1. Go to `https://jnimbles03.github.io/farm-trader/` — the landing page we ship shows the current board. If that loads, the pipeline is live.
2. In the browser console on `farm.meyerinterests.com`:

   ```js
   fetch("https://jnimbles03.github.io/farm-trader/prices.json", {cache: "no-store"})
     .then(r => r.json()).then(console.log)
   ```

   If real numbers come back and the site still shows 4.35 / 11.15 / 5.21, the JSX change didn't make it out — rebuild / redeploy the site.
