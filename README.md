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

- TDA still open (about 10 epochs) → read from account state (`account`)
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
- **Pending claims:** epochs whose MEV claim hasn't happened yet are excluded from averages.
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

## Roadmap

- [x] Validate `commission_history.py` against live data
- [x] MEV commission history (Jito per-epoch tip distribution accounts)
- [x] Block-reward commission (SIMD-0123)
- [x] Realized delegator yield per SOL staked, ranked across a shortlist
- [ ] Ongoing monitor that flags commission changes or vote-credit slippage