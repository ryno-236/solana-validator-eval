#!/usr/bin/env python3
"""
Check block-revenue (priority fee) sharing for Solana validators, straight from chain.

Two ways a validator can share block revenue with delegators (checked Oct 2026):

1. Protocol: SIMD-0123. Commission is `blockRevenueCommissionBps` in vote state v4
   (default 10000 = validator keeps everything); the delegator share accrues in
   `pendingDelegatorRewards` and is paid at the epoch boundary. Gated by feature
   `block_revenue_sharing` (Crbnc267...). Until that feature activates nothing is
   shared, whatever the field says. Vote state keeps only the current value, so
   once active its history is not recoverable from account state; realized payouts
   to stake accounts are the record.

2. Off-protocol: Jito's Priority Fee Distribution program. Same shape as MEV tip
   distribution: a per-epoch PDA ["PF_DISTRIBUTION_ACCOUNT", vote, epoch] whose
   `validator_commission_bps` is fixed at initialization, claimed by delegators via
   a merkle tree. Unlike MEV tips, funding is voluntary: the validator decides how
   much to transfer in (`total_lamports_transferred`), so commission alone does not
   say how much is shared. Accounts close ~10 epochs after creation; older epochs
   are recovered from the initialize instruction (commission only, not amount).

Usage:
    python block_revenue.py VOTE_ACCT_1 VOTE_ACCT_2 --epochs 50 --csv block.csv
"""
import argparse
import base64
import csv
import hashlib
import struct
import sys
import time

from commission_history import DEFAULT_RPC, rpc, settle_args
from mev_history import b58decode, b58encode, commission_from_history, find_pda

BLOCK_REVENUE_FEATURE = "Crbnc267wkJvFhYakqwWWX57sx5bkrftJLekzy6yWKLT"  # SIMD-0123
PRIORITY_FEE_PROGRAM = "Priority6weCZ5HwDn29NxLFpb7TDp2iLZ6XKc5e8d3"
PFDA_SEED = b"PF_DISTRIBUTION_ACCOUNT"
PFDA_ACCOUNT_DISC = hashlib.sha256(b"account:PriorityFeeDistributionAccount").digest()[:8]
INIT_PFDA_DISC = hashlib.sha256(b"global:initialize_priority_fee_distribution_account").digest()[:8]


def feature_activation_slot(url, feature):
    """Activation slot, or None if not active. Feature accounts hold Option<u64>."""
    acct = rpc(url, "getAccountInfo", [feature, {"encoding": "base64"}])["value"]
    if acct is None:
        return None
    raw = base64.b64decode(acct["data"][0])
    return struct.unpack_from("<Q", raw, 1)[0] if raw[0] == 1 else None


def parse_pfda(raw):
    """PriorityFeeDistributionAccount: disc, vote, upload authority, Option<MerkleRoot>,
    epoch_created_at u64, validator_commission_bps u16, expires_at u64,
    total_lamports_transferred u64, bump u8."""
    o = 8 + 32 + 32
    o += 1 + (64 if raw[o] == 1 else 0)
    epoch, bps, _expires, transferred = struct.unpack_from("<QHQQ", raw, o)
    return {"epoch": epoch, "bps": bps, "transferred": transferred}


def live_pfdas(url, vote):
    accts = rpc(url, "getProgramAccounts", [PRIORITY_FEE_PROGRAM, {"encoding": "base64", "filters": [
        {"memcmp": {"offset": 0, "bytes": b58encode(PFDA_ACCOUNT_DISC)}},
        {"memcmp": {"offset": 8, "bytes": vote}}]}])
    out = {}
    for a in accts:
        p = parse_pfda(base64.b64decode(a["account"]["data"][0]))
        p["address"] = a["pubkey"]
        out[p["epoch"]] = p
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("accounts", nargs="*", help="vote account addresses")
    ap.add_argument("-f", "--file", help="file with one vote account per line")
    ap.add_argument("--epochs", type=int, default=30, help="completed epochs to look back (default 30)")
    ap.add_argument("--rpc", default=DEFAULT_RPC, help="RPC endpoint URL (default: $SOLANA_RPC_URL, else Helius via $HELIUS_API_KEY or .env, else public mainnet)")
    ap.add_argument("--csv", help="optional path to write per-epoch results")
    ap.add_argument("--sleep", type=float, default=None, help="pause between calls in seconds (default 0.3 on the public endpoint, else 0)")
    args = ap.parse_args()
    settle_args(args)

    votes = [a.partition("=")[0] for a in args.accounts]
    if args.file:
        with open(args.file) as fh:
            votes += [ln.strip().partition("=")[0] for ln in fh if ln.strip() and not ln.startswith("#")]
    votes = list(dict.fromkeys(votes))
    if not votes:
        ap.error("no vote accounts given")

    sched = rpc(args.rpc, "getEpochSchedule")
    last_done = rpc(args.rpc, "getEpochInfo")["epoch"] - 1
    epochs = list(range(last_done, last_done - args.epochs, -1))
    act = feature_activation_slot(args.rpc, BLOCK_REVENUE_FEATURE)
    # Mainnet has no warmup epochs, so slot // slots_per_epoch is the epoch.
    act_epoch = None if act is None else act // sched["slotsPerEpoch"]
    print(f"Checking block-revenue sharing for epochs {epochs[-1]}..{epochs[0]}")
    print(f"SIMD-0123 block_revenue_sharing: "
          + ("NOT ACTIVE (protocol shares nothing)" if act_epoch is None else f"active since epoch {act_epoch}") + "\n")

    rows = []  # (epoch, vote, protocol_bps, protocol_source, pfda, pfda_bps, transferred, pfda_source)
    state = {}
    for v in votes:
        info = rpc(args.rpc, "getAccountInfo", [v, {"encoding": "jsonParsed"}])["value"]["data"]["parsed"]["info"]
        state[v] = info
        live = live_pfdas(args.rpc, v)
        for e in epochs:
            if act_epoch is None or e < act_epoch:
                proto_bps, proto_src = 10000, "feature inactive"
            else:
                proto_bps, proto_src = info.get("blockRevenueCommissionBps", 10000), "current state only"
            if e in live:
                p = live[e]
                rows.append((e, v, proto_bps, proto_src, p["address"], p["bps"], p["transferred"], "account"))
                continue
            addr = find_pda([PFDA_SEED, b58decode(v), struct.pack("<Q", e)], PRIORITY_FEE_PROGRAM)
            try:
                found = commission_from_history(args.rpc, addr, args.sleep, PRIORITY_FEE_PROGRAM, INIT_PFDA_DISC)
            except Exception as exc:
                rows.append((e, v, proto_bps, proto_src, addr, None, None, f"error: {exc}"))
                print(f"{v} epoch {e}: {exc}", file=sys.stderr)
                continue
            if found is None:
                rows.append((e, v, proto_bps, proto_src, addr, None, None, "none"))
            else:
                rows.append((e, v, proto_bps, proto_src, addr, found[0], None, "init tx"))
            time.sleep(args.sleep)

    print(f"{'vote account':<46} {'protoBps':>8} {'pending':>8} {'jitoPF':>7} {'maxBps':>7} {'SOL in':>8}")
    print("-" * 92)
    for v in votes:
        mine = [r for r in rows if r[1] == v]
        pf = [r for r in mine if r[5] is not None]
        err = sum(1 for r in mine if r[7].startswith("error"))
        sent = sum(r[6] for r in pf if r[6]) / 1e9
        mx = max((r[5] for r in pf), default=None)
        print(f"{v:<46} {state[v].get('blockRevenueCommissionBps', '-'):>8} "
              f"{int(state[v].get('pendingDelegatorRewards', 0)) / 1e9:>8.3f} {len(pf):>7} "
              f"{'-' if mx is None else mx:>7} {sent:>8.3f}" + (f"  ({err} errors)" if err else ""))
    print("\n'protoBps' = current vote-state block revenue commission (10000 = keeps all);")
    print("  'pending' = SOL accrued for delegators under SIMD-0123.")
    print("'jitoPF' = epochs with a Jito priority-fee distribution account; 'maxBps' its highest")
    print("  commission; 'SOL in' = lamports the validator chose to transfer in (live accounts only).")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["epoch", "vote_account", "protocol_commission_bps", "protocol_source",
                        "pf_distribution_account", "pf_commission_bps", "pf_lamports_transferred", "pf_source"])
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
