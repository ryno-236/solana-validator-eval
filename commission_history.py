#!/usr/bin/env python3
"""
Verify historical inflation commission for Solana validators, straight from chain.

How it works (checked against mainnet, Agave 4.3, vote state v4, Oct 2026):
  At each epoch boundary the commission share of a validator's inflation reward
  is paid to its *inflation rewards collector* (vote state v4 field; defaults to
  the vote account itself, but SIMD-0232 lets it be any system account, and a
  few validators use one). Delegators' shares are paid to their stake accounts,
  spread over many blocks (partitioned rewards, SIMD-0118).

  Reward records no longer carry a usable commission: `commission` is null on
  every record, Voting rewards included. So commission is established two ways:

  1. Payout records (getInflationReward on stake, vote and collector accounts):
     - every vote account / collector now gets a Voting record each epoch,
       with 0 lamports when no commission was taken
     - collector record = 0 lamports and stake paid -> 0%           ("collector zero")
     - collector record > 0                          -> commission > 0 ("collector paid")
     - commission / commissionBps field present      -> used directly ("record")
     - stake paid but no collector record            -> unknown; commission may have gone
       to an account we did not query                ("no collector record")
  2. Point-value cross-check, independent of any commission field:
       delegator reward = stake * credits_earned * point_value * (1 - commission)
     credits come from the vote account's epochCredits (64 epochs kept on-chain).
     point_value is exact for the latest epoch (EpochRewards sysvar). For older
     epochs it is the best reward/(stake*credits) seen in the batch, so the
     implied commission is relative to the best validator queried.

  We query a STAKE account delegated to each validator. By default the script
  finds one automatically: the largest delegated to the validator and active
  since before the lookback window (via getProgramAccounts on the Stake program).

Usage:
    pip install requests
    python commission_history.py VOTE_ACCT_1 VOTE_ACCT_2 --epochs 50 --csv history.csv
    python commission_history.py VOTE_ACCT=STAKE_ACCT ...       # pin a specific stake account
    python commission_history.py -f validators.txt --rpc https://<your-rpc>

Notes:
- Pass VOTE account addresses (not identity addresses).
- RPC endpoint: --rpc, else $SOLANA_RPC_URL, else Helius if $HELIUS_API_KEY is set
  (environment or .env), else the public mainnet endpoint.
- Covers inflation commission only. MEV commission lives in Jito's per-epoch
  tip distribution accounts; block-revenue commission (SIMD-0123) is separate.
"""
import argparse
import base64
import csv
import os
import struct
import sys
import time

import requests

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def load_dotenv(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Minimal .env reader (KEY=VALUE lines); real environment variables win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def default_rpc():
    """$SOLANA_RPC_URL, else Helius if $HELIUS_API_KEY is set (env or .env), else public."""
    load_dotenv()
    if os.environ.get("SOLANA_RPC_URL"):
        return os.environ["SOLANA_RPC_URL"]
    if os.environ.get("HELIUS_API_KEY"):
        return f"https://mainnet.helius-rpc.com/?api-key={os.environ['HELIUS_API_KEY']}"
    return PUBLIC_RPC


def redact(url):
    """Endpoint without its query string, safe to print."""
    return url.split("?", 1)[0]


def settle_args(args):
    """Fill endpoint-dependent defaults and say which endpoint is in use (key redacted)."""
    if args.sleep is None:
        args.sleep = 0.3 if args.rpc == PUBLIC_RPC else 0.0
    print(f"RPC: {redact(args.rpc)}", file=sys.stderr)


DEFAULT_RPC = default_rpc()
_session = requests.Session()  # reuse connections: one TLS handshake, not one per call
STAKE_PROGRAM = "Stake11111111111111111111111111111111111111"
EPOCH_REWARDS_SYSVAR = "SysvarEpochRewards1111111111111111111111111"
VOTER_OFFSET = 124            # Delegation.voter_pubkey within a stake account
DELEG_SLICE = (156, 24)       # stake u64, activation_epoch u64, deactivation_epoch u64
U64_MAX = 2**64 - 1


def rpc(url, method, params=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    for attempt in range(6):
        try:
            resp = _session.post(url, json=payload, timeout=60)
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(2 ** attempt)
            continue
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code >= 400:  # not raise_for_status(): its message includes the URL (API key)
            raise RuntimeError(f"{method}: HTTP {resp.status_code} from {redact(url)}")
        data = resp.json()
        if data.get("error", {}).get("code") == 429:  # public RPC: per-method limit
            time.sleep(2 ** attempt)
            continue
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error'].get('message', data['error'])}")
        return data["result"]
    raise RuntimeError(f"{method}: rate limited or unreachable after retries")


def vote_state(url, vote):
    """Current vote state: commission (%), inflation collector, {epoch: credits earned}."""
    info = rpc(url, "getAccountInfo", [vote, {"encoding": "jsonParsed"}])["value"]["data"]["parsed"]["info"]
    bps = info.get("inflationRewardsCommissionBps")
    credits = {int(c["epoch"]): int(c["credits"]) - int(c["previousCredits"]) for c in info["epochCredits"]}
    return {
        "commission": bps / 100 if bps is not None else info.get("commission"),
        "collector": info.get("inflationRewardsCollector") or vote,
        "credits": credits,
    }


def latest_point_value(url, epoch_info):
    """(epoch, lamports per point) for the most recent reward distribution, from the
    EpochRewards sysvar. Only the latest epoch is kept on-chain."""
    info = rpc(url, "getAccountInfo", [EPOCH_REWARDS_SYSVAR, {"encoding": "jsonParsed"}])["value"]["data"]["parsed"]["info"]
    # The sysvar is rewritten at the start of each epoch with the previous epoch's
    # totals; confirm it belongs to the current epoch's distribution.
    first_slot = epoch_info["absoluteSlot"] - epoch_info["slotIndex"]
    first_block = rpc(url, "getBlocksWithLimit", [first_slot, 1])[0]
    first_height = rpc(url, "getBlock", [first_block, {"transactionDetails": "none", "rewards": False,
                                                       "maxSupportedTransactionVersion": 0}])["blockHeight"]
    if int(info["distributionStartingBlockHeight"]) < first_height:
        return None, None
    return epoch_info["epoch"] - 1, int(info["totalRewards"]) / int(info["totalPoints"])


def undelegated_lamports(url, stake):
    """Lamports in a stake account that earn nothing: rent reserve plus any excess
    deposited on top of the delegation. Assumed constant over the window."""
    acct = rpc(url, "getAccountInfo", [stake, {"encoding": "jsonParsed"}])["value"]
    return acct["lamports"] - int(acct["data"]["parsed"]["info"]["stake"]["delegation"]["stake"])


def find_stake_account(url, vote, start_epoch):
    """Largest stake account delegated to `vote`, active since before start_epoch."""
    accts = rpc(url, "getProgramAccounts", [STAKE_PROGRAM, {
        "encoding": "base64",
        "dataSlice": {"offset": DELEG_SLICE[0], "length": DELEG_SLICE[1]},
        "filters": [{"dataSize": 200}, {"memcmp": {"offset": VOTER_OFFSET, "bytes": vote}}],
    }])
    best = None
    for a in accts:
        raw = base64.b64decode(a["account"]["data"][0])
        if len(raw) < 24:
            continue
        stake, act, deact = struct.unpack("<QQQ", raw)
        if act >= start_epoch or deact != U64_MAX:
            continue  # too new to cover the window, or deactivating/inactive
        if best is None or stake > best[1]:
            best = (a["pubkey"], stake)
    return best[0] if best else None


def classify(s_item, payees):
    """Commission (%) and method from the stake record and the collector/vote records."""
    s_amt = s_item.get("amount") if s_item else None
    for p in payees:
        if p and p.get("commissionBps") is not None:
            return p["commissionBps"] / 100, "record"
        if p and p.get("commission") is not None:
            return p["commission"], "record"
    paid = [p["amount"] for p in payees if p]
    if any(paid):
        return None, "collector paid"
    if not s_amt:
        return None, "no reward record"
    if paid:  # a 0-lamport Voting record exists for the collector
        return 0, "collector zero"
    return None, "no collector record"


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

    epoch_info = rpc(args.rpc, "getEpochInfo")
    last_done = epoch_info["epoch"] - 1
    epochs = list(range(last_done, last_done - args.epochs, -1))
    print(f"Current epoch {epoch_info['epoch']}; checking epochs {epochs[-1]}..{epochs[0]}\n")

    pv_epoch, pv_exact = latest_point_value(args.rpc, epoch_info)
    state = {v: vote_state(args.rpc, v) for v in votes}

    # Resolve a stake account per validator
    stake_for = {}
    for v in votes:
        if pins[v]:
            stake_for[v] = pins[v]
            continue
        try:
            s = find_stake_account(args.rpc, v, epochs[-1])
        except Exception as exc:
            print(f"{v}: stake account lookup failed ({exc}).\n"
                  f"  Use an RPC that allows getProgramAccounts, or pin one: {v}=<STAKE_ACCT>",
                  file=sys.stderr)
            s = None
        if s is None:
            print(f"{v}: no stake account active across the whole window found; skipping.", file=sys.stderr)
        stake_for[v] = s
        time.sleep(args.sleep)

    # Per epoch, query each validator's stake account, vote account and (if different)
    # current inflation collector together. Both payee candidates are queried because
    # the collector may have changed during the window.
    queried = [v for v in votes if stake_for[v]]
    idle = {v: undelegated_lamports(args.rpc, stake_for[v]) for v in queried}
    payees = {v: [v] + ([state[v]["collector"]] if state[v]["collector"] != v else []) for v in queried}
    rows = []  # dicts, one per (epoch, validator)

    for e in epochs:
        if not queried:
            break
        addrs = [stake_for[v] for v in queried] + [p for v in queried for p in payees[v]]
        try:
            res = rpc(args.rpc, "getInflationReward", [addrs, {"epoch": e}])
        except Exception as exc:  # usually: node lacks history for this epoch
            for v in queried:
                rows.append({"epoch": e, "vote": v, "stake": stake_for[v], "method": f"error: {exc}"})
            print(f"epoch {e}: error ({exc})", file=sys.stderr)
            time.sleep(args.sleep)
            continue
        by_addr = dict(zip(addrs[len(queried):], res[len(queried):]))
        batch = []
        for i, v in enumerate(queried):
            s_item = res[i]
            p_items = [by_addr[p] for p in payees[v]]
            comm, method = classify(s_item, p_items)
            s_amt = s_item.get("amount") if s_item else None
            credits = state[v]["credits"].get(e)
            per_point = None
            if s_amt and credits:
                stake_pre = s_item["postBalance"] - s_amt - idle[v]
                per_point = s_amt / (stake_pre * credits)
            row = {"epoch": e, "vote": v, "stake": stake_for[v], "commission": comm,
                   "stake_reward": s_amt, "commission_reward": sum(p["amount"] for p in p_items if p),
                   "credits": credits, "per_point": per_point, "method": method}
            rows.append(row)
            batch.append(row)
        # Point-value cross-check: exact for the latest epoch, else best in batch.
        ref = pv_exact if e == pv_epoch else max((r["per_point"] for r in batch if r["per_point"]), default=None)
        for r in batch:
            if ref and r["per_point"]:
                r["implied"] = round(100 * (1 - r["per_point"] / ref), 3)  # negative = paid more than inflation alone
                r["implied_basis"] = "exact" if e == pv_epoch else "relative"
        time.sleep(args.sleep)

    print(f"{'vote account':<46} {'now':>5} {'ok':>4} {'none':>5} {'err':>4} {'other':>5} {'max':>5} {'impl':>6}  non-zero epochs")
    print("-" * 115)
    for v in votes:
        cur = state[v]["commission"]
        cur_s = "?" if cur is None else f"{cur:g}%"
        if not stake_for[v]:
            print(f"{v:<46} {cur_s:>5}  -- no stake account to query (see stderr)")
            continue
        mine = [r for r in rows if r["vote"] == v]
        ok = [r for r in mine if r.get("commission") is not None]
        paid = [r for r in mine if r["method"] == "collector paid"]
        none = sum(1 for r in mine if r["method"] == "no reward record")
        err = sum(1 for r in mine if r["method"].startswith("error"))
        other = len(mine) - len(ok) - len(paid) - none - err
        nonzero = [f"{r['epoch']}:{r['commission']:g}%" for r in ok if r["commission"] != 0]
        nonzero += [f"{r['epoch']}:>0%" for r in paid]
        mx = max((r["commission"] for r in ok), default=None)
        mx_s = ">0%" if paid else ("-" if mx is None else f"{mx:g}%")
        impl = max((r["implied"] for r in mine if "implied" in r), default=None)
        impl_s = "-" if impl is None else f"{impl:.2f}%"
        flag = f"{len(nonzero)} epochs: " + ", ".join(nonzero[:6]) + (" ..." if len(nonzero) > 6 else "") if nonzero else "none"
        print(f"{v:<46} {cur_s:>5} {len(ok):>4} {none:>5} {err:>4} {other:>5} {mx_s:>5} {impl_s:>6}  {flag}")
    print("\n'now' = current inflation commission. 'ok' = epochs proven 0% (collector got a")
    print("  0-lamport reward record while delegators were paid) or with a commission field.")
    print("'none' = no reward that epoch (delinquent?). 'err' = RPC error; try an archival RPC.")
    print("'other' = stake paid but no collector record found; inspect in the CSV.")
    print("'impl' = max commission implied by point value (reward / stake / credits);")
    print(f"  exact for epoch {pv_epoch}, otherwise relative to the best validator in this run.")
    print("Stake account (and custom inflation collector) used:")
    for v in votes:
        coll = state[v]["collector"]
        print(f"  {v} -> {stake_for[v] or '-'}" + (f"   collector {coll}" if coll != v else ""))

    if args.csv:
        cols = [("epoch", "epoch"), ("vote", "vote_account"), ("stake", "stake_account"),
                ("commission", "commission_pct"), ("implied", "implied_commission_pct"),
                ("implied_basis", "implied_basis"), ("stake_reward", "stake_reward_lamports"),
                ("commission_reward", "commission_reward_lamports"), ("credits", "vote_credits"),
                ("method", "method")]
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([c[1] for c in cols])
            for r in rows:
                w.writerow([r.get(c[0]) for c in cols])
        print(f"Wrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
