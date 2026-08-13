# How the PG&E portal actually works

Captured 2026-08-12 against a live signed-in session. Re-run this when something
breaks; `audit doctor` will tell you *that* it broke, this tells you how to find
out why.

## The shape of it

`m.pge.com` redirects to `myaccount.pge.com/myaccount/s/`, which is a **Salesforce
Experience Cloud (Lightning) community**. That single fact determines everything
else, and it is not what the plan for this tool originally assumed.

There are no REST endpoints. Every call — the session check, the bill list, the
PDF download — is the same POST:

```
POST /myaccount/s/sfsites/aura?r=<n>&aura.ApexAction.execute=<n>
```

with a form-encoded body carrying:

| field | what it is |
|---|---|
| `message` | JSON: `{"actions":[{"descriptor":"aura://ApexActionController/ACTION$execute","params":{"classname":…,"method":…,"params":{…}}}]}` |
| `aura.context` | JSON: `{"fwuid":…,"app":"siteforce:communityApp","loaded":{…}}` |
| `aura.token` | per-session CSRF token |
| `aura.pageURI` | e.g. `/myaccount/s/bill-and-payment-history` |

`fwuid` is the Lightning framework build id. **It changes on every Salesforce
release** (roughly three times a year, plus patches), so it must be scraped from
the page at session start, never hardcoded. `aura.token` likewise.

## Known actions

| Purpose | Apex class and method | Params |
|---|---|---|
| Session/guest check | `MyAcct_SessionValidatorController.isGuestUserCheck` | — |
| Download one bill PDF | `MyAcct_DownloadBillPdf.httpCalloutDownloadBill` | `billidfrombillhistory` |

The bill *list* action was not captured: the table is fetched once on page load
and paginates client-side, so hooking XHR after load never sees it. To capture
it, install the hook (below) and then reload with the devtools open, or use a
`document_start` content script.

## The finding that matters

**The PDF never exists as a URL.** `httpCalloutDownloadBill` returns ~700 KB of
JSON with the PDF base64-encoded inside it, and the page builds a `blob:` URL
client-side. So there is no cookie-authenticated `GET …/bill.pdf` to fetch, and
"log in with a browser, hand the cookies to httpx, download the file" does not
work on its own — the download is itself an Aura action.

Anything talking to this portal must speak Aura.

## Re-capturing

Sign in, open the bill history page, then in the console:

```js
window.__cap = [];
const send = XMLHttpRequest.prototype.send, open = XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open = function (m, u, ...r) { this.__u = u; return open.call(this, m, u, ...r); };
XMLHttpRequest.prototype.send = function (b) {
  if (this.__u?.includes('/sfsites/aura')) {
    this.addEventListener('load', () => window.__cap.push({ body: String(b), len: this.responseText.length }));
  }
  return send.call(this, b);
};
```

Then act, and read the class and method out of each captured body:

```js
window.__cap.map(c => JSON.parse(new URLSearchParams(c.body).get('message')).actions
  .map(a => `${a.params.classname}.${a.params.method}`));
```

Never paste a captured body anywhere: it contains `aura.token` and the session id.

## Other things seen

- Adobe Target (`pge.tt.omtrdc.net`) and Adobe Analytics
  (`pacificgasandelectricco.sc.omtrdc.net`) are loaded on every page. Marketing
  telemetry, not obviously bot detection, but it is fingerprinting-adjacent and
  a scripted client will not look like a browser to it.
- The analytics payload carries `c33=production:2026-08-05T04:07:34Z`, which is
  PG&E's own deploy stamp — a useful thing to record alongside a capture, since
  it dates the observed behaviour.
- Login is at `/myaccount/s/login/` under `aura.app=markup://siteforce:loginApp2`,
  a *different* app descriptor from the authenticated community
  (`siteforce:communityApp`), so the context differs between the two phases.
