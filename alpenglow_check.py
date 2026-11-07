#!/usr/bin/env python3
"""
Check whether validators lost consensus (inflation) rewards after Alpenglow, and why.

How Alpenglow pays inflation (Agave source, Oct 2026; mainnet since epoch 1064):
  Votes are no longer transactions. For each slot s, the leader of slot s+8 posts a
  reward certificate (BLS aggregate + bitmap) of the validators whose notarize/skip
  vote for s it collected. Each included validator earns

      0.5 x (R / slots_per_epoch) x stake / total_stake

  and the leader earns the same amount again for every validator it included.
  R is the epoch's maximum inflation, stored on-chain in the off-curve account
  PDA(["vote_reward_account"], Alpenglow feature id) for the current and previous
  epoch. Stake weights come from the epoch-stakes snapshot, i.e. stake effective in
  epoch e-1. The amounts are added to the vote account's epochCredits, so credits
  are now lamports. At the epoch boundary each stake account gets
  credits x its stake / validator's delegated stake in e x (1 - commission).

  So per validator and epoch:
    total_eff  = credits / (R x s[e-1] / S[e-1])           1.0 = full stake share
    leader     = assigned leader slots vs expected (luck) and blocks produced
    vote_eff   = (credits - leader credits) / (0.5 x R x s[e-1] / S[e-1])
                 the voting half alone: the direct "trouble voting" signal
  Leader credits = 0.5 x R/slots x blocks produced x c, where c is the network's
  average certificate coverage, solved from all validators' credits (every inclusion
  pays the voter and the leader equally). The network's own vote_eff is shown as a
  reference: slots whose s+8 block was skipped pay no one.

  delegator index = delegator's inflation per SOL / (R / S[e-1]): what a delegator
  actually got relative to a perfect 0% validator, including commission and dilution
  from stake that joined since the snapshot.

  s[e-1] is exact: the validator's delegated stake in e-1, recovered from a
  delegator's payout in e-1 (credits x (1 - commission) / payout per SOL), so the
  first Alpenglow epoch only gets total_eff from current stake, not the split.
  S[e-1] is the network's effective stake from the StakeHistory sysvar.

Usage:
    python alpenglow_check.py -f candidates.txt --csv ag.csv
    python alpenglow_check.py VOTE_ACCT VOTE_ACCT=STAKE_ACCT --baseline 10
"""
import argparse
import base64
import csv
import statistics
import struct
import sys
import time

from commission_history import DEFAULT_RPC, rpc, settle_args
from mev_history import find_pda
from realized_yield import (SECONDS_PER_YEAR, SLOTS_PER_EPOCH, candidate_stake_accounts,
                            epoch_start_time, scan_claims)

ALPENGLOW_FEATURE = "A1pengvuM6JEcyNuTnMqepBKhwHE3N6PmUrdATGawhJS"
FIRST_AG_EPOCH = 1064  # mainnet migration epoch
STAKE_HISTORY = "SysvarStakeHistory1111111111111111111111111"
U64_MAX = 2**64 - 1
FLAG_BELOW = 0.02  # flag vote_eff this far under the network


def epoch_inflation(url):
    """{epoch: max possible validator reward} for the current and previous epoch."""
    addr = find_pda([b"vote_reward_account"], ALPENGLOW_FEATURE)
    raw = base64.b64decode(rpc(url, "getAccountInfo", [addr, {"encoding": "base64"}])["value"]["data"][0])
    reward, _, epoch = struct.unpack_from("<QQQ", raw, 0)
    out = {epoch: reward}
    if raw[24] == 1:
        reward, _, epoch = struct.unpack_from("<QQQ", raw, 25)
        out[epoch] = reward
    return out


def inflation_for(known, epoch):
    """R for any epoch: on-chain when stored, else extrapolated at the observed
    per-epoch rate (R moves ~0.03% per epoch)."""
    if epoch in known:
        return known[epoch]
    lo, hi = min(known), max(known)
    ratio = known[hi] / known[lo] if hi > lo else 1.0
    return known[lo] * ratio ** (epoch - lo)


def network_stake(url):
    """{epoch: effective stake} from the StakeHistory sysvar."""
    raw = base64.b64decode(rpc(url, "getAccountInfo", [STAKE_HISTORY, {"encoding": "base64"}])["value"]["data"][0])
    n = struct.unpack_from("<Q", raw)[0]
    return {e: eff for e, eff, _, _ in (struct.unpack_from("<QQQQ", raw, 8 + 32 * i) for i in range(n))}


def earned_credits(epoch_credits):
    """{epoch: credits earned} from a parsed epochCredits list, skipping the migration marker."""
    return {int(c["epoch"]): int(c["credits"]) - int(c["previousCredits"])
            for c in epoch_credits if int(c["epoch"]) != U64_MAX}


def leader_counts(url, epoch, sleep):
    """({identity: assigned slots}, {identity: blocks produced}, total blocks) for an epoch."""
    first, assigned, produced = epoch * SLOTS_PER_EPOCH, {}, {}
    leaders = []
    for s in range(first, first + SLOTS_PER_EPOCH, 5000):
        leaders += rpc(url, "getSlotLeaders", [s, min(5000, first + SLOTS_PER_EPOCH - s)])
        time.sleep(sleep)
    blocks = set()
    for s in range(first, first + SLOTS_PER_EPOCH, 100000):
        blocks.update(rpc(url, "getBlocks", [s, min(s + 99999, first + SLOTS_PER_EPOCH - 1)]))
    for i, who in enumerate(leaders):
        assigned[who] = assigned.get(who, 0) + 1
        if first + i in blocks:
            produced[who] = produced.get(who, 0) + 1
    return assigned, produced, len(blocks)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("accounts", nargs="*", help="VOTE or VOTE=STAKE entries")
    ap.add_argument("-f", "--file", action="append", help="file with one VOTE or VOTE=STAKE per line (repeatable)")
    ap.add_argument("--baseline", type=int, default=10, help="TowerBFT epochs before Alpenglow to compare (default 10)")
    ap.add_argument("--rpc", default=DEFAULT_RPC, help="RPC endpoint URL (default: $SOLANA_RPC_URL, else Helius via $HELIUS_API_KEY or .env, else public mainnet)")
    ap.add_argument("--csv", help="optional path to write per-epoch results")
    ap.add_argument("--sleep", type=float, default=None, help="pause between calls in seconds (default 0.3 on the public endpoint, else 0)")
    args = ap.parse_args()
    settle_args(args)

    entries = list(args.accounts)
    for path in args.file or []:
        with open(path) as fh:
            entries += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    pins = {}
    for e in entries:
        vote, _, stake = e.partition("=")
        pins.setdefault(vote.strip(), stake.strip() or None)
    votes = list(pins)
    if not votes:
        ap.error("no vote accounts given")

    now = rpc(args.rpc, "getEpochInfo")["epoch"]
    ag_epochs = list(range(FIRST_AG_EPOCH, now))
    tower_epochs = list(range(FIRST_AG_EPOCH - args.baseline, FIRST_AG_EPOCH))
    R = epoch_inflation(args.rpc)
    S = network_stake(args.rpc)
    starts = {e: epoch_start_time(args.rpc, e) for e in range(FIRST_AG_EPOCH, now + 1)}
    print(f"Alpenglow epochs {ag_epochs[0]}..{ag_epochs[-1]}; TowerBFT baseline {tower_epochs[0]}..{tower_epochs[-1]}")
    print(f"R on-chain for epochs {sorted(R)}; older epochs extrapolated\n")

    # Every vote account's credits: network references (tower max, AG coverage).
    va = rpc(args.rpc, "getVoteAccounts")
    everyone = [a["votePubkey"] for a in va["current"] + va["delinquent"]]
    credits, info = {}, {}
    for i in range(0, len(everyone), 100):
        res = rpc(args.rpc, "getMultipleAccounts", [everyone[i:i + 100], {"encoding": "jsonParsed"}])["value"]
        for v, acct in zip(everyone[i:i + 100], res):
            if acct:
                credits[v] = earned_credits(acct["data"]["parsed"]["info"]["epochCredits"])
    for i in range(0, len(votes), 100):
        res = rpc(args.rpc, "getMultipleAccounts", [votes[i:i + 100], {"encoding": "jsonParsed"}])["value"]
        for v, acct in zip(votes[i:i + 100], res):
            info[v] = acct["data"]["parsed"]["info"]
            credits[v] = earned_credits(info[v]["epochCredits"])
    tower_max = {e: max(c.get(e, 0) for c in credits.values()) for e in tower_epochs}

    print("Leader schedules and produced blocks per epoch...", file=sys.stderr)
    leaders = {e: leader_counts(args.rpc, e, args.sleep) for e in ag_epochs}
    net = {}
    for e in ag_epochs:
        total = sum(c.get(e, 0) for c in credits.values())
        net[e] = {"vote_eff": total / inflation_for(R, e),
                  "coverage": total * SLOTS_PER_EPOCH / (inflation_for(R, e) * leaders[e][2])}

    # A clean delegator stake account per validator: exact delegated stake per epoch.
    stake_of, payout = {}, {}
    start_slot = (FIRST_AG_EPOCH - 1) * SLOTS_PER_EPOCH
    for v in votes:
        cands = [pins[v]] if pins[v] else candidate_stake_accounts(args.rpc, v, FIRST_AG_EPOCH - 1)[:3]
        for stake in cands:
            _, changed = scan_claims(args.rpc, stake, {}, start_slot, args.sleep)
            if not changed or pins[v]:
                stake_of[v] = stake
                break
        else:
            print(f"{v}: no clean wallet-owned stake account; pin one with {v}=<STAKE>", file=sys.stderr)
    later = {v: 0 for v in stake_of}
    now_deleg = {}
    for v, stake in stake_of.items():
        acct = rpc(args.rpc, "getAccountInfo", [stake, {"encoding": "jsonParsed"}])["value"]
        now_deleg[v] = int(acct["data"]["parsed"]["info"]["stake"]["delegation"]["stake"])
    for e in reversed(ag_epochs):
        res = rpc(args.rpc, "getInflationReward", [list(stake_of.values()), {"epoch": e}])
        for v, r in zip(stake_of, res):
            amt = r["amount"] if r else 0
            later[v] += amt
            payout[(v, e)] = (amt, now_deleg[v] - later[v])  # (reward, delegated during e)
        time.sleep(args.sleep)

    rows = []
    for v in votes:
        comm = int(info[v].get("inflationRewardsCommissionBps", info[v].get("commission", 0) * 100)) / 10000
        node = info[v]["nodePubkey"]
        c = credits[v]
        val_stake = {}  # validator's delegated stake during e, from the delegator payout
        for e in ag_epochs:
            if (v, e) in payout and payout[(v, e)][0] and c.get(e):
                amt, deleg = payout[(v, e)]
                val_stake[e] = c[e] * (1 - comm) * deleg / amt
        for e in ag_epochs:
            r_e = inflation_for(R, e)
            s_w = val_stake.get(e - 1)
            row = {"epoch": e, "vote": v, "credits": c.get(e, 0), "weight_stake": s_w,
                   "net_vote_eff": net[e]["vote_eff"], "total_eff": None, "assigned": None,
                   "expected": None, "produced": None, "vote_eff": None, "delegator_index": None,
                   "delegator_apr": None}
            assigned, produced, _ = leaders[e]
            row["assigned"], row["produced"] = assigned.get(node, 0), produced.get(node, 0)
            if s_w:
                share = s_w / S[e - 1]
                row["total_eff"] = c.get(e, 0) / (r_e * share)
                row["expected"] = SLOTS_PER_EPOCH * share
                leader_credits = 0.5 * r_e / SLOTS_PER_EPOCH * row["produced"] * net[e]["coverage"]
                row["vote_eff"] = (c.get(e, 0) - leader_credits) / (0.5 * r_e * share)
            if (v, e) in payout:
                amt, deleg = payout[(v, e)]
                row["delegator_index"] = amt / deleg / (r_e / S[e - 1])
                row["delegator_apr"] = amt / deleg / (starts[e + 1] - starts[e]) * SECONDS_PER_YEAR
            rows.append(row)

    print(f"Network: " + "  ".join(f"{e}: vote_eff {net[e]['vote_eff']:.3f}, coverage {net[e]['coverage']:.3f}"
                                   for e in ag_epochs) + "\n")
    hdr = (f"{'vote account':<46} {'tower':>6} {'vote_eff':>8} {'vs net':>7} {'total':>6} "
           f"{'ldr luck':>8} {'ldr skip':>8} {'deleg idx':>9} {'deleg APR':>9}  flag")
    print(hdr)
    print("-" * len(hdr))
    summary = []
    for v in votes:
        mine = [r for r in rows if r["vote"] == v]
        split = [r for r in mine if r["vote_eff"] is not None]
        tower = statistics.mean(credits[v].get(e, 0) / tower_max[e] for e in tower_epochs)
        ve = statistics.mean(r["vote_eff"] for r in split) if split else None
        gap = statistics.mean(r["vote_eff"] - r["net_vote_eff"] for r in split) if split else None
        tot = statistics.mean(r["total_eff"] for r in split) if split else None
        exp = sum(r["expected"] for r in split)
        luck = sum(r["assigned"] for r in split) / exp if exp else None
        asg = sum(r["assigned"] for r in split)
        skip = 1 - sum(r["produced"] for r in split) / asg if asg else None
        idx = [r["delegator_index"] for r in mine if r["delegator_index"] is not None]
        apr_rows = [r for r in mine if r["delegator_apr"] is not None]
        apr = statistics.mean(r["delegator_apr"] for r in apr_rows) if apr_rows else None
        flag = []
        if gap is not None and gap < -FLAG_BELOW:
            flag.append("VOTING BELOW NETWORK")
        if skip is not None and skip > 0.05:
            flag.append("SKIPPING LEADER SLOTS")
        if v not in stake_of:
            flag.append("no stake account")
        f = lambda x, fmt: "-" if x is None else format(x, fmt)
        print(f"{v:<46} {tower:>6.4f} {f(ve, '8.3f')} {f(gap, '+7.3f')} {f(tot, '6.3f')} "
              f"{f(luck, '8.3f')} {f(skip, '8.2%')} {f(statistics.mean(idx) if idx else None, '9.3f')} "
              f"{f(apr, '9.3%')}  {', '.join(flag)}")
        summary.append(v)
    print(f"\n'tower' = mean vote credits / network best, {tower_epochs[0]}..{tower_epochs[-1]} (1.0 = perfect).")
    print(f"'vote_eff' = voting half alone, mean over epochs with an exact stake weight ({ag_epochs[1]}+);")
    print("  'vs net' = minus the network's vote_eff that epoch. Flagged when below by more than "
          f"{FLAG_BELOW}.")
    print("'total' = credits / full stake share (both halves). 'ldr luck' = leader slots assigned / expected;")
    print("  'ldr skip' = assigned slots with no block. Luck averages out; skips are the operator's.")
    print("'deleg idx' = delegator inflation per SOL / perfect 0% validator (commission and dilution included).")
    print("'deleg APR' = delegator inflation APR, mean over Alpenglow epochs.")
    print("Stake accounts used: " + ", ".join(f"{v[:8]}->{s[:8]}" for v, s in stake_of.items()))

    if args.csv:
        cols = ["epoch", "vote", "credits", "weight_stake", "total_eff", "assigned", "expected", "produced",
                "vote_eff", "net_vote_eff", "delegator_index", "delegator_apr"]
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
