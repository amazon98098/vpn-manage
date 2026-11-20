# NetPlus payment timing and multi-order routine

Status: FineArts halted. NetPlus is the active vendor focus.

## Observed top-up / card-to-card flow

1. Wallet → Increase balance → enter amount in toman.
2. Choose card-to-card (this method has its own min/max; observed min about 20,000 toman).
3. Bot shows payment instructions: unique exact amount in rial, destination card, cardholder name.
4. Admin/system must transfer that exact amount within the allowed time window.
5. Preferred path: bot auto-approves the transfer (usually within about 30 seconds). Success text then confirms the wallet charge / renew / create outcome.
6. Fallback path: if auto-approve does not happen, use the button "I paid | send receipt" and manually send the receipt.
7. Receipt button / manual receipt path is believed to become available after roughly 3 to 5 minutes, not immediately.
8. Never treat "I paid" alone as success. Success only after NetPlus confirmation text for that renew/create/charge.

## Real operator habit (important)

Mike almost never charges the wallet first and then spends wallet balance.
Almost every time he pays inside the specific renew or create process for that one transfer only.

So the primary automation path is:
renew/create flow → package/user selection → payment instructions for that order → transfer exact unique amount → auto-approve or receipt fallback → vendor success text.

Wallet top-up remains a secondary/rare path, not the default routine.

## Critical design rules for our system

- Every payment session is a timed state machine with deadlines.
- Track at least: payment instructions shown time, unique amount, card, related subscription/order, auto-approve wait, receipt-window open time, final vendor confirmation.
- Auto-approve watch window: expect success often under ~30 seconds; keep listening beyond that until timeout.
- Receipt fallback: only offer/click send-receipt after the vendor’s receipt window is open (assume ~3–5 minutes unless we measure exact timing later).
- Limited transfer time: if the payment invoice expires, cancel/restart cleanly; do not reuse stale unique amounts.
- Concurrent renewals/creations: do not mix payment sessions.
  - One active vendor payment conversation step at a time per vendor account, or hard-isolate sessions.
  - Queue multiple jobs.
  - Match inbound vendor success/failure messages to the exact pending unique amount / order id.
  - Never attach confirmation from order A to order B.
- Human remains responsible for the actual bank transfer; automation tracks state, reminds, and continues after vendor confirmation.
- For multiple jobs in one period: prepare a clear routine — create invoice → show pay info → wait auto-approve → if timeout enter receipt mode → on vendor success continue next queued job.

## Transfer between accounts

After success text `انتقال سرویس با موفقیت انجام شد`, NetPlus shows a **بازگشت** button (often `backorder`) that returns to the subscriptions list.
Do **not** send `/start` between transfers — tap بازگشت, then on the refreshed list click another subscription button on the **current page** to open its options. When the page is empty, tap **بعدی**.

## Renew decision (important)

After opening a service and tapping renew, NetPlus asks for duration / plan with options like:
- one month / two months / three months (change plan path)
- renew current plan (`تمدید پلن فعلی` / callback like `exntedagei`)

Operator rule:
- If the customer wants the same package → choose **current plan**.
- If the customer asked for a different volume/duration → choose the matching month, then the matching package.

Our admin bot must ask this before driving NetPlus, then follow only that branch.

### Observed current-plan renew path (258_keyvan_heydari_1)

1. `/services` → quick search → type username → status opens
2. Tap renew (`extend_<id>`)
3. Duration/plan screen → tap **تمدید پلن فعلی** (`exntedagei`)
4. Invoice appears (product, toman amount, days, GB, wallet balance) → tap **تایید تمدید** (`confirmserivce`)
5. If wallet is 0: new message “موجودی کافی نیست” with **کارت به کارت** (`cart_to_offline`)
6. Payment instructions: unique amount in **rial**, card number, holder; button **پرداخت کردم | ارسال رسید** (`sendresidcart-...`)
7. Human transfers exact amount → wait auto-approve success text, else receipt fallback later


## Findings from main account history (7,566 messages, Jun 3 – Nov 2, 2026)

- About 394 card payment invoices. Direct pay for renew (~200) and create (~60) dominates; wallet top-ups (~40) were mostly in June.
- Auto-approval time after the payment instructions appear: median about 32 seconds, 90% within about 2.5 minutes, rare outliers much longer.
- Receipt path was needed only about twice. The receipt prompt appeared about 4 minutes after the instructions; renewal succeeded about 1 minute after sending the photo.
- About 92 invoices had no success afterwards (abandoned or not paid). A new invoice is generated each attempt with a different unique rial amount.
- Mike's fast renew routine: send the services shortcut command, type part of the name in quick search, open the service, tap renew, pick the plan, confirm the invoice, choose card-to-card, pay.
- Fast create routine: buy shortcut command, choose duration and package, send the new username, then the payment instructions appear and creation succeeds after payment.
- Bot shortcut commands seen in use: services, buy, wallet.
- Each service has a stable internal id in its buttons (the same id appears in the service, renew, link and transfer buttons). Store it with each subscription.
- Alerts arrive in the private chat: days left, megabytes left (both come with a direct renew button), service deleted for not renewing, and expired status.
- Service status screen gives total traffic, used, remaining with percent, Jalali expiry date with time left, last connection, and client app.
- Usernames mostly follow a customer-number prefix convention, like a number, an underscore, then the name.
- Errors to handle: bot is off, invalid command, order info not found, username already used, username format invalid, renewal error, renewal not possible on this panel.
- Older payment message format (June) differed from the current one; parser must tolerate format changes.
- From history: about 178 service usernames seen, about 101 likely still active, about 77 deleted for not renewing.

## Inventory sync (Phase 4)

- `/services` often opens **quick search** («عبارت جستجو»), not the paginated purchased list.
- Working recovery (same as transfer probes): if search opens, send reply-keyboard label  
  `🛍 مشاهده سرویس های خریداری شده من` (and variants). Soft `/start` then list label is a last resort.
- List screens contain `product_<id>` buttons and usually text «اشتراک های خریداری شده».
- Sync pauses new renew/create drives while walking NetPlus UI.
- If the live list still cannot open, sync falls back to scraping recent chat history for `product_` buttons (partial inventory).

## Open measurements to capture later

- Exact invoice expiry time shown by NetPlus.
- Exact delay before "I paid | send receipt" becomes usable.
- Exact success-message formats for wallet charge, renew, and new service creation.
