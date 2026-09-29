/**
 * Roku Ads — Shopify Custom Pixel: server-side CAPI + browser checkout events.
 *
 * Runs in Shopify's sandboxed web-pixel environment and covers the Roku events GTM
 * cannot fire, because GTM does not run on Shopify's hosted checkout and thank-you pages:
 *
 *   PURCHASE           browser (rkp) + server CAPI (via the conversion proxy), both with
 *                      event_id = order id so Roku deduplicates them (10-minute window)
 *   INITIATE_CHECKOUT  browser (rkp) on checkout_started
 *   ADD_PAYMENT_INFO   browser (rkp) on payment_info_submitted
 *
 * Storefront events (PAGE_VIEW, ADD_TO_CART) are GTM tags, see ../gtm/.
 *
 * Setup: Shopify Admin → Settings → Customer events → Add custom pixel → paste this
 * file → fill in the constants below → Save → Connect.
 */

const EVENT_GROUP_ID = 'YOUR_EVENT_GROUP_ID';                    // Ads Manager → Events → CAPI
const CF_ENDPOINT    = 'https://YOUR-CONVERSION-PROXY-URL/track'; // printed by ./deploy.sh conversion-proxy
const PIXEL_SECRET   = '__PIXEL_SECRET__';                       // value of the pixel_secret secret

// Optional scope filter, mirrored server-side in config.yaml. Empty = every order.
// Example for a multi-currency store that only advertises on Roku in the US:
//   const ALLOWED_CURRENCIES = ['USD']; const ALLOWED_COUNTRIES = ['US'];
const ALLOWED_CURRENCIES = [];
const ALLOWED_COUNTRIES  = [];

// ---------------------------------------------------------------------------
// Load the Roku pixel (rkp). rkp() is a synchronous queue, so events queue safely
// before the loader script finishes downloading.
// ---------------------------------------------------------------------------
!function (e, r) {
  if (!e.rkp) {
    var t = e.rkp = function () {
      var a = Array.prototype.slice.call(arguments);
      a.push(Date.now());
      t.eventProcessor ? t.eventProcessor.apply(t, a) : t.queue.push(a);
    };
    t.initiatorVersion = "1.0";
    t.queue = [];
    t.load = function (u) {
      var s = r.createElement("script");
      s.async = !0; s.src = u;
      var n = r.getElementsByTagName("script")[0];
      (n ? n.parentNode : r.body).insertBefore(s, n);
    };
    rkp.load("https://cdn.ravm.tv/ust/dist/rkp.loader.js");
  }
}(window, document);

rkp('init', EVENT_GROUP_ID);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function currencyOf(checkout) {
  return checkout?.totalPrice?.currencyCode || checkout?.currencyCode || '';
}

function countryOf(checkout) {
  return checkout?.billingAddress?.countryCode || checkout?.shippingAddress?.countryCode || '';
}

/** Currency must match when a list is set; country only when Shopify exposed one. */
function checkoutInScope(checkout) {
  const currency = currencyOf(checkout);
  const country  = countryOf(checkout);
  if (ALLOWED_CURRENCIES.length && !ALLOWED_CURRENCIES.includes(currency)) return false;
  if (ALLOWED_COUNTRIES.length && country && !ALLOWED_COUNTRIES.includes(country)) return false;
  return true;
}

/** Line items → CAPI contents ({id, quantity, price}). */
function capiContents(lineItems) {
  return (lineItems || []).map((item) => {
    const price = parseFloat(item.variant?.price?.amount ?? item.finalLinePrice?.amount ?? 0);
    return {
      id:       item.variant?.sku || String(item.variant?.id || item.id || ''),
      quantity: item.quantity || 1,
      ...(Number.isFinite(price) && price > 0 ? { price } : {}),
    };
  });
}

/** The JS pixel names the price field item_price; the CAPI names it price. */
function rkpContents(contents) {
  return contents.map((c) => ({
    id:       c.id,
    quantity: c.quantity,
    ...(c.price ? { item_price: c.price } : {}),
  }));
}

function checkoutEvent(name, checkout) {
  const contents = rkpContents(capiContents(checkout.lineItems));
  rkp('event', name, {
    value:        parseFloat(checkout.totalPrice?.amount || 0),
    currency:     currencyOf(checkout),
    contents,
    content_ids:  contents.map((c) => c.id).filter(Boolean),
    content_type: 'product',
    num_items:    contents.reduce((n, c) => n + (c.quantity || 0), 0),
  });
}

// ---------------------------------------------------------------------------
// PURCHASE: browser rkp + server CAPI with a shared event_id
// ---------------------------------------------------------------------------
analytics.subscribe('checkout_completed', async (event) => {
  const checkout = event?.data?.checkout;
  const order    = checkout?.order;
  if (!order?.id || !checkoutInScope(checkout)) return;

  const eventId    = String(order.id);
  const currency   = currencyOf(checkout);
  const value      = parseFloat(checkout.totalPrice?.amount || 0);  // decimal, not cents
  const contents   = capiContents(checkout.lineItems);
  const contentIds = contents.map((c) => c.id).filter(Boolean);
  const numItems   = contents.reduce((n, c) => n + (c.quantity || 0), 0);
  const sourceUrl  = event?.context?.document?.location?.href || '';

  rkp('event', 'PURCHASE', {
    event_id:         eventId,
    order_id:         eventId,
    value,
    currency,
    contents:         rkpContents(contents),
    content_ids:      contentIds,
    content_type:     'product',
    num_items:        numItems,
    event_source_URL: sourceUrl,
  });

  // Fire and forget: the browser event already covers the conversion if this fails.
  try {
    await fetch(CF_ENDPOINT, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json', 'X-Pixel-Secret': PIXEL_SECRET },
      body: JSON.stringify({
        event_id:    eventId,
        order_id:    eventId,
        value,
        currency,
        country:     countryOf(checkout) || null,
        email:       checkout.email || order.customer?.email || null,
        user_agent:  (typeof navigator !== 'undefined' && navigator.userAgent) || null,
        source_url:  sourceUrl,
        contents,
        content_ids: contentIds,
        num_items:   numItems,
      }),
      keepalive: true,
    });
  } catch (_) {}
});

// ---------------------------------------------------------------------------
// Checkout-stage browser events (GTM cannot see these on Shopify checkout)
// ---------------------------------------------------------------------------
analytics.subscribe('checkout_started', (event) => {
  const checkout = event?.data?.checkout;
  if (checkout && checkoutInScope(checkout)) checkoutEvent('INITIATE_CHECKOUT', checkout);
});

analytics.subscribe('payment_info_submitted', (event) => {
  const checkout = event?.data?.checkout;
  if (checkout && checkoutInScope(checkout)) checkoutEvent('ADD_PAYMENT_INFO', checkout);
});
