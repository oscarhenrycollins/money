---
name: money
description: Use when the owner asks about his spending, transactions to review, categories or money rules.
version: 0.1.0
author: Hermes Agent
license: GPL-3.0-only
platforms: [linux, macos]
metadata:
  hermes:
    tags: [finance, budgeting, transactions, spending]
    related_skills: [wizard-simplefin, card-benefits]
---
# Money

The owner's money app at /money/. It reads one bank-connection app (SimpleFIN by default; Monarch or Teller if chosen under Accounts → Bank connection) and layers his own reviewed state, categories, merchant names, notes, hidden flags and rules on top.

Read: `money_overview` (current connection, accounts, how many need review, categories, rules), `money_transactions` (his categories and names applied; filters from/to, account, search, category, reviewed, amounts), `money_spending` (one month by category; refunds net, transfers and card payments excluded).

Change only when he asks: `money_update` with `source` and `id` (or `ids`). Setting `make_rule` with `match` text also categorizes future matching transactions. The app has an Undo for the last change; edits never alter the bank's data.

Negative amounts are money out. For card-benefit credits, search the transaction and report it as "looks posted"; he confirms in Card Benefits.
