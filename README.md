# Solana Validator Selection

Tools for choosing a validator for native SOL staking, optimizing for one thing:
**net yield earned by a delegating staker**. Liquidity is not a concern.

Everything is verified from on-chain data rather than trusting dashboards.

## Background

A delegator's yield each epoch comes from three sources:

| Source | Delegator's share |
|---|---|
| Inflation rewards | × (1 − inflation commission) × validator's vote-credit efficiency |
| MEV tips (Jito) | × (1 − MEV commission), after the block engine's cut and the ~3% TipRouter fee |
| Block rewards (priority fees) | Protocol sharing (SIMD-0123) is not active on mainnet yet; until then only voluntary off-protocol sharing (Jito Priority Fee Distribution) reaches delegators |

The best validator has 0% inflation commission (held consistently over time), top vote
credits, shares MEV and block rewards at low commission, and has strong per-slot block revenue.

## Scripts

All scripts use only `requests` and must be run from this folder: the newer ones import shared
helpers from `commission_history.py` and `mev_history.py`.

### `commission_history.py`

Verifies a validator's historical inflation commission per epoch, which catches commission rugs.

Reward records no longer carry a usable commission: since vote state v4, `commission` is null
on every record, and `commissionBps` appears only on epochs up to 1029 on the public RPC. So
commission is established two independent ways:

1. **Payout records.** For each epoch it calls `getInflationReward` on a delegated stake account,
   the vote account, and the validator's inflation-rewards collector if it differs (SIMD-0232;
   a few mainnet validators route commission to a separate account). Every collector now gets a
   Voting record each epoch, with 0 lamports when no commission was taken.
   - Collector record 0 lamports, stake paid → 0% (`collector zero`)
   - Collector record > 0 → commission > 0 (`collector paid`)
   - `commissionBps` / `commission` present → used directly (`record`)
   - Stake paid, no collector record → unknown (`no collector record`); a null vote-account
     record does *not* prove 0%, because commission may have gone to a custom collector
2. **Point-value cross-check**, which needs no commission field:
   `delegator reward = delegated stake × credits earned × point value × (1 − commission)`.
   Credits come from the vote account's `epochCredits` (64 epochs on-chain). Point value is exact
   for the latest epoch (EpochRewards sysvar: total rewards / total points); for older epochs it
   is the best value in the run, so `implied_commission_pct` is relative to the best validator.

Validated on mainnet (epochs 1006–1055) against a 10% validator and a 5% validator with a custom
collector: both came out at exactly 10.00% and 5.00% implied.

A delegated stake account is found automatically: the largest one active across the whole
lookback window, located via `getProgramAccounts` on the Stake program. This can be a stake-pool
account (for pt1 it picked `FMSsC3…`, a JitoSOL pool account that the pool merges into every
epoch). The commission verdict is unaffected, but `implied_commission_pct` can drift; pin a
wallet-owned account (as `realized_yield.py` selects) if that column looks off.

```bash
pip install requests
python commission_history.py <VOTE_ACCT> [<VOTE_ACCT> ...] --epochs 50 --csv history.csv
python commission_history.py <VOTE_ACCT>=<STAKE_ACCT>        # pin a specific stake account
python commission_history.py -f validators.txt --rpc https://<archival-rpc>
```

**Reading the output:** a clean validator shows `ok` close to the number of epochs checked,
`max` at 0%, and `none` under non-zero epochs. `other` counts epochs that matched no pattern;
inspect those rows in the CSV.

### `mev_history.py`

Verifies a validator's historical Jito MEV commission per epoch.

Each epoch a Jito validator creates a TipDistributionAccount (TDA), a PDA of the Tip Distribution
program (`4R3gSG8B…`) with seeds `["TIP_DISTRIBUTION_ACCOUNT", vote, epoch]`. Its
`validator_commission_bps` is set in `initialize_tip_distribution_account`, and the program has no
instruction to change it afterwards, so the value at initialization is the one that applied.

- TDA still open (about 10 epochs) → read from account state (`account`), fetched by derived
  address with `getMultipleAccounts`, so no account index is needed
- TDA closed → decode the initialize instruction from the PDA's transaction history (`init tx`)
- PDA never used → no TDA that epoch, so no MEV was shared with delegators (`no TDA`)

Fees outside the validator's control come off before delegators see anything: the block engine's
cut (before tips reach the TDA) and the TipRouter fee (~3% of the TDA; claims now go through
TipRouter, `RouterBmu…`).

```bash
python mev_history.py <VOTE_ACCT> [<VOTE_ACCT> ...] --epochs 50 --csv mev.csv
```

### `block_revenue.py`

Checks whether a validator shares block revenue (priority fees) with delegators.

- **Protocol (SIMD-0123).** Commission is `blockRevenueCommissionBps` in vote state v4, and the
  delegator share accrues in `pendingDelegatorRewards`. The feature gate `block_revenue_sharing`
  (`Crbnc267…`) is **not activated** on mainnet as of epoch 1056; its feature account doesn't
  exist yet. Until it activates, nothing is shared, and every vote account sits at the 10000 bps
  default anyway. Vote state keeps only the current value, so once the feature is live the
  commission's history can't be read back from account state; realized payouts are the record.
- **Off-protocol (Jito Priority Fee Distribution, `Priority6we…`).** Same shape as MEV tip
  distribution: a per-epoch PDA `["PF_DISTRIBUTION_ACCOUNT", vote, epoch]` with commission fixed at
  initialization, so history is recoverable the same way. Funding is voluntary, though: the
  validator decides how much to transfer in, so a 0 bps account can still share nothing. Only
  about 3 validators use it (Oct 2026).

```bash
python block_revenue.py <VOTE_ACCT> [<VOTE_ACCT> ...] --epochs 50 --csv block.csv
```

### `realized_yield.py`

Ranks validators by **realized** delegator yield: SOL actually received per SOL delegated, from all
three sources, measured on a real stake account.

- **Stake account:** wallet-owned (withdrawer on the ed25519 curve, which excludes stake pools and
  program vaults), active before the window, and with no delegation changes in the window (split,
  merge, delegate, deactivate, move-stake). Withdrawing claimed lamports, transfers and spam dust
  are fine: they never touch the delegation. It tries the three largest; pin one with `VOTE=STAKE`.
- **Inflation** comes from `getInflationReward`. **MEV and block-reward claims** come from the
  account's own transactions, matched to an epoch by the Jito distribution PDA that paid them.
- **Denominator:** the exact delegated stake that epoch, reconstructed as current delegation minus
  inflation credited since. MEV and block claims land as undelegated lamports and don't compound.
- **Pending claims:** epochs whose MEV claim hasn't happened yet (TDA still open, no claim) are
  excluded from averages. Exception: a TDA at 10000 bps never produces a delegator claim, so its
  MEV is counted as a definitive zero rather than pending.
- **Annualized by wall-clock time, not epoch count.** Slot time was cut in steps during this window
  (420 → 416 → 366 → 317 → 267 → 218 ms; epochs went from ~50 h to 26.2 h from epoch 1053), and
  per-epoch inflation shrinks with epoch length. APR = Σ per-epoch yield ÷ Σ epoch duration × 1 year,
  with durations measured from block times.

```bash
python realized_yield.py <VOTE_ACCT> [<VOTE_ACCT> ...] --epochs 50 --csv yield.csv
python realized_yield.py -f candidates.txt
```

## Findings (epochs 1006–1055, checked Oct 2026)

Shortlist: `pt1LsjkNwqCKdYYfc35ToDkqtEG9pswLTJNaMo8inft`, `CV7uvPY1Hk5Avb2NvkGJoUzGnipZrEZK27j5rQJoUae9`.

| | pt1Lsjk… | CV7uvPY… |
|---|---|---|
| Inflation commission | 0% all 50 epochs (both checks agree) | 0% all 50 epochs |
| MEV commission | 0 bps all 50 epochs; **800 bps until epoch 894** | 0 bps all 50 epochs; 0 bps in leftover accounts back to 869 |
| Block-revenue sharing | none (SIMD-0123 inactive; no Jito PF accounts) | none |
| Realized total APR | **5.281%** (inflation 5.037%, MEV 0.244%) | **5.241%** (inflation 5.039%, MEV 0.201%) |
| Lowest epoch (annualized) | 4.89% | 4.90% |

The two are effectively tied. CV7 earns slightly more inflation in 47 of 50 epochs (a consistent but
tiny 0.002 pp). pt1's 0.044 pp MEV lead is not statistically significant (about 1.2 standard errors;
ahead in 33 of 50 epochs). Over this window the difference is about 0.4 SOL a year per 1,000 SOL.

Reference validators measured alongside them confirmed the method: G83jy4… (10% inflation, 8% MEV)
4.672%; VotESB… (5% inflation via a custom collector) 4.846%; B1rsc6jv… (10% inflation, shares
priority fees, +0.543% block APR) 5.237%. A validator with 0% inflation commission that also shared
priority fees would beat the shortlist; none has been found yet.

### Candidate batches

Each batch was run through all four scripts with pt1 as the reference. Realized APR is total
(inflation + MEV); no candidate shared block revenue. "Tied" means the MEV gap to pt1 is under about
2 standard errors across 50 epochs.

**Batch 1 (epochs 1007–1056).** All 0% inflation commission (inflation APR 5.026–5.030%).

| Validator | MEV commission | Realized APR |
|---|---|---|
| pt1Lsjk… (ref) | 0 bps | 5.273% |
| CV7uvPY…, NoRDTy8…, 2g2QU1N…, HeTyhZd…, cover89…, THWfRpc… | 0 bps | 5.213–5.232%, tied with pt1 |
| FahWJg2… | 0 bps | 5.182%, measurably lower MEV |
| 49DJjUX… | **500 bps** (300 until epoch 869) | 5.174% |
| 9rwCwfL… | 0 bps | 5.160%, measurably lower MEV |
| BeSov1o… | **1000 bps** | 5.139% |

**Batch 2 (epochs 1009–1058).**

| Validator | Inflation commission | MEV commission | Realized APR |
|---|---|---|---|
| pt1Lsjk… (ref) | 0% | 0 bps | 5.254% |
| B6nDYYL… | 0% | 0 bps | 5.196%, tied with pt1 (t = 0.4) |
| 8GbwASq… | 0% | 0 bps | 5.167% (MEV gap t = 2.8) |
| bkpkQKg… | 0% | 0 bps | 5.145% (t = 3.4) |
| he1iusu… | 0% | 0 bps; no TDA in epoch 1025 | 5.110% (t = 4.6) |
| ANCVpxE… | **5%** | **no TDA in epochs 1009–1046**, 0 bps from 1047 | 4.822% |
| EXhYxF2… | **6%** | **10000 bps** (keeps all MEV) in every epoch | 4.709% |

Takeaways:

- Among 0% validators inflation APR is identical to within 0.004 pp; the whole spread is MEV. Inflation
  commission shows up exactly as expected (ANCVpx 0.95 × 5.011 = 4.760%, EXhYxF 0.94 × 5.011 = 4.710%).
- A dashboard showing only inflation commission would miss EXhYxF's 100% MEV commission and
  ANCVpx's 38 epochs without any Jito tip sharing.
- The two largest validators measured (he1ius, 8Gbw) earn measurably less MEV per SOL than pt1 at
  the same 0 bps; the missed epoch explains only a small part of he1ius's gap.
- `commission_history.py` showed B6nDYY at 1.32% implied before epoch 1052. That was the owner
  withdrawing undelegated lamports (it breaks the constant-idle assumption), not commission: the
  exact-delegation inflation APR in `realized_yield.py` is 5.010%, i.e. 0%. Likewise 9rwC's 101%
  implied came from a tiny account merged mid-window; a pinned clean account gives 0%.

Mainnet facts worth knowing, all checked on-chain:

- Feature activations: vote state v4 at epoch 949, delayed commission changes at 991,
  basis-point commissions at 1003, custom commission collectors at 1031; SIMD-0123 not scheduled.
- Slot time was cut in steps from about 420 ms to 218 ms (epoch 1053), so epochs are now 26.2 h.
- Jito's tip and priority-fee distribution programs have no instruction to change a commission
  after initialization.

## Known limitations

- Takes **vote account** addresses, not identity addresses.
- Speed: the public RPC rate-limits `getTransaction`. The same 10-epoch, 2-validator
  `realized_yield.py` job took 94 s on the public RPC and 21 s on a Helius free key, with identical
  output. A 50-epoch, 5-validator ranking takes about 2 minutes on Helius.
- `commission_history.py`'s point-value cross-check is limited to the 64 epochs of `epochCredits`
  kept on-chain.
- `realized_yield.py`'s APY assumes MEV and block claims are restaked; claims land as undelegated
  lamports and don't compound on their own. APR is the conservative figure.
- Once SIMD-0123 activates, block revenue may be paid in the same stake reward records as
  inflation; `implied_commission_pct` would then go negative (no longer clipped at 0).
- The point-value check assumes the stake account's undelegated lamports (rent + excess) stayed
  constant over the window; a deposit, split or merge mid-window skews `implied_commission_pct`.
- RPC endpoint: `--rpc`, else `$SOLANA_RPC_URL`, else Helius if `HELIUS_API_KEY` is set (in the
  environment or a `.env` file here, which is git-ignored), else the public endpoint. The key is
  never printed; errors show the endpoint without its query string.
- The Helius free tier can refuse `getProgramAccounts` on the Tip Distribution program ("account
  index service overloaded"). Only `mev_history.py`'s side note on unclosed TDAs from before the
  window depends on it; it prints "lookup unavailable" instead of failing.

## Design notes: monitoring after Alpenglow (Nov 2026, not built)

Notes from a design discussion, saved for later development. Facts were checked on mainnet at
epoch 1076 and against Agave source at commit `1c5f40a`.

### Why monitor instead of chasing yield

- Among good 0%/0-bps validators the realized APR spread is about 0.05 pp a year, mostly MEV noise
  (tip sizes are heavy-tailed; the *number* of leader slots barely varies over a year).
- An unnoticed failure costs far more. A validator dropping out or raising commission to 100% costs
  about 5% APR while it lasts: one week is about 0.1% of stake, roughly two years of the gap between
  the best and second-best validator.
- With native staking the validator can't touch principal (no live slashing), so the worst case is
  lost yield until you leave. **How fast you notice a problem matters more than which good validator
  you picked.** The design is alerts first, with a periodic (e.g. quarterly) yield review.

### Alpenglow facts that change the picture

- **Alpenglow is live from epoch 1064** (feature activated at slot 459,648,000). Votes are no
  longer transactions.
- **Inflation now depends on leader slots.** The leader earns credits for every vote it includes in
  its reward certificates, so pt1's per-epoch `total_eff` swings 0.87–1.04 with its leader-slot
  assignment, and skipped leader slots now cost inflation as well as MEV. The 0.004 pp inflation
  spread measured under TowerBFT probably doesn't hold anymore. See `alpenglow_check.py`.
- **Validator Admission Ticket (VAT, SIMD-0357).** At the first block of epoch N the runtime picks
  the voter set for epoch N+1. A vote account gets in only if it has a BLS key, has stake, is in
  the top 2,000 by stake, and holds at least rent-exempt minimum (0.0198 SOL) + VAT
  (+ `pendingDelegatorRewards` once SIMD-0123 is active). Accounts that pass have the VAT burned
  from the **vote account** (a `VATDebit` reward record in that block). The VAT is 1.6 SOL per
  epoch at 400 ms slots and **0.8 SOL at today's 200 ms slots**, about 270 SOL a year. A vote
  account that fails is left out of the whole next epoch: **its delegators earn zero**, and it
  shows up only as delinquent.
- **0%/0-bps validators have nothing flowing into their vote account.** Commission is what refills
  it (MEV commission arrives there through TipRouter), so a 0/0 operator must top it up by hand.
  Epoch 1076 snapshot: 61 active validators (16.2M SOL) had ≤3 epochs of runway; pt1 had 13
  and CV7 had 25.
- **Real cases.** D4Em5FzP… (0%) dropped 0.8 SOL per epoch until it couldn't pay and was left out
  from epoch 1074; it was later topped up. EXhYxF…, FQwewNX…, J21SMPF… and NDf9Pv6… (about 1.8M SOL
  delegated) had their vote accounts emptied to one address about an hour before the epoch 1069
  boundary and have been delinquent since epoch 1070. Their fee payers overlap, so one party
  controls all four. Whether it was an exit or a stolen key is unknown.
- **The validator count didn't drop at Alpenglow.** Vote accounts earning credits fell slowly
  from 688 (epoch 1017) to 658 (epoch 1076), with no cliff at 1064. This counts only vote accounts
  that still exist, from 64 epochs of `epochCredits`.
- **SIMD-0123 (block-revenue sharing) is not active.** Its feature account `Crbnc267…` doesn't
  exist, all 693 vote accounts are at `blockRevenueCommissionBps` 10000, and every
  `pendingDelegatorRewards` is 0. The fields exist in vote state v4 but do nothing yet.

### What to alert on (one check per epoch)

| Signal | Why | Source |
|---|---|---|
| **VAT runway** = (vote balance − rent − pending rewards) ÷ VAT, warn at ≤3 epochs for 0% validators | Most likely way a 0/0 validator stops paying; gives epochs of warning. Alert immediately on a sudden withdrawal that leaves it below the threshold | `getAccountInfo` on the vote account |
| Delinquent / missing from the voter set | Zero rewards from that epoch on | `getVoteAccounts`, epoch `VATDebit` records |
| Inflation, MEV and block-revenue commission | Commission increases; scheduled changes may be visible before they apply (delayed commission changes since epoch 991) | Vote state, TDA, PFDA |
| TDA created this epoch | No TDA means no MEV for delegators | `mev_history.py` |
| Skip rate and vote efficiency vs. network | Now affects inflation as well as MEV | `alpenglow_check.py` |
| Own stake account's realized reward vs. the model | Catches anything not anticipated | `getInflationReward`, claims |
| SIMD-0123 feature account appears | Block commission becomes a third commission to compare on and a new place for quiet increases. Vote state keeps only the current value, so snapshot it every epoch | `getAccountInfo` on `Crbnc267…` |

### Things to evaluate when choosing a validator (filters, not a ranking)

- **Clean commission history** across inflation, MEV and TDA presence (the existing scripts).
- **A sustainable business.** A 0/0 validator lives on block revenue (priority fees, kept 100%
  until SIMD-0123) plus self-stake income, against VAT and hardware. Either it is big enough for
  block fees to cover costs, or it's a loss leader for a parent business (e.g. he1ius → Helius).
  The risky profile is small, 0/0, with no visible parent business and little self-stake.
- **Stake composition**: self / SFDP / stake pools / organic, by classifying each delegated stake
  account's withdrawer. Self-stake is only a lower bound, since an operator can use unlinked
  wallets. SFDP membership is a mild quality signal, not a red flag. Depending on it is the
  risk, and commission caps go away when a validator leaves the program. The test: if SFDP and
  pool stake left, would it still cover its costs?
- **Operator quality**: skip rate and MEV per produced block. Decompose MEV per SOL as
  leader slots per SOL (luck) × (1 − skip rate) × tips per produced block × (1 − commission).
  he1ius (t = 4.6) and 8Gbw (t = 2.8) trailed pt1 at the same 0 bps, which suggests a real
  operator gap, not luck.
- **Screen out sandwiching** validators (public trackers; check how reliable they are). It's
  ethically bad, and it gets validators removed from SFDP and pools.
- **Network-health tiebreakers**: outside the superminority, not in crowded data centers, client
  diversity.
- **Spread across two validators** so one failure costs half while the alert reaches you.

### Benchmarks for the periodic review

- **Your own stake account** (exact; no proxy account needed).
- **The peer set fixed at decision time** (an honest regret test), plus a rolling set of current
  best candidates.
- **LSTs** (JitoSOL, mSOL, …) via pool exchange rate: the alternative you could actually buy,
  after pool fees. A possible tax difference vs. native staking is worth asking a professional about.
- *Optional:* the network's stake-weighted average from a cheap model (credits × point value,
  TDA balances), checked against realized numbers for the peers.
- Switch only on an alert or a gap that holds at about 2 standard errors and exceeds the cost
  of switching (about one epoch of rewards).

### Architecture sketch

A daily collector (after the epoch boundary) that refactors the scripts into functions returning
rows; stores per-epoch × validator rows in SQLite, allowing updates because MEV claims arrive late;
diffs against the previous epoch for alerts (push notifications); and produces a static
dashboard from the store. Suggested order: store → own account and peers → alerts (VAT
runway first) → LST benchmark → dashboard → network model.

## Roadmap

- [x] Validate `commission_history.py` against live data
- [x] MEV commission history (Jito per-epoch tip distribution accounts)
- [x] Block-reward commission (SIMD-0123)
- [x] Realized delegator yield per SOL staked, ranked across a shortlist
- [x] Alpenglow inflation check (`alpenglow_check.py`)
- [ ] Ongoing monitor (see design notes above): VAT runway, delinquency, commission changes,
      TDA presence, skip rate, SIMD-0123 activation
- [ ] Stake composition and skip-rate / MEV-per-block metrics