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

### Device trust, which is not MFA

`login` answers `retMessage: "verifymfa :"` when it does not recognise the
device, along with a masked email and phone and `cookieExpiryDays: 180`. That
wording is misleading. The account has **no** multi-factor authentication; the
portal verifies *devices*, and a browser that was verified once carries the
result for 180 days, which is why a person never sees a challenge.

The device identity is the pair `LSKey-c$browsercookie` and
`LSKey-c$validationCookie`. They are created by the login page's **own
JavaScript**, so they never arrive over `Set-Cookie` and no amount of fetching
will produce them — a scripted client that invents fresh values is simply a new
device every run. Copy them once from a signed-in browser, on the console at
`myaccount.pge.com`:

```js
Object.fromEntries(document.cookie.split(';')
  .map(c => c.trim().split('='))
  .filter(([k]) => k === 'LSKey-c$browsercookie' || k === 'LSKey-c$validationCookie'))
```

and put them in `.env` as `PGE_BROWSER_COOKIE` and `PGE_VALIDATION_COOKIE`. They
are device identifiers, not credentials — the password is still required — but
they belong in `.env` with everything else, not in a config file.

Chasing this as though it were MFA is a real detour: the component does ship
`MyAcct_Apex_CustomMFAController.handleChoiceofMFA` /
`.verifySignInCode` / `.resendOTP`, so an OTP flow is implementable, but a
correctly identified device never reaches it.

To capture a login without recording the credential, hook **before** submitting
and store only `operationName`, `classname`, `method` and `Object.keys(params)`
— never the values — and persist to `localStorage`, because a successful login
navigates and destroys anything held in a page variable.

### The bill list, and why hooking it is hard

The call is:

```
vlocity_cmt.BusinessProcessDisplayController.GenericInvoke2NoCont
  sClassName:  vlocity_cmt.IntegrationProcedureService
  sMethodName: MyAcct_IP_GetBillPayHistoryData
  input, options: JSON strings        → ~37 KB response
```

Calling it from Python currently fails with `Invalid value '' for query
parameter historyFilter. is not a valid enum value`. The *value* is not the
problem — the input **key** that feeds `historyFilter` is still unknown, so the
parameter arrives empty. `All Activity`, `Bill Charges` and `Payments` are the
three enum values the UI offers.

Two things make capturing that key harder than it looks, both verified
2026-08-12:

1. **The call only fires on a full page load.** The account picker and the
   Filter dropdown both re-render from data already in memory; neither
   refetches. Changing the filter, clearing and reselecting the account, and
   in-app navigation back to the page all produced zero new requests for it.
2. **Patching `XMLHttpRequest.prototype` does not intercept Aura.** Two
   separate reasons stack up. Injected tooling usually runs in an *isolated
   world*, where the patch is invisible to the page; and injecting a `<script>`
   into the main world is *still* not enough, because Aura takes its XHR from a
   pristine same-origin iframe rather than from `window`. Measured directly:
   27 POSTs to `/sfsites/aura` completed while a main-world prototype hook
   recorded none of them. To hook it, patch each frame in `window.frames`, not
   just `window` — or read bodies out of devtools/CDP instead, since the
   request-listing tool here reports URLs and status only, not payloads.

**The bill id does not require any of this.** Each rendered row carries it
directly:

```js
// inside the LWC's shadow root: c-myacct_lwc_viewbillandpaymenthistory_updated
document.querySelectorAll('a.pdf-link')  // → data-id="<opaque base64-ish token>"
```

That `data-id` *is* `billidfrombillhistory`, the one argument
`MyAcct_DownloadBillPdf.httpCalloutDownloadBill` needs. So statement download
is unblocked independently of the list API: a shadow-DOM walk collecting
`(date, amount, data-id)` yields the whole history, and everything after that is
pure Python. Treat those ids as session-sensitive — they look like tokens and
should not be pasted into logs or tool output.

## The statement layout changed inside the audited window

`bill_history` lists 24 statements, September 2024 to August 2026, and they are
not all the same document. Anything reconciling a multi-year range meets at
least two layouts, so `audit run` reports a parse failure per statement and
carries on rather than aborting the batch.

What differs, as of the 2025→2026 redesign:

| | through 2025 | 2026 |
|---|---|---|
| cycle and sub-period dates | `10/01/2025 – 10/28/2025`, separated by an **en dash** (U+2013) | `12/30/2025 to 01/29/2026` |
| section total | label wraps, and the sidebar interleaves its own lines *between* the label and the amount | label and amount adjacent |

Both are handled. The en dash is the one to remember: matching a plain hyphen
looks like it should work and still misses every older statement, and the
failure surfaces as "no billing cycle found", which reads as a corrupt PDF
rather than as an unrecognised layout.

### Statements before November 2025 contain no recoverable text

Not a corrupt download, and not one bad file: **every** statement from at least
2025-07 back is affected, which is why 2024-09 to 2025-10 cannot be audited at
all. They are built from **Type 3 fonts** -- glyphs drawn as procedures -- and
the `/ToUnicode` map that should say what each glyph means instead declares
**~56% of them to be U+0020**. Measured:

| statement | glyph mappings | mapped to space |
|---|---|---|
| 2025-08-05 | 5052 | 2812 (55.7%) |
| 2025-09-05 | 5047 | 2807 (55.6%) |
| 2025-10-03 | 4974 | 2769 (55.7%) |
| 2025-11-05 | 264 | 8 (3.0%) |

So extraction is working perfectly and faithfully returning spaces. That is the
whole explanation for the ~11.6 million characters layout mode produces: over
half the document really does claim to be whitespace, and the rest decodes to an
arbitrary substitution (`&`, `!`, `"`, `#`...). **pypdf and poppler agree** --
`pdftotext -layout` finds no `Statement Date:` either, so this is not a
library-choice problem and trying another extractor is wasted effort. Only OCR
of rendered pages could recover them.

`_glyphs_are_spaces` detects this and says so, because "unreadable document" and
"layout the parser has not been taught" need opposite responses, and the earlier
message ("it is a scan... download it again from the portal") sent the reader
after a fix that cannot work.

### The 2025-11 and 2025-12 statements parse but do not self-check

Their sections sum to more than the amount due -- 2025-11 prints delivery
181.19 plus MCE generation 86.99 against 213.89 due. The cause is visible in
the summary, which on this layout carries a line belonging to no section:

    Amount Due on Previous Statement           359.66
    Payment(s) Received Since Last Statement  -359.66
    Previous Unpaid Balance                      0.00
    Current PG&E Electric Delivery Charges     181.19
    Electric Adjustments                       -58.23     <-- no section holds this
    MCE Electric Generation Charges             86.99

181.19 + 86.99 - 58.23 = 209.95, still 3.94 short of 213.89, so `Electric
Adjustments` is necessary but not sufficient and the remainder is unexplained.
Until it is, `self_check` refuses to reconcile these, which is the design
working: pricing against a parse that does not add up would report a parser
defect as a billing defect.

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
