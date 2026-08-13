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
| Sign in | `MyAcct_customLoginLWCController.login` | `username`, `password`, `startUrl`, `uuid`, `browsercookie`, `validationCookie` |

Sign-in is **PG&E's own controller**, not Salesforce's stock
`LightningLoginFormController` — a client written against that one fails looking
like bad credentials. `browsercookie` and `validationCookie` are cookies
(`LSKey-c$browsercookie`, `LSKey-c$validationCookie`) that the login page sets
when it is fetched and then expects handed back, so the page must be loaded
before the POST. `uuid` is a fresh client-side UUID. The login form runs under
`aura.app = siteforce:loginApp2`, not the authenticated `siteforce:communityApp`.

To capture a login without recording the credential, hook **before** submitting
and store only `operationName`, `classname`, `method` and `Object.keys(params)`
— never the values — and persist to `localStorage`, because a successful login
navigates and destroys anything held in a page variable.

**The bill list** is still uncaptured. It is fetched once when
`/s/bill-and-payment-history` loads and paginates client-side, so hooking XHR
after load never sees it. Install the hook below and reload with devtools open,
or use a `document_start` content script.

## Green Button is a different system entirely

Usage export is **not** Aura and not Salesforce. The usage page embeds a
Visualforce page, `/myaccount/apex/myAcct_VF_GreenButton`, which hosts an Oracle
Opower widget, and the export runs against Opower's own GraphQL API on its own
host under `*.opower.com`.

The widget is nested three levels deep — shadow root, then Visualforce iframe —
which is why a DOM walk of the top-level page finds nothing. **Open the
Visualforce page directly in its own tab** and the whole form renders,
unclipped, with its controls reachable:

    https://myaccount.pge.com/myaccount/apex/myAcct_VF_GreenButton

Radio ids: `period-all`, `period-bill` (+ `period-bill-select`), `period-date`
(+ `date-selector--select-date-from` / `-to`), and `csv` / `xml`.

### The export, end to end

```
POST https://<host>.opower.com/ei/edge/apis/dsm-graphql-v1/cws/graphql
headers: authorization: Bearer <jwt>
         opower-selected-entities: urn:opower:v1:account:pge:uuid:<uuid>
         x-requested-with: XMLHttpRequest
```

1. `WUE_GenerateUsageExportFile(usageExportFileConfigurationInput)` → `{uuid}`.
   The input carries `format: "CSV"`, `utilityCode: "pge"`, `urns`, and
   `timeInterval: "2025-12-30T00:00:00-08:00/2026-01-29T23:59:59-08:00"`.
2. `WUE_GetExportJob(jobUuid)` → poll. `isRunning` then `isFinished` with
   `result` set to a **pre-authenticated Oracle object-storage URL**. That URL
   needs no credentials of ours, so it is fetched with a bare client.
3. The file is a ZIP holding one CSV. Despite the archive being named
   `DailyUsageData.zip`, the CSV is
   `pge_electric_usage_interval_data_<sa>_<n>_<from>_to_<to>.csv` —
   **15-minute intervals**, 2,887 rows for a 31-day cycle.

`maxAgeOfDataInDays: 1095`, so three years of interval history is available.

### Two things this settles

**Bill periods are enumerable, with their real boundaries.**
`WUE_GetUsageExportBills` populates `period-bill-select`, whose option values are
the cycles themselves:

    Dec 30, 2025 - Jan 29, 2026
      → 2025-12-30T00:00:00-08:00/2026-01-29T23:59:59-08:00

**The meter read is at local midnight.** The boundaries above are `T00:00:00`
and `T23:59:59` in the offset in force at each end. So `--read-hour` defaults to
0 because that is what the utility says, not because it was assumed — and
exporting *by bill period* rather than by date range removes the question
entirely, since the utility defines the window.

### Where the credentials come from

The Visualforce page's own HTML carries the Opower host and the bearer token, so
both are scraped per session rather than configured — the token is short-lived
and the host is not ours to pin. If the account urn is not in the page,
`billingAccountByAuthContext` derives it from the token itself, which is the one
source that cannot disagree with the credentials in use.

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
