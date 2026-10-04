---
name: scout-update
description: Update the Scout service. Works through what Scout needs from a person or a big model: API enrollments to sign up for, recipes whose last run failed, hosts on hold waiting for a site's answer, and recent failures worth a new move. Use when the user says /scout-update, "update scout", or hands Scout over for maintenance.
---

# /scout-update

Go through these four lists in order and report each one. Fix what you can yourself; the rest goes to the user as a
short checklist they can act on.

## 1. Enrollments

Call `enroll` with `action='list'`. Each open request has a `todo`: where to sign up, the cost, the terms, and where the
key goes.
- Give the user the `todo` lines as they are, grouped by cost: free first, then paid or contract.
- Never ask the user to paste a key into the chat. Keys go in the keys file Scout names (`~/.config/scout-mcp/keys.env`,
  mode 600) or in `SCOUT_KEY_<NAME>` environment variables.
- When the user says a key is in place, call `enroll` with `action='done'` for that host. Scout tests the key on the
  API's sample url before it marks the site enrolled.
- If a signup is waiting on approval, call `enroll` with `action='applied'`. If the user doesn't want it, call
  `enroll` with `action='decline'`.
- Once a site is enrolled, compile a recipe against its API (`reach` the sample, then `compile`), so small models
  use the API with one call. The recipe never contains the key; Scout adds it.

## 2. Recipes

Call `recipes`. For each one whose `status` says the last run failed, reach the failing input, see what changed, fix the
step and `compile` again with the same examples plus the failing one. Report what changed.

## 3. Holds

Call `memory` with no host and look for hosts with a `hold`.
- An automatic hold (it has an `until`) needs nothing: Scout probes once when it ends.
- A hold someone set by hand is waiting on a site's answer. Ask the user whether the site replied. Lift it with
  `hold(host, lift=true)` only when they say it has. Keep the pace the site asked for, using `min_interval`.

## 4. Cached sites

For each site the user pulls a lot from, call `cache` with `action='status'`. A job in state `done` is ready for
`cached=true` runs. A stopped or failed job: report it, and restart it only if the user asks.

## 5. Failures

Read `recent_failures` from the same `memory` call. For walls with no official route, look for the site's API,
developer program or bulk download and record it with `enroll` `action='request'`. For other failures, propose a move
only when you have evidence it works, such as a real url you saw in `site_map` or `families`.

## Report

Finish with what you fixed, the user's checklist (enrollments to sign up for, sites to answer), and what is still open.
