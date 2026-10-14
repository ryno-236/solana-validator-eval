#!/usr/bin/env python3
"""
Rank validators by realized delegator yield: SOL actually received per SOL staked,
per epoch, from all reward sources. Everything comes from chain data.

How it works:
  For each validator, pick a plain delegator stake account: owned by a wallet
  (withdrawer on the ed25519 curve, which excludes stake pools and program-owned
  vaults), active before the window, not deactivating, and with no splits,
  merges or redelegations during the window (withdrawing claimed lamports is
  fine: it never touches the delegation). Then per epoch e:

    inflation  getInflationReward(stake, e).amount
    MEV        claims from the validator's Jito TipDistributionAccount for e
    block      claims from its Jito PriorityFeeDistributionAccount for e
               (SIMD-0123 protocol sharing is not active on mainnet yet; once it
               is, its payouts will arrive with the epoch's stake rewards)

  Claims are found in the stake account's own transaction history and matched to
  an epoch by the distribution account (PDA) they were paid from. The amount is
  what that account paid out, which equals what the stake account received.

  Yield per SOL = received / delegated stake during e. Delegated stake is exact:
  today's delegation minus the inflation rewards credited since (rewards compound
  into the delegation; MEV and block claims land as undelegated lamports).

  Epochs whose MEV claim has not happened yet (distribution account still open,
  no claim) are 'pending' and left out of the averages.

  Annualized by wall-clock time, not epoch count: slot time was cut in steps from
  ~420ms to ~218ms during 2026, and per-epoch rewards shrink with epoch length.
  APR = sum of per-epoch yields / sum of epoch durations x 1 year, with each
  epoch's duration measured from block times. APY compounds at the current epoch
  length (assumes claims are restaked).

Usage:
    python realized_yield.py VOTE_ACCT_1 VOTE_ACCT_2 --epochs 50 --csv yield.csv
    python realized_yield.py VOTE_ACCT=STAKE_ACCT ...     # pin a stake account
    python realized_yield.py -f candidates.txt
"""
import argparse
import base64
import csv
import struct
import sys
import time

from block_revenue import PFDA_SEED, PRIORITY_FEE_PROGRAM
from commission_history import DEFAULT_RPC, STAKE_PROGRAM, U64_MAX, rpc, settle_args
from mev_history import TDA_SEED, TIP_DISTRIBUTION_PROGRAM, _on_curve, b58decode, find_pda, live_tdas

SLOTS_PER_EPOCH = 432000  # mainnet: fixed, no warmup
SECONDS_PER_YEAR = 365.25 * 86400
# StakeInstruction tags that change a delegation: DelegateStake, Split, Deactivate,
# Merge, DeactivateDelinquent, Redelegate, MoveStake
DELEGATION_CHANGING = {2, 3, 5, 7, 14, 15, 16}
# StakeStateV2 slice from offset 12: staker(32) withdrawer(32) lockup(48) voter(32)
# stake u64, activation u64, deactivation u64
META_SLICE = (12, 168)


def candidate_stake_accounts(url, vote, start_epoch):
    """Wallet-owned stake accounts delegated to `vote`, active since before
    start_epoch, largest first."""
    accts = rpc(url, "getProgramAccounts", [STAKE_PROGRAM, {
        "encoding": "base64",
        "dataSlice": {"offset": META_SLICE[0], "length": META_SLICE[1]},
        "filters": [{"dataSize": 200}, {"memcmp": {"offset": 124, "bytes": vote}}],
    }])
    out = []
    for a in accts:
        raw = base64.b64decode(a["account"]["data"][0])
        withdrawer = raw[32:64]
        stake, act, deact = struct.unpack_from("<QQQ", raw, 144)
        if act >= start_epoch or deact != U64_MAX or not _on_curve(withdrawer):
            continue
        out.append((stake, a["pubkey"]))
    return [p for _, p in sorted(out, reverse=True)]


def signatures_since(url, address, start_slot):
    sigs, before = [], None
    while True:
        page = rpc(url, "getSignaturesForAddress", [address, {"limit": 1000, **({"before": before} if before else {})}])
        sigs += [s for s in page if s["slot"] >= start_slot]
        if len(page) < 1000 or page[-1]["slot"] < start_slot:
            return [s for s in sigs if s["err"] is None]


def scan_claims(url, stake, dist_accounts, start_slot, sleep):
    """({(kind, epoch): lamports}, [txs that changed the delegation]) for the stake account
    since start_slot. dist_accounts maps distribution PDA -> (kind, epoch).

    Withdrawals, plain transfers and spam dust only move undelegated lamports, so they
    don't affect the yield denominator; only delegation-changing Stake instructions do."""
    claims, changed = {}, []
    for s in signatures_since(url, stake, start_slot):
        tx = rpc(url, "getTransaction", [s["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 0}])
        meta = tx["meta"]
        msg = tx["transaction"]["message"]
        keys = (msg["accountKeys"]
                + meta.get("loadedAddresses", {}).get("writable", [])
                + meta.get("loadedAddresses", {}).get("readonly", []))
        delta = {k: meta["postBalances"][i] - meta["preBalances"][i] for i, k in enumerate(keys)}
        paid = [(dist_accounts[k], -delta[k]) for k in keys if k in dist_accounts and delta[k] < 0]
        if paid and delta.get(stake, 0) > 0:
            for key, amt in paid:
                claims[key] = claims.get(key, 0) + min(amt, delta[stake])
        ixs = msg["instructions"] + [ix for inner in meta.get("innerInstructions") or [] for ix in inner["instructions"]]
        for ix in ixs:
            if keys[ix["programIdIndex"]] != STAKE_PROGRAM or stake not in (keys[j] for j in ix["accounts"]):
                continue
            data = b58decode(ix["data"])
            if len(data) >= 4 and struct.unpack_from("<I", data)[0] in DELEGATION_CHANGING:
                changed.append((s["slot"], s["signature"], struct.unpack_from("<I", data)[0]))
        time.sleep(sleep)
    return claims, changed


def epoch_start_time(url, epoch):
    slot = rpc(url, "getBlocksWithLimit", [epoch * SLOTS_PER_EPOCH, 1])[0]
    return rpc(url, "getBlockTime", [slot])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("accounts", nargs="*", help="VOTE or VOTE=STAKE entries")
    ap.add_argument("-f", "--file", help="file with one VOTE or VOTE=STAKE per line")
    ap.add_argument("--epochs", type=int, default=30, help="completed epochs to look back (default 30)")
    ap.add_argument("--rpc", default=DEFAULT_RPC, help="RPC endpoint URL (default: $SOLANA_RPC_URL, else Helius via $HELIUS_API_KEY or .env, else public mainnet)")
    ap.add_argument("--csv", help="optional path to write per-epoch results")
    ap.add_argument("--sleep", type=float, default=None, help="pause between calls in seconds (default 0.3 on the public endpoint, else 0)")
    args = ap.parse_args()
    settle_args(args)

    entries = list(args.accounts)
    if args.file:
        with open(args.file) as fh:
            entries += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    pins = {}
    for e in entries:
        vote, _, stake = e.partition("=")
        pins.setdefault(vote.strip(), stake.strip() or None)
    votes = list(pins)
    if not votes:
        ap.error("no vote accounts given")

    now_epoch = rpc(args.rpc, "getEpochInfo")["epoch"]
    epochs = list(range(now_epoch - 1, now_epoch - 1 - args.epochs, -1))
    start_slot = epochs[-1] * SLOTS_PER_EPOCH
    starts = {e: epoch_start_time(args.rpc, e) for e in range(epochs[-1], now_epoch + 1)}
    dur = {e: starts[e + 1] - starts[e] for e in epochs}  # seconds
    cur_epoch_s = dur[epochs[0]]
    print(f"Epochs {epochs[-1]}..{epochs[0]}; epoch length {min(dur.values()) / 3600:.1f}-"
          f"{max(dur.values()) / 3600:.1f} h (latest {cur_epoch_s / 3600:.2f} h)\n")

    rows = []  # per (epoch, vote)
    chosen, notes = {}, {}
    for v in votes:
        dist = {}
        for e in range(epochs[-1] - 11, epochs[0] + 1):  # claims arrive an epoch or more late
            dist[find_pda([TDA_SEED, b58decode(v), struct.pack("<Q", e)], TIP_DISTRIBUTION_PROGRAM)] = ("mev", e)
            dist[find_pda([PFDA_SEED, b58decode(v), struct.pack("<Q", e)], PRIORITY_FEE_PROGRAM)] = ("block", e)
        cands = [pins[v]] if pins[v] else candidate_stake_accounts(args.rpc, v, epochs[-1])[:3]
        picked = None
        for stake in cands:
            claims, other = scan_claims(args.rpc, stake, dist, start_slot, args.sleep)
            if not other or pins[v]:
                picked = (stake, claims, other)
                break
            print(f"{v}: {stake} changed its delegation {len(other)} times in the window; trying next", file=sys.stderr)
        if picked is None:
            print(f"{v}: no clean wallet-owned stake account found; pin one with {v}=<STAKE>", file=sys.stderr)
            continue
        chosen[v] = picked
        open_tdas = live_tdas(args.rpc, v)
        notes[v] = {e for e, t in open_tdas.items() if e in epochs}

    # Inflation for all chosen stake accounts, one call per epoch.
    infl = {}
    stakes = [chosen[v][0] for v in chosen]
    for e in epochs:
        res = rpc(args.rpc, "getInflationReward", [stakes, {"epoch": e}])
        for v, r in zip(chosen, res):
            infl[(v, e)] = r
        time.sleep(args.sleep)

    for v, (stake, claims, other) in chosen.items():
        acct = rpc(args.rpc, "getAccountInfo", [stake, {"encoding": "jsonParsed"}])["value"]
        delegation = int(acct["data"]["parsed"]["info"]["stake"]["delegation"]["stake"])
        later = 0  # inflation credited after epoch e (rewards for e and newer)
        for e in epochs:  # newest first
            r = infl[(v, e)]
            amt = r["amount"] if r else 0
            later += amt
            delegated = delegation - later  # stake during epoch e, before its reward
            mev = claims.get(("mev", e), 0)
            block = claims.get(("block", e), 0)
            pending = mev == 0 and e in notes[v]  # TDA still open and not yet claimed
            rows.append({"epoch": e, "vote": v, "stake": stake, "delegated": delegated, "seconds": dur[e],
                         "inflation": amt, "mev": mev, "block": block, "pending": pending})
        if other:
            print(f"{v}: pinned {stake} changed its delegation {len(other)} times; yields may be off", file=sys.stderr)

    # Ranking
    summary = []
    for v in chosen:
        mine = [r for r in rows if r["vote"] == v and not r["pending"]]
        if not mine:
            continue
        secs = sum(r["seconds"] for r in mine)
        apr = {k: sum(r[k] / r["delegated"] for r in mine) / secs * SECONDS_PER_YEAR
               for k in ("inflation", "mev", "block")}
        total = sum(apr.values())
        worst = min((r["inflation"] + r["mev"] + r["block"]) / r["delegated"] / r["seconds"] * SECONDS_PER_YEAR
                    for r in mine)
        summary.append((total, v, apr, len(mine), sum(1 for r in mine if r["inflation"] == 0), worst))
    summary.sort(reverse=True)
    per_year = SECONDS_PER_YEAR / cur_epoch_s

    print(f"{'#':>2} {'vote account':<46} {'n':>3} {'infl APR':>9} {'MEV APR':>8} {'block APR':>9} "
          f"{'total APR':>9} {'APY':>7} {'per epoch':>10} {'worst':>7} {'miss':>4}")
    print("-" * 128)
    for i, (t, v, m, n, miss, worst) in enumerate(summary, 1):
        apy = (1 + t / per_year) ** per_year - 1
        print(f"{i:>2} {v:<46} {n:>3} {m['inflation']:>9.3%} {m['mev']:>8.3%} {m['block']:>9.3%} "
              f"{t:>9.3%} {apy:>7.3%} {t / per_year:>10.3e} {worst:>7.3%} {miss:>4}")
    print("\n'n' = epochs used (pending MEV claims excluded). APRs are time-weighted over those epochs.")
    print(f"'per epoch' = SOL per SOL staked per epoch at the latest epoch length ({cur_epoch_s / 3600:.2f} h).")
    print("'worst' = lowest single epoch, annualized. 'miss' = epochs with no inflation reward.")
    print("Stake accounts measured:")
    for v, (stake, _, other) in chosen.items():
        pend = sorted(e for e in notes[v] if any(r["pending"] for r in rows if r["vote"] == v and r["epoch"] == e))
        print(f"  {v} -> {stake}" + (f"   pending MEV: {pend}" if pend else "")
              + (f"   WARNING {len(other)} delegation changes" if other else ""))

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["epoch", "vote_account", "stake_account", "delegated_lamports", "inflation_lamports",
                        "mev_lamports", "block_lamports", "total_per_sol", "epoch_seconds", "annualized_apr",
                        "mev_pending"])
            for r in rows:
                tot = (r["inflation"] + r["mev"] + r["block"]) / r["delegated"]
                w.writerow([r["epoch"], r["vote"], r["stake"], r["delegated"], r["inflation"], r["mev"],
                            r["block"], f"{tot:.6e}", r["seconds"], f"{tot / r['seconds'] * SECONDS_PER_YEAR:.6f}",
                            r["pending"]])
        print(f"Wrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
