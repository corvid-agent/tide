# tide

A **recurring micro-allowance** — a pull payment stream — on Algorand
TestNet, matured by [Arcron](https://github.com/CorvidLabs/arcron) keepers.
Sibling of [arcron-beacon](https://github.com/corvid-agent/arcron-beacon),
[plod](https://github.com/corvid-agent/plod), and
[epitaph](https://github.com/corvid-agent/epitaph).

**The keeper matures the tide; the beneficiary pulls.**

**Unaudited. TestNet only. Not deployed (appId = 0).** Deploy needs a
human's explicit go — see issue #1.

## What it does

The owner **funds** the app, names a **beneficiary** once, and sets a
**drip** — the µALGO matured per keeper tick. The Arcron keeper network
calls `tick()` on an hourly cadence; each tick matures one more drip into
the claimable pool. The beneficiary **pulls** the accumulated allowance
with `claim()` whenever they like; the contract never pushes.

Pull, not push, on purpose:

- **Inner payments are the lesson.** `claim()` pays `matured × drip` µALGO
  out of the app's own balance with an inner transaction. The app is the
  sender; the beneficiary just asks.
- **Fee pooling is the cost.** The inner payment carries a 1000 µALGO fee,
  so the outer `claim()` call must set a **flat fee ≥ 2000 µALGO**
  (1000 outer + 1000 inner). A claim sent with the default 1000 µALGO fee
  fails on fee underspend. One pooled fee, paid by the beneficiary, only
  when they actually pull — versus a push design that spends a fee every
  tick whether or not anyone wants the money.

`tick()` is best-effort, not catch-up: a missed or skipped tick is a missed
drip. And it never promises money the pool cannot pay — if the app balance
minus a 100000 µALGO floor cannot cover `drip × (matured + 1)`, the tick
returns 0 and no drip matures.

## The traps this contract avoids

Read [docs/integrating.md](https://github.com/CorvidLabs/arcron/blob/main/docs/integrating.md)
in the Arcron repo first. Every one of these was learned the hard way:

1. **Zero create args.** A uint64 create_arg is how a sloppy deploy script
   confuses the keeper app id with a cadence and locks an interval at ~68
   years. `create()` takes nothing; the keeper is named once via
   `set_keeper`, the drip via `set_drip`.
2. **Keeper auth is `Application(keeper).address`, never `itob`.** Arcron's
   inner call comes from the keeper *application account*. Comparing the
   sender against `itob(keeper_app_id)` compares 8 bytes to a 32-byte
   address and never matches.
3. **Fail soft after keeper auth.** A hook that rejects gets exponentially
   backed off by keeper bots and burns upkeep escrow on retries. After the
   two authorization asserts in `tick()`, every no-work path **returns 0** —
   beneficiary unset, pool underfunded, both of them. Nothing asserts once
   the keeper is authenticated. `claim()` is fail-soft too: a claim with
   nothing matured is a quiet no-op.
4. **`set_keeper` and `set_beneficiary` are one-time.** `set_keeper` is
   creator-only; `set_beneficiary` is owner-only. Neither can be re-pointed.
   (`set_drip` is deliberately re-settable — the owner controls the rate,
   never the destination.)
5. **Compile clean.** Verified: puyapy 5.10.1 compiles this contract with
   zero errors (artifacts committed under `smart_contracts/tide/out/`).
6. **The register interval *is* the cadence.** One tick matures one drip,
   so the allowance accrues at the upkeep interval — **1286 rounds ≈ 1 h**
   (at ~2.8 s/round). Use **SKIP_AHEAD**: death-by-backoff is the only bad
   outcome, and a skipped tick is just a skipped drip.

## State layout (global)

Declared order; keys are stored by name. Schema from the compiled arc56:
**4 uint64 + 2 byte slices**, no local state.

| slot | key             | type           | meaning                                      |
| ---- | --------------- | -------------- | -------------------------------------------- |
| 0    | `keeper_app`    | uint64         | Arcron keeper app id; 0 until `set_keeper`    |
| 1    | `drip`          | uint64         | µALGO matured per tick; ≥ 1000, owner-set     |
| 2    | `matured`       | uint64         | unclaimed drips since the last `claim`        |
| 3    | `claimed_total` | uint64         | lifetime µALGO paid out to the beneficiary    |
| 4    | `owner`         | address (32 B) | may `set_drip` / `set_beneficiary`; creator   |
| 5    | `beneficiary`   | address (32 B) | may `claim`; zero address until named, once   |

Claimable right now = `matured × drip` (also the `claimable()` readonly).

## ABI

Selectors are `sha512_256(signature)[:4]`, as compiled by puyapy 5.10.1.

| method                        | selector     | auth               | notes                                       |
| ----------------------------- | ------------ | ------------------ | ------------------------------------------- |
| `create()void`                | `0x4c5c61ba` | (create)           | zero create args, on purpose                |
| `set_keeper(uint64)void`      | `0xc4c1d8f7` | creator, one-time  | ABI lowers `Application` to `uint64`        |
| `set_beneficiary(address)void`| `0x42158f1b` | owner, one-time    | ABI lowers `Account` to `address`           |
| `set_drip(uint64)void`        | `0x52cfac60` | owner              | re-settable; floor 1000 µALGO               |
| `fund(pay)void`               | `0x51531b75` | anyone             | payment to the app address, amount > 0      |
| `tick()uint64`                | `0x4d4d5f0b` | keeper app account | fail-soft; returns new `matured` or 0       |
| `claim()void`                 | `0xf1577726` | beneficiary        | inner payment; outer flat fee ≥ 2000 µALGO  |
| `claimable()uint64`           | `0x03cfb591` | readonly           | `matured × drip`                            |

## Keeper registration recipe

Register an upkeep on the Arcron TestNet keeper app **769891898** (escrow
address `M4YFP33L5VIFRF53X53WUMQWBOWSLYQNBSSAJV2SORGF43L36XBY7OREUA`) via

```
register(pay,pay,uint64,byte[][],uint64,uint64,uint64,uint64,uint64,uint64)uint64
```

with:

- **target app** = the deployed tide app id; **call args** = the bare
  `tick()` selector (`0x4d4d5f0b`), ABI-encoded as `byte[][]`
  (10 bytes on the wire: count + offset + length + selector).
- **interval = 1286 rounds** (~1 h) — the drip cadence. One tick, one drip.
- **fee per execution = 4000 µALGO**.
- **skip policy = 1 (SKIP_AHEAD)** — a missed call is a missed drip;
  accrual is best-effort, never catch-up. Never leave the zero default.
- **payment 1 = MBR**, to the keeper app address:
  `2500 + 400 × (139 + len(call_args))` µALGO → for the bare selector,
  `2500 + 400 × 149 = 62100` µALGO.
- **payment 2 = escrow**, to the keeper app address: **500000 µALGO**
  (125 executions at 4000 µALGO; top up before it runs dry).
- Both payments go to the **keeper app address** (escrow address of app
  769891898), not to tide.
- After registering, read the upkeep box `u` + `itob(upkeep_id)` **fresh**
  from the keeper app (indexer `/v2/applications/769891898/box?name=...`) —
  never trust a cached copy when checking `next_execution_round`.

Order matters: deploy → `set_keeper` → `set_beneficiary` → `set_drip` →
`fund` → register, because `tick` hard-asserts until the keeper is set
(and fail-softs until the beneficiary is named and the pool is funded).

## How a human deploys this later

**TestNet only. Never commit a mnemonic. Never deploy without the human go
(issue #1).**

1. Fund a throwaway TestNet account (dispenser). The mnemonic lives in
   env/CI secrets, never in git.
2. Compile: `puyapy smart_contracts/tide/contract.py --out-dir out`
   (or reuse the committed artifacts).
3. Deploy the app with **zero create args**. Record the app id and the app
   (escrow) address.
4. Call `set_keeper` with keeper app **769891898** (creator-only, one-time).
5. Call `set_beneficiary` with the allowance recipient (owner-only,
   one-time — there is no re-point method).
6. Call `set_drip` with the per-tick amount (≥ 1000 µALGO; owner-only,
   re-settable).
7. Call `fund` with a payment to the app address (anyone may top up; the
   deployer's initial funding covers the account min balance — the stream
   only matures drips the balance can cover on top of a 100000 µALGO floor).
8. Register the upkeep on keeper 769891898 per the recipe above (issue #2).
9. Set `"appId"` and `"appAddress"` in `docs/deploy.json` — the board
   lights up on its own (issue #3).

Reminder for claims: the beneficiary's `claim()` call must carry a **flat
fee ≥ 2000 µALGO** to pool the inner payment's fee.

## Layout

```
smart_contracts/tide/contract.py   the Puya (Algorand Python) source — the whole thing
smart_contracts/tide/out/          committed puyapy 5.10.1 artifacts (arc56 + TEAL)
docs/                              GitHub Pages split-flap board (NOT DEPLOYED until appId > 0)
docs/deploy.json                   {"appId": 0, ...} — the board's single source of config
```

Compiled artifacts are committed here on purpose (unlike arcron-beacon) so
the reviewed bytecode hash is pinned in git.

**Pending:** the token that wrote this repo lacks the `workflow` scope, so
no Pages publish workflow is committed. **A human must enable GitHub Pages
from `/docs` on `main` in the repository settings** (Settings → Pages →
Source: Deploy from a branch → `main` `/docs`). A `pages.yml` copied from
[corvid-agent/plod](https://github.com/corvid-agent/plod) is welcome when a
suitably-scoped credential exists.

## Build locally

```bash
pip install puyapy==5.10.1
puyapy smart_contracts/tide/contract.py --out-dir out
```

Verified at authoring time: compiles clean on puyapy 5.10.1; global schema
4 uint64 + 2 byte slices; selectors as tabulated above; the committed
`out/Tide.arc56.json` is byte-identical to the compiler output (git blob
`de97ce939e9385ddb3176b2e1df2dad569cb85f5`). Mock-chain tests cannot prove
keeper integration (inner calls, MBR) — that belongs to a LocalNet/TestNet
e2e at deploy time.

## The board

`docs/` is a split-flap/CRT status board in the spirit of
[corvid-agent/arcron-beacon](https://github.com/corvid-agent/arcron-beacon)
and [corvid-agent/waddle](https://github.com/corvid-agent/waddle). While
`appId` is 0 it shows **NOT DEPLOYED**. Once `appId > 0` it reads the app's
global state from the public indexer
(`https://testnet-idx.algonode.cloud`) and flaps out the claimable amount,
the drip, the ticks matured, the lifetime claimed total, the beneficiary,
and the live pool balance (from the indexer account endpoint, when
`appAddress` is configured). If the feed is unreachable it falls back to
the last good snapshot (marked STALE) rather than guessing. Read-only, no
wallet, no keys.

Unaudited. TestNet only. Not deployed.
