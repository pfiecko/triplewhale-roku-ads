# triplewhale-roku-ads

Two-way **Roku Ads** integration for Shopify stores that report in **Triple Whale**:

- **Spend import:** Roku Ads API → Triple Whale, so Roku CTV spend, impressions and
  Roku-reported conversions show up as a channel with a campaign → ad group → creative hierarchy.
- **Conversions:** Shopify → Roku Conversions API (CAPI) plus the Roku browser pixel, deduplicated,
  so Roku can attribute and optimise on your purchases.

Part of a family of Triple Whale connectors that share one layout and one Data-In client:
[triplewhale-shop-campaigns](https://github.com/pfiecko/triplewhale-shop-campaigns) ·
[triplewhale-roku-ads](https://github.com/pfiecko/triplewhale-roku-ads) ·
[triplewhale-openai-ads](https://github.com/pfiecko/triplewhale-openai-ads)

```
            spend import                                      conversions
Roku Ads API ──async report──► roku-ads-tw-import      Shopify checkout (custom pixel)
  OAuth2 refresh token           │ create → poll →        │  rkp PURCHASE (browser) ─────────► Roku
  in Secret Manager              │ download CSV → map     │  POST /track + X-Pixel-Secret
                                 ▼                        ▼
         Triple Whale Data-In /ads  channel "Roku Ads"   roku-ads-conversion-proxy ──CAPI──► Roku
                                                           hash email · IP + UA · JWT server-side
```

## Spend import

### Mapping

Report dimensions: `date, campaign_id, campaign_name, ad_group_id, creative_id, creative_name,
activity_name`; metrics: `spend, impressions, actions, order_value`.

| Data-In field | Roku source |
|---|---|
| campaign | `campaign_id` / `campaign_name` |
| adset | `ad_group_id` (Roku exposes no ad group name, so it is labelled `Ad group <id>`) |
| ad | `creative_id` / `creative_name` |
| `spend`, `impressions` | `spend`, `impressions` (account currency, converted to ISO) |
| `conversions`, `conversion_value` | `action_<event>` / `order_value_<event>` for `triplewhale.conversion_event` |
| `clicks`, `visits` | 0 (CTV has no clicks) |

Roku pivots conversions into one column pair per event (`purchase`, `add_to_cart`,
`page_view`, …). Only the configured goal event is imported. Set it to empty to import spend only.

### OAuth

Roku uses the authorization-code grant **once** to mint a long-lived refresh token; every run
after that uses the refresh-token grant server to server. `/oauth/callback` stores the refresh
token in Secret Manager, and `/run` writes back a rotated one when Roku returns it.

| Route | Auth | Purpose |
|---|---|---|
| `GET /oauth/start` | none | 302 to the Roku authorization URL, with a CSRF `state` |
| `GET /oauth/callback` | `state` | exchange the code, store the refresh token |
| `GET\|POST /run` | `X-Run-Secret` header | import; `?days=N` or `?since=&until=`, `?dry_run=1`, `?accounts=1`, `?debug=1` |

The function is public because Roku must reach the callback, so `/run` is gated by a shared
secret. The OAuth `state` is derived from that secret with HMAC, so the secret never appears in
a URL.

### Refresh and idempotency

Each run re-imports a 14-day trailing window (Roku restates for at least 8 hours and attributes
over 14 days). Triple Whale upserts on shop + channel + account + date + campaign + ad set + ad,
so re-sent rows overwrite. Creatives that drop to zero inside the window are re-sent as 0.

## Conversions

| Roku event | Fires from | Trigger |
|---|---|---|
| `PAGE_VIEW` | GTM `gtm/01-init.html` | DOM Ready, all pages |
| `ADD_TO_CART` | GTM `gtm/02-add-to-cart.html` | your GA4 `add_to_cart` event |
| `INITIATE_CHECKOUT` | Shopify custom pixel | `checkout_started` |
| `ADD_PAYMENT_INFO` | Shopify custom pixel | `payment_info_submitted` |
| `PURCHASE` (browser) | Shopify custom pixel | `checkout_completed` |
| `PURCHASE` (server) | conversion proxy → CAPI | `checkout_completed` → `/track` |

Checkout-stage events live in the custom pixel because GTM does not run on Shopify's hosted
checkout. A GTM tag listening for `begin_checkout` catches only a handful of checkouts.

**Deduplication:** the browser and server `PURCHASE` both use `event_id = order id`. Roku
collapses events sharing an `event_id` within 10 minutes.

**Field mapping (server):** `user_data.em` is SHA-256 of the email (lowercased, trimmed, `+alias`
removed) with `is_hashed: true`; `client_ip_address` and `client_user_agent` come from the
request; `custom_data.value` is a decimal amount, **not cents**; `contents` use `price`, while the
browser pixel uses `item_price` (the pixel maps between them).

**Optional scope filter:** `conversions.allowed_currencies` / `allowed_countries` (and the
matching constants in the pixel and GTM init tag) limit which orders are reported, for example
only US buyers paying USD on a multi-currency store. Empty lists report everything.

## Setup

Requirements: a GCP project with Cloud Functions, Cloud Run, Cloud Scheduler and Secret Manager
enabled; `gcloud`; Python 3 with PyYAML.

1. **Config:** `cp config.example.yaml config.yaml` and fill it in.
2. **Roku developer app:** Ads Manager → Organization → Ads API → add a developer app. Set the
   redirect URI to `roku.redirect_uri` exactly. Save the client id and secret.
3. **Secrets and IAM** (names as in your config):
   ```bash
   PID=my-gcp-project; SA=tw-importer@$PID.iam.gserviceaccount.com
   add() { gcloud secrets create "$1" --project=$PID 2>/dev/null || true
           printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- --project=$PID; }
   add roku-oauth-client-id     '…'
   add roku-oauth-client-secret '…'
   add roku-import-run-secret   "$(openssl rand -hex 32)"
   add roku-capi-token          '…'   # Ads Manager → Events → CAPI
   add roku-pixel-secret        "$(openssl rand -hex 32)"
   add triplewhale-api-key      '…'   # scope "Ads: Write"
   gcloud secrets create roku-oauth-refresh-token --project=$PID   # filled by /oauth/callback
   for s in roku-oauth-client-id roku-oauth-client-secret roku-oauth-refresh-token \
            roku-import-run-secret roku-capi-token roku-pixel-secret triplewhale-api-key; do
     gcloud secrets add-iam-policy-binding $s --project=$PID \
       --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
   done
   gcloud secrets add-iam-policy-binding roku-oauth-refresh-token --project=$PID \
     --member="serviceAccount:$SA" --role=roles/secretmanager.secretVersionAdder
   ```
4. **Spend import:**
   - `./deploy.sh spend-import`
   - open `/oauth/start` once and approve
   - `curl -H "X-Run-Secret: …" "$URL/run?days=7&dry_run=1"`
   - `./deploy.sh spend-import --schedule` (daily by default)
5. **Conversions:**
   - `./deploy.sh conversion-proxy`
   - fill in the constants in `shopify-pixel/pixel.js` and add it under Shopify Admin →
     Settings → Customer events → Add custom pixel
   - add the two GTM tags, with a constant variable `{{const - roku event group id}}`
   - keep `use_test_endpoint: true` until events appear in Ads Manager's test view, then set it to
     `false` and redeploy

## Local development

```bash
pip install -r requirements-dev.txt
pytest
CONFIG_PATH=tests/config.test.yaml python spend-import/main.py --fixture tests/fixtures/roku_report.csv
```

## Gotchas

- **Test events never connect the CAPI source.** Events sent to the test endpoint land in the
  test view only. They do not count as conversions and the source stays "not connected" until
  production events arrive.
- **Do not set `partner_name`.** It is for third parties sending on someone else's behalf. On a
  first-party integration it files events under a partner bucket and can return 403.
- **The proxy returns 502 when Roku rejects an event.** Earlier versions returned 200 either way,
  which hid every rejection.
- **Logs:** functions-framework leaves the root logger at WARNING. `lib/runtime.py` raises it to
  INFO; without that, no `log.info` line reaches Cloud Logging.
- **Report generation is slow.** With the conversion columns a report can take 8 to 13 minutes,
  hence the 900-second function timeout.
- **Currency** comes back as a display name ("United States Dollar") and is mapped to ISO before
  sending.
- **The pixel secret is public.** Anyone can read it in the pixel source, so treat it as a spam
  filter. The proxy re-validates everything it forwards.

## Layout

| Path | What |
|---|---|
| `spend-import/main.py` | OAuth routes, `/run`, Roku report → Triple Whale mapping, CLI |
| `spend-import/roku.py` | Roku Ads API client: OAuth, accounts, async reports |
| `conversion-proxy/main.py` | pixel → Roku CAPI proxy |
| `shopify-pixel/pixel.js` | Shopify custom pixel: CAPI purchase and browser checkout events |
| `gtm/` | storefront GTM tags: init + PAGE_VIEW, ADD_TO_CART |
| `lib/` | shared modules, identical across the connector family |
| `deploy.sh` | `./deploy.sh spend-import\|conversion-proxy [--schedule]` |
| `tests/` | unit tests and a synthetic Roku report |

## License

[MIT](LICENSE)
