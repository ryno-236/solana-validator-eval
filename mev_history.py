#!/usr/bin/env python3
"""
Verify historical Jito MEV commission for Solana validators, straight from chain.

How it works (checked against mainnet and jito-programs source, Oct 2026):
  Each epoch a Jito validator creates a TipDistributionAccount (TDA), a PDA of the
  Tip Distribution program with seeds ["TIP_DISTRIBUTION_ACCOUNT", vote, epoch_le].
  Tips earned during the epoch land in it; after the epoch, TipRouter uploads a
  merkle root and delegators claim their share straight into their stake accounts.

  The commission is `validator_commission_bps`, set by the validator's identity in
  `initialize_tip_distribution_account`. The program has no instruction to change
  it afterwards, so the value at initialization is the one that applied.

  TDAs are closed about 10 epochs after creation, so:
    - live TDA     -> read commission and total tips from account state  ("account")
    - closed TDA   -> decode the initialize instruction from the PDA's
                      transaction history                                 ("init tx")
    - no history   -> the validator created no TDA that epoch: no tips were
                      shared with delegators (not running Jito, or no blocks) ("no TDA")

  Fees outside the validator's control, already deducted from what delegators see:
  the Jito block engine's cut (taken before tips reach the TDA) and the TipRouter
  fee (~3% of the TDA, a claimant in the merkle tree).

Usage:
    python mev_history.py VOTE_ACCT_1 VOTE_ACCT_2 --epochs 50 --csv mev.csv
    python mev_history.py -f validators.txt --rpc https://<your-rpc>
"""
import argparse
import base64
import csv
import hashlib
import struct
import sys
import time

from commission_history import DEFAULT_RPC, rpc

TIP_DISTRIBUTION_PROGRAM = "4R3gSG8BpU4t19KYj8CfnbtRpnT8gtk4dvTHxVRwc2r7"
TDA_SEED = b"TIP_DISTRIBUTION_ACCOUNT"
INIT_TDA_DISC = hashlib.sha256(b"global:initialize_tip_distribution_account").digest()[:8]

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b):
    n, s = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    return b"\0" * (len(s) - len(s.lstrip("1"))) + n.to_bytes((n.bit_length() + 7) // 8, "big")


# Program-derived addresses must lie off the ed25519 curve.
_P = 2**255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _on_curve(b):
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    return (x * x - x2) % _P == 0


def find_pda(seeds, program):
    prog = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise ValueError("no PDA found")


def tda_address(vote, epoch):
    return find_pda([TDA_SEED, b58decode(vote), struct.pack("<Q", epoch)], TIP_DISTRIBUTION_PROGRAM)


def parse_tda(raw):
    """TipDistributionAccount (Anchor): disc, vote, upload authority, Option<MerkleRoot>,
    epoch_created_at u64, validator_commission_bps u16, expires_at u64, bump u8."""
    o = 8 + 32 + 32
    total_tips = None
    if raw[o] == 1:
        total_tips = struct.unpack_from("<Q", raw, o + 1 + 32)[0]  # MerkleRoot.max_total_claim
        o += 1 + 32 + 32
    else:
        o += 1
    epoch, bps = struct.unpack_from("<QH", raw, o)
    return {"epoch": epoch, "bps": bps, "total_tips": total_tips}


def live_tdas(url, vote):
    """{epoch: parsed TDA} for every TDA of `vote` still open on-chain."""
    accts = rpc(url, "getProgramAccounts", [TIP_DISTRIBUTION_PROGRAM, {
        "encoding": "base64", "filters": [{"memcmp": {"offset": 8, "bytes": vote}}]}])
    out = {}
    for a in accts:
        t = parse_tda(base64.b64decode(a["account"]["data"][0]))
        t["address"] = a["pubkey"]
        out[t["epoch"]] = t
    return out


def commission_from_history(url, address, sleep, program=TIP_DISTRIBUTION_PROGRAM, init_disc=INIT_TDA_DISC):
    """(bps, signer) from the initialize instruction of a closed distribution account,
    or None if the PDA has no transaction history (account never created). Works for
    any Jito distribution program whose initialize args are (Pubkey, u16 bps, u8 bump)."""
    sigs, before = [], None
    while True:
        page = rpc(url, "getSignaturesForAddress", [address, {"limit": 1000, **({"before": before} if before else {})}])
        sigs += page
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    if not sigs:
        return None
    # Oldest first; the initialize instruction is normally the very first transaction.
    for s in [s for s in reversed(sigs) if s["err"] is None][:10]:
        tx = rpc(url, "getTransaction", [s["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 0}])
        keys = tx["transaction"]["message"]["accountKeys"]
        for ix in tx["transaction"]["message"]["instructions"]:
            if keys[ix["programIdIndex"]] != program:
                continue
            data = b58decode(ix["data"])
            if data[:8] == init_disc:
                # args: merkle_root_upload_authority Pubkey, validator_commission_bps u16, bump u8
                return struct.unpack_from("<H", data, 8 + 32)[0], keys[0]
        time.sleep(sleep)
    raise RuntimeError(f"{address}: initialize instruction not found in oldest transactions")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("accounts", nargs="*", help="vote account addresses")
    ap.add_argument("-f", "--file", help="file with one vote account per line")
    ap.add_argument("--epochs", type=int, default=30, help="completed epochs to look back (default 30)")
    ap.add_argument("--rpc", default=DEFAULT_RPC, help="RPC endpoint URL (default: $SOLANA_RPC_URL or public mainnet)")
    ap.add_argument("--csv", help="optional path to write per-epoch results")
    ap.add_argument("--sleep", type=float, default=0.3, help="pause between calls (seconds)")
    args = ap.parse_args()

    votes = [a.partition("=")[0] for a in args.accounts]
    if args.file:
        with open(args.file) as fh:
            votes += [ln.strip().partition("=")[0] for ln in fh if ln.strip() and not ln.startswith("#")]
    votes = list(dict.fromkeys(votes))
    if not votes:
        ap.error("no vote accounts given")

    last_done = rpc(args.rpc, "getEpochInfo")["epoch"] - 1
    epochs = list(range(last_done, last_done - args.epochs, -1))
    print(f"Checking MEV commission for epochs {epochs[-1]}..{epochs[0]}\n")

    rows = []  # (epoch, vote, tda, bps, total_tips, source)
    older = {}
    for v in votes:
        live = live_tdas(args.rpc, v)
        older[v] = {e: t["bps"] for e, t in live.items() if e < epochs[-1]}
        for e in epochs:
            if e in live:
                t = live[e]
                rows.append((e, v, t["address"], t["bps"], t["total_tips"], "account"))
                continue
            addr = tda_address(v, e)
            try:
                found = commission_from_history(args.rpc, addr, args.sleep)
            except Exception as exc:
                rows.append((e, v, addr, None, None, f"error: {exc}"))
                print(f"{v} epoch {e}: {exc}", file=sys.stderr)
                continue
            if found is None:
                rows.append((e, v, addr, None, None, "no TDA"))
            else:
                rows.append((e, v, addr, found[0], None, "init tx"))
            time.sleep(args.sleep)

    print(f"{'vote account':<46} {'ok':>4} {'noTDA':>6} {'err':>4} {'max':>6}  non-zero epochs")
    print("-" * 100)
    for v in votes:
        mine = [r for r in rows if r[1] == v]
        ok = [r for r in mine if r[3] is not None]
        no_tda = [r[0] for r in mine if r[5] == "no TDA"]
        err = sum(1 for r in mine if r[5].startswith("error"))
        mx = max((r[3] for r in ok), default=None)
        nonzero = [f"{r[0]}:{r[3]}bps" for r in ok if r[3]]
        flag = ", ".join(nonzero[:8]) + (" ..." if len(nonzero) > 8 else "") if nonzero else "none"
        mx_s = "-" if mx is None else f"{mx}bps"
        print(f"{v:<46} {len(ok):>4} {len(no_tda):>6} {err:>4} {mx_s:>6}  {flag}")
        if no_tda:
            print(f"{'':<46} no TDA in epochs: {', '.join(map(str, no_tda))}")
        if older[v]:
            print(f"{'':<46} unclosed TDAs before window: "
                  + ", ".join(f"{e}:{b}bps" for e, b in sorted(older[v].items())))
    print("\n'ok' = epochs with a verified MEV commission (live account, or initialize instruction).")
    print("'noTDA' = no tip distribution account that epoch: no MEV shared with delegators.")
    print("Delegators also bear fees the validator does not set: the block engine cut and ~3% TipRouter fee.")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["epoch", "vote_account", "tip_distribution_account", "mev_commission_bps",
                        "total_tips_lamports", "source"])
            w.writerows(rows)
        print(f"Wrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
